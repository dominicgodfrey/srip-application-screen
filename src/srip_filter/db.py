"""Persistence layer (P1) — asyncpg pool, migrations, and the typed store functions.

The only module that speaks SQL. Everything above it passes/receives pydantic models or
plain dicts; everything below it is one Neon Postgres database owned exclusively by this
service (PRD v3 §1.1). Plain SQL, no ORM — three tables:

  * ``applications`` — one row per submission; per-mode webhook payloads + content hashes;
    the grading queue is the ``status`` column (claimed with ``FOR UPDATE SKIP LOCKED``).
  * ``llm_cache``    — persistent ``(task, sha256(input))`` cache; re-grades re-bill only
    changed fields (PRD v3 §2.3).
  * ``events``       — non-PII operational ledger. **Never** pass essay/explanation/resume
    text into ``details`` — submission ids and structural facts only.

Connection strings come from the environment (``DATABASE_URL`` / ``DATABASE_URL_TEST``),
never config.yaml — they contain credentials.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import asyncpg

from .config import project_root

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = project_root() / "db" / "migrations"

# Arbitrary but stable key for the pg advisory lock that serializes migrations (P11.3).
_MIGRATION_LOCK_KEY = 3_771_020_301

UpsertResult = Literal["accepted", "unchanged"]


class PurgeCountMismatch(Exception):
    """The live row count moved between the confirmation dialog and the confirm click.

    Deliveries arrive continuously, so a purge is only safe if it destroys exactly the rows the
    operator was shown a count for. Raised instead of deleting; the caller re-previews.
    """

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(
            f"row count changed since preview: expected {expected}, found {actual}"
        )
        self.expected = expected
        self.actual = actual

# Application lifecycle states (the queue). Mirrors the 001_init CHECK constraint.
STATUS_RECEIVED = "received"
STATUS_GRADING = "grading"
STATUS_GRADED = "graded"
STATUS_ERROR = "error"
STATUS_STORED = "stored"  # delivered but essay grading not requested — terminal


def content_hash(payload: dict[str, Any]) -> str:
    """Canonical sha256 of a JSON payload — the per-mode idempotency key (PRD v3 §2.3).

    Canonical = sorted keys, compact separators, UTF-8; two semantically identical
    payloads always hash identically regardless of key order in transit.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_pool(dsn: str, *, min_size: int = 0, max_size: int = 2) -> asyncpg.Pool:
    """Create the asyncpg pool. One pool per process, closed at shutdown (lifespan).

    ``statement_cache_size=0`` is mandatory against Neon's **pooled** (``-pooler``)
    endpoint (P11.4): PgBouncer in transaction mode hands each transaction a different
    server connection, so asyncpg's cached prepared statements go missing — and they fail
    intermittently under load, not cleanly. Harmless on a direct endpoint, so it is set
    unconditionally rather than sniffed from the DSN.
    """
    return await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, statement_cache_size=0
    )


# ================================================================================================
# Migrations
# ================================================================================================


