"""Tests for the orchestration core (Phase 8). Synthetic data only, no API spend.

8.1 covers the deterministic glue — :func:`build_base_record` (identity/dedup assembly) and
:func:`affirmation_ok` (unchecked-affirmation → NEEDS_REVIEW, but only when the column resolved).
8.2/8.3/8.4 (the LLM-driven runner, the batch runner, and the end-to-end §12 + fail-fast suite)
land in later commits with a scripted :class:`FakeLLMClient`.
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.ingest import (
    AFFIRMATION,
    EMAIL,
    ESSAY1,
    ESSAY2,
    FIRST_NAME,
    GPA,
    LAST_NAME,
    ApplicantRow,
    DedupedRow,
    HeaderResolution,
)
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.models import (
    CourseItem,
    DedupInfo,
    TaskCOutput,
    TaskDOutput,
)
from srip_filter.pipeline import affirmation_ok, build_base_record, grade_one

APP = AppConfig()

# A natural, >100-word essay: clears the Stage-1 length gate (60-500 hard, 100-350 target → no
# soft penalty) and the gibberish heuristics (varied vocabulary, no long runs). Synthetic content.
_GOOD_ESSAY = (
    "Software engineering motivates me because it turns abstract ideas into tools that people "
    "can actually use, and I find that deeply rewarding. Over the past two years I taught myself "
    "Python and JavaScript, built a small web application for my school newspaper, and helped "
    "organize a coding club for younger students who had never written a single line of code "
    "before joining us. I am applying to this track because I want a rigorous foundation in "
    "computer science and the chance to work alongside mentors who care about doing careful, "
    "honest research that matters. In the long run I hope to study how machine learning systems "
    "can be made more transparent, reliable, and genuinely fair for the communities that "
    "increasingly depend on them every day."
)
_GOOD_ESSAY_2 = _GOOD_ESSAY + " Additionally I enjoy collaborating with curious teammates."


def _resolution(*roles: str) -> HeaderResolution:
    """A resolution where the given roles resolved to a dummy header."""
    return HeaderResolution(role_to_header={role: f"<{role}>" for role in roles})


def _deduped(**overrides: str) -> DedupedRow:
    base = dict(submission_id="s1", first_name="Ann", last_name="Lee", email="a@b.com")
    base.update(overrides)
    return DedupedRow(row=ApplicantRow(**base), dedup=DedupInfo())


# ------------------------------------------------------------------------------------------------
# 8.1 — build_base_record
# ------------------------------------------------------------------------------------------------


def test_base_record_fills_identity() -> None:
    rec = build_base_record(_deduped(), _resolution(FIRST_NAME, LAST_NAME, EMAIL))
    assert rec.submission_id == "s1"
    assert rec.name == "Ann Lee"
    assert rec.email == "a@b.com"


def test_base_record_starts_non_terminal() -> None:
    # A survivor placeholder: rank_records treats RANKED as a gate-survivor to score.
    rec = build_base_record(_deduped(), _resolution())
    assert rec.outcome == "RANKED"
    assert rec.final_score is None
    assert rec.rank is None


def test_base_record_program_choices_empty_to_none() -> None:
    rec = build_base_record(_deduped(first_choice="Summer 2026- HONORS"), _resolution())
    assert rec.program_choices.first == "Summer 2026- HONORS"
    assert rec.program_choices.second is None
    assert rec.program_choices.third is None


def test_base_record_carries_dedup_block() -> None:
    deduped = DedupedRow(
        row=ApplicantRow(submission_id="s2", first_name="Bo", last_name="Ng", email="b@c.com"),
        dedup=DedupInfo(is_duplicate_email=True, kept=True, notes="kept first of 2"),
    )
    rec = build_base_record(deduped, _resolution())
    assert rec.dedup.is_duplicate_email is True
    assert rec.dedup.notes == "kept first of 2"


def test_base_record_name_handles_partial() -> None:
    # Identity validation guarantees both names downstream, but assembly must not emit stray space.
    rec = build_base_record(_deduped(last_name=""), _resolution())
    assert rec.name == "Ann"


# ------------------------------------------------------------------------------------------------
# 8.1 — affirmation_ok
# ------------------------------------------------------------------------------------------------


def test_affirmation_present_and_checked_is_ok() -> None:
    row = ApplicantRow(submission_id="s1", affirmation="I affirm this is truthful.")
    assert affirmation_ok(row, _resolution(AFFIRMATION)) is True


def test_affirmation_present_and_blank_is_not_ok() -> None:
    row = ApplicantRow(submission_id="s1", affirmation="")
    assert affirmation_ok(row, _resolution(AFFIRMATION)) is False


def test_affirmation_present_and_whitespace_is_not_ok() -> None:
    row = ApplicantRow(submission_id="s1", affirmation="   ")
    assert affirmation_ok(row, _resolution(AFFIRMATION)) is False


def test_affirmation_column_absent_never_routes() -> None:
    # The column did not resolve: a blank value must NOT be read as "unchecked" for everyone.
    row = ApplicantRow(submission_id="s1", affirmation="")
    assert affirmation_ok(row, _resolution()) is True


# ------------------------------------------------------------------------------------------------
# 8.2 — grade_one per-applicant fail-fast runner (mocked LLM)
# ------------------------------------------------------------------------------------------------

# Roles the survivor path needs resolved: identity, GPA, both essays, and the affirmation.
_SURVIVOR_ROLES = (FIRST_NAME, LAST_NAME, EMAIL, GPA, ESSAY1, ESSAY2, AFFIRMATION)


def _task_d(*, on_topic: bool = True, is_gibberish: bool = False) -> TaskDOutput:
    return TaskDOutput(
        is_gibberish=is_gibberish,
        on_topic=on_topic,
        relevance_confidence=0.9,
        quality_score=18,
        grammar_spelling_penalty=0,
        saliency_notes="",
        rationale="",
    )


def _task_c() -> TaskCOutput:
    return TaskCOutput(
        courses=[
            CourseItem(
                name="AP Computer Science A",
                grade_raw="A",
                grade_pct=95,
                category="cs",
                counts=True,
                category_weight=1.0,
            )
        ],
        rationale="",
    )


def _good_handler(task, user, schema):  # type: ignore[no-untyped-def]
    if task == "task_d":
        return _task_d()
    if task == "task_c":
        return _task_c()
    raise AssertionError(f"unexpected task {task}")


def _applicant(
    *,
    gpa: str = "3.5",
    essay1: str = _GOOD_ESSAY,
    essay2: str = _GOOD_ESSAY_2,
    coursework: str = "AP Computer Science A: A",
    affirmation: str = "I affirm this is truthful.",
    institution: str = "High School",
) -> DedupedRow:
    row = ApplicantRow(
        submission_id="s1",
        first_name="Ann",
        last_name="Lee",
        email="a@b.com",
        gpa=gpa,
        essay1=essay1,
        essay2=essay2,
        coursework=coursework,
        affirmation=affirmation,
        institution=institution,
    )
    return DedupedRow(row=row, dedup=DedupInfo())


async def test_survivor_is_ranked_with_full_scores() -> None:
    client = FakeLLMClient(APP, handler=_good_handler)
    rec = await grade_one(_applicant(), _resolution(*_SURVIVOR_ROLES), client, APP)
    assert rec.outcome == "RANKED"
    assert rec.decided_at_stage == "stage8"
    assert rec.final_score is None  # Stage 8 composes the score; grade_one leaves it None
    assert rec.rank is None
    assert rec.scores.gpa_points == pytest.approx(20.0)  # (3.5 - 3.0)/1.0 * 40
    assert rec.scores.essay.total == pytest.approx(36.0)  # 18 + 18, no penalties
    assert rec.scores.coursework_bonus == pytest.approx(2.85)  # 1.0 * 0.95 * 3.0
    assert rec.scores.school_bonus == 0.0  # "High School" → no match
    assert rec.scores.resume_bonus == 0.0
    assert rec.llm_calls == ["task_d_e1", "task_d_e2", "task_c"]  # GPA resolved deterministically
    assert rec.coursework_breakdown[0].name == "AP Computer Science A"


async def test_stage1_reject_spends_zero_llm() -> None:
    client = FakeLLMClient(APP, handler=_good_handler)
    rec = await grade_one(
        _applicant(essay1="too short to count"), _resolution(*_SURVIVOR_ROLES), client, APP
    )
    assert rec.outcome == "REJECTED"
    assert rec.decided_at_stage == "stage1"
    assert "length" in rec.primary_reason.lower()
    assert client.calls == []  # fail-fast: no tokens past a Stage-1 reject


async def test_unchecked_affirmation_needs_review_zero_llm() -> None:
    client = FakeLLMClient(APP, handler=_good_handler)
    rec = await grade_one(
        _applicant(affirmation=""), _resolution(*_SURVIVOR_ROLES), client, APP
    )
    assert rec.outcome == "NEEDS_REVIEW"
    assert rec.decided_at_stage == "affirmation"
    assert client.calls == []  # affirmation is checked before any LLM stage


async def test_essay_parse_failure_routes_to_needs_review() -> None:
    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_d":
            raise LLMParseFailure(task, "bad json")
        return _good_handler(task, user, schema)

    client = FakeLLMClient(APP, handler=handler)
    rec = await grade_one(_applicant(), _resolution(*_SURVIVOR_ROLES), client, APP)
    assert rec.outcome == "NEEDS_REVIEW"
    assert rec.decided_at_stage == "stage4"
    assert rec.primary_reason == "LLM_PARSE_FAILURE"


async def test_unexpected_error_becomes_needs_review() -> None:
    # Essay headers unresolved: the survivor path raises KeyError reading role_to_header[ESSAY1].
    client = FakeLLMClient(APP, handler=_good_handler)
    resolution = _resolution(FIRST_NAME, LAST_NAME, EMAIL, GPA)  # no ESSAY1/ESSAY2, no AFFIRMATION
    rec = await grade_one(_applicant(), resolution, client, APP)
    assert rec.outcome == "NEEDS_REVIEW"
    assert rec.decided_at_stage == "error"
    assert rec.errors and "KeyError" in rec.errors[0]
