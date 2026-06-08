"""Pydantic v2 schemas (Phase 0.3).

Two families of models:

* **LLM contracts** — the structured-output shapes for Tasks A/B/C/D (PRD §8). Every field is
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
    grade_raw: str = Field(description="Grade exactly as written by the applicant.")
    grade_pct: int = Field(ge=0, le=100, description="Grade normalized to a 0-100 percentage.")
    category: CourseCategory
    counts: bool = Field(description="False if grade_pct < 80 or category == 'other'.")
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
    """A simple boolean gate result (profanity, gibberish)."""

    hit: bool = False


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
    resume_bonus: float = 0.0  # always 0 in current scope (deferred)


class SchoolMatch(_Model):
    matched_name: str | None = None
    list: SchoolListName | None = None
    fuzzy_score: float = 0.0


class AuditRecord(_Model):
    """One decision record per applicant (PRD §9)."""

    submission_id: str
    name: str = ""
    email: str = ""
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

    coursework_breakdown: list[CourseItem] = Field(default_factory=list)
    school_match: SchoolMatch = Field(default_factory=SchoolMatch)

    reasons: list[str] = Field(default_factory=list)
    llm_calls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
