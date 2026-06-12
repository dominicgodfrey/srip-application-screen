"""Stage 0 — ingest data contract (Phase 1.1).

This module pins the CSV → canonical-field mapping (PRD §2) and resolves a real Fillout
export's headers against it *gracefully* — surfacing what is missing or unrecognized rather
than throwing on the first surprise.

Why matching is not a plain ``==`` on every header: Fillout column titles are the full
question text and several are very long (the essays, the extenuating-circumstances field, the
affirmation checkboxes). The PRD only quotes them in part, and form copy drifts between
cycles. So short, stable headers are matched exactly while the long question columns are
matched by a distinctive substring. Either way a header must resolve to exactly one role, and
each role to exactly one header — ambiguity is reported, never guessed through.

Phase 1.1 is schema-only: header constants, the resolver, and the ``ApplicantRow``
representation. Reading the CSV with pandas, whitespace normalization, identity validation,
and dedup are Phases 1.2–1.5.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import IO

import pandas as pd
from pydantic import BaseModel, ConfigDict

from .models import DedupInfo

# ================================================================================================
# Canonical field roles
# ================================================================================================
# Stable role names used by the whole pipeline. The raw CSV header is an implementation detail
# resolved at ingest; everything downstream refers to a role, never a header string.

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
    """How one canonical role is located among a CSV's headers.

    A header matches this spec if (after trimming) it equals one of ``exact``, **or** it
    contains every substring in ``contains`` (case-insensitive). ``contains`` is the escape
    hatch for the long, drift-prone question columns whose verbatim text we cannot pin.
    """

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


# The data contract (PRD §2). Order is documentation, not significance.
# ``required`` is reserved for fields without which an applicant cannot be processed at all:
# the identity keys (Phase 1.3) plus the core graded signals (GPA + both essays). Everything
# else is optional — its absence is handled downstream as neutral/NEEDS_REVIEW, never a crash.
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


# ================================================================================================
# Header resolution
# ================================================================================================


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
    """Resolve headers and raise if the contract is unsatisfiable.

    "Graceful" here means the *resolution* never raises — callers that want a soft report use
    :func:`resolve_headers`. This wrapper is for the pipeline entry point that must refuse a
    structurally wrong upload, with a message naming exactly what is wrong.
    """
    resolution = resolve_headers(headers)
    if resolution.ok:
        return resolution
    problems: list[str] = []
    if resolution.missing_required:
        problems.append(f"missing required columns: {', '.join(resolution.missing_required)}")
    if resolution.ambiguous:
        problems.append(f"ambiguous columns (matched >1 header): {', '.join(resolution.ambiguous)}")
    raise HeaderValidationError("; ".join(problems))


# ================================================================================================
# ApplicantRow — one canonicalized input row
# ================================================================================================


class ApplicantRow(BaseModel):
    """One CSV row mapped onto canonical roles.

    Every field is a whitespace-normalized string (defaulting to ""); GPA normalization, essay
    gating, and the rest of the pipeline run on these later. Unknown keys are forbidden so a
    mapping bug surfaces immediately.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    institution: str = ""
    state: str = ""
    phone: str = ""
    first_choice: str = ""
    second_choice: str = ""
    third_choice: str = ""
    gpa: str = ""
    gpa_explanation: str = ""
    coursework: str = ""
    resume_url: str = ""
    linkedin: str = ""
    essay1: str = ""
    essay2: str = ""
    affirmation: str = ""

    @classmethod
    def from_record(cls, record: dict[str, object], resolution: HeaderResolution) -> ApplicantRow:
        """Build a row from a raw header→value record using a resolved header mapping.

        Only resolved roles are populated; an unresolved optional role stays at its "" default.
        Each value is whitespace-normalized via :func:`normalize_cell` (missing / NaN / blank →
        "", outer whitespace trimmed).
        """
        values: dict[str, str] = {}
        for role, header in resolution.role_to_header.items():
            values[role] = normalize_cell(record.get(header, ""))
        return cls(**values)


# ================================================================================================
# CSV loading + cell normalization (Phase 1.2)
# ================================================================================================

# Encodings tried in order. Fillout exports are UTF-8 (often BOM-prefixed); cp1252 is the
# common Windows-spreadsheet fallback before a last-resort latin-1 that never raises.
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp1252", "latin-1")


def normalize_cell(value: object) -> str:
    """Coerce a raw cell to a trimmed string; ``None``/NaN/whitespace-only → "".

    Outer whitespace only — interior newlines/spacing in essays are preserved so word-count
    and grading stages see the text as written.
    """
    if value is None:
        return ""
    # pandas NaN (a float) compares unequal to itself; treat as blank.
    if isinstance(value, float) and value != value:  # noqa: PLR0124 - NaN check
        return ""
    return str(value).strip()


