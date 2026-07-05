"""Webhook payload → pipeline input mapping (P4, PRD v3 §2.2/§4).

The v3 front door's equivalent of Stage 0: turn a validated
:class:`~srip_filter.models.EssaysModePayload` (plus the optionally stored resume-mode
payload) into the :class:`~srip_filter.ingest.ApplicantRow` the existing stages consume,
plus the per-essay metadata (exact word bounds, question text) that v3's strict Stage 1
needs. No LLM, no I/O.

Mapping rules that carry decisions:

* **Essays:** ``required_essays[0]`` → essay 1, ``[1]`` → essay 2 (order is the site's
  dispatch order; ``field_key`` is carried for the audit trail). The optional technical
  essay is ``optional_essays[0]``. Fewer than two required essays ⇒ the application is
  unscoreable → ``NEEDS_REVIEW`` (never silently rejected); surplus entries are noted in
  ``mapping_notes`` (contract-drift signal), not graded.
* **GPA:** ``gpa.unweighted`` is primary (deterministic path). A weighted-only
  submission sets ``force_task_a`` — the deterministic /5 conversion would misread a
  weighted scale (PRD v3 §4 Stage 2). The site's legacy joined string is passed through
  as-is until WEBSITE_ASKS #3 lands.
* **International:** derived, not trusted from a sentinel — a non-blank
  ``state_of_residence`` that is not a US state/DC/territory name ⇒ international.
  (The dropdown sends full names; the exact non-US sentinel is unconfirmed — ask #6.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ingest import ApplicantRow
from .models import EssaysModePayload, GpaPayload, ResumeModePayload

# Full names as the dropdown sends them (owner: full state names). Lowercased for lookup.
US_STATE_NAMES: frozenset[str] = frozenset(
    name.lower()
    for name in (
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
        "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
        "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
        "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
        "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
        "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
        "District of Columbia", "Puerto Rico", "Guam", "American Samoa",
        "U.S. Virgin Islands", "Northern Mariana Islands",
    )
)


def is_international(state_of_residence: str) -> bool:
    """True when a non-blank state value is not a US state/DC/territory full name."""
    state = state_of_residence.strip().lower()
    return bool(state) and state not in US_STATE_NAMES


@dataclass(frozen=True)
class EssayMeta:
    """Per-essay grading metadata carried alongside the text (PRD v3 §4 Stage 1)."""

    question: str = ""
    field_key: str = ""
    min_words: int | None = None
    max_words: int | None = None

    @property
    def target_range(self) -> str | None:
        """Human-readable band for the Task D prompt ("100-350"), None without bounds."""
        if self.min_words is None and self.max_words is None:
            return None
        lo = self.min_words if self.min_words is not None else 0
        hi = self.max_words if self.max_words is not None else "∞"
        return f"{lo}-{hi}"


@dataclass(frozen=True)
class WebhookApplicant:
    """Everything the v3 per-row runner needs for one application."""

    row: ApplicantRow
    e1: EssayMeta = field(default_factory=EssayMeta)
    e2: EssayMeta = field(default_factory=EssayMeta)
    e3: EssayMeta = field(default_factory=EssayMeta)
    cohort_name: str = ""
    state_of_residence: str = ""
    international: bool = False
    force_task_a: bool = False  # weighted-only GPA → Task A, never the /5 fraction path
    missing_required_essays: bool = False  # <2 required essays delivered → NEEDS_REVIEW
    mapping_notes: tuple[str, ...] = ()  # contract-drift observations for the audit trail


def _gpa_string(gpa: GpaPayload | str | None) -> tuple[str, bool]:
    """Reduce the payload GPA to (raw string for Stage 2, force_task_a flag)."""
    if gpa is None:
        return "", False
    if isinstance(gpa, str):  # legacy joined string until WEBSITE_ASKS #3
        return gpa.strip(), False
    unweighted = (gpa.unweighted or "").strip()
    weighted = (gpa.weighted or "").strip()
    if unweighted:
        return unweighted, False
    if weighted:
        return weighted, True  # weighted-only: deterministic /N conversion would be wrong
    return "", False


def map_essays_payload(
    payload: EssaysModePayload, *, resume_payload: ResumeModePayload | None = None
) -> WebhookApplicant:
    """Build the pipeline input from the stored payload(s). Pure.

    ``resume_payload`` fills ``resume_url`` when the resume-mode delivery arrived
    separately (a row may hold both payloads, PRD v3 §1.1); the essays-mode value wins
    when both carry one.
    """
    notes: list[str] = []

    required = payload.required_essays
    optional = payload.optional_essays
    missing_required = len(required) < 2
    if missing_required:
        notes.append(
            f"payload delivered {len(required)} required essay(s); 2 expected — unscoreable"
        )
    if len(required) > 2:
        notes.append(
            f"payload delivered {len(required)} required essays; entries 3+ not graded "
            "(contract drift — check the live question config)"
        )
    if len(optional) > 1:
        notes.append(
            f"payload delivered {len(optional)} optional essays; entries 2+ not graded"
        )

    def entry(seq, i):
        return seq[i] if len(seq) > i else None

    r1, r2, o1 = entry(required, 0), entry(required, 1), entry(optional, 0)

    gpa_raw, force_task_a = _gpa_string(payload.gpa)
    resume_url = payload.resume_url or (resume_payload.resume_url if resume_payload else None)

    name = (payload.student_name or "").strip()
    row = ApplicantRow(
        submission_id=str(payload.submission_id),
        # The webhook carries one display name; keep it whole in first_name (the audit
        # record joins first+last with a space, so this renders correctly).
        first_name=name,
        last_name="",
        email=payload.user_email.strip(),
        institution=payload.institution.strip(),
        state=payload.state_of_residence.strip(),
        first_choice=payload.first_choice.strip(),
        second_choice=payload.second_choice.strip(),
        third_choice=payload.third_choice.strip(),
        gpa=gpa_raw,
        gpa_explanation=payload.gpa_explanation.strip(),
        coursework=payload.relevant_coursework.strip(),
        resume_url=(resume_url or "").strip(),
        essay1=(r1.answer if r1 else "").strip(),
        essay2=(r2.answer if r2 else "").strip(),
        essay3=(o1.answer if o1 else "").strip(),
        programming_languages=payload.programming_languages.strip(),
        github_profile=payload.github_profile.strip(),
        sub_track=payload.sub_track.strip(),
    )

    def meta(e) -> EssayMeta:
        if e is None:
            return EssayMeta()
        return EssayMeta(
            question=e.question, field_key=e.field_key,
            min_words=e.min_words, max_words=e.max_words,
        )

    return WebhookApplicant(
        row=row,
        e1=meta(r1),
        e2=meta(r2),
        e3=meta(o1),
        cohort_name=payload.cohort_name,
        state_of_residence=payload.state_of_residence.strip(),
        international=is_international(payload.state_of_residence),
        force_task_a=force_task_a,
        missing_required_essays=missing_required,
        mapping_notes=tuple(notes),
    )
