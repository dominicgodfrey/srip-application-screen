"""Tests for the LLM client boundary (Phase 0.4). No network — FakeLLMClient only."""

import asyncio

import httpx
import pytest
from openai import RateLimitError

from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure, OpenAILLMClient
from srip_filter.models import TaskAOutput, TaskDOutput


async def _no_sleep(_seconds: float) -> None:
    """Backoff is correctness, not something to sit through in a unit test."""
    return None


def _config(max_concurrency: int = 8) -> AppConfig:
    cfg = AppConfig()
    return cfg.model_copy(
        update={"llm": cfg.llm.model_copy(update={"max_concurrency": max_concurrency})}
    )


def _task_d() -> TaskDOutput:
    return TaskDOutput(
        is_gibberish=False,
        on_topic=True,
        relevance_confidence=0.9,
        quality_score=15,
        grammar_spelling_penalty=0,
        saliency_notes="",
        rationale="",
    )


def test_model_for_maps_tasks() -> None:
    client = FakeLLMClient(_config())
    assert client.model_for("task_a") == "gpt-4.1-mini"
    assert client.model_for("task_b") == "gpt-4.1"
    assert client.model_for("task_c") == "gpt-4.1-mini"
    assert client.model_for("task_d") == "gpt-4.1"


def test_openai_client_builds_without_network() -> None:
    # Constructing AsyncOpenAI with a dummy key makes no network call.
    client = OpenAILLMClient(_config(), api_key="sk-test")
    assert client.model_for("task_a") == "gpt-4.1-mini"


async def test_complete_returns_parsed_model() -> None:
    client = FakeLLMClient(_config(), handler=lambda t, u, s: _task_d())
    out = await client.complete("task_d", system="s", user="essay", schema=TaskDOutput)
    assert isinstance(out, TaskDOutput)
    assert out.quality_score == 15
    assert client.calls == [("task_d", "essay")]


async def test_in_run_cache_dedups_identical_input() -> None:
    client = FakeLLMClient(_config(), handler=lambda t, u, s: _task_d())
    r1 = await client.complete("task_d", system="s", user="same", schema=TaskDOutput)
    r2 = await client.complete("task_d", system="s", user="same", schema=TaskDOutput)
    assert r1 == r2
    assert len(client.calls) == 1  # second call served from cache
    await client.complete("task_d", system="s", user="different", schema=TaskDOutput)
    assert len(client.calls) == 2


async def test_bounded_concurrency() -> None:
    active = 0
    max_active = 0

    async def handler(task: str, user: str, schema: type) -> TaskDOutput:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return _task_d()

    client = FakeLLMClient(_config(max_concurrency=2), handler=handler)
    await asyncio.gather(
        *(
            client.complete("task_d", system="s", user=f"essay {i}", schema=TaskDOutput)
            for i in range(6)
        )
    )
    assert max_active <= 2
    assert len(client.calls) == 6


async def test_retry_once_then_parse_failure() -> None:
    def boom(task: str, user: str, schema: type) -> TaskAOutput:
        raise ValueError("bad json")

    client = FakeLLMClient(_config(), handler=boom)
    with pytest.raises(LLMParseFailure):
        await client.complete("task_a", system="s", user="x", schema=TaskAOutput)
    assert len(client.calls) == 2  # initial attempt + one retry


async def test_parse_failure_is_terminal_no_retry() -> None:
    def boom(task: str, user: str, schema: type) -> TaskAOutput:
        raise LLMParseFailure("task_a", "explicit")

    client = FakeLLMClient(_config(), handler=boom)
    with pytest.raises(LLMParseFailure):
        await client.complete("task_a", system="s", user="x", schema=TaskAOutput)
    assert len(client.calls) == 1  # not retried


async def test_transient_errors_are_retried_to_the_full_budget(monkeypatch) -> None:
    """The bug this pins cost a whole calibration run (2026-07-29): a 30k-TPM rate limit
    turned 307 of 466 real applications into NEEDS_REVIEW, because a 429 was handled exactly
    like unparseable output. A rate limit is our problem, not the applicant's."""
    monkeypatch.setattr(
        "srip_filter.llm.client.asyncio.sleep", _no_sleep  # keep the test instant
    )
    attempts = {"n": 0}

    def rate_limited(task: str, user: str, schema: type) -> TaskAOutput:
        attempts["n"] += 1
        raise RateLimitError(
            "429 slow down",
            response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
            body=None,
        )

    cfg = AppConfig()
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"max_attempts": 5})})
    client = FakeLLMClient(cfg, handler=rate_limited)
    with pytest.raises(LLMParseFailure) as err:
        await client.complete("task_a", system="s", user="x", schema=TaskAOutput)
    assert attempts["n"] == 5  # NOT 2 — the whole transient budget is spent
    assert "transient" in str(err.value)  # the audit trail can say which kind


async def test_a_transient_blip_then_success_costs_no_review(monkeypatch) -> None:
    """The realistic case: one 429, then the call goes through. Nothing should reach a human."""
    monkeypatch.setattr("srip_filter.llm.client.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def flaky(task: str, user: str, schema: type) -> TaskDOutput:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError(
                "429",
                response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
                body=None,
            )
        return _task_d()

    client = FakeLLMClient(_config(), handler=flaky)
    out = await client.complete("task_d", system="s", user="x", schema=TaskDOutput)
    assert out.quality_score == 15
    assert calls["n"] == 2


async def test_terminal_errors_still_stop_after_one_retry(monkeypatch) -> None:
    """Unparseable output is a property of the input — retrying it just re-burns tokens, so
    the PRD §8 policy (initial attempt + one retry) must survive the transient change."""
    monkeypatch.setattr("srip_filter.llm.client.asyncio.sleep", _no_sleep)

    def boom(task: str, user: str, schema: type) -> TaskAOutput:
        raise ValueError("bad json")

    cfg = AppConfig()
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"max_attempts": 6})})
    client = FakeLLMClient(cfg, handler=boom)
    with pytest.raises(LLMParseFailure) as err:
        await client.complete("task_a", system="s", user="x", schema=TaskAOutput)
    assert len(client.calls) == 2  # not 6
    assert "terminal" in str(err.value)


async def test_no_handler_routes_to_parse_failure() -> None:
    client = FakeLLMClient(_config())
    with pytest.raises(LLMParseFailure):
        await client.complete("task_a", system="s", user="x", schema=TaskAOutput)
