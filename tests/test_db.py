"""P1 persistence-layer tests.

Run against a REAL Postgres (dev Neon branch) via ``DATABASE_URL_TEST`` — asyncpg has no
useful in-memory stand-in, and hash/locking semantics are exactly what must be proven.
The whole module skips cleanly when the env var is unset, so the core suite stays
zero-dependency (CLAUDE.md testing rules).

Isolation: each test run works in a throwaway schema (``srip_test_<pid>``) created by the
session fixture and dropped afterward, so parallel/aborted runs never collide and the dev
branch stays clean. Synthetic data only.
"""

from __future__ import annotations

import os
import uuid

import pytest

from srip_filter import db as dbmod
from srip_filter.db import (
    apply_migrations,
    cache_get,
    cache_put,
    claim_next,
    content_hash,
    delete_submission,
    finish_graded,
    get_application,
    list_applications,
    mark_error,
    upsert_application,
)

DSN = os.environ.get("DATABASE_URL_TEST")

pytestmark = pytest.mark.skipif(
    not DSN, reason="DATABASE_URL_TEST not set (dev Neon branch needed for db tests)"
)


# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def schema_name() -> str:
    return f"srip_test_{os.getpid()}"


@pytest.fixture
async def pool(schema_name: str):
    """Fresh pool bound to a throwaway schema; migrations applied; dropped on teardown.

    ``setup=`` (per acquire), NOT ``init=`` (once per connection): asyncpg runs ``RESET
    ALL`` when a connection is released back to the pool, which wipes a search_path set
    in ``init``. With ``init`` only the very first acquire was isolated and every one
    after silently operated on ``public`` — which is how a "throwaway schema" suite
    ended up writing to the real tables. ``server_settings={"search_path": ...}`` does
    not work here either; it does not survive the reset.
    """
    import asyncpg

    async def _bind_schema(conn: asyncpg.Connection) -> None:
        await conn.execute(f"SET search_path TO {schema_name}")

    setup = await asyncpg.connect(DSN)
    await setup.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
    await setup.execute(f"CREATE SCHEMA {schema_name}")
    await setup.close()

    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=_bind_schema)
    await apply_migrations(p)
    try:
        yield p
    finally:
        await p.close()
        teardown = await asyncpg.connect(DSN)
        await teardown.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await teardown.close()


def _sid() -> str:
    return str(uuid.uuid4())


