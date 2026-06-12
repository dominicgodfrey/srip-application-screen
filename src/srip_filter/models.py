"""Pydantic v2 schemas (Phase 0.3).

Two families of models:

* **LLM contracts** — the structured-output shapes for Tasks A/B/C/D (PRD §8) and Task E
  (resume signal extraction, Phase 12). Every field is
  required (no defaults) and unknown keys are forbidden, so each maps cleanly to an OpenAI
  Structured Outputs ``json_schema`` (``additionalProperties: false``, all-required).
* **Audit record** — the per-applicant decision record (PRD §9), built in Python and emitted
  to ``decisions.jsonl``. These carry convenience defaults.

Deviation note: ``TaskDOutput.is_gibberish`` is an addition to the PRD §8.3 schema — gibberish
detection was moved into the LLM as a Task-D backstop (see PLAN.md decisions log).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Outcome = Literal["REJECTED", "RANKED", "NEEDS_REVIEW"]
Confidence = Literal["high", "med", "low"]
GpaSource = Literal["deterministic", "llm"]
CourseCategory = Literal["cs", "math", "data", "other"]
SchoolListName = Literal["us_top20", "intl_top50"]
AssignmentStatus = Literal["assigned", "waitlisted", "unassignable"]


class _Model(BaseModel):
    """Base: forbid unknown keys (also yields the closed schema strict outputs require)."""

    model_config = ConfigDict(extra="forbid")


# ============================================================================================
# LLM contracts (PRD §8) — all fields required, structured-output friendly
# ============================================================================================


class TaskAOutput(_Model):
    """Task A — GPA normalization for ambiguous/non-standard values (PRD §6.1)."""

    normalized_gpa: float | None = Field(
        description="4.0-scale equivalent, capped at 4.0; null if it cannot be placed."
    )
    original_scale: str = Field(
        description="Detected source scale, e.g. weighted_gt_4, percentage, out_of_10, unknown."
    )
    conversion_method: str = Field(description="Short description of how the value was derived.")
    confidence: Confidence
    requires_manual_review: bool = Field(
        description="True if the value cannot be safely placed and a human must resolve it."
    )
    rationale: str = Field(description="1-2 sentence justification for the audit log.")


class TaskBOutput(_Model):
    """Task B — low-GPA extenuating-circumstances adequacy (PRD §8.2)."""

    explanation_adequate: bool
    strength_of_reason: float = Field(ge=0.0, le=1.0)
    realistic: bool
    severity_vs_reason_balanced: bool = Field(
        description="Does the reason's strength scale with the size of the GPA gap?"
    )
    recommended_outcome: Literal["rank", "reject"]
    rationale: str = Field(description="1-2 sentence justification for the audit log.")


class CourseItem(_Model):
    """A single decomposed course (PRD §8.4)."""

    name: str
    grade_raw: str = Field(
        description="Grade exactly as written by the applicant; empty string if none stated."
    )
    grade_pct: int | None = Field(
        ge=0,
        le=100,
        description="Grade normalized to a 0-100 percentage; null when no grade was stated.",
    )
    category: CourseCategory
    counts: bool = Field(
        description="False if category == 'other' or an explicit grade falls below a B."
    )
    category_weight: float = Field(
        ge=0.0,
        description="cs=1.0, math=0.8, data=0.6, other=0.0 (tunable in config).",
    )


class TaskCOutput(_Model):
    """Task C — coursework decomposition + relevance (PRD §8.4)."""

    courses: list[CourseItem]
    rationale: str = Field(description="Short note for the audit log.")


class TaskDOutput(_Model):
    """Task D — per-essay gibberish/relevance gates + quality score (PRD §8.3).

    ``is_gibberish`` and ``on_topic`` are gates: either one failing disqualifies the whole
    application. The remaining fields feed the additive essay score.
    """

    is_gibberish: bool = Field(
        description="Checked first; true => REJECTED (keyboard-mashing / good-faith failure)."
    )
    on_topic: bool = Field(description="False => REJECTED as off-topic for the given prompt.")
    relevance_confidence: float = Field(ge=0.0, le=1.0)
    quality_score: int = Field(ge=0, le=20, description="Specificity, coherence, saliency.")
    grammar_spelling_penalty: int = Field(
        ge=0,
        le=3,
        description="Slight penalty for genuine errors only; never for ESL writing.",
    )
    saliency_notes: str = Field(description="What made the essay strong or weak.")
    rationale: str = Field(description="1-2 sentences for the audit log.")


class TaskEOutput(_Model):
    """Task E — resume signal extraction (PRD §7.2, Phase 12).

    Mechanical extraction (mini tier): the model **counts and classifies** signals relevant to
    software engineering; it never prices them. The deterministic layer
    (:func:`srip_filter.scoring.resume.resume_signal_bonus`) applies the config weights —
    the Task C "model classifies, config prices" pattern. Bonus-only: nothing here can reject.
    """

    is_resume: bool = Field(
        description="True if the text is actually a resume/CV (not a cover letter, blank page, "
        "or unrelated document)."
    )
    relevant_projects: int = Field(
        ge=0,
        description="Count of concrete software/CS/data projects (personal, school, or club).",
    )
    relevant_experience: int = Field(
        ge=0,
        description="Count of internships, jobs, or research positions relevant to software/CS.",
    )
    relevant_awards: int = Field(
        ge=0,
        description="Count of CS/STEM competition awards, hackathon placements, olympiads.",
    )
    skills_relevance: float = Field(
        ge=0.0,
        le=1.0,
        description="Depth of programming languages/tools/frameworks listed, 0 (none) to 1.",
    )
    highlights: str = Field(description="Short note on the strongest signals, for the audit log.")
    rationale: str = Field(description="1-2 sentence justification for the audit log.")


# ============================================================================================
# Audit record (PRD §9) — built in Python, emitted to decisions.jsonl
# ============================================================================================


class ProgramChoices(_Model):
    first: str | None = None
    second: str | None = None
    third: str | None = None


class DedupInfo(_Model):
    is_duplicate_email: bool = False
    is_duplicate_name: bool = False
    kept: bool = True
    notes: str = ""


class EssayLengthGate(_Model):
    e1_wc: int = 0
    e2_wc: int = 0
    e1_ok: bool = True
    e2_ok: bool = True
    hard_fail: bool = False


class HitGate(_Model):
    """A boolean gate result (profanity, gibberish) plus what tripped it.

    ``terms`` makes a hit auditable: for profanity it lists the offending tokens; for
    gibberish it names the deterministic signals that fired (prefixed ``e1:``/``e2:``) or
    ``task_d`` when the LLM backstop flagged it. Empty when ``hit`` is False.
    """

    hit: bool = False
    terms: list[str] = Field(default_factory=list)


class GpaGate(_Model):
    passed: bool = False
    reason: str = ""


class EssayRelevanceGate(_Model):
    e1_on_topic: bool | None = None
    e2_on_topic: bool | None = None


class Gates(_Model):
    essay_length: EssayLengthGate = Field(default_factory=EssayLengthGate)
    profanity: HitGate = Field(default_factory=HitGate)
    gibberish: HitGate = Field(default_factory=HitGate)
    gpa_gate: GpaGate = Field(default_factory=GpaGate)
    essay_relevance: EssayRelevanceGate = Field(default_factory=EssayRelevanceGate)


class GpaAssessment(_Model):
    """Stage-2/3 GPA result for the audit record (PRD §9 'gpa' block + §6.1 fields)."""

    raw: str | None = None
    normalized_gpa: float | None = None
    original_scale: str | None = None
    conversion_method: str | None = None
    confidence: Confidence | None = None
    below_threshold: bool | None = None
    requires_manual_review: bool = False
    source: GpaSource | None = None
    explanation_text: str = ""  # the applicant's extenuating-circumstances text, verbatim
    explanation_eval: TaskBOutput | None = None  # populated only if Task B ran


class EssaySubscores(_Model):
    e1: float = 0.0
    e2: float = 0.0
    total: float = 0.0


class Scores(_Model):
    gpa_points: float = 0.0
    essay: EssaySubscores = Field(default_factory=EssaySubscores)
    coursework_bonus: float = 0.0
    school_bonus: float = 0.0
    resume_bonus: float = 0.0  # 0 unless Stage 6 extracts signals (Phase 12); kill switch -> 0


class SchoolMatch(_Model):
    matched_name: str | None = None
    list: SchoolListName | None = None
    fuzzy_score: float = 0.0


class ResumeAssessment(_Model):
    """Stage-6 resume result for the audit record (Phase 12, PRD §7.2).

    Carries the fetch/extract/Task-E trail — **never the resume bytes or text** (the
    fetch→extract→discard memory rule; resume content is PII and is dropped the moment the
    signals are extracted). ``failure`` holds a typed reason when any step failed; the bonus
    degrades to 0 and the applicant is unaffected otherwise (bonus-only, §0.3).
    """

    url_present: bool = False
    url: str = ""  # the resume link as submitted, so a reviewer can open it from the audit UI
    attempted: bool = False  # False when the kill switch (bonus_max == 0) or no URL skipped it
    fetched: bool = False
    extracted_chars: int = 0
    signals: TaskEOutput | None = None  # populated only when Task E ran
    failure: str = ""  # "" = no failure; otherwise a typed reason for the audit log


class EssayTexts(_Model):
    """The applicant's two essays, verbatim, carried on the audit record for the audit UI.

    Returned to the user inside ``decisions.jsonl`` (their own uploaded data) and held only in
    the transient in-memory job — never persisted server-side. Needed so a human auditor can
    read the essays (and see the highlighted profanity/gibberish section) without re-opening
    the source CSV.
    """

    e1: str = ""
    e2: str = ""


class AuditRecord(_Model):
    """One decision record per applicant (PRD §9)."""

    submission_id: str
    name: str = ""
    email: str = ""
    phone: str = ""
    program_choices: ProgramChoices = Field(default_factory=ProgramChoices)
    dedup: DedupInfo = Field(default_factory=DedupInfo)

    outcome: Outcome
    final_score: float | None = None
    rank: int | None = None
    decided_at_stage: str = ""
    primary_reason: str = ""

    gates: Gates = Field(default_factory=Gates)
    gpa: GpaAssessment = Field(default_factory=GpaAssessment)
    scores: Scores = Field(default_factory=Scores)
    essays: EssayTexts = Field(default_factory=EssayTexts)

    # True when a human pushed a REJECTED/NEEDS_REVIEW applicant into the ranking via the
    # audit UI (the §10.2 human-resolution path). The original gate verdicts stay visible in
    # `gates`/`reasons`; this flag keeps the override honest in the audit trail.
    manual_override: bool = False

    coursework_breakdown: list[CourseItem] = Field(default_factory=list)
    school_match: SchoolMatch = Field(default_factory=SchoolMatch)
    resume: ResumeAssessment = Field(default_factory=ResumeAssessment)

    reasons: list[str] = Field(default_factory=list)
    llm_calls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ============================================================================================
# Cohort assignment (PRD §11, Phase 11) — downstream of ranking, consumes AuditRecords
# ============================================================================================


class CohortCapacities(_Model):
    """Per-tier seat caps for cohort assignment (PRD §11).

    ``None`` = unlimited — the default, since demand realistically won't hit any cap, in which
    case every applicant lands in their first choice. These are a per-request staff knob, not
    config: the whole point is live what-if recomputation as the numbers change.
    """

    honors: int | None = Field(default=None, ge=0)
    intensive: int | None = Field(default=None, ge=0)
    regular: int | None = Field(default=None, ge=0)

    def for_tier(self, tier: str) -> int | None:
        """Capacity for a canonical tier name; tiers without a declared cap are unlimited."""
        value = getattr(self, tier, None)
        return value if isinstance(value, int) else None


class CohortAssignment(_Model):
    """One applicant's cohort outcome — a row in ``cohort_assignments.csv`` / the staff UI table.

    ``choice_number`` is the 1-based position of the assigned tier among the applicant's
    *distinct* listed choices (repeats collapse). ``excluded_by_cost`` lists the tiers the
    applicant ranked *above* their first choice — never assignable under the cost ceiling
    (higher tiers cost more; the first choice caps what they signed up to pay) — kept visible
    for the staff audit trail.
    """

    submission_id: str
    name: str = ""
    email: str = ""
    phone: str = ""
    rank: int | None = None
    final_score: float | None = None
    status: AssignmentStatus
    assigned_tier: str | None = None
    choice_number: int | None = None
    excluded_by_cost: list[str] = Field(default_factory=list)
    choices: list[str] = Field(default_factory=list)
    reason: str = ""


class TierSummary(_Model):
    """Fill state of one tier after assignment. ``open_seats`` is ``None`` when unlimited."""

    capacity: int | None = None
    filled: int = 0
    open_seats: int | None = None
    first_choice_demand: int = 0


class CohortSummary(_Model):
    """Run-level facts for the staff view: fill state, demand, satisfaction, and warnings."""

    total_ranked: int = 0
    assigned: int = 0
    waitlisted: int = 0
    unassignable: int = 0
    tiers: dict[str, TierSummary] = Field(default_factory=dict)
    choice_satisfaction: dict[str, int] = Field(default_factory=dict)
    needs_review_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class CohortResult(_Model):
    """Full output of :func:`srip_filter.cohort.assign_cohorts`.

    Returned to the staff user (JSON or CSV), never persisted — stateless like everything else.
    Each list is rank-ordered; every ``RANKED`` input record appears in exactly one of them.
    """

    assignments: list[CohortAssignment] = Field(default_factory=list)
    waitlist: list[CohortAssignment] = Field(default_factory=list)
    unassignable: list[CohortAssignment] = Field(default_factory=list)
    summary: CohortSummary = Field(default_factory=CohortSummary)
