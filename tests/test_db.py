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
    oldest_pending_seconds,
    reap_stale_claims,
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


class _InterferingPool:
    """Real pool whose handed-out connection misbehaves once, on cue.

    The two atomicity guarantees below are about what happens *between* two statements
    inside one transaction — a row landing mid-purge, a crash between delete and
    tombstone. Reproducing either with genuine concurrency needs a seam in production
    code purely for the test; intercepting one call on the connection needs none, and
    exercises the same branch deterministically. Everything not intercepted is forwarded
    to the real connection, so the transaction, the rollback, and the SQL are all real.
    """

    def __init__(self, pool, *, count_lie: int | None = None, fail_on: str | None = None):
        self._pool = pool
        self._count_lie = count_lie
        self._fail_on = fail_on

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                self._cm = outer._pool.acquire()
                return _Conn(await self._cm.__aenter__())

            async def __aexit__(self, *exc):
                return await self._cm.__aexit__(*exc)

        class _Conn:
            def __init__(self, conn):
                self._conn = conn
                self._counted = False

            async def fetchval(self, query, *args):
                # Stand in for the pre-count seeing a smaller set than the DELETE will.
                if outer._count_lie is not None and not self._counted and "COUNT" in query:
                    self._counted = True
                    return outer._count_lie
                return await self._conn.fetchval(query, *args)

            async def execute(self, query, *args):
                if outer._fail_on is not None and outer._fail_on in query:
                    raise RuntimeError("simulated crash mid-transaction")
                return await self._conn.execute(query, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        return _Ctx()


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


async def test_oldest_pending_seconds_tracks_only_ungraded_rows(pool):
    """Feeds /health. None on an empty queue so an idle service never reads as broken."""
    assert await oldest_pending_seconds(pool) is None

    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    fresh = await oldest_pending_seconds(pool)
    assert fresh is not None and 0 <= fresh < 60
    # Must be a float, not the Decimal EXTRACT hands back: /health JSON-encodes it, and a
    # Decimal 500s the degraded path only — invisible to any test that fakes this value.
    assert isinstance(fresh, float)

    await pool.execute(
        "UPDATE applications SET updated_at = NOW() - INTERVAL '2 hours' WHERE submission_id = $1",
        uuid.UUID(sid),
    )
    assert await oldest_pending_seconds(pool) > 7000

    # Grading it clears the signal: only 'received' rows count as waiting.
    await claim_next(pool)
    assert await oldest_pending_seconds(pool) is None


async def test_reaper_requeues_only_claims_older_than_the_window(pool):
    """P11.2: a killed serverless invocation leaves a row in 'grading' with nobody on it."""
    fresh, stale = _sid(), _sid()
    await upsert_application(pool, submission_id=fresh, payload=_payload())
    await upsert_application(pool, submission_id=stale, payload=_payload(extra="s"))
    await claim_next(pool)
    await claim_next(pool)

    assert await reap_stale_claims(pool, 900) == 0  # both claims are seconds old
    await pool.execute(
        "UPDATE applications SET updated_at = NOW() - INTERVAL '1 hour' WHERE submission_id = $1",
        uuid.UUID(stale),
    )
    assert await reap_stale_claims(pool, 900) == 1
    assert (await get_application(pool, stale))["status"] == dbmod.STATUS_RECEIVED
    assert (await get_application(pool, fresh))["status"] == dbmod.STATUS_GRADING
    assert str((await claim_next(pool))["submission_id"]) == stale


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


async def test_summaries_drop_the_payload_and_the_essay_text(pool):
    """The projection is real SQL (`audit_record - 'essays'`), so assert it against Postgres.

    The API suite's fake store can only mimic this; if the SQL stopped stripping, nothing
    there would notice and the most-hit endpoint would quietly start shipping essays again.
    """
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload(), cohort_name="calib")
    await finish_graded(
        pool,
        sid,
        audit_record={
            "outcome": "RANKED",
            "name": "Syn Thetic",
            "final_score": 100.0,
            "essays": {"e1": "SECRET ESSAY ONE", "e2": "SECRET ESSAY TWO", "e3": ""},
            "gpa": {"raw": "3.8 / 4.0", "normalized_gpa": 3.8},
        },
        outcome="RANKED",
        final_score=100.0,
    )

    [summary] = await dbmod.list_application_summaries(pool, cohort_name="calib")

    assert "payload" not in summary
    assert summary["has_payload"] is True          # the boolean the UI actually needs
    assert "essays" not in summary["audit_record"]
    # Everything ranking and the listing read must survive.
    assert summary["audit_record"]["gpa"]["normalized_gpa"] == 3.8
    assert summary["audit_record"]["outcome"] == "RANKED"
    assert summary["final_score"] == 100.0
    assert summary["cohort_name"] == "calib"

    # The full read is unchanged — exports and the promote path still need both.
    [full] = await list_applications(pool, cohort_name="calib")
    assert full["audit_record"]["essays"]["e1"] == "SECRET ESSAY ONE"
    assert full["payload"] is not None


