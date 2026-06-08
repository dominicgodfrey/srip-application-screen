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

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

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

    Every field is a raw string straight from the cell (defaulting to ""); typing, GPA
    normalization, and gating happen in later stages. Unknown keys are forbidden so a mapping
    bug surfaces immediately. Whitespace normalization is Phase 1.2's job — this model only
    fixes the *shape*.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    institution: str = ""
    state: str = ""
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
        Missing/NaN-like cell values become "". Values are ``str()``-coerced but otherwise left
        untouched (no trimming yet — see Phase 1.2).
        """
        values: dict[str, str] = {}
        for role, header in resolution.role_to_header.items():
            raw = record.get(header, "")
            values[role] = "" if raw is None else str(raw)
        return cls(**values)
