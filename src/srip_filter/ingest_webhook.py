"""Webhook payload → pipeline input mapping (PRD v3 §2.2/§4). No LLM, no I/O.

Mapping rules that carry decisions:

* **Essays:** the arrays are authoritative and ordered by the site's own ``ats_role`` tagging,
  so there is no field_key guessing. Fewer than two required essays is unscoreable →
  ``NEEDS_REVIEW``; surplus entries are noted as contract drift, not graded.
* **Named fields come from ``all_answers``** — the only place ``field_key`` exists. An
  expected-but-absent key is noted, and absent and blank are the same to the gates (D3).
* **GPA:** ``gpa_unweighted`` is primary; a weighted-only submission sets ``force_task_a``,
  since the deterministic /5 conversion would misread a weighted scale.
* **International:** derived rather than trusted from a sentinel — a non-blank state that is
  not a US state/DC/territory name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .applicant import ApplicantRow
from .models import ApplicationPayload

# `programming_languages` and `github_profile` are deliberately absent: they are not on the
# live CS form, so their absence is normal and must never raise a drift note.
EXPECTED_ANSWER_KEYS: tuple[str, ...] = (
    "gpa_explanation",
    "relevant_coursework",
    "institution",
    "state_of_residence",
)

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
    """Per-essay metadata alongside the text — question only. Word bounds retired 2026-07-28:
    the site validates them at submit, so re-checking here could only fire on stale config."""

    question: str = ""


@dataclass(frozen=True)
class WebhookApplicant:
    """Everything the per-row runner needs for one application."""

    row: ApplicantRow
    e1: EssayMeta = field(default_factory=EssayMeta)
    e2: EssayMeta = field(default_factory=EssayMeta)
    e3: EssayMeta = field(default_factory=EssayMeta)
    cohort_name: str = ""
    state_of_residence: str = ""
    international: bool = False
    force_task_a: bool = False  # weighted-only GPA → Task A, never the /5 fraction path
    missing_required_essays: bool = False  # → NEEDS_REVIEW
    mapping_notes: tuple[str, ...] = ()  # contract-drift observations for the audit trail


def _gpa_string(unweighted: str | None, weighted: str | None) -> tuple[str, bool]:
    """Reduce the payload GPA to (raw string for Stage 2, force_task_a flag)."""
    if u := (unweighted or "").strip():
        return u, False
    if w := (weighted or "").strip():
        return w, True  # weighted-only: deterministic /N conversion would be wrong
    return "", False


def map_application_payload(payload: ApplicationPayload) -> WebhookApplicant:
    """Build the pipeline input from the stored combined payload. Pure."""
    notes: list[str] = []

    answers = payload.answers()
    for key in EXPECTED_ANSWER_KEYS:
        if key not in answers:
            notes.append(
                f"all_answers has no {key!r} entry — check the live question config "
                "(field renamed or removed)"
            )

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

    gpa_raw, force_task_a = _gpa_string(payload.gpa_unweighted, payload.gpa_weighted)
    state = answers.get("state_of_residence", "")

    row = ApplicantRow(
        submission_id=str(payload.submission_id),
        # One display name arrives; keep it whole here — the audit record joins first+last
        # with a space, so it still renders correctly.
        first_name=(payload.student_name or "").strip(),
        last_name="",
        email=payload.user_email.strip(),
        institution=answers.get("institution", ""),
        state=state,
        first_choice=(payload.tier_first_choice or "").strip(),
        second_choice=(payload.tier_second_choice or "").strip(),
        third_choice=(payload.tier_third_choice or "").strip(),
        gpa=gpa_raw,
        gpa_explanation=answers.get("gpa_explanation", ""),
        coursework=answers.get("relevant_coursework", ""),
        resume_url=(payload.resume_url or "").strip(),
        essay1=(r1.answer if r1 else "").strip(),
        essay2=(r2.answer if r2 else "").strip(),
        essay3=(o1.answer if o1 else "").strip(),
        # Not on the live CS form — blank is the normal case, never a drift signal.
        programming_languages=answers.get("programming_languages", ""),
        github_profile=answers.get("github_profile", ""),
        sub_track=(payload.detected_sub_track or "").strip(),
    )

    return WebhookApplicant(
        row=row,
        e1=EssayMeta(question=r1.question if r1 else ""),
        e2=EssayMeta(question=r2.question if r2 else ""),
        e3=EssayMeta(question=o1.question if o1 else ""),
        cohort_name=payload.cohort_name,
        state_of_residence=state,
        international=is_international(state),
        force_task_a=force_task_a,
        missing_required_essays=missing_required,
        mapping_notes=tuple(notes),
    )
