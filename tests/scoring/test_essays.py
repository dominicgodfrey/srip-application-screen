"""Tests for Stage 4 essay LLM grading (Phase 4, Task D). Synthetic data only, no API spend.

4.1 pins the Task D prompt shape; 4.2 pins the pure post-processing math; 4.3 drives the
aggregator with a :class:`FakeLLMClient` (reject-on-either-essay, parse-failure routing,
total-score composition, and that a gated essay yields no score).
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.ingest import ApplicantRow
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.llm.prompts import task_d as task_d_prompt
from srip_filter.models import TaskDOutput
from srip_filter.scoring.essays import grade_essays, score_one_essay

APP = AppConfig()
CFG = APP.essay_scoring


def _task_d(
    *,
    is_gibberish: bool = False,
    on_topic: bool = True,
    quality_score: int = 15,
    grammar_spelling_penalty: int = 0,
) -> TaskDOutput:
    return TaskDOutput(
        is_gibberish=is_gibberish,
        on_topic=on_topic,
        relevance_confidence=0.9,
        quality_score=quality_score,
        grammar_spelling_penalty=grammar_spelling_penalty,
        saliency_notes="",
        rationale="",
    )


# ------------------------------------------------------------------------------------------------
# 4.1 — Task D prompt shape
# ------------------------------------------------------------------------------------------------


def test_system_prompt_is_json_only_and_esl_safe() -> None:
    system = task_d_prompt.SYSTEM.lower()
    assert "only json" in system
    assert "esl" in system  # the ESL safeguard is spelled out
    assert "gibberish" in system and "on_topic" in system  # both gates named


def test_user_prompt_renders_template() -> None:
    rendered = task_d_prompt.user_prompt("Why do you want to apply?", 230, "Because I love code.")
    assert 'PROMPT: """Why do you want to apply?"""' in rendered
    assert "WORD_COUNT: 230" in rendered
    assert "TARGET_RANGE: 100-350" in rendered
    assert 'ESSAY: """Because I love code."""' in rendered


# ------------------------------------------------------------------------------------------------
# 4.2 — score_one_essay (pure)
# ------------------------------------------------------------------------------------------------


def test_clean_essay_scores_quality_minus_grammar_penalty() -> None:
    r = score_one_essay(_task_d(quality_score=13, grammar_spelling_penalty=2), CFG)
    assert not r.gated
    assert r.score == pytest.approx(11.0)  # 13 - 2


def test_no_penalty_scores_full_quality() -> None:
    r = score_one_essay(_task_d(quality_score=15), CFG)
    assert r.score == pytest.approx(15.0)


def test_the_grammar_penalty_never_drives_a_score_negative() -> None:
    # The penalty is capped at 3 by config, but the floor is asserted here regardless: a
    # negative subscore would be a deduction, and nothing in the model deducts.
    r = score_one_essay(_task_d(quality_score=1, grammar_spelling_penalty=3), CFG)
    assert r.score == 0.0
    assert not r.gated  # floored, but still on-topic and genuine — not a gate


def test_score_capped_at_quality_max_each() -> None:
    """Defensive: the clamp holds even for a value the schema would have refused.

    ``quality_score`` is ``le=15`` and the grammar penalty is ``ge=0``, so nothing valid can
    exceed the cap today — the point is that raising ``quality_max_each`` or loosening the
    schema later cannot silently produce an out-of-band subscore. Hence ``model_construct``,
    which skips validation on purpose.
    """
    over = TaskDOutput.model_construct(
        is_gibberish=False,
        on_topic=True,
        relevance_confidence=1.0,
        quality_score=99,
        grammar_spelling_penalty=0,
        saliency_notes="",
        rationale="",
    )
    assert score_one_essay(over, CFG).score == pytest.approx(float(CFG.quality_max_each))


def test_gibberish_essay_is_gated_and_scores_zero() -> None:
    r = score_one_essay(_task_d(is_gibberish=True, quality_score=13), CFG)
    assert r.gated and r.is_gibberish
    assert r.score == 0.0


def test_off_topic_essay_is_gated_and_scores_zero() -> None:
    r = score_one_essay(_task_d(on_topic=False, quality_score=13), CFG)
    assert r.gated and not r.on_topic
    assert r.score == 0.0


# ------------------------------------------------------------------------------------------------
# 4.3 — grade_essays aggregator (mocked Task D)
# ------------------------------------------------------------------------------------------------


def _row(essay1: str = "essay one text", essay2: str = "essay two text") -> ApplicantRow:
    return ApplicantRow(submission_id="s1", essay1=essay1, essay2=essay2)


def _client(handler) -> FakeLLMClient:  # type: ignore[no-untyped-def]
    return FakeLLMClient(APP, handler=handler)


async def _grade(client: FakeLLMClient) -> object:
    return await grade_essays(_row(), "P1", "P2", client, APP)


async def test_both_essays_pass_composes_total() -> None:
    client = _client(lambda t, u, s: _task_d(quality_score=13, grammar_spelling_penalty=1))
    r = await grade_essays(_row(), "P1", "P2", client, APP)
    assert r.verdict == "pass"
    assert r.primary_reason == ""
    assert r.subscores.e1 == pytest.approx(12.0)  # 13 - 1
    assert r.subscores.e2 == pytest.approx(12.0)  # 13 - 1
    assert r.subscores.total == pytest.approx(24.0)
    assert r.essay_relevance.e1_on_topic is True and r.essay_relevance.e2_on_topic is True
    assert r.gibberish.hit is False


async def test_off_topic_either_essay_rejects_with_no_score() -> None:
    # essay2 (the second call) is off-topic.
    def handler(t, u, s):  # type: ignore[no-untyped-def]
        return _task_d(on_topic="essay two text" not in u, quality_score=13)

    client = _client(handler)
    r = await _grade(client)
    assert r.verdict == "reject"
    assert "Essay 2 off-topic" == r.primary_reason
    assert r.subscores.e2 == 0.0  # off-topic essay yields no score


async def test_gibberish_either_essay_rejects() -> None:
    def handler(t, u, s):  # type: ignore[no-untyped-def]
        return _task_d(is_gibberish="essay one text" in u)

    client = _client(handler)
    r = await _grade(client)
    assert r.verdict == "reject"
    assert r.primary_reason == "Essay 1 is gibberish"
    assert r.gibberish.hit is True


async def test_gibberish_reported_before_relevance_in_reason() -> None:
    # essay1 gibberish, essay2 off-topic: fail-fast order names gibberish first.
    def handler(t, u, s):  # type: ignore[no-untyped-def]
        if "essay one text" in u:
            return _task_d(is_gibberish=True)
        return _task_d(on_topic=False)

    client = _client(handler)
    r = await _grade(client)
    assert r.verdict == "reject"
    assert r.primary_reason == "Essay 1 is gibberish"


async def test_parse_failure_routes_to_needs_review_never_rejects() -> None:
    def boom(t, u, s):  # type: ignore[no-untyped-def]
        raise LLMParseFailure(t, "bad json")

    client = _client(boom)
    r = await _grade(client)
    assert r.verdict == "needs_review"
    assert r.primary_reason == "LLM_PARSE_FAILURE"
    assert r.subscores.total == 0.0


async def test_identical_essays_dedup_within_run() -> None:
    client = _client(lambda t, u, s: _task_d())
    await grade_essays(_row("same", "same"), "P", "P", client, APP)
    # Same prompt + same essay text + same wc -> one cache key, one call.
    assert len(client.calls) == 1


async def test_task_d_is_the_task_name() -> None:
    client = _client(lambda t, u, s: _task_d())
    await _grade(client)
    assert all(call[0] == "task_d" for call in client.calls)
    assert len(client.calls) == 2  # one per essay
