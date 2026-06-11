"""LLM I/O boundary (Phase 0.4).

A thin, replaceable wrapper around OpenAI Structured Outputs. Responsibilities:

* parse responses directly into the Task A/B/C/D pydantic models (PRD §8);
* an **in-run cache** keyed by ``(task, sha256(input))`` so identical inputs and retries are
  free within a single run (stateless: it does not persist across runs);
* **bounded concurrency** via an ``asyncio.Semaphore`` sized from ``config.llm.max_concurrency``;
* a **retry-once then ``LLMParseFailure``** fallback — the pipeline turns that into a
  NEEDS_REVIEW row, never a silent rejection (PRD §8).

Prompt templates live in ``prompts/`` and are passed in by the per-task modules; this client is
task-agnostic. The real OpenAI call path is exercised only by the optional live suite
(``RUN_LLM_TESTS=1``); unit tests use :class:`FakeLLMClient`.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar, cast

from openai import AsyncOpenAI
from pydantic import BaseModel

from srip_filter.config import AppConfig, require_openai_key

TaskName = Literal["task_a", "task_b", "task_c", "task_d", "task_e"]
FakeHandler = Callable[[str, str, type[BaseModel]], "BaseModel | Awaitable[BaseModel]"]

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class LLMParseFailure(Exception):
    """Raised when a response cannot be parsed/validated after one retry.

    The pipeline catches this and routes the applicant to NEEDS_REVIEW with reason
    "LLM_PARSE_FAILURE" — never a silent rejection (PRD §8).
    """

    def __init__(self, task: str, detail: str) -> None:
        super().__init__(f"[{task}] {detail}")
        self.task = task
        self.detail = detail


class BaseLLMClient(ABC):
    """Shared boundary behavior: in-run cache, bounded concurrency, retry-once fallback.

    Subclasses implement :meth:`_call_once`, the one-shot parsed call.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._cache: dict[tuple[str, str], BaseModel] = {}
        self._semaphore = asyncio.Semaphore(config.llm.max_concurrency)

    def model_for(self, task: TaskName) -> str:
        """Resolve the pinned model id for a task from config."""
        return cast(str, getattr(self._config.llm.models, task))

    @staticmethod
    def _cache_key(task: str, text: str) -> tuple[str, str]:
        return (task, hashlib.sha256(text.encode("utf-8")).hexdigest())

    async def complete(
        self,
        task: TaskName,
        *,
        system: str,
        user: str,
        schema: type[T],
        cache_text: str | None = None,
    ) -> T:
        """Run a structured task and return the parsed model.

        Cached for this client's lifetime by ``(task, sha256(cache_text))``, defaulting to the
        user prompt, so identical inputs and retries do not re-bill within a run.
        """
        key = self._cache_key(task, cache_text if cache_text is not None else user)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("LLM cache hit task=%s", task)
            return cast(T, cached)
        async with self._semaphore:
            result = await self._complete_with_retry(task, system, user, schema)
        self._cache[key] = result
        return result

    async def _complete_with_retry(
        self, task: TaskName, system: str, user: str, schema: type[T]
    ) -> T:
        model = self.model_for(task)
        last_error: Exception | None = None
        for attempt in range(2):  # initial attempt + one retry (PRD §8)
            try:
                return await self._call_once(task, model, system, user, schema)
            except LLMParseFailure:
                raise  # already terminal; do not retry
            except Exception as error:  # boundary: any failure becomes a NEEDS_REVIEW signal
                last_error = error
                logger.warning("LLM task=%s attempt=%d failed: %s", task, attempt + 1, error)
        raise LLMParseFailure(task, f"failed after retry: {last_error}")

    @abstractmethod
    async def _call_once(
        self, task: TaskName, model: str, system: str, user: str, schema: type[T]
    ) -> T:
        """Make one structured call; return a parsed ``schema`` instance or raise."""
        raise NotImplementedError


class OpenAILLMClient(BaseLLMClient):
    """Real client: OpenAI Structured Outputs parsed straight into pydantic models."""

    def __init__(
        self,
        config: AppConfig,
        *,
        api_key: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(config)
        self._client = client or AsyncOpenAI(
            api_key=api_key or require_openai_key(),
            max_retries=config.llm.max_retries,
            timeout=config.llm.request_timeout_s,
        )

    async def _call_once(
        self, task: TaskName, model: str, system: str, user: str, schema: type[T]
    ) -> T:
        completion = await self._client.chat.completions.parse(
            model=model,
            temperature=self._config.llm.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=schema,
        )
        message = completion.choices[0].message
        if message.refusal:
            raise RuntimeError(f"model refused: {message.refusal}")
        if message.parsed is None:
            raise RuntimeError("no parsed content in response")
        return message.parsed


class FakeLLMClient(BaseLLMClient):
    """Test double driven by a handler. No network, no API spend.

    ``handler(task, user, schema)`` returns a parsed model (or an awaitable of one). Raise
    :class:`LLMParseFailure` from it to exercise the NEEDS_REVIEW path, or any other exception
    to exercise the retry. Each ``_call_once`` is recorded in :attr:`calls`.
    """

    def __init__(self, config: AppConfig, handler: FakeHandler | None = None) -> None:
        super().__init__(config)
        self._handler = handler
        self.calls: list[tuple[str, str]] = []

    async def _call_once(
        self, task: TaskName, model: str, system: str, user: str, schema: type[T]
    ) -> T:
        self.calls.append((task, user))
        if self._handler is None:
            raise LLMParseFailure(task, "FakeLLMClient has no handler configured")
        result = self._handler(task, user, schema)
        if inspect.isawaitable(result):
            result = await result
        return cast(T, result)
