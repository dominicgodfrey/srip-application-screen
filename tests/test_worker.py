"""P3 worker tests — queue draining, per-row crash isolation, durable LLM cache.

No real database: the store boundary is monkeypatched with an in-memory fake queue,
which is exactly the right altitude — SKIP LOCKED semantics were proven in test_db.py;
here we prove the loop's behavior around them (PRD v3 invariants #8 and #9).
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from srip_filter import worker as worker_mod
from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient
from srip_filter.worker import GradeResult, process_one, run_worker

# ------------------------------------------------------------------------------------------------
# Fake store boundary
# ------------------------------------------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in for the db module's queue functions."""

    def __init__(self, rows: list[dict]) -> None:
        self.queue = list(rows)
        self.graded: list[tuple[str, str, float | None]] = []
        self.errors: list[tuple[str, str]] = []
        self.events: list[tuple[str, str | None]] = []

    async def claim_next(self, pool):
        return self.queue.pop(0) if self.queue else None

    async def finish_graded(self, pool, sid, *, audit_record, outcome, final_score):
        self.graded.append((sid, outcome, final_score))

    async def mark_error(self, pool, sid, note):
        self.errors.append((sid, note))

    async def add_event(self, pool, kind, *, submission_id=None, details=None):
        self.events.append((kind, submission_id))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch):
    def _install(rows: list[dict]) -> _FakeStore:
        s = _FakeStore(rows)
        for name in ("claim_next", "finish_graded", "mark_error", "add_event"):
            monkeypatch.setattr(worker_mod.dbmod, name, getattr(s, name))
        return s

    return _install


def _row(sid: str) -> dict:
    return {"submission_id": sid, "essays_payload": {"synthetic": True}}


async def _ok_grade(row: dict) -> GradeResult:
    return GradeResult(audit_record={"id": str(row["submission_id"])}, outcome="RANKED",
                       final_score=100.0)


# ------------------------------------------------------------------------------------------------
# process_one / run_worker
# ------------------------------------------------------------------------------------------------


async def test_process_one_grades_and_persists(store) -> None:
    s = store([_row("a")])
    assert await process_one(object(), _ok_grade) is True
    assert s.graded == [("a", "RANKED", 100.0)]
    assert ("graded", "a") in s.events
    assert await process_one(object(), _ok_grade) is False  # queue empty


async def test_crash_isolates_row_and_loop_continues(store) -> None:
    """PRD v3 invariant #9 — a poisoned row can't stall the queue."""
    s = store([_row("a"), _row("poison"), _row("c")])

    async def grade(row: dict) -> GradeResult:
        if row["submission_id"] == "poison":
            raise ValueError("essay text would be in this message — must not be stored")
        return await _ok_grade(row)

    while await process_one(object(), grade):
        pass

    assert [g[0] for g in s.graded] == ["a", "c"]
    assert s.errors == [("poison", "ValueError")]  # class name only, never the message


async def test_run_worker_drains_then_stops_promptly(store) -> None:
    s = store([_row("a"), _row("b")])
    stop = asyncio.Event()

    async def grade(row: dict) -> GradeResult:
        if len(s.graded) == 1:  # after the second claim, ask the loop to stop
            stop.set()
        return await _ok_grade(row)

    # Generous poll: if stop didn't short-circuit the idle wait, this would time out.
    await asyncio.wait_for(
        run_worker(object(), grade, poll_seconds=30.0, stop=stop), timeout=5.0
    )
    assert [g[0] for g in s.graded] == ["a", "b"]


async def test_run_worker_survives_claim_failure(store, monkeypatch) -> None:
    s = store([_row("a")])
    calls = {"n": 0}
    real_claim = s.claim_next

    async def flaky_claim(pool):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db blip")
        return await real_claim(pool)

    monkeypatch.setattr(worker_mod.dbmod, "claim_next", flaky_claim)
    stop = asyncio.Event()

    async def grade(row: dict) -> GradeResult:
        stop.set()
        return await _ok_grade(row)

    await asyncio.wait_for(
        run_worker(object(), grade, poll_seconds=0.01, stop=stop), timeout=5.0
    )
    assert [g[0] for g in s.graded] == ["a"]  # blip absorbed, row still graded


# ------------------------------------------------------------------------------------------------
# Durable LLM cache (invariant #8: identical re-delivery re-bills nothing)
# ------------------------------------------------------------------------------------------------


class _Out(BaseModel):
    value: int


class _DictBackend:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.puts = 0

    async def get(self, task: str, sha: str):
        return self.rows.get((task, sha))

    async def put(self, task: str, sha: str, output: dict, model: str) -> None:
        self.puts += 1
        self.rows[(task, sha)] = output


async def test_durable_cache_survives_client_restart() -> None:
    """A fresh client (new in-run cache) must hit the backend, not the model."""
    cfg = AppConfig()
    backend = _DictBackend()

    first = FakeLLMClient(cfg, lambda task, user, schema: _Out(value=7))
    first.cache_backend = backend
    out1 = await first.complete("task_a", system="s", user="same input", schema=_Out)
    assert out1.value == 7
    assert backend.puts == 1
    assert len(first.calls) == 1

    second = FakeLLMClient(cfg, lambda task, user, schema: _Out(value=999))
    second.cache_backend = backend
    out2 = await second.complete("task_a", system="s", user="same input", schema=_Out)
    assert out2.value == 7  # served from the durable cache
    assert second.calls == []  # zero model calls — nothing re-billed
    assert backend.puts == 1


async def test_corrupt_backend_row_degrades_to_miss() -> None:
    cfg = AppConfig()
    backend = _DictBackend()
    client = FakeLLMClient(cfg, lambda task, user, schema: _Out(value=1))
    client.cache_backend = backend
    await client.complete("task_a", system="s", user="u", schema=_Out)
    key = next(iter(backend.rows))
    backend.rows[key] = {"wrong_field": "garbage"}  # simulate schema drift

    fresh = FakeLLMClient(cfg, lambda task, user, schema: _Out(value=2))
    fresh.cache_backend = backend
    out = await fresh.complete("task_a", system="s", user="u", schema=_Out)
    assert out.value == 2  # honest re-bill, no crash
    assert len(fresh.calls) == 1


async def test_no_backend_preserves_v2_behavior() -> None:
    cfg = AppConfig()
    client = FakeLLMClient(cfg, lambda task, user, schema: _Out(value=3))
    out = await client.complete("task_a", system="s", user="u", schema=_Out)
    assert out.value == 3
    # In-run cache still dedups within the client's lifetime.
    await client.complete("task_a", system="s", user="u", schema=_Out)
    assert len(client.calls) == 1