async def apply_migrations(pool: asyncpg.Pool, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply unapplied ``*.sql`` files in filename order; each in its own transaction.

    Tracks applied filenames in ``schema_migrations`` (created here on first run).
    Returns the filenames applied this call. Idempotent: re-running applies nothing.

    Serverless-safe (P11.3): the whole pass runs under a session-level advisory lock, so
    concurrent cold starts / drains cannot race the same DDL. A caller that loses the race
    returns ``[]`` immediately rather than waiting — the winner is doing the work.
    """
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _MIGRATION_LOCK_KEY):
            logger.info("migrations: another instance holds the lock; skipping")
            return []
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  filename   TEXT PRIMARY KEY,
                  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            done = {
                r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            applied: list[str] = []
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                    )
                applied.append(path.name)
            return applied
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)


# ================================================================================================
# Applications — upsert (webhook ingest) and queue (worker)
# ================================================================================================


async def upsert_application(
    pool: asyncpg.Pool,
    *,
    submission_id: str,
    payload: dict[str, Any],
    grade: bool = True,
    cohort_name: str = "",
    user_email: str = "",
    student_name: str = "",
    sub_track: str = "",
    submitted_at: datetime | None = None,
) -> UpsertResult:
    """Idempotently store one webhook payload for one submission (PRD v3 §2.3).

    The content hash decides everything:

    * no row → insert → ``"accepted"``;
    * row exists, hash identical → nothing touched → ``"unchanged"`` (invariant #8: a
      re-delivery changes no outcome and re-bills nothing);
    * row exists, hash differs (re-submission) → payload replaced, identity refreshed,
      status reset so the worker re-grades → ``"accepted"``.

    ``grade`` comes from ``"essays" in ats_run``. False parks the row in terminal
    ``'stored'`` so no drain ever claims it; a later delivery that does request essays
    flips it back through the changed-hash path.

    The row lock (``FOR UPDATE``) serializes concurrent deliveries of the same submission
    (admin re-runs / ``untested_only`` races are harmless).
    """
    new_hash = content_hash(payload)
    status = STATUS_RECEIVED if grade else STATUS_STORED

    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT payload_hash AS h FROM applications WHERE submission_id = $1 FOR UPDATE",
            submission_id,
        )
        if row is None:
            await conn.execute(
                """
                INSERT INTO applications
                  (submission_id, cohort_name, user_email, student_name, sub_track,
                   submitted_at, payload, payload_hash, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                submission_id,
                cohort_name,
                user_email,
                student_name,
                sub_track,
                submitted_at,
                json.dumps(payload),
                new_hash,
                status,
            )
            return "accepted"
        if row["h"] == new_hash:
            return "unchanged"
        await conn.execute(
            """
            UPDATE applications
               SET payload      = $2,
                   payload_hash = $3,
                   cohort_name  = CASE WHEN $4 <> '' THEN $4 ELSE cohort_name END,
                   user_email   = CASE WHEN $5 <> '' THEN $5 ELSE user_email END,
                   student_name = CASE WHEN $6 <> '' THEN $6 ELSE student_name END,
                   sub_track    = CASE WHEN $7 <> '' THEN $7 ELSE sub_track END,
                   submitted_at = COALESCE($8, submitted_at),
                   status       = $9,
                   updated_at   = NOW()
             WHERE submission_id = $1
            """,
            submission_id,
            json.dumps(payload),
            new_hash,
            cohort_name,
            user_email,
            student_name,
            sub_track,
            submitted_at,
            status,
        )
        return "accepted"


async def claim_next(pool: asyncpg.Pool) -> dict[str, Any] | None:
    """Claim one ``received`` row for grading (``status → grading``); None if queue empty.

    ``FOR UPDATE SKIP LOCKED`` makes concurrent claims contention-free: two workers never
    receive the same row. Oldest-updated first so re-submissions queue fairly.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"""
            SELECT * FROM applications
             WHERE status = '{STATUS_RECEIVED}'
             ORDER BY updated_at ASC
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """
        )
        if row is None:
            return None
        await conn.execute(
            f"UPDATE applications SET status = '{STATUS_GRADING}', updated_at = NOW() "
            "WHERE submission_id = $1",
            row["submission_id"],
        )
        return _to_dict(row)


async def reap_stale_claims(pool: asyncpg.Pool, stale_seconds: float) -> int:
    """Return rows stuck in ``grading`` to ``received``; returns how many (P11.2).

    An always-on worker drained gracefully on shutdown. A serverless invocation killed
    mid-row cannot — without this, one timeout parks that application in ``grading``
    forever and no drain ever looks at it again.
    """
    result = await pool.execute(
        f"""
        UPDATE applications SET status = '{STATUS_RECEIVED}', updated_at = NOW()
         WHERE status = '{STATUS_GRADING}' AND updated_at < NOW() - $1::interval
        """,
        timedelta(seconds=stale_seconds),
    )
    return int(result.split()[-1])


async def oldest_pending_seconds(pool: asyncpg.Pool) -> float | None:
    """Age in seconds of the oldest row still waiting to be graded; None if none are.

    The one number that reveals a dead grading path. If the cron stops firing, the drain
    starts erroring, or claims keep failing, this climbs without bound — and it stays
    ``None`` when there is simply nothing to do, so an idle service never looks broken.
    Deliberately an age and not a count: ``/health`` is public, and an age leaks no
    information about application volume.
    """
    age = await pool.fetchval(
        f"""
        SELECT EXTRACT(EPOCH FROM (NOW() - MIN(updated_at)))
          FROM applications WHERE status = '{STATUS_RECEIVED}'
        """
    )
    # EXTRACT returns Postgres `numeric`, which asyncpg hands back as `Decimal` — and
    # Decimal is not JSON-serializable, so /health would 500 on the degraded path only.
    return None if age is None else float(age)


async def finish_graded(
    pool: asyncpg.Pool,
    submission_id: str,
    *,
    audit_record: dict[str, Any],
    outcome: str,
    final_score: float | None,
) -> None:
    """Persist a grading result and release the row (``status → graded``)."""
    await pool.execute(
        f"""
        UPDATE applications
           SET audit_record = $2, outcome = $3, final_score = $4,
               status = '{STATUS_GRADED}', updated_at = NOW()
         WHERE submission_id = $1
        """,
        submission_id,
        json.dumps(audit_record),
        outcome,
        final_score,
    )


async def mark_error(pool: asyncpg.Pool, submission_id: str, note: str) -> None:
    """Release a row whose grading crashed (``status → error``; PRD v3 invariant #9).

    ``note`` goes to ``events`` — structural facts only, never applicant content.
    """
    await pool.execute(
        f"UPDATE applications SET status = '{STATUS_ERROR}', updated_at = NOW() "
        "WHERE submission_id = $1",
        submission_id,
    )
    await add_event(pool, "grading_error", submission_id=submission_id, details={"note": note})


# ================================================================================================
# Applications — reads and lifecycle (UI / exports / retention)
# ================================================================================================


async def get_application(pool: asyncpg.Pool, submission_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT * FROM applications WHERE submission_id = $1", submission_id
    )
    return _to_dict(row) if row else None


_ORDER = "ORDER BY submitted_at ASC NULLS LAST"  # stable base order: oldest submission first

# Everything except the two heavyweight blobs. `audit_record - 'essays'` drops the verbatim
# essay text (a jsonb key delete, done in Postgres); `payload` is replaced by a boolean.
_SUMMARY_COLUMNS = """
    submission_id, cohort_name, user_email, student_name, sub_track,
    submitted_at, created_at, updated_at, status, outcome, final_score,
    payload_hash,
    (payload IS NOT NULL) AS has_payload,
    audit_record - 'essays' AS audit_record
"""


async def list_applications(
    pool: asyncpg.Pool, *, cohort_name: str | None = None
) -> list[dict[str, Any]]:
    """Every column of every application (optionally one cohort) — payload and essays included.

    The heavy read, and deliberately so: exports serialize the full audit record, and the
    manual-promote path re-grades from the stored payload. **For listing, ranking, counting
    or cohort maths use :func:`list_application_summaries` instead** — see its docstring.
    """
    where = "" if cohort_name is None else "WHERE cohort_name = $1"
    args = [] if cohort_name is None else [cohort_name]
    rows = await pool.fetch(f"SELECT * FROM applications {where} {_ORDER}", *args)
    return [_to_dict(r) for r in rows]


async def list_application_summaries(
    pool: asyncpg.Pool, *, cohort_name: str | None = None
) -> list[dict[str, Any]]:
    """Same rows, without the two payloads nothing on a listing screen reads.

    ``SELECT *`` here was the dominant cost in the service. Every dashboard load, summary,
    cohort what-if and audit-detail open pulled `payload` (the whole submitted application)
    **and** `audit_record` (which embeds the essay text a second time) for every row, then
    JSON-decoded both in Python. At the ~2 000-application design target that is tens of MB
    off Neon per request, on a serverless function, to render a table of names and scores.
    Opening one applicant's audit detail did it for their entire cohort, just to compute one
    rank.

    Projecting the columns and deleting the ``essays`` key in Postgres leaves the audit
    record complete enough for :class:`~srip_filter.models.AuditRecord` to validate
    (``essays`` has a default) and for read-time ranking, which needs only outcome, score,
    cohort and the tiebreakers.
    """
    where = "" if cohort_name is None else "WHERE cohort_name = $1"
    args = [] if cohort_name is None else [cohort_name]
    rows = await pool.fetch(f"SELECT {_SUMMARY_COLUMNS} FROM applications {where} {_ORDER}", *args)
    return [_to_dict(r) for r in rows]


async def count_by_outcome(
    pool: asyncpg.Pool, *, cohort_name: str | None = None
) -> dict[str, int]:
    """Outcome counts (falling back to lifecycle status for ungraded rows), computed in SQL."""
    where = "" if cohort_name is None else "WHERE cohort_name = $1"
    args = [] if cohort_name is None else [cohort_name]
    rows = await pool.fetch(
        f"SELECT COALESCE(outcome, status) AS label, COUNT(*) AS n "
        f"FROM applications {where} GROUP BY 1",
        *args,
    )
    return {r["label"]: r["n"] for r in rows}


async def delete_submission(pool: asyncpg.Pool, submission_id: str) -> bool:
    """Hard-delete one applicant (individual removal request, PRD v3 §9). Tombstoned.

    Delete and tombstone share one transaction, matching :func:`purge_applications` and
    the §9 guarantee. This is the removal-request path — the one case where being able to
    show *that the deletion happened* matters later — so it must not be possible to lose
    the ledger entry to a crash between two separate statements.
    """
    async with pool.acquire() as conn, conn.transaction():
        status = await conn.execute(
            "DELETE FROM applications WHERE submission_id = $1", submission_id
        )
        deleted = status.endswith("1")
        if deleted:
            await conn.execute(
                "INSERT INTO events (kind, submission_id, details) VALUES ($1, $2, NULL)",
                "submission_deleted",
                submission_id,
            )
    return deleted


async def purge_preview(
    pool: asyncpg.Pool, *, cohort_name: str | None = None
) -> dict[str, Any]:
    """Exactly what a purge of this scope would destroy (PRD v3 §9 close-cycle).

    Feeds the confirmation dialog, so it must describe the *scale* honestly rather than
    reassuringly: row count, per-outcome split, the cohorts affected, and the submission-date
    span. ``cohort_name=None`` means every cohort — a full wipe, which also clears ``llm_cache``
    (see :func:`purge_applications` for why the cache cannot be scoped to one cohort).

    Read-only. Counts are a snapshot: deliveries keep arriving, which is why
    :func:`purge_applications` takes an ``expected_count`` rather than trusting this.
    """
    where, args = ("WHERE cohort_name = $1", [cohort_name]) if cohort_name else ("", [])
    summary = await pool.fetchrow(
        f"""
        SELECT COUNT(*) AS total,
               COUNT(audit_record) AS with_audit_record,
               MIN(submitted_at) AS earliest,
               MAX(submitted_at) AS latest
        FROM applications {where}
        """,
        *args,
    )
    outcomes = await pool.fetch(
        f"SELECT COALESCE(outcome, status) AS label, COUNT(*) AS n "
        f"FROM applications {where} GROUP BY 1 ORDER BY n DESC",
        *args,
    )
    cohorts = await pool.fetch(
        f"SELECT cohort_name, COUNT(*) AS n FROM applications {where} "
        f"GROUP BY 1 ORDER BY n DESC",
        *args,
    )
    return {
        "cohort_name": cohort_name,
        "total": summary["total"],
        "with_audit_record": summary["with_audit_record"],
        "earliest": summary["earliest"],
        "latest": summary["latest"],
        "by_outcome": {r["label"]: r["n"] for r in outcomes},
        "by_cohort": {r["cohort_name"]: r["n"] for r in cohorts},
        # Only a full wipe clears the cache, so a scoped purge must say what it leaves behind.
        "llm_cache_rows": await pool.fetchval("SELECT COUNT(*) FROM llm_cache"),
        "llm_cache_cleared": cohort_name is None,
    }


async def purge_applications(
    pool: asyncpg.Pool, *, cohort_name: str | None = None, expected_count: int
) -> dict[str, Any]:
    """Hard-delete every application in scope; return a non-PII receipt (PRD v3 §9).

    ``expected_count`` must equal the live row count or nothing is deleted and
    :class:`PurgeCountMismatch` is raised. Webhook deliveries continue during the seconds a
    human spends reading the confirmation dialog, so without this guard a purge could destroy
    rows nobody was shown — the count is the only thing the operator actually consented to.

    A full wipe (``cohort_name=None``) also truncates ``llm_cache``. The cache is keyed
    ``(task, sha256(input))`` with no cohort column, so it cannot be scoped; and its ``output``
    holds model-generated commentary derived from essay text, which has no business outliving
    the applications it describes. The cost is re-billing a re-grade, which is exactly the
    tradeoff a full wipe implies. Deliberately NOT touched by a scoped purge.

    Everything runs in one transaction, so a mid-purge failure leaves the table untouched
    rather than half-deleted. The tombstone carries counts and a timestamp only.

    **The count is verified twice, before and after the delete.** ``FOR UPDATE`` locks the
    rows it *finds*; it is not a predicate lock, so under READ COMMITTED it cannot stop an
    INSERT landing between the count and the DELETE — and the DELETE takes a fresh snapshot
    that would happily include the new row. Re-checking the number actually deleted closes
    that window: a purge that would destroy a different set than the operator was shown
    aborts and rolls back instead.
    """
    where, args = ("WHERE cohort_name = $1", [cohort_name]) if cohort_name else ("", [])
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Locks the rows we are about to delete, so a concurrent *update* cannot move a
            # row out of scope underneath us. Arriving rows are caught by the post-check.
            live = await conn.fetchval(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM applications {where} FOR UPDATE) t",
                *args,
            )
            if live != expected_count:
                raise PurgeCountMismatch(expected=expected_count, actual=live)
            status = await conn.execute(f"DELETE FROM applications {where}", *args)
            deleted = int(status.rsplit(" ", 1)[-1])
            if deleted != expected_count:
                # Raising inside the transaction rolls the DELETE back: nothing is destroyed.
                raise PurgeCountMismatch(expected=expected_count, actual=deleted)
            cache_cleared = 0
            if cohort_name is None:
                cache_cleared = await conn.fetchval("SELECT COUNT(*) FROM llm_cache")
                await conn.execute("TRUNCATE llm_cache")
            receipt = {
                "applications_deleted": deleted,
                "llm_cache_rows_deleted": cache_cleared,
                "scope": cohort_name or "ALL_COHORTS",
            }
            # Same connection/transaction: the tombstone is part of the purge, not a follow-up
            # that could be lost if the process died between the two.
            await conn.execute(
                "INSERT INTO events (kind, submission_id, details) VALUES ($1, $2, $3)",
                "purge",
                None,
                json.dumps(receipt),
            )
    logger.warning(
        "purge committed scope=%s applications=%d llm_cache=%d",
        receipt["scope"],
        deleted,
        cache_cleared,
    )
    return receipt


# ================================================================================================
# LLM cache + events
# ================================================================================================


async def cache_get(pool: asyncpg.Pool, task: str, input_sha256: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT output FROM llm_cache WHERE task = $1 AND input_sha256 = $2",
        task,
        input_sha256,
    )
    return json.loads(row["output"]) if row else None


async def cache_put(
    pool: asyncpg.Pool, task: str, input_sha256: str, output: dict[str, Any], model: str = ""
) -> None:
    await pool.execute(
        """
        INSERT INTO llm_cache (task, input_sha256, output, model)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (task, input_sha256) DO NOTHING
        """,
        task,
        input_sha256,
        json.dumps(output),
        model,
    )


class PgCacheBackend:
    """Adapter satisfying :class:`srip_filter.llm.client.CacheBackend` over ``llm_cache``.

    Handed to the LLM client at startup (``client.cache_backend = PgCacheBackend(pool)``)
    so every structured call is durably memoized (PRD v3 §5).
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, task: str, input_sha256: str) -> dict[str, Any] | None:
        return await cache_get(self._pool, task, input_sha256)

    async def put(self, task: str, input_sha256: str, output: dict[str, Any], model: str) -> None:
        await cache_put(self._pool, task, input_sha256, output, model)


async def add_event(
    pool: asyncpg.Pool,
    kind: str,
    *,
    submission_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append to the non-PII operational ledger.

    ``details`` must contain structural facts only (counts, stage names, error classes)
    — NEVER essay, explanation, or resume text (CLAUDE.md security rules).
    """
    await pool.execute(
        "INSERT INTO events (kind, submission_id, details) VALUES ($1, $2, $3)",
        kind,
        submission_id,
        json.dumps(details) if details is not None else None,
    )


# ================================================================================================
# Helpers
# ================================================================================================

async def count_recent_events(
    pool: asyncpg.Pool, kind: str, within_seconds: float, *, actor: str | None = None
) -> int:
    """How many ``kind`` events landed inside the window (P12.2 login throttle).

    The throttle has to be shared across serverless instances, and ``events`` is already
    the cross-instance ledger. Safe for this use: a failed login is a structural fact
    about a single shared credential — no identity, no PII.

    ``actor`` narrows the count to one ``details->>'actor'`` bucket, which is how the
    per-client tier is counted. That value is a salted hash produced by
    :func:`api.auth.client_key`, never an address — see its docstring for why the ledger
    must not learn one.
    """
    if actor is None:
        return await pool.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = $1 AND created_at > NOW() - $2::interval",
            kind,
            timedelta(seconds=within_seconds),
        )
    return await pool.fetchval(
        "SELECT COUNT(*) FROM events WHERE kind = $1 AND created_at > NOW() - $2::interval "
        "AND details->>'actor' = $3",
        kind,
        timedelta(seconds=within_seconds),
        actor,
    )


_JSONB_COLS = ("payload", "audit_record", "details")


def _to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """asyncpg Record → plain dict, JSONB text columns decoded to Python objects."""
    out = dict(row)
    for col in _JSONB_COLS:
        if out.get(col) is not None and isinstance(out[col], str):
            out[col] = json.loads(out[col])
    return out
