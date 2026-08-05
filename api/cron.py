"""`POST /api/cron/drain` — the serverless grading driver (P11.1–11.3, PRD v3 §3).

On an always-on host the grading worker was a loop (`worker.run_worker`). On Vercel there
is no such process, so a per-minute cron invocation does the same work in a bounded burst:

    migrate (advisory-locked) → reap stale claims → process_one until budget/cap/empty

It drives the **unmodified** ``worker.process_one``, so per-row isolation (invariant #9)
and the idempotent claim (``FOR UPDATE SKIP LOCKED``) are exactly the ones already tested.
Overlapping invocations are therefore safe by construction.

Auth is Vercel's own: scheduled invocations carry ``Authorization: Bearer $CRON_SECRET``.
It fails closed — no secret configured ⇒ 503, so a misconfigured deploy never exposes an
open grading trigger. The path is on the no-session allowlist because cron has no cookie.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from srip_filter import db as dbmod
from srip_filter.pipeline import make_grade_fn
from srip_filter.worker import process_one

from .webhook_auth import constant_time_match

logger = logging.getLogger(__name__)

DRAIN_PATH = "/api/cron/drain"


def _bearer(header: str | None) -> str:
    scheme, _, token = (header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authorized(header: str | None, secret: str) -> bool:
    """Constant-time bearer check, sharing the webhook's encoding-safe comparison.

    Same primitive as ``X-ATS-Secret`` deliberately: this endpoint is unauthenticated
    until the token matches, so it must not raise on a hostile header either.
    """
    return constant_time_match(_bearer(header), (secret,))


def register_cron(app: FastAPI) -> None:
    """Attach the drain endpoint. Reads secret/pool/client/config off ``app.state``."""

    @app.post(DRAIN_PATH, response_model=None, tags=["ops"])
    async def drain(request: Request) -> Response:
        secret: str | None = app.state.cron_secret
        if not secret:
            return JSONResponse(status_code=503, content={"detail": "Cron is not configured."})
        if not _authorized(request.headers.get("authorization"), secret):
            logger.warning("cron drain rejected: bad or missing bearer token")
            return JSONResponse(status_code=401, content={"detail": "Invalid credentials."})

        pool = app.state.db_pool
        if pool is None:
            return JSONResponse(status_code=503, content={"detail": "Database is not configured."})

        cfg = app.state.config
        migrated = await dbmod.apply_migrations(pool)
        reaped = await dbmod.reap_stale_claims(pool, cfg.worker.stale_grading_seconds)
        grade_fn = make_grade_fn(app.state.llm_client, cfg)

        started = time.monotonic()
        processed = 0
        while (
            processed < cfg.worker.drain_max_rows
            and time.monotonic() - started < cfg.worker.drain_budget_seconds
            and await process_one(pool, grade_fn)
        ):
            processed += 1

        return JSONResponse(
            content={
                "migrated": migrated,
                "reaped": reaped,
                "processed": processed,
                "elapsed": round(time.monotonic() - started, 3),
            }
        )
