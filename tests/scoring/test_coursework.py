"""Tests for Stage 5 coursework bonus (Phase 5, Task C). Synthetic data only, no API spend.

5.1 pins the Task C prompt shape; 5.2 pins the pure bonus math (weights by category, the <80% and
'other' zero-outs, the cap, never-negative, empty->0); 5.3 drives the aggregator with a
:class:`FakeLLMClient` (empty->no call, parse-failure->0 bonus, bonus composition).
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.ingest import ApplicantRow
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.llm.prompts import task_c as task_c_prompt
from srip_filter.models import CourseCategory, CourseItem, TaskCOutput
from srip_filter.scoring.coursework import coursework_bonus, score_coursework

APP = AppConfig()
CFG = APP.coursework


def _course(
    *,
    name: str = "Course",
    grade_pct: int = 95,
    category: CourseCategory = "cs",
    # weight/counts are deliberately set to wrong values to prove the system recomputes them.
    counts: bool = False,
    category_weight: float = 0.0,
) -> CourseItem:
    return CourseItem(
        name=name,
        grade_raw="A",
        grade_pct=grade_pct,
        category=category,
        counts=counts,
        category_weight=category_weight,
    )


def _task_c(*courses: CourseItem) -> TaskCOutput:
    return TaskCOutput(courses=list(courses), rationale="")


# ------------------------------------------------------------------------------------------------
# 5.1 — Task C prompt shape
# ------------------------------------------------------------------------------------------------


def test_system_prompt_is_json_only_and_names_categories() -> None:
    system = task_c_prompt.SYSTEM.lower()
    assert "only json" in system
    assert "cs" in system and "math" in system and "data" in system and "other" in system


def test_user_prompt_renders_template() -> None:
    rendered = task_c_prompt.user_prompt("AP CS A: 95, Calculus BC: 88")
    assert rendered == 'COURSEWORK_RAW: """AP CS A: 95, Calculus BC: 88"""'


# ------------------------------------------------------------------------------------------------
# 5.2 — coursework_bonus (pure)
# ------------------------------------------------------------------------------------------------


def test_empty_coursework_scores_zero() -> None:
    r = coursework_bonus(_task_c(), CFG)
    assert r.bonus == 0.0
    assert r.courses == []


def test_cs_course_uses_cs_weight() -> None:
    # per_course = weight_cs(1.0) * unit(3.0) = 3.0 — flat; the grade never scales the bonus
    r = coursework_bonus(_task_c(_course(grade_pct=95, category="cs")), CFG)
    assert r.bonus == pytest.approx(3.0)


def test_math_and_data_weighted_below_cs() -> None:
    # math: 0.8 * 3.0 = 2.4 ; data: 0.6 * 3.0 = 1.8 (flat per-course contributions)
    math = coursework_bonus(_task_c(_course(category="math")), CFG).bonus
    data = coursework_bonus(_task_c(_course(category="data")), CFG).bonus
    cs = coursework_bonus(_task_c(_course(category="cs")), CFG).bonus
    assert math == pytest.approx(2.4)
    assert data == pytest.approx(1.8)
    assert cs > math > data  # relevance ordering holds


def test_grade_does_not_scale_bonus() -> None:
    # An 85 and a 100 in the same category contribute identically.
    low = coursework_bonus(_task_c(_course(category="cs", grade_pct=85)), CFG).bonus
    high = coursework_bonus(_task_c(_course(category="cs", grade_pct=100)), CFG).bonus
    assert low == high == pytest.approx(3.0)


def test_ungraded_course_counts_at_full_weight() -> None:
    # No explicit grade (grade_pct=None) is neutral — the course still counts.
    r = coursework_bonus(_task_c(_course(category="cs", grade_pct=None)), CFG)
    assert r.bonus == pytest.approx(3.0)
    assert r.courses[0].counts is True


def test_other_category_contributes_zero() -> None:
    r = coursework_bonus(_task_c(_course(category="other", grade_pct=100)), CFG)
    assert r.bonus == 0.0
    assert r.courses[0].counts is False
    assert r.courses[0].category_weight == pytest.approx(CFG.weight_other)


def test_explicit_grade_below_floor_excludes_course() -> None:
    # 78 (C+) < 80 floor -> the course is excluded entirely, even for CS.
    r = coursework_bonus(_task_c(_course(category="cs", grade_pct=78)), CFG)
    assert r.bonus == 0.0
    assert r.courses[0].counts is False


def test_grade_at_floor_counts() -> None:
    # exactly the floor (80) counts at the flat contribution: 1.0 * 3.0 = 3.0
    r = coursework_bonus(_task_c(_course(category="cs", grade_pct=80)), CFG)
    assert r.bonus == pytest.approx(3.0)
    assert r.courses[0].counts is True


def test_weights_and_counts_recomputed_not_trusted_from_llm() -> None:
    # The LLM claims a high weight + counts on an 'other' course; the system overrides both.
    bogus = _course(category="other", grade_pct=100, counts=True, category_weight=5.0)
    r = coursework_bonus(_task_c(bogus), CFG)
    assert r.bonus == 0.0
    assert r.courses[0].counts is False
    assert r.courses[0].category_weight == pytest.approx(CFG.weight_other)


def test_bonus_is_capped() -> None:
    # Many strong CS courses would sum past the cap; bonus is clamped to bonus_max.
    many = [_course(category="cs", grade_pct=100) for _ in range(20)]
    r = coursework_bonus(_task_c(*many), CFG)
    assert r.bonus == pytest.approx(CFG.bonus_max)


def test_bonus_never_negative() -> None:
    # No course can ever produce a negative; absence of relevant coursework is neutral.
    r = coursework_bonus(_task_c(_course(category="other", grade_pct=50)), CFG)
    assert r.bonus >= 0.0


# ------------------------------------------------------------------------------------------------
# 5.3 — score_coursework aggregator (mocked Task C)
# ------------------------------------------------------------------------------------------------


def _row(coursework: str = "AP CS A: 95") -> ApplicantRow:
    return ApplicantRow(submission_id="s1", coursework=coursework)


def _client(handler) -> FakeLLMClient:  # type: ignore[no-untyped-def]
    return FakeLLMClient(APP, handler=handler)


async def test_empty_cell_makes_no_call() -> None:
    client = _client(lambda t, u, s: _task_c(_course()))
    r = await score_coursework(_row(""), client, APP)
    assert r.bonus == 0.0
    assert r.courses == []
    assert client.calls == []  # no token spent on a blank optional signal


async def test_whitespace_only_cell_makes_no_call() -> None:
    client = _client(lambda t, u, s: _task_c(_course()))
    r = await score_coursework(_row("   "), client, APP)
    assert r.bonus == 0.0
    assert client.calls == []


async def test_bonus_composition_from_task_c() -> None:
    client = _client(
        lambda t, u, s: _task_c(
            _course(category="cs", grade_pct=95),
            _course(category="math", grade_pct=90),
        )
    )
    r = await score_coursework(_row(), client, APP)
    # cs: 1.0*3.0 = 3.0 ; math: 0.8*3.0 = 2.4 (flat; grades don't scale)
    assert r.bonus == pytest.approx(5.4)
    assert len(r.courses) == 2
    assert r.error == ""
    assert r.raw is not None


async def test_uses_task_c_model_name() -> None:
    client = _client(lambda t, u, s: _task_c(_course()))
    await score_coursework(_row(), client, APP)
    assert all(call[0] == "task_c" for call in client.calls)
    assert len(client.calls) == 1


async def test_parse_failure_degrades_to_zero_bonus_never_blocks() -> None:
    def boom(t, u, s):  # type: ignore[no-untyped-def]
        raise LLMParseFailure(t, "bad json")

    client = _client(boom)
    r = await score_coursework(_row(), client, APP)
    assert r.bonus == 0.0  # bonus-only signal degrades to neutral
    assert r.courses == []
    assert "LLM_PARSE_FAILURE" in r.error  # recorded for the audit, not a rejection
    assert r.raw is None
