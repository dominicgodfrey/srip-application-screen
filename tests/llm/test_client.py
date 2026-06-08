"""Tests for the LLM client boundary (Phase 0.4). No network — FakeLLMClient only."""

import asyncio

import pytest

from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure, OpenAILLMClient
from srip_filter.models import TaskAOutput, TaskDOutput


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


async def test_no_handler_routes_to_parse_failure() -> None:
    client = FakeLLMClient(_config())
    with pytest.raises(LLMParseFailure):
        await client.complete("task_a", system="s", user="x", schema=TaskAOutput)
