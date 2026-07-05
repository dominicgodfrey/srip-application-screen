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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import asyncpg

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = _PROJECT_ROOT / "db" / "migrations"

PayloadMode = Literal["essays", "resume"]
UpsertResult = Literal["accepted", "unchanged"]

# Application lifecycle states (the queue). Mirrors the 001_init CHECK constraint.
STATUS_RECEIVED = "received"
STATUS_GRADING = "grading"
STATUS_GRADED = "graded"
STATUS_ERROR = "error"


def content_hash(payload: dict[str, Any]) -> str:
    """Canonical sha256 of a JSON payload — the per-mode idempotency key (PRD v3 §2.3).

    Canonical = sorted keys, compact separators, UTF-8; two semantically identical
    payloads always hash identically regardless of key order in transit.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    """Create the asyncpg pool. One pool per process, closed at shutdown (lifespan)."""
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)


# ================================================================================================
# Migrations
# ================================================================================================


async def apply_migrations(pool: asyncpg.Pool, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply unapplied ``*.sql`` files in filename order; each in its own transaction.

    Tracks applied filenames in ``schema_migrations`` (created here on first run).
    Returns the filenames applied this call. Idempotent: re-running applies nothing.
    """
    async with pool.acquire() as conn:
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


# ================================================================================================
# Applications — upsert (webhook ingest) and queue (worker)
# ================================================================================================


async def upsert_application(
    pool: asyncpg.Pool,
    *,
    mode: PayloadMode,
    submission_id: str,
    payload: dict[str, Any],
    cohort_name: str = "",
    user_email: str = "",
    student_name: str = "",
    sub_track: str = "",
    submitted_at: datetime | None = None,
) -> UpsertResult:
    """Idempotently store one webhook payload for one submission (PRD v3 §2.3).

    Per-mode content hash decides everything:

    * no row → insert, ``status='received'`` → ``"accepted"``;
    * row exists, this mode's hash identical → nothing touched → ``"unchanged"``;
    * row exists, hash differs (re-submission, or the other mode arriving) → this mode's
      payload/hash replaced, identity refreshed, ``status`` reset to ``'received'`` so the
      worker re-grades → ``"accepted"``.

    The row lock (``FOR UPDATE``) serializes concurrent deliveries of the same submission
    (admin re-runs / ``untested_only`` races are harmless).
    """
    new_hash = content_hash(payload)
    payload_col = f"{mode}_payload"
    hash_col = f"{mode}_hash"

    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"SELECT {hash_col} AS h FROM applications WHERE submission_id = $1 FOR UPDATE",
            submission_id,
        )
        if row is None:
            await conn.execute(
                f"""
                INSERT INTO applications
                  (submission_id, cohort_name, user_email, student_name, sub_track,
                   submitted_at, {payload_col}, {hash_col}, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, '{STATUS_RECEIVED}')
                """,
                submission_id,
                cohort_name,
                user_email,
                student_name,
                sub_track,
                submitted_at,
                json.dumps(payload),
                new_hash,
            )
            return "accepted"
        if row["h"] == new_hash:
            return "unchanged"
        await conn.execute(
            f"""
            UPDATE applications
               SET {payload_col} = $2,
                   {hash_col}    = $3,
                   cohort_name   = CASE WHEN $4 <> '' THEN $4 ELSE cohort_name END,
                   user_email    = CASE WHEN $5 <> '' THEN $5 ELSE user_email END,
                   student_name  = CASE WHEN $6 <> '' THEN $6 ELSE student_name END,
                   sub_track     = CASE WHEN $7 <> '' THEN $7 ELSE sub_track END,
                   submitted_at  = COALESCE($8, submitted_at),
                   status        = '{STATUS_RECEIVED}',
                   updated_at    = NOW()
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


async def list_applications(
    pool: asyncpg.Pool, *, cohort_name: str | None = None
) -> list[dict[str, Any]]:
    """All applications (optionally one cohort), oldest submission first (stable base order)."""
    if cohort_name is None:
        rows = await pool.fetch("SELECT * FROM applications ORDER BY submitted_at ASC NULLS LAST")
    else:
        rows = await pool.fetch(
            "SELECT * FROM applications WHERE cohort_name = $1 "
            "ORDER BY submitted_at ASC NULLS LAST",
            cohort_name,
        )
    return [_to_dict(r) for r in rows]


async def delete_submission(pool: asyncpg.Pool, submission_id: str) -> bool:
    """Hard-delete one applicant (individual removal request, PRD v3 §9). Tombstoned."""
    status = await pool.execute(
        "DELETE FROM applications WHERE submission_id = $1", submission_id
    )
    deleted = status.endswith("1")
    if deleted:
        await add_event(pool, "submission_deleted", submission_id=submission_id)
    return deleted


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

_JSONB_COLS = ("essays_payload", "resume_payload", "audit_record", "details")


def _to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """asyncpg Record → plain dict, JSONB text columns decoded to Python objects."""
    out = dict(row)
    for col in _JSONB_COLS:
        if out.get(col) is not None and isinstance(out[col], str):
            out[col] = json.loads(out[col])
    return out
