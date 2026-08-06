"""CSV ingest — the Fillout-export reader, kept for ``scripts/replay.py`` only.

**Not part of the deployed service.** Applications arrive over the webhook; this exists so the
replay tool can turn a CSV export into authenticated POSTs. It is the one place pandas is used,
and pandas is a dev dependency — importing it in a production install fails, which is the
intended signal. :class:`~srip_filter.applicant.ApplicantRow` lives in its own pandas-free
module to keep that true.

Headers resolve *gracefully*, surfacing what is missing or unrecognized rather than throwing on
the first surprise. Short stable headers match exactly; the long question columns (whose text
drifts between cycles) match on a distinctive substring. Either way a header must resolve to
exactly one role and each role to one header — ambiguity is reported, never guessed through.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import IO

import pandas as pd

from .applicant import ApplicantRow
from .models import DedupInfo

# --- Canonical field roles ---
# The raw CSV header is an implementation detail resolved at ingest; everything downstream
# refers to a role, never a header string.

SUBMISSION_ID = "submission_id"
FIRST_NAME = "first_name"
LAST_NAME = "last_name"
EMAIL = "email"
INSTITUTION = "institution"
STATE = "state"
PHONE = "phone"
FIRST_CHOICE = "first_choice"
SECOND_CHOICE = "second_choice"
THIRD_CHOICE = "third_choice"
GPA = "gpa"
GPA_EXPLANATION = "gpa_explanation"
COURSEWORK = "coursework"
RESUME_URL = "resume_url"
LINKEDIN = "linkedin"
ESSAY1 = "essay1"
ESSAY2 = "essay2"
AFFIRMATION = "affirmation"


@dataclass(frozen=True)
class ColumnSpec:
    """How one role is located among a CSV's headers: a header matches if it equals one of
    ``exact`` or contains every substring in ``contains`` — the escape hatch for the long,
    drift-prone question columns whose verbatim text cannot be pinned."""

    role: str
    required: bool
    exact: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()

    def matches(self, header: str) -> bool:
        norm = header.strip()
        if norm in self.exact:
            return True
        if self.contains:
            low = norm.lower()
            return all(token.lower() in low for token in self.contains)
        return False


# The data contract (PRD §2). Order is documentation, not significance. ``required`` covers
# only fields without which an applicant cannot be processed: the identity keys and the core
# graded signals. Everything else is neutral or NEEDS_REVIEW downstream, never a crash.
COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec(SUBMISSION_ID, required=True, exact=("Submission ID",)),
    ColumnSpec(FIRST_NAME, required=True, exact=("Student First Name",)),
    ColumnSpec(LAST_NAME, required=True, exact=("Student Last Name",)),
    ColumnSpec(EMAIL, required=True, exact=("What is your email address?",)),
    ColumnSpec(
        INSTITUTION,
        required=False,
        exact=("Please list your undergraduate institution of study below.",),
        contains=("undergraduate institution",),
    ),
    ColumnSpec(STATE, required=False, exact=("What is your state of residence?",)),
    ColumnSpec(
        PHONE,
        required=False,
        exact=("What is your phone number?",),
        contains=("phone number",),
    ),
    ColumnSpec(FIRST_CHOICE, required=False, exact=("First Choice",)),
    ColumnSpec(SECOND_CHOICE, required=False, exact=("Second Choice (optional)",)),
    ColumnSpec(THIRD_CHOICE, required=False, exact=("Third Choice (optional)",)),
    ColumnSpec(GPA, required=True, exact=("GPA",)),
    ColumnSpec(GPA_EXPLANATION, required=False, contains=("extenuating circumstances",)),
    ColumnSpec(COURSEWORK, required=False, exact=("Relevant Coursework",)),
    ColumnSpec(RESUME_URL, required=False, exact=("Resume (optional)",)),
    ColumnSpec(LINKEDIN, required=False, exact=("LinkedIn (optional)",)),
    ColumnSpec(ESSAY1, required=True, contains=("What motivates you to apply",)),
    ColumnSpec(ESSAY2, required=True, contains=("foundation for future research",)),
    ColumnSpec(AFFIRMATION, required=False, contains=("affirm",)),
)

# Form-internal columns we deliberately ignore (PRD §2). Listed so the resolver can keep them
# out of the "unrecognized headers" report — they are expected noise, not a contract surprise.
IGNORED_HEADERS: frozenset[str] = frozenset({"Errors", "Url", "Network ID"})

_SPEC_BY_ROLE: dict[str, ColumnSpec] = {spec.role: spec for spec in COLUMN_SPECS}
REQUIRED_ROLES: tuple[str, ...] = tuple(s.role for s in COLUMN_SPECS if s.required)


# --- Header resolution ---


@dataclass(frozen=True)
class HeaderResolution:
    """Outcome of matching a CSV's headers against the data contract.

    ``role_to_header`` maps each resolved role to the actual header string to read. The other
    fields are the graceful-failure report: callers decide whether ``missing_required`` or an
    ambiguity is fatal, rather than the resolver raising.
    """

    role_to_header: dict[str, str] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()
    unrecognized_headers: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every required role resolved and nothing was ambiguous."""
        return not self.missing_required and not self.ambiguous


