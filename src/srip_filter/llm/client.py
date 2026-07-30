"""LLM I/O boundary (Phase 0.4).

A thin, replaceable wrapper around OpenAI Structured Outputs. Responsibilities:

* parse responses directly into the Task A/B/C/D pydantic models (PRD §8);
* an **in-run cache** keyed by ``(task, sha256(input))`` so identical inputs and retries are
  free within a single run (stateless: it does not persist across runs);
* **bounded concurrency** via an ``asyncio.Semaphore`` sized from ``config.llm.max_concurrency``;
* **two different failure policies**, because conflating them cost a whole calibration run
  (2026-07-29: a 30k-TPM rate limit turned 307 of 466 real applications into NEEDS_REVIEW):
  a *transient* error (429, timeout, connection, 5xx) is retried with exponential backoff up
  to ``config.llm.max_attempts``, while a *terminal* one (unparseable/invalid output) gets
  the PRD §8 initial-attempt-plus-one-retry and then raises ``LLMParseFailure``. Only the
  latter should ever become a NEEDS_REVIEW row; a rate limit is our problem, not the
  applicant's.

Prompt templates live in ``prompts/`` and are passed in by the per-task modules; this client is
task-agnostic. The real OpenAI call path is exercised only by the optional live suite
(``RUN_LLM_TESTS=1``); unit tests use :class:`FakeLLMClient`.
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

# Failures that say "ask again shortly", not "this input cannot be graded". Retrying these is
# the difference between a slow drain and a needs-review queue full of healthy applications.
TRANSIENT_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


class TokenBucket:
    """Paces requests against a tokens-per-minute ceiling, so 429s never happen.

    Backoff alone is reactive: once demand exceeds the account's TPM, most calls fail once
    before succeeding, and a long burst can exhaust a row's retry budget and route a healthy
    application to a human. This bucket makes a batch *slower* instead of *lossy* — the drain
    grades everything, just spread over more minutes.

    Continuous refill (not per-minute buckets) so a burst is smoothed rather than allowed to
    slam the first second of every minute.

    ponytail: per-process, because the queue-claim model means one drain at a time. Two
    overlapping drains would each hold their own bucket and collectively exceed the limit —
    the transient backoff is the backstop for that. Move this into Postgres only if
    overlapping drains ever become normal.
    """

    def __init__(self, tokens_per_minute: int) -> None:
        self.rate = tokens_per_minute / 60.0  # tokens per second
        self.capacity = float(tokens_per_minute)
        self._tokens = float(tokens_per_minute)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int) -> float:
        """Wait until ``tokens`` are available, then spend them. Returns seconds waited.

        A single request larger than the whole per-minute budget would never be satisfiable,
        so it is clamped to the capacity — better one over-budget call (which backoff can
        absorb) than a permanent hang.
        """
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
    """Durable second-level cache behind the in-run dict (P3, PRD v3 §5).

    The Postgres implementation is :class:`srip_filter.db.PgCacheBackend`; tests use a
    dict-backed fake. ``get`` returns the previously stored ``model_dump`` payload (or
    None); the client re-validates it into the task schema, so a corrupt row degrades to
    a cache miss, never a crash.
    """

    async def get(self, task: str, input_sha256: str) -> dict | None: ...

    async def put(self, task: str, input_sha256: str, output: dict, model: str) -> None: ...


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
        # Optional durable cache (P3). Settable, not a constructor arg, so the many
        # existing FakeLLMClient call sites stay untouched; None preserves v2 behavior.
        self.cache_backend: CacheBackend | None = None
        # Rate limiting belongs to the real network boundary only: OpenAILLMClient sets this,
        # so FakeLLMClient (and therefore the whole test suite) is never paced.
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

        Two cache levels, same key ``(task, sha256(cache_text or user))``: the in-run dict
        (free retries within a process lifetime), then the optional durable backend —
        Postgres ``llm_cache`` in production — so re-grades re-bill only changed fields
        (PRD v3 §2.3). A backend row that fails schema validation degrades to a miss.
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
                except Exception:  # corrupt/stale row: treat as a miss, re-bill honestly
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
        """Rough token cost of one round trip: prompt chars/4 plus an output allowance.

        Deliberately an estimate — importing a tokenizer to pace requests would add a
        dependency and a model-version coupling to save a few percent of accuracy. Rounding
        up is the safe direction: it paces slightly early.
        """
        return (len(system) + len(user)) // 4 + self._config.llm.estimated_output_tokens

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff, capped. ``attempt`` is 0-based."""
        return min(self._config.llm.backoff_max_s, 2.0**attempt)

    async def _complete_with_retry(
        self, task: TaskName, system: str, user: str, schema: type[T]
    ) -> T:
        """Call the model, retrying by failure *kind* (see the module docstring).

        Transient failures get ``max_attempts`` tries with exponential backoff — a sustained
        rate limit must slow the pipeline down, never route healthy applications to a human.
        Terminal failures keep the PRD §8 policy: initial attempt plus one retry, then
        ``LLMParseFailure``. The raised message names the kind so the audit trail can say
        *why* a row is unscoreable.
        """
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
                # content back at us (a pydantic ValidationError echoing the raw output, an
                # OpenAI refusal naming what it refused), and logs are non-PII by law. The
                # kind plus the attempt counter is what diagnoses a 429 storm; the full
                # message still reaches the audit record via LLMParseFailure below.
                logger.warning(
                    "LLM task=%s attempt=%d/%d failed (%s): %s",
                    task,
                    attempt + 1,
                    max_attempts,
                    "transient" if transient else "terminal",
                    type(error).__name__,
                )
                # A non-transient error is a property of the input; one retry is the PRD §8
                # allowance and a third attempt would just re-burn tokens.
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
