"""LLM I/O boundary — a thin wrapper around OpenAI Structured Outputs, task-agnostic (prompts
live in ``prompts/``). It caches by ``(task, sha256(input))``, bounds concurrency, and paces
against a TPM ceiling.

**The two failure policies are the load-bearing part.** Conflating them cost a whole calibration
run: a 30k-TPM rate limit turned 307 of 466 real applications into NEEDS_REVIEW (2026-07-29). A
*transient* error (429, timeout, connection, 5xx) is retried with backoff to
``llm.max_attempts``; a *terminal* one (unparseable output) gets the PRD §8 attempt-plus-retry
and raises. Only terminal failures may become NEEDS_REVIEW — a rate limit is our problem, not
the applicant's.

No test calls a real model; the OpenAI path is exercised by hand via ``scripts/replay.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, TypeVar, cast

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from srip_filter.config import AppConfig, require_openai_key

TaskName = Literal["task_a", "task_b", "task_c", "task_d", "task_e", "task_f"]
FakeHandler = Callable[[str, str, type[BaseModel]], "BaseModel | Awaitable[BaseModel]"]

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# "Ask again shortly", not "this input cannot be graded". Retrying these is the difference
# between a slow drain and a needs-review queue full of healthy applications.
TRANSIENT_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


class TokenBucket:
    """Paces requests against a tokens-per-minute ceiling so 429s never happen — a batch becomes
    *slower* rather than *lossy*. Refill is continuous, so a burst is smoothed instead of
    slamming the first second of every minute.

    Per-process, because the queue-claim model means one drain at a time; two overlapping drains
    would each hold a bucket and collectively exceed the limit, with the transient backoff as
    the backstop. Move it into Postgres only if overlapping drains become normal.
    """

    def __init__(self, tokens_per_minute: int) -> None:
        self.rate = tokens_per_minute / 60.0  # tokens per second
        self.capacity = float(tokens_per_minute)
        self._tokens = float(tokens_per_minute)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int) -> float:
        """Wait until ``tokens`` are available, then spend them; returns seconds waited. A
        request larger than the whole budget is clamped to capacity — better one over-budget
        call, which backoff can absorb, than a permanent hang."""
        want = float(min(tokens, self.capacity))
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= want:
                    self._tokens -= want
                    return waited
                shortfall = want - self._tokens
                delay = max(0.05, shortfall / self.rate)
            waited += delay
            await asyncio.sleep(delay)


class CacheBackend(Protocol):
    """Durable second-level cache behind the in-run dict (PRD v3 §5) — Postgres in production,
    a dict in tests. ``get`` returns a stored ``model_dump`` payload, which the client
    re-validates, so a corrupt row degrades to a cache miss rather than a crash."""

    async def get(self, task: str, input_sha256: str) -> dict | None: ...

    async def put(self, task: str, input_sha256: str, output: dict, model: str) -> None: ...


class LLMParseFailure(Exception):
    """A response could not be parsed after one retry. The pipeline routes the applicant to
    NEEDS_REVIEW — never a silent rejection (PRD §8)."""

    def __init__(self, task: str, detail: str) -> None:
        super().__init__(f"[{task}] {detail}")
        self.task = task
        self.detail = detail


class BaseLLMClient(ABC):
    """Shared boundary behavior: caching, bounded concurrency, and the retry policy. Subclasses
    implement :meth:`_call_once`, the one-shot parsed call."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._cache: dict[tuple[str, str], BaseModel] = {}
        self._semaphore = asyncio.Semaphore(config.llm.max_concurrency)
        # Settable rather than a constructor arg, so existing call sites stay untouched.
        self.cache_backend: CacheBackend | None = None
        # Only the real network boundary sets this, so the test suite is never paced.
        self.bucket: TokenBucket | None = None

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

        Two cache levels on one key: the in-run dict, then the durable backend, so re-grades
        re-bill only changed fields (PRD v3 §2.3). A row that fails validation degrades to a miss.
        """
        key = self._cache_key(task, cache_text if cache_text is not None else user)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("LLM cache hit task=%s", task)
            return cast(T, cached)
        if self.cache_backend is not None:
            stored = await self.cache_backend.get(key[0], key[1])
            if stored is not None:
                try:
                    result = schema.model_validate(stored)
                except Exception:  # corrupt row: treat as a miss and re-bill honestly
                    logger.warning("durable LLM cache row invalid task=%s; ignoring", task)
                else:
                    logger.debug("durable LLM cache hit task=%s", task)
                    self._cache[key] = result
                    return result
        async with self._semaphore:
            result = await self._complete_with_retry(task, system, user, schema)
        self._cache[key] = result
        if self.cache_backend is not None:
            await self.cache_backend.put(
                key[0], key[1], result.model_dump(mode="json"), self.model_for(task)
            )
        return result

    def _estimate_tokens(self, system: str, user: str) -> int:
        """Rough token cost of one round trip. Deliberately an estimate: a tokenizer would add a
        dependency and a model-version coupling for a few percent, and rounding up only paces
        slightly early."""
        return (len(system) + len(user)) // 4 + self._config.llm.estimated_output_tokens

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff, capped. ``attempt`` is 0-based."""
        return min(self._config.llm.backoff_max_s, 2.0**attempt)

    async def _complete_with_retry(
        self, task: TaskName, system: str, user: str, schema: type[T]
    ) -> T:
        """Call the model, retrying by failure *kind* (see the module docstring). The raised
        message names the kind, so the audit trail can say *why* a row is unscoreable."""
        model = self.model_for(task)
        max_attempts = max(2, self._config.llm.max_attempts)
        last_error: Exception | None = None
        transient_seen = False

        for attempt in range(max_attempts):
            try:
                if self.bucket is not None:
                    waited = await self.bucket.acquire(self._estimate_tokens(system, user))
                    if waited > 1.0:
                        logger.info("LLM task=%s paced %.1fs to stay under TPM", task, waited)
                return await self._call_once(task, model, system, user, schema)
            except LLMParseFailure:
                raise  # already terminal; do not retry
            except Exception as error:
                last_error = error
                transient = isinstance(error, TRANSIENT_ERRORS)
                transient_seen = transient_seen or transient
                # Class name, not str(error): a terminal failure's message can quote applicant
                # content back at us, and logs are non-PII. The kind plus the attempt counter
                # diagnoses a 429 storm; the full message reaches the audit record below.
                logger.warning(
                    "LLM task=%s attempt=%d/%d failed (%s): %s",
                    task,
                    attempt + 1,
                    max_attempts,
                    "transient" if transient else "terminal",
                    type(error).__name__,
                )
                # A terminal error is a property of the input: a third attempt re-burns tokens.
                if not transient and attempt >= 1:
                    break
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(self._backoff_seconds(attempt))

        kind = "transient" if transient_seen else "terminal"
        raise LLMParseFailure(
            task, f"{kind} failure after {max_attempts} attempt(s): {last_error}"
        )

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
        if config.llm.tokens_per_minute > 0:
            self.bucket = TokenBucket(config.llm.tokens_per_minute)

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
    """Test double driven by a handler — no network, no API spend. ``handler(task, user,
    schema)`` returns a parsed model or an awaitable; raise :class:`LLMParseFailure` from it for
    the NEEDS_REVIEW path, or anything else for the retry. Calls are recorded in :attr:`calls`."""

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