def resolve_headers(headers: list[str]) -> HeaderResolution:
    """Match a list of CSV headers to canonical roles, reporting gaps instead of raising.

    Guarantees a 1:1 role↔header resolution: a role claimed by more than one header, or a
    header claiming more than one role, is recorded in ``ambiguous`` and left unresolved.
    """
    role_to_headers: dict[str, list[str]] = {}
    header_to_roles: dict[str, list[str]] = {}
    for header in headers:
        for spec in COLUMN_SPECS:
            if spec.matches(header):
                role_to_headers.setdefault(spec.role, []).append(header)
                header_to_roles.setdefault(header, []).append(spec.role)

    ambiguous: list[str] = []
    role_to_header: dict[str, str] = {}
    for role, matched in role_to_headers.items():
        # A header that resolved to several roles is itself ambiguous; don't trust it for any.
        clean = [h for h in matched if len(header_to_roles[h]) == 1]
        if len(clean) == 1:
            role_to_header[role] = clean[0]
        else:
            ambiguous.append(role)

    resolved = set(role_to_header)
    missing_required = tuple(r for r in REQUIRED_ROLES if r not in resolved and r not in ambiguous)
    missing_optional = tuple(
        s.role
        for s in COLUMN_SPECS
        if not s.required and s.role not in resolved and s.role not in ambiguous
    )
    unrecognized = tuple(
        h
        for h in headers
        if h.strip() not in IGNORED_HEADERS and not header_to_roles.get(h)
    )
    return HeaderResolution(
        role_to_header=role_to_header,
        missing_required=missing_required,
        missing_optional=missing_optional,
        unrecognized_headers=unrecognized,
        ambiguous=tuple(sorted(ambiguous)),
    )


class HeaderValidationError(ValueError):
    """Raised when a CSV cannot be processed: a required role is missing or ambiguous."""


def validate_headers(headers: list[str]) -> HeaderResolution:
    """Resolve headers and raise if the contract is unsatisfiable, naming exactly what is wrong.
    Callers that want a soft report use :func:`resolve_headers`, which never raises."""
    resolution = resolve_headers(headers)
    if resolution.ok:
        return resolution
    problems: list[str] = []
    if resolution.missing_required:
        problems.append(f"missing required columns: {', '.join(resolution.missing_required)}")
    if resolution.ambiguous:
        problems.append(f"ambiguous columns (matched >1 header): {', '.join(resolution.ambiguous)}")
    raise HeaderValidationError("; ".join(problems))


# --- ApplicantRow — one canonicalized input row ---


def row_from_record(record: dict[str, object], resolution: HeaderResolution) -> ApplicantRow:
    """Build an :class:`~srip_filter.applicant.ApplicantRow` from a raw CSV record; unresolved
    optional roles stay at their "" default.

    A module function rather than a classmethod: ``ApplicantRow`` lives elsewhere so the scoring
    layer never imports this module (and so never imports pandas), and a classmethod would drag
    ``HeaderResolution`` back across that line.
    """
    values = {
        role: normalize_cell(record.get(header, ""))
        for role, header in resolution.role_to_header.items()
    }
    return ApplicantRow(**values)