def read_csv_records(
    source: str | Path | bytes | IO[bytes],
) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV into ``(headers, records)`` with every cell a normalized string.

    Encoding-safe: tries UTF-8 (BOM-aware), then cp1252, then latin-1 (which cannot raise), so
    a stray non-UTF-8 byte never crashes ingest. All columns are read as strings — no numeric
    inference (a GPA of ``4.0`` must not become a float) — and blanks come back as "" rather
    than NaN. Records keep the original CSV header strings as keys for
    :meth:`ApplicantRow.from_record`.
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


# ================================================================================================
# Identity validation (Phase 1.3)
# ================================================================================================
# An applicant we cannot identify cannot be reported on, deduped, or returned to the owner, so a
# row missing first name, last name, OR email is *dropped at ingest* — distinct from the
# pipeline's REJECTED/NEEDS_REVIEW outcomes, which only apply to identifiable applicants.
#
# Per the PLAN decisions log: a blank GPA or empty essay is NOT an identity problem and is kept —
# those flow downstream (blank GPA -> NEEDS_REVIEW, empty essay -> REJECTED), preserving the
# legitimate international contingent that leaves GPA unscalable.

IDENTITY_ROLES: tuple[str, ...] = (FIRST_NAME, LAST_NAME, EMAIL)


@dataclass(frozen=True)
class DroppedRow:
    """A row removed at ingest because it lacks the fields needed to identify an applicant."""

    row_index: int  # 0-based position among the data rows that were read
    submission_id: str  # may be "" if that cell was also blank
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
    """Split rows on whether they carry first name, last name, and email.

    A row missing any of the three is dropped and recorded (index, submission id, which fields
    were blank). Everything else is kept verbatim for dedup (Phase 1.4) and the pipeline.
    """
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


# ================================================================================================
# Deduplication (Phase 1.4)
# ================================================================================================
# Two independent signals, handled differently per PRD §2:
#   * Email duplicates (6 in the reference set): same person submitting twice. Keep the FIRST
#     occurrence, drop the surplus, flag both ends with is_duplicate_email.
#   * Name-pair duplicates without a shared email (8 in the reference set): likely siblings or
#     re-applications under a new email. Flag is_duplicate_name but KEEP all — never auto-merge,
#     since they may be genuinely different applicants.


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

    Email is the primary key: the first row for a given (case-insensitive) email is kept and
    every later one is dropped as surplus, both flagged ``is_duplicate_email``. Among the kept
    rows, any shared name-pair is flagged ``is_duplicate_name`` and all members are retained —
    by construction these have distinct emails, so they may be siblings or re-applications and
    must not be merged. Input order is preserved.
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

    # Name-pair flagging runs only over the kept set (surplus emails already removed).
    name_counts = Counter(_norm_name(d.row) for d in result.kept)
    for deduped in result.kept:
        if name_counts[_norm_name(deduped.row)] > 1:
            deduped.dedup.is_duplicate_name = True
            name_note = "shares name with another applicant (different email); not merged"
            deduped.dedup.notes = (
                f"{deduped.dedup.notes}; {name_note}" if deduped.dedup.notes else name_note
            )
    return result


# ================================================================================================
# Stage 0 orchestration (Phase 1.5)
# ================================================================================================


@dataclass(frozen=True)
class IngestReport:
    """Human-auditable summary of what ingest did — counts and the drop/dup ledger.

    Surfaced to the owner so a shrinking row count is explained, never silent. Carries no
    essay/GPA content, only structural facts (ids, indices, which fields were blank).
    """

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

    rows: list[DedupedRow]  # identifiable, deduped rows ready for the pipeline
    resolution: HeaderResolution
    report: IngestReport


def ingest_csv(source: str | Path | bytes | IO[bytes]) -> IngestResult:
    """Run Stage 0 end-to-end: read → validate headers → build rows → identity → dedup.

    Raises :class:`HeaderValidationError` if the CSV's columns can't satisfy the data contract
    (the only hard failure; the API layer turns this into a graceful 4xx). Otherwise returns
    the kept rows plus an :class:`IngestReport` accounting for every row that was dropped or
    flagged. Pure read pipeline — no LLM calls, no disk writes.
    """
    headers, records = read_csv_records(source)
    resolution = validate_headers(headers)

    rows = [ApplicantRow.from_record(record, resolution) for record in records]
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
