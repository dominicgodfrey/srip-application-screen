"""Stage 5 — coursework bonus (Phase 5, Task C).

Runs only on Stage 1-4 survivors and is **bonus-only** (PRD §0.3/§5/§7): it can add to
``final_score``, never subtract, and can never change a ``REJECTED``/``NEEDS_REVIEW`` outcome.
Empty ``Relevant Coursework`` → 0 bonus with no LLM call.

Task C decomposes the free-text cell into courses, classifies each cs/math/data/other, and
normalizes each grade to a 0-100 percentage. The deterministic layer then applies the config
weights + the 80% floor and sums a capped bonus. The work is split so the LLM call is isolated
and the §8.4 bonus math stays fully testable with zero API spend:

  * 5.2 pure bonus math — :func:`coursework_bonus`  (pure, no LLM)
  * 5.3 Stage 5 aggregator — :func:`score_coursework` (LLM)

Two deliberate decisions (see PLAN.md Notes):

* **Weights/counts are recomputed from config**, never trusted from the model — the model
  classifies ``category`` and normalizes ``grade_pct``; the system owns the tunable weights and
  the 80% floor (mirroring how Stage 3 computes ``gpa_points`` deterministically).
* A Task C parse failure → **0 bonus + an audit error note, not NEEDS_REVIEW** — a bonus-only
  signal that cannot be extracted is neutral; the applicant stays fully scoreable on the required
  signals (GPA + essays).

Thresholds come from ``AppConfig.coursework``; no magic numbers here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import AppConfig, CourseworkConfig
from ..ingest import ApplicantRow
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_c as task_c_prompt
from ..models import CourseCategory, CourseItem, TaskCOutput

# ================================================================================================
# 5.2 — Pure coursework bonus math (no LLM, PRD §5 / §8.4)
# ================================================================================================
# Recompute each course's weight + counts from config (using the LLM's category + grade_pct),
# then sum per_course = weight * (grade_pct/100) * unit over counting courses, capped and floored
# at 0. The reconciled courses[] (with recomputed weight/counts) is what goes into the audit, so
# the record shows exactly what the system used — not the model's own guesses.


@dataclass(frozen=True)
class CourseworkResult:
    """Bonus + the reconciled per-course breakdown for the audit record.

    ``courses`` carries each :class:`CourseItem` with ``category_weight``/``counts`` recomputed
    from config, so :attr:`bonus` is fully reconstructable from it. ``bonus`` is in
    ``[0, coursework_bonus_max]``.
    """

    bonus: float
    courses: list[CourseItem] = field(default_factory=list)


def _weight_for(category: CourseCategory, cfg: CourseworkConfig) -> float:
    """Resolve the config weight for a category (cs/math/data/other)."""
    return {
        "cs": cfg.weight_cs,
        "math": cfg.weight_math,
        "data": cfg.weight_data,
        "other": cfg.weight_other,
    }[category]


def coursework_bonus(out: TaskCOutput, cfg: CourseworkConfig) -> CourseworkResult:
    """Apply the PRD §8.4 bonus math to a Task C output. Pure function, never negative.

    For each course, weight and ``counts`` are **recomputed from config**: ``counts`` is
    ``category != "other" and grade_pct >= min_grade_pct``, and a counting course contributes
    ``weight * (grade_pct/100) * unit``. The sum is capped at ``bonus_max`` and floored at 0.
    Returns the bonus plus the reconciled ``courses[]`` (weights/counts as actually applied).
    """
    reconciled: list[CourseItem] = []
    total = 0.0
    for course in out.courses:
        weight = _weight_for(course.category, cfg)
        counts = course.category != "other" and course.grade_pct >= cfg.min_grade_pct
        if counts:
            total += weight * (course.grade_pct / 100.0) * cfg.unit
        reconciled.append(
            course.model_copy(update={"category_weight": weight, "counts": counts})
        )
    bonus = max(0.0, min(cfg.bonus_max, total))
    return CourseworkResult(bonus=round(bonus, 4), courses=reconciled)


# ================================================================================================
# 5.3 — Stage 5 aggregator (LLM)
# ================================================================================================
# score_coursework short-circuits an empty cell (no token), otherwise calls Task C and applies
# 5.2. A parse failure degrades to 0 bonus + an error note — never NEEDS_REVIEW/REJECTED, because
# a bonus-only signal that cannot be extracted is neutral (PRD §0.3).


@dataclass(frozen=True)
class Stage5Result:
    """Reduced outcome of Stage 5 for one application.

    ``bonus`` drops into ``Scores.coursework_bonus`` and ``courses`` into
    ``AuditRecord.coursework_breakdown``. ``error`` is "" normally; on a Task C parse failure it
    carries a note for ``AuditRecord.errors`` while the applicant stays scoreable (bonus 0).
    ``raw`` is the Task C output for the audit, or ``None`` when no call was made / it failed.
    """

    bonus: float
    courses: list[CourseItem]
    error: str
    raw: TaskCOutput | None


async def score_coursework(
    row: ApplicantRow, client: BaseLLMClient, cfg: AppConfig
) -> Stage5Result:
    """Stage 5 end to end: decompose coursework with Task C and compute the capped bonus.

    An empty ``Relevant Coursework`` cell → ``bonus=0`` with no token spent. Otherwise Task C
    runs (cached/bounded by the client) and 5.2 applies the config weights + 80% floor. A Task C
    :class:`LLMParseFailure` (after the client's retry) degrades to ``bonus=0`` with an audit
    error note — never ``NEEDS_REVIEW``/``REJECTED`` (bonus-only signal; absence is neutral).
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