def _payload(**overrides) -> dict:
    base = {
        "submission_id": "x",
        "gpa_unweighted": "3.8 / 4.0",
        "gpa_weighted": None,
        "required_essays": [{"question": "Q1", "answer": "synthetic essay text"}],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------------------------------
# Migrations
# ------------------------------------------------------------------------------------------------


async def test_migrations_apply_once_then_noop(pool):
    # The fixture already applied them; a second run must be a no-op.
    applied = await apply_migrations(pool)
    assert applied == []
    # And the tables exist.
    for table in ("applications", "llm_cache", "events", "schema_migrations"):
        assert await pool.fetchval("SELECT to_regclass($1)", table) is not None


# ------------------------------------------------------------------------------------------------
# Upsert / idempotency semantics (PRD v3 §2.3; invariant #8 groundwork)
# ------------------------------------------------------------------------------------------------


async def test_first_delivery_is_accepted_and_queued(pool):
    sid = _sid()
    result = await upsert_application(
        pool, submission_id=sid, payload=_payload(), user_email="a@example.com"
    )
    assert result == "accepted"
    row = await get_application(pool, sid)
    assert row is not None
    assert row["status"] == dbmod.STATUS_RECEIVED
    assert row["payload"]["gpa_unweighted"] == "3.8 / 4.0"
    assert row["payload_hash"] == content_hash(_payload())


async def test_identical_redelivery_is_unchanged_and_touches_nothing(pool):
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    # Simulate the worker having finished so we can prove no reset happens.
    await finish_graded(
        pool, sid, audit_record={"outcome": "RANKED"}, outcome="RANKED", final_score=101.5
    )
    result = await upsert_application(pool, submission_id=sid, payload=_payload())
    assert result == "unchanged"
    row = await get_application(pool, sid)
    assert row["status"] == dbmod.STATUS_GRADED  # untouched: no requeue
    assert row["final_score"] == 101.5


async def test_changed_content_requeues_for_regrade(pool):
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    await finish_graded(
        pool, sid, audit_record={"outcome": "RANKED"}, outcome="RANKED", final_score=90.0
    )
    changed = _payload(required_essays=[{"question": "Q1", "answer": "REVISED essay"}])
    result = await upsert_application(pool, submission_id=sid, payload=changed)
    assert result == "accepted"
    row = await get_application(pool, sid)
    assert row["status"] == dbmod.STATUS_RECEIVED  # requeued
    assert row["payload_hash"] == content_hash(changed)


async def test_delivery_without_essays_is_stored_and_never_claimed(pool):
    """ats_run without "essays" ⇒ terminal 'stored', so no drain ever spends tokens on it."""
    sid = _sid()
    assert (
        await upsert_application(
            pool, submission_id=sid, payload=_payload(ats_run=["resume"]), grade=False
        )
        == "accepted"
    )
    row = await get_application(pool, sid)
    assert row["status"] == dbmod.STATUS_STORED
    assert await claim_next(pool) is None  # invisible to the queue

    # A later delivery that DOES request essays flips it back through changed-hash.
    assert (
        await upsert_application(pool, submission_id=sid, payload=_payload(), grade=True)
        == "accepted"
    )
    assert (await get_application(pool, sid))["status"] == dbmod.STATUS_RECEIVED


async def test_content_hash_is_key_order_independent():
    a = {"x": 1, "y": {"b": 2, "a": 3}}
    b = {"y": {"a": 3, "b": 2}, "x": 1}
    assert content_hash(a) == content_hash(b)


# ------------------------------------------------------------------------------------------------
# Queue semantics (claim / finish / error)
# ------------------------------------------------------------------------------------------------


async def test_claim_marks_grading_and_next_claim_gets_a_different_row(pool):
    sid1, sid2 = _sid(), _sid()
    await upsert_application(pool, submission_id=sid1, payload=_payload())
    await upsert_application(
        pool, submission_id=sid2, payload=_payload(extra="two")
    )
    first = await claim_next(pool)
    second = await claim_next(pool)
    assert first is not None and second is not None
    assert {first["submission_id"], second["submission_id"]} == {
        uuid.UUID(sid1),
        uuid.UUID(sid2),
    }
    assert await claim_next(pool) is None  # queue drained


async def test_error_row_leaves_queue_and_is_tombstoned(pool):
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    claimed = await claim_next(pool)
    assert claimed is not None
    await mark_error(pool, sid, "boom: synthetic failure class")
    row = await get_application(pool, sid)
    assert row["status"] == dbmod.STATUS_ERROR
    assert await claim_next(pool) is None
    kinds = [r["kind"] for r in await pool.fetch("SELECT kind FROM events")]
    assert "grading_error" in kinds


# ------------------------------------------------------------------------------------------------
# Cache, listing, delete
# ------------------------------------------------------------------------------------------------


async def test_llm_cache_round_trip_and_conflict_keeps_first(pool):
    assert await cache_get(pool, "task_d", "abc") is None
    await cache_put(pool, "task_d", "abc", {"quality_score": 12}, model="gpt-x")
    await cache_put(pool, "task_d", "abc", {"quality_score": 99}, model="gpt-x")
    assert (await cache_get(pool, "task_d", "abc"))["quality_score"] == 12


async def test_list_scopes_by_cohort(pool):
    a, b = _sid(), _sid()
    await upsert_application(
        pool, submission_id=a, payload=_payload(), cohort_name="su26-cs"
    )
    await upsert_application(
        pool, submission_id=b, payload=_payload(extra="z"), cohort_name="su27-cs"
    )
    su26 = await list_applications(pool, cohort_name="su26-cs")
    assert [str(r["submission_id"]) for r in su26] == [a]
    assert len(await list_applications(pool)) >= 2


async def test_delete_submission_hard_deletes_and_tombstones(pool):
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    assert await delete_submission(pool, sid) is True
    assert await get_application(pool, sid) is None
    assert await delete_submission(pool, sid) is False  # honest double-delete
    kinds = [r["kind"] for r in await pool.fetch("SELECT kind FROM events")]
    assert "submission_deleted" in kinds