async def test_summaries_survive_an_ungraded_row(pool):
    """`audit_record - 'essays'` on a NULL record must stay NULL, not become an empty object."""
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    [summary] = await dbmod.list_application_summaries(pool)
    assert summary["audit_record"] is None
    assert summary["status"] == "received"


async def test_outcome_counts_are_computed_in_sql(pool):
    a, b, c = _sid(), _sid(), _sid()
    for sid in (a, b, c):
        await upsert_application(pool, submission_id=sid, payload=_payload(x=sid),
                                 cohort_name="calib")
    await finish_graded(pool, a, audit_record={"outcome": "RANKED"}, outcome="RANKED",
                        final_score=100.0)
    await finish_graded(pool, b, audit_record={"outcome": "REJECTED"}, outcome="REJECTED",
                        final_score=None)

    counts = await dbmod.count_by_outcome(pool, cohort_name="calib")

    assert counts == {"RANKED": 1, "REJECTED": 1, "received": 1}  # ungraded falls back to status


async def test_delete_submission_hard_deletes_and_tombstones(pool):
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())
    assert await delete_submission(pool, sid) is True
    assert await get_application(pool, sid) is None
    assert await delete_submission(pool, sid) is False  # honest double-delete
    kinds = [r["kind"] for r in await pool.fetch("SELECT kind FROM events")]
    assert "submission_deleted" in kinds


async def test_delete_and_tombstone_are_one_transaction(pool):
    """PRD v3 §9: the removal-request path cannot delete without recording that it did.

    A crash between the DELETE and the ledger INSERT must take the DELETE with it —
    otherwise the one case where you later need to *prove* the removal happened is
    exactly the case with no evidence.
    """
    sid = _sid()
    await upsert_application(pool, submission_id=sid, payload=_payload())

    with pytest.raises(RuntimeError):
        await delete_submission(_InterferingPool(pool, fail_on="INSERT INTO events"), sid)

    assert await get_application(pool, sid) is not None  # rolled back, still there
    assert await pool.fetchval(
        "SELECT COUNT(*) FROM events WHERE kind = 'submission_deleted'"
    ) == 0


# ------------------------------------------------------------------------------------------------
# Bulk purge (PRD v3 §9 close-cycle)
# ------------------------------------------------------------------------------------------------


async def test_purge_preview_describes_the_scope_without_deleting(pool):
    a, b, c = _sid(), _sid(), _sid()
    await upsert_application(pool, submission_id=a, payload=_payload(), cohort_name="calib")
    await upsert_application(pool, submission_id=b, payload=_payload(x=1), cohort_name="calib")
    await upsert_application(pool, submission_id=c, payload=_payload(x=2), cohort_name="live")
    await finish_graded(pool, a, audit_record={"outcome": "RANKED"}, outcome="RANKED",
                        final_score=100.0)
    await cache_put(pool, "task_d", "hash-1", {"quality_score": 12}, model="gpt-x")

    scoped = await dbmod.purge_preview(pool, cohort_name="calib")
    assert scoped["total"] == 2
    assert scoped["by_cohort"] == {"calib": 2}
    assert scoped["with_audit_record"] == 1
    assert scoped["llm_cache_cleared"] is False  # a scoped purge leaves the cache alone
    assert scoped["llm_cache_rows"] == 1

    every = await dbmod.purge_preview(pool)
    assert every["total"] == 3
    assert every["by_cohort"] == {"calib": 2, "live": 1}
    assert every["llm_cache_cleared"] is True

    # Read-only: previewing twice must not have destroyed anything.
    assert len(await list_applications(pool)) == 3


