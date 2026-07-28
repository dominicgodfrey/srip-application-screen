"""Webhook payload → pipeline input mapping (P4, PRD v3 §2.2/§4).

The v3 front door's equivalent of Stage 0: turn a validated
:class:`~srip_filter.models.EssaysModePayload` (plus the optionally stored resume-mode
payload) into the :class:`~srip_filter.ingest.ApplicantRow` the existing stages consume,
plus the per-essay metadata (exact word bounds, question text) that v3's strict Stage 1
needs. No LLM, no I/O.

Mapping rules that carry decisions:

* **Essays:** ``required_essays[0]`` → essay 1, ``[1]`` → essay 2 (the site orders by
  ``sort_order``: motivation, then trajectory). The bonus essay is ``optional_essays[0]``.
  The site tags these via ``ats_role`` on the live question config, so the arrays are
  authoritative — no field_key guessing. Fewer than two required essays ⇒ unscoreable →
  ``NEEDS_REVIEW`` (never silently rejected); surplus entries are noted in
  ``mapping_notes`` (contract-drift signal), not graded.
* **Named fields come from ``all_answers``** — the full form dump is the only place
  ``field_key`` exists. An expected-but-absent key appends a ``mapping_notes`` entry;
  absent and blank are the same thing to the gates (owner decision D3, 2026-07-27).
* **GPA:** ``gpa_unweighted`` is primary (deterministic path). A weighted-only
  submission sets ``force_task_a`` — the deterministic /5 conversion would misread a
  weighted scale (PRD v3 §4 Stage 2).
* **International:** derived, not trusted from a sentinel — a non-blank
  ``state_of_residence`` that is not a US state/DC/territory name ⇒ international. The
  live dropdown's sole non-US value, ``"Non-U.S. Territory"``, falls out of this
  unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ingest import ApplicantRow
from .models import ApplicationPayload

# Named answers we read out of `all_answers`. `programming_languages` and `github_profile`
# are NOT on the live CS form (repo seed only, verified 2026-07-28) — absent is normal for
# them, so they stay out of the expected set and never raise a drift note.
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
    """Per-essay metadata carried alongside the text — question text only.

    Word bounds retired 2026-07-28: the site server-validates them at submit (400, the
    submission never lands), so re-checking here could only ever fire on our own stale
    config. Task D falls back to its module-default target range.
    """

    question: str = ""


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
        # The webhook carries one display name; keep it whole in first_name (the audit
        # record joins first+last with a space, so this renders correctly).
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
