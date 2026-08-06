"""Stage 5 — coursework bonus, Task C (PRD §0.3/§5/§7).

Bonus-only: it can add to ``final_score``, never subtract, and never changes an outcome. Task C
decomposes the free-text cell into classified courses with normalized grades; the deterministic
layer then applies the config weights and the 80% floor. Two deliberate choices: weights and
``counts`` are recomputed from config rather than trusted from the model, and a parse failure
degrades to 0 bonus plus an audit note rather than ``NEEDS_REVIEW`` — a bonus-only signal that
cannot be extracted is neutral, and the required signals still score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..applicant import ApplicantRow
from ..config import AppConfig, CourseworkConfig
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_c as task_c_prompt
from ..models import CourseCategory, CourseItem, TaskCOutput

# --- Pure coursework bonus math (no LLM, PRD §5 / §8.4) ---


@dataclass(frozen=True)
class CourseworkResult:
    """Bonus in ``[0, bonus_max]`` plus the reconciled breakdown: each :class:`CourseItem` carries
    the weight/counts actually applied, so the audit shows what the system used, not the model's
    guesses, and the bonus is reconstructable from it."""

    bonus: float
    courses: list[CourseItem] = field(default_factory=list)


def _weight_for(category: CourseCategory, cfg: CourseworkConfig) -> float:
    """Resolve the config weight for a category."""
    return {
        "cs": cfg.weight_cs,
        "math": cfg.weight_math,
        "data": cfg.weight_data,
        "other": cfg.weight_other,
    }[category]


def coursework_bonus(out: TaskCOutput, cfg: CourseworkConfig) -> CourseworkResult:
    """Apply the coursework bonus math to a Task C output. Pure, never negative.

    Grades are exclusion-only: a course counts unless it is ``"other"`` or carries an explicit
    grade below ``min_grade_pct``, and a counting course contributes a flat ``weight * unit`` —
    the grade never scales the bonus. The sum is capped at ``bonus_max``.
    """
    reconciled: list[CourseItem] = []
    total = 0.0
    for course in out.courses:
        weight = _weight_for(course.category, cfg)
        grade_ok = course.grade_pct is None or course.grade_pct >= cfg.min_grade_pct
        counts = course.category != "other" and grade_ok
        if counts:
            total += weight * cfg.unit
        reconciled.append(course.model_copy(update={"category_weight": weight, "counts": counts}))
    bonus = max(0.0, min(cfg.bonus_max, total))
    return CourseworkResult(bonus=round(bonus, 4), courses=reconciled)


# --- Stage 5 aggregator (LLM) ---


@dataclass(frozen=True)
class Stage5Result:
    """Reduced Stage-5 outcome. ``error`` is "" normally; on a parse failure it carries a note for
    ``AuditRecord.errors`` while the applicant stays scoreable at bonus 0. ``raw`` is ``None``
    when no call was made or it failed."""

    bonus: float
    courses: list[CourseItem]
    error: str
    raw: TaskCOutput | None


async def score_coursework(
    row: ApplicantRow, client: BaseLLMClient, cfg: AppConfig
) -> Stage5Result:
    """Stage 5 end to end: decompose coursework with Task C and compute the capped bonus.

    An empty cell short-circuits to ``bonus=0`` with no token spent; a parse failure degrades to
    ``bonus=0`` plus an audit note, never ``NEEDS_REVIEW``/``REJECTED``.
    """
    if not row.coursework.strip():
        return Stage5Result(bonus=0.0, courses=[], error="", raw=None)

    try:
        out = await client.complete(
            "task_c",
            system=task_c_prompt.SYSTEM,
            user=task_c_prompt.user_prompt(row.coursework),
            schema=TaskCOutput,
        )
    except LLMParseFailure:
        return Stage5Result(
            bonus=0.0,
            courses=[],
            error="LLM_PARSE_FAILURE: coursework bonus not extracted (neutral)",
            raw=None,
        )

    result = coursework_bonus(out, cfg.coursework)
    return Stage5Result(bonus=result.bonus, courses=result.courses, error="", raw=out)