async def test_scoped_purge_deletes_only_its_cohort_and_keeps_the_cache(pool):
    a, b = _sid(), _sid()
    await upsert_application(pool, submission_id=a, payload=_payload(), cohort_name="calib")
    await upsert_application(pool, submission_id=b, payload=_payload(x=1), cohort_name="live")
    await cache_put(pool, "task_d", "hash-1", {"quality_score": 12}, model="gpt-x")

    receipt = await dbmod.purge_applications(pool, cohort_name="calib", expected_count=1)

    assert receipt == {
        "applications_deleted": 1,
        "llm_cache_rows_deleted": 0,
        "scope": "calib",
    }
    assert await get_application(pool, a) is None
    assert await get_application(pool, b) is not None  # the other cohort survives
    assert await pool.fetchval("SELECT COUNT(*) FROM llm_cache") == 1


async def test_full_wipe_clears_applications_and_the_derived_cache(pool):
    """A full wipe must not leave model commentary about deleted applicants behind."""
    a, b = _sid(), _sid()
    await upsert_application(pool, submission_id=a, payload=_payload(), cohort_name="calib")
    await upsert_application(pool, submission_id=b, payload=_payload(x=1), cohort_name="live")
    await cache_put(pool, "task_d", "hash-1", {"rationale": "about an essay"}, model="gpt-x")
    await cache_put(pool, "task_b", "hash-2", {"rationale": "about a GPA note"}, model="gpt-x")

    receipt = await dbmod.purge_applications(pool, cohort_name=None, expected_count=2)

    assert receipt["applications_deleted"] == 2
    assert receipt["llm_cache_rows_deleted"] == 2
    assert receipt["scope"] == "ALL_COHORTS"
    assert await list_applications(pool) == []
    assert await pool.fetchval("SELECT COUNT(*) FROM llm_cache") == 0


async def test_purge_is_tombstoned_with_counts_and_no_pii(pool):
    sid = _sid()
    await upsert_application(
        pool, submission_id=sid, payload=_payload(), cohort_name="calib",
        user_email="syn@example.com", student_name="Syn Thetic",
    )

    await dbmod.purge_applications(pool, cohort_name="calib", expected_count=1)

    row = await pool.fetchrow("SELECT * FROM events WHERE kind = 'purge'")
    assert row is not None
    assert row["submission_id"] is None  # a bulk purge is about counts, not one applicant
    blob = row["details"]
    assert "syn@example.com" not in blob and "Syn Thetic" not in blob
    assert '"applications_deleted": 1' in blob


async def test_count_drift_aborts_the_purge_and_deletes_nothing(pool):
    """The dialog's count is what the operator consented to; deliveries keep arriving."""
    a, b = _sid(), _sid()
    await upsert_application(pool, submission_id=a, payload=_payload(), cohort_name="calib")
    await upsert_application(pool, submission_id=b, payload=_payload(x=1), cohort_name="calib")

    # Operator was shown 1 (a second application landed while the dialog was open).
    with pytest.raises(dbmod.PurgeCountMismatch) as err:
        await dbmod.purge_applications(pool, cohort_name="calib", expected_count=1)

    assert err.value.expected == 1 and err.value.actual == 2
    assert len(await list_applications(pool)) == 2  # nothing destroyed
    assert await pool.fetchval("SELECT COUNT(*) FROM events WHERE kind='purge'") == 0


async def test_a_row_landing_mid_purge_aborts_it_and_deletes_nothing(pool):
    """The pre-count is not enough: FOR UPDATE is not a predicate lock.

    It locks the rows it finds, so it cannot stop an INSERT committing between the count
    and the DELETE — and the DELETE takes a fresh READ COMMITTED snapshot that would
    include the new row. Here the pre-count is made to report 1 while 2 rows are really
    in scope, which is exactly what that race looks like from inside the transaction.
    The post-check must roll the whole thing back.
    """
    a, b = _sid(), _sid()
    await upsert_application(pool, submission_id=a, payload=_payload(), cohort_name="calib")
    await upsert_application(pool, submission_id=b, payload=_payload(x=1), cohort_name="calib")

    with pytest.raises(dbmod.PurgeCountMismatch) as err:
        await dbmod.purge_applications(
            _InterferingPool(pool, count_lie=1), cohort_name="calib", expected_count=1
        )

    assert (err.value.expected, err.value.actual) == (1, 2)
    assert len(await list_applications(pool)) == 2  # the DELETE was rolled back
    assert await pool.fetchval("SELECT COUNT(*) FROM events WHERE kind='purge'") == 0


async def test_purge_of_an_empty_scope_is_a_harmless_noop(pool):
    receipt = await dbmod.purge_applications(pool, cohort_name="nobody-here", expected_count=0)
    assert receipt["applications_deleted"] == 0
