"""P4 — Stage 4b technical-essay bonus tests (Task F pricing + aggregator ladder)."""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig, TechnicalEssayConfig
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.models import TaskFOutput
from srip_filter.scoring.technical_essay import (
    score_technical_essay,
    technical_essay_bonus,
)

APP = AppConfig()
CFG = APP.technical_essay

QUESTION = "Describe a technical problem you are independently curious about."
ESSAY = " ".join(["word"] * 120)


def _task_f(
    *,
    on_topic: bool = True,
    gibberish: bool = False,
    depth: int = 5,
    exploration: int = 5,
    impact: int = 5,
) -> TaskFOutput:
    return TaskFOutput(
        on_topic=on_topic,
        gibberish=gibberish,
        technical_depth_0_10=depth,
        exploration_level_0_10=exploration,
        impact_0_10=impact,
        rationale="",
    )


# ------------------------------------------------------------------------------------------------
# Pure pricing math
# ------------------------------------------------------------------------------------------------


def test_max_signals_price_to_bonus_max() -> None:
    assert technical_essay_bonus(_task_f(depth=10, exploration=10, impact=10), CFG) == 20.0


def test_zero_signals_price_to_zero() -> None:
    assert technical_essay_bonus(_task_f(depth=0, exploration=0, impact=0), CFG) == 0.0


def test_midlevel_signals_price_linearly() -> None:
    # Equal weights: (5+5+5)/30 * 20 = 10.
    assert technical_essay_bonus(_task_f(), CFG) == pytest.approx(10.0)


def test_off_topic_or_gibberish_zeroes_bonus() -> None:
    assert technical_essay_bonus(_task_f(on_topic=False, depth=10), CFG) == 0.0
    assert technical_essay_bonus(_task_f(gibberish=True, depth=10), CFG) == 0.0


def test_weights_reprice_from_config_only() -> None:
    cfg = TechnicalEssayConfig(bonus_max=20, weight_depth=2.0, weight_exploration=1.0,
                               weight_impact=1.0)
    # (2*10 + 1*0 + 1*0) / (10*4) * 20 = 10 — depth alone carries half under double weight.
    assert technical_essay_bonus(_task_f(depth=10, exploration=0, impact=0), cfg) == 10.0


def test_bonus_never_negative_and_capped() -> None:
    assert technical_essay_bonus(_task_f(depth=10, exploration=10, impact=10),
                                 TechnicalEssayConfig(bonus_max=0)) == 0.0
    for d, e, i in ((0, 0, 0), (10, 10, 10), (3, 7, 1)):
        b = technical_essay_bonus(_task_f(depth=d, exploration=e, impact=i), CFG)
        assert 0.0 <= b <= CFG.bonus_max


# ------------------------------------------------------------------------------------------------
# Aggregator ladder (absent -> over-max -> Task F -> parse failure)
# ------------------------------------------------------------------------------------------------


async def test_absent_essay_is_neutral_and_free() -> None:
    client = FakeLLMClient(APP)  # no handler: any call would raise
    r = await score_technical_essay("   ", QUESTION, 500, client, APP)
    assert r.bonus == 0.0
    assert r.assessment.present is False
    assert r.assessment.skipped_reason == "absent"
    assert client.calls == []  # zero tokens for an absent optional signal


async def test_over_max_voids_bonus_without_llm_call() -> None:
    client = FakeLLMClient(APP)
    long_essay = " ".join(["word"] * 501)
    r = await score_technical_essay(long_essay, QUESTION, 500, client, APP)
    assert r.bonus == 0.0
    assert r.assessment.over_max is True
    assert "over_max" in r.assessment.skipped_reason
    assert client.calls == []  # the site does not validate optional essays; we void, not bill


async def test_no_max_words_means_no_over_max_check() -> None:
    client = FakeLLMClient(APP, handler=lambda t, u, s: _task_f())
    long_essay = " ".join(["word"] * 501)
    r = await score_technical_essay(long_essay, QUESTION, None, client, APP)
    assert r.assessment.over_max is False
    assert r.bonus == pytest.approx(10.0)


async def test_good_essay_scores_and_records_signals() -> None:
    client = FakeLLMClient(APP, handler=lambda t, u, s: _task_f(depth=8, exploration=7,
                                                                impact=6))
    r = await score_technical_essay(ESSAY, QUESTION, 500, client, APP)
    assert r.bonus == pytest.approx(20 * (8 + 7 + 6) / 30)
    assert r.llm_called and r.assessment.signals is not None
    assert client.calls and client.calls[0][0] == "task_f"
    assert QUESTION in client.calls[0][1]  # the live prompt rides the user message


async def test_parse_failure_degrades_to_zero_bonus_with_note() -> None:
    def boom(t, u, s):  # type: ignore[no-untyped-def]
        raise LLMParseFailure(t, "bad json")

    client = FakeLLMClient(APP, handler=boom)
    r = await score_technical_essay(ESSAY, QUESTION, 500, client, APP)
    assert r.bonus == 0.0  # bonus signal: parse failure is neutral, never NEEDS_REVIEW
    assert r.assessment.skipped_reason == "llm_parse_failure"
    assert r.errors and "task_f" in r.errors[0]
