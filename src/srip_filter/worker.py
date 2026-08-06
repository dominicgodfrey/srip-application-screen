"""Grading worker (PRD v3 §3) — drains the Postgres queue one row at a time.

The queue is the ``applications.status`` column, claimed with ``FOR UPDATE SKIP LOCKED`` so
overlapping workers never collide. This module knows nothing about HTTP or the pipeline: it
drives a caller-supplied ``grade_fn``.

Isolation (invariant #9): a ``grade_fn`` crash marks *that* row ``error`` and the loop moves
on, so one poisoned application can never stall the queue; a DB hiccup is logged and retried
after the poll interval, so the loop itself never dies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import db as dbmod

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GradeResult:
    """What a ``grade_fn`` must produce for one claimed row."""

    audit_record: dict[str, Any]
    outcome: str  # REJECTED | RANKED | NEEDS_REVIEW
    final_score: float | None


GradeFn = Callable[[dict[str, Any]], Awaitable[GradeResult]]


async def process_one(pool: Any, grade_fn: GradeFn) -> bool:
    """Claim and grade a single row. Returns False when the queue is empty.

    Only the exception *class name* is recorded or logged: messages and tracebacks can quote
    applicant text (a ValidationError naming an essay, an OpenAI refusal quoting what it
    refused), and both the ledger and the logs are non-PII. The audit record carries the detail.
    """
    row = await dbmod.claim_next(pool)
    if row is None:
        return False
    sid = str(row["submission_id"])
    try:
        result = await grade_fn(row)
    except Exception as error:
        logger.error(
            "grading crashed submission_id=%s error=%s", sid, type(error).__name__
        )
        await dbmod.mark_error(pool, sid, type(error).__name__)
        return True
    await dbmod.finish_graded(
        pool,
        sid,
        audit_record=result.audit_record,
        outcome=result.outcome,
        final_score=result.final_score,
    )
    await dbmod.add_event(
        pool, "graded", submission_id=sid, details={"outcome": result.outcome}
    )
    return True


async def run_worker(
    pool: Any,
    grade_fn: GradeFn,
    *,
    poll_seconds: float,
    stop: asyncio.Event,
) -> None:
    """The long-running loop: drain the queue, then idle-poll until ``stop`` is set. Any
    unexpected iteration failure is absorbed — the loop backs off one interval and continues."""
    logger.info("grading worker started (poll=%.1fs)", poll_seconds)
    while not stop.is_set():
        try:
            processed = await process_one(pool, grade_fn)
        except Exception:
            # Traceback kept, unlike process_one: grade_fn and its applicant text are caught
            # in there, so what reaches here is queue plumbing — asyncpg errors about our
            # infrastructure. Without a traceback a dropped-connection loop is undiagnosable.
            logger.exception("worker iteration failed; backing off")
            processed = False
        if not processed:
            # Wait one interval, but wake immediately on stop so shutdown is prompt.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    logger.info("grading worker stopped")
