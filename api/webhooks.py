"""`POST /webhooks/applications` — the website-facing front door (P2, PRD v3 §2).

Handler discipline (CLAUDE.md security rules):

* **verify → validate → upsert → 202, nothing else.** Grading belongs to the worker (P3);
  the website's dispatcher aborts at 15 s, so the ACK must be milliseconds.
* **Bad auth touches nothing** (PRD v3 invariant #7): a 401 writes no row and no event —
  an unauthenticated caller must not be able to fill the database or the ledger. The
  reason is logged server-side only; the response body stays generic.
* **Never a 500 on bad input:** oversize → 413, unparseable/unsupported/malformed → 422
  with field locations only (no echoed values — payloads are minors' PII).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from srip_filter import db as dbmod
from srip_filter.models import (
    EssaysModePayload,
    UnsupportedModeError,
    parse_webhook_payload,
)

from .webhook_auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookAuthError,
    verify_webhook,
)

logger = logging.getLogger(__name__)


def _error(code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail})


def _safe_validation_detail(err: ValidationError) -> list[dict[str, Any]]:
    """Field locations + messages only — never the offending input values (PII)."""
    return [
        {"loc": [str(part) for part in e["loc"]], "msg": e["msg"], "type": e["type"]}
        for e in err.errors(include_input=False, include_url=False)
    ]


def register_webhooks(app: FastAPI) -> None:
    """Attach the webhook route. Reads config/secrets/pool off ``app.state`` at request
    time so tests can inject all three without a lifespan."""

    # Status codes as int literals — Phase 9.2 repo convention (also avoids the renamed
    # fastapi status-constant deprecations).
    @app.post(
        "/webhooks/applications",
        status_code=202,
        tags=["webhooks"],
        response_model=None,
    )
    async def receive_application(request: Request) -> Response:
        cfg = app.state.config
        secrets: tuple[str, ...] = app.state.webhook_secrets or ()
        if not secrets:
            # Service misconfiguration, not a caller problem — but still no stack trace.
            return _error(503, "Webhook is not configured.")

        body = await request.body()
        if len(body) > cfg.webhook.max_body_bytes:
            return _error(413, "Payload too large.")

        try:
            verify_webhook(
                request.headers.get(TIMESTAMP_HEADER),
                request.headers.get(SIGNATURE_HEADER),
                body,
                secrets,
                max_skew_seconds=cfg.webhook.max_skew_seconds,
            )
        except WebhookAuthError as err:
            # Reason to the server log only; generic body out (probes learn nothing).
            logger.warning("webhook auth failed: %s", err.reason)
            return _error(401, "Invalid signature.")

        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(422, "Body is not valid JSON.")
        if not isinstance(data, dict):
            return _error(422, "Body must be a JSON object.")

        # The admin panel's connectivity Test button (PRD v3 §2.2): signed ⇒ 200, no row.
        if data.get("_test") is True:
            return JSONResponse(status_code=200, content={"ok": True})

        try:
            payload = parse_webhook_payload(data)
        except UnsupportedModeError as err:
            return _error(422, str(err))
        except ValidationError as err:
            return JSONResponse(
                status_code=422,
                content={"detail": _safe_validation_detail(err)},
            )

        pool = app.state.db_pool
        if pool is None:
            return _error(503, "Database is not configured.")

        is_essays = isinstance(payload, EssaysModePayload)
        result = await dbmod.upsert_application(
            pool,
            mode="essays" if is_essays else "resume",
            submission_id=str(payload.submission_id),
            payload=data,
            cohort_name=payload.cohort_name,
            user_email=payload.user_email,
            student_name=payload.student_name or "",
            sub_track=payload.sub_track if is_essays else "",
            submitted_at=payload.submitted_at,
        )
        await dbmod.add_event(
            pool,
            "delivery",
            submission_id=str(payload.submission_id),
            details={"mode": payload.ats_mode, "result": result},
        )
        return JSONResponse(status_code=202, content={"status": result})
