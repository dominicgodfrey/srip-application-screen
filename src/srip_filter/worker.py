"""Grading worker (P3, PRD v3 §3) — drains the Postgres queue, one row at a time.

The queue is the ``applications.status`` column; :func:`srip_filter.db.claim_next` hands
out rows with ``FOR UPDATE SKIP LOCKED`` so any number of workers (we run one) never
collide. The worker knows nothing about HTTP and nothing about the pipeline internals —
it drives a ``grade_fn`` supplied by the caller (P4 wires the real webhook→pipeline
mapping; tests inject fakes).

Isolation rules ("when grading begins, it finishes", PRD v3 invariant #9):

* a ``grade_fn`` crash marks THAT row ``error`` (+ a non-PII tombstone) and the loop
  moves on — one poisoned application can never stall the queue;
* a claim/store failure (DB hiccup) is logged and retried after the poll interval —
  the loop itself never dies.
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
    """What a ``grade_fn`` must produce for one claimed row (PRD v3 §1.1)."""

    audit_record: dict[str, Any]
    outcome: str  # REJECTED | RANKED | NEEDS_REVIEW
    final_score: float | None


GradeFn = Callable[[dict[str, Any]], Awaitable[GradeResult]]


async def process_one(pool: Any, grade_fn: GradeFn) -> bool:
    """Claim and grade a single row. Returns False when the queue is empty.

    The error note passed to :func:`db.mark_error` is the exception *class name only* —
    exception messages can quote applicant text and the events ledger is non-PII by law.
    """
    row = await dbmod.claim_next(pool)
    if row is None:
        return False
    sid = str(row["submission_id"])
    try:
        result = await grade_fn(row)
    except Exception as error:
        logger.exception("grading crashed submission_id=%s", sid)
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
    """The long-running loop: drain the queue, then idle-poll until ``stop`` is set.

    Lifespan-managed (started/cancelled by the FastAPI shell). Any unexpected iteration
    failure — a dropped DB connection, a claim error — is logged and absorbed; the loop
    backs off one poll interval and continues.
    """
    logger.info("grading worker started (poll=%.1fs)", poll_seconds)
    while not stop.is_set():
        try:
            processed = await process_one(pool, grade_fn)
        except Exception:
            logger.exception("worker iteration failed; backing off")
            processed = False
        if not processed:
            # Queue empty (or iteration failed): wait one interval, but wake immediately
            # on stop so shutdown is prompt.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    logger.info("grading worker stopped")