# --- CSV loading + cell normalization ---

# Tried in order: exports are UTF-8 (often BOM-prefixed), cp1252 is the Windows-spreadsheet
# fallback, and latin-1 is a last resort that never raises.
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp1252", "latin-1")


def normalize_cell(value: object) -> str:
    """Coerce a raw cell to a trimmed string; ``None``/NaN/whitespace-only → "". Outer
    whitespace only — an essay's interior spacing must reach grading as written."""
    if value is None:
        return ""
    # pandas NaN (a float) compares unequal to itself; treat as blank.
    if isinstance(value, float) and value != value:  # noqa: PLR0124 - NaN check
        return ""
    return str(value).strip()


def read_csv_records(
    source: str | Path | bytes | IO[bytes],
) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV into ``(headers, records)``, every cell a normalized string keyed by the
    original header.

    Encodings are tried in turn so a stray non-UTF-8 byte never crashes ingest, and all columns
    are read as strings — no numeric inference, since a GPA of ``4.0`` must not become a float.
    """
    raw = _read_bytes(source)
    last_err: UnicodeDecodeError | None = None
    for encoding in _ENCODINGS:
        try:
            frame = pd.read_csv(
                BytesIO(raw),
                dtype=str,
                keep_default_na=False,
                na_filter=False,
                encoding=encoding,
            )
            break
        except UnicodeDecodeError as err:  # try the next, more permissive encoding
            last_err = err
    else:  # pragma: no cover - latin-1 decodes any byte, so this is unreachable in practice
        raise last_err  # type: ignore[misc]

    headers = [str(col) for col in frame.columns]
    records = [
        {col: normalize_cell(val) for col, val in row.items()}
        for row in frame.to_dict(orient="records")
    ]
    return headers, records


def _read_bytes(source: str | Path | bytes | IO[bytes]) -> bytes:
    """Read raw bytes from a path, a bytes blob, or an already-open binary buffer."""
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    return source.read()


# --- Identity validation ---
# An unidentifiable applicant cannot be reported on or deduped, so a row missing a name or email
# is *dropped at ingest* — distinct from REJECTED/NEEDS_REVIEW, which apply to identifiable
# applicants. A blank GPA or empty essay is not an identity problem and is kept, so it can flow
# downstream to the outcome it deserves.

IDENTITY_ROLES: tuple[str, ...] = (FIRST_NAME, LAST_NAME, EMAIL)


@dataclass(frozen=True)
class DroppedRow:
    """A row removed at ingest because it lacks the fields needed to identify an applicant."""

    row_index: int  # 0-based, among the data rows read
    submission_id: str  # "" if that cell was blank too
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class IdentityResult:
    """Partition of input rows into identifiable (kept) and unidentifiable (dropped)."""

    kept: list[ApplicantRow] = field(default_factory=list)
    dropped: list[DroppedRow] = field(default_factory=list)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def validate_identity(rows: list[ApplicantRow]) -> IdentityResult:
    """Split rows on whether they carry first name, last name, and email; a row missing any of
    the three is dropped and recorded, the rest kept verbatim."""
    result = IdentityResult()
    for index, row in enumerate(rows):
        missing = tuple(role for role in IDENTITY_ROLES if not getattr(row, role))
        if missing:
            result.dropped.append(
                DroppedRow(row_index=index, submission_id=row.submission_id, missing_fields=missing)
            )
        else:
            result.kept.append(row)
    return result


# --- Deduplication ---
# Two signals, handled differently per PRD §2: a shared email is the same person twice, so the
# first is kept and the surplus dropped; a shared name *without* a shared email is likely
# siblings or a re-application, so all are flagged and kept — never auto-merged.


@dataclass
class DedupedRow:
    """An ApplicantRow paired with its dedup audit info (PRD §9 'dedup' block)."""

    row: ApplicantRow
    dedup: DedupInfo


@dataclass(frozen=True)
class DedupResult:
    """Outcome of dedup: rows retained for the pipeline, and surplus email dupes removed."""

    kept: list[DedupedRow] = field(default_factory=list)
    dropped: list[DedupedRow] = field(default_factory=list)


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _norm_name(row: ApplicantRow) -> tuple[str, str]:
    return (row.first_name.strip().lower(), row.last_name.strip().lower())


def deduplicate(rows: list[ApplicantRow]) -> DedupResult:
    """Collapse email duplicates and flag (but keep) same-name, different-email applicants.

    Email is the primary key, so the first row for one is kept and later ones dropped as
    surplus. Shared name-pairs among the survivors have distinct emails by construction — they
    may be siblings or re-applications — so they are flagged and all retained. Order preserved.
    """
    email_counts = Counter(_norm_email(r.email) for r in rows if _norm_email(r.email))

    seen_emails: set[str] = set()
    result = DedupResult()
    for row in rows:
        email = _norm_email(row.email)
        if email and email in seen_emails:
            note = f"surplus submission; first of {email_counts[email]} sharing this email kept"
            result.dropped.append(
                DedupedRow(row, DedupInfo(is_duplicate_email=True, kept=False, notes=note))
            )
            continue
        if email:
            seen_emails.add(email)
        is_email_dup = bool(email) and email_counts[email] > 1
        notes = (
            f"kept first of {email_counts[email]} submissions sharing this email"
            if is_email_dup
            else ""
        )
        result.kept.append(
            DedupedRow(row, DedupInfo(is_duplicate_email=is_email_dup, kept=True, notes=notes))
        )

    # Only over the kept set — surplus emails are already gone.
    name_counts = Counter(_norm_name(d.row) for d in result.kept)
    for deduped in result.kept:
        if name_counts[_norm_name(deduped.row)] > 1:
            deduped.dedup.is_duplicate_name = True
            name_note = "shares name with another applicant (different email); not merged"
            deduped.dedup.notes = (
                f"{deduped.dedup.notes}; {name_note}" if deduped.dedup.notes else name_note
            )
    return result


# --- Stage 0 orchestration ---


@dataclass(frozen=True)
class IngestReport:
    """Counts and the drop/dup ledger, so a shrinking row count is explained rather than silent.
    Structural facts only — no essay or GPA content."""

    total_rows_read: int
    kept_count: int
    identity_dropped: list[DroppedRow]
    duplicate_email_dropped: list[DedupedRow]
    duplicate_name_flagged: int
    unrecognized_headers: tuple[str, ...]
    missing_optional_roles: tuple[str, ...]


@dataclass(frozen=True)
class IngestResult:
    """Everything Stage 0 produces: the kept rows and the report explaining the rest."""

    rows: list[DedupedRow]  # identifiable and deduped, ready for the pipeline
    resolution: HeaderResolution
    report: IngestReport


def ingest_csv(source: str | Path | bytes | IO[bytes]) -> IngestResult:
    """Run Stage 0 end-to-end: read → validate headers → build rows → identity → dedup.

    Headers that cannot satisfy the data contract raise :class:`HeaderValidationError` — the
    only hard failure. Otherwise every dropped or flagged row is accounted for in the report.
    """
    headers, records = read_csv_records(source)
    resolution = validate_headers(headers)

    rows = [row_from_record(record, resolution) for record in records]
    identity = validate_identity(rows)
    dedup = deduplicate(identity.kept)

    report = IngestReport(
        total_rows_read=len(rows),
        kept_count=len(dedup.kept),
        identity_dropped=identity.dropped,
        duplicate_email_dropped=dedup.dropped,
        duplicate_name_flagged=sum(1 for d in dedup.kept if d.dedup.is_duplicate_name),
        unrecognized_headers=resolution.unrecognized_headers,
        missing_optional_roles=resolution.missing_optional,
    )
    return IngestResult(rows=dedup.kept, resolution=resolution, report=report)
