"""FastAPI application factory — webhook receiver plus session-gated admin UI. The core stays
HTTP-free; everything web lives here.

``create_app`` takes its dependencies as arguments so tests can inject a config and a
``FakeLLMClient`` for a zero-spend suite; in production they are built once in the lifespan.
The module-level ``app`` is the uvicorn entry point.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Query, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from srip_filter import db as dbmod
from srip_filter.cohort import assign_cohorts
from srip_filter.config import AppConfig, get_config, get_secrets
from srip_filter.llm.client import BaseLLMClient, FakeLLMClient, OpenAILLMClient
from srip_filter.models import CohortCapacities, CohortResult
from srip_filter.pipeline import make_grade_fn
from srip_filter.worker import run_worker

from .admin_api import register_admin_api
from .auth import (
    SESSION_COOKIE,
    LoginThrottle,
    client_key,
    is_open_path,
    safe_next_path,
    security_headers,
    sign_session,
    valid_session,
    verify_password,
    wants_html,
)
from .cohorts import CohortFormat, cohort_response, parse_decisions_jsonl
from .cron import register_cron
from .jobs import read_upload_capped
from .schemas import ErrorResponse
from .web import register_pages
from .webhooks import register_webhooks

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent


class _RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that always revalidates. Without a Cache-Control header browsers apply
    heuristic freshness and serve a stale app.js for minutes after a deploy; ``no-cache`` still
    allows conditional requests, so the cost is one revalidation per asset per load."""

    def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Dev/demo only: a zero-spend, no-key FakeLLMClient so the UI can be demoed end to end without
# an OpenAI key. Never set in production — it calls no model.
_DEV_FAKE_LLM_ENV = "SRIP_DEV_FAKE_LLM"

# Local dev only: the in-process grading loop. Unset on Vercel, where the per-minute cron drain
# is the sole driver — a polling loop per serverless instance is DB churn for no gain.
_LOCAL_WORKER_ENV = "SRIP_LOCAL_WORKER"

# Omitted/None = unlimited. Module-level so the stringified annotation resolves when FastAPI
# builds the route signature.
_Capacity = Annotated[int | None, Query(ge=0, description="Seat cap; omit for unlimited.")]


def _wire_core_logging() -> None:
    """Give the root logger a stderr handler so ``srip_filter.*`` records are visible.

    uvicorn and Vercel configure only their own loggers, so ours propagate to a handler-less
    root and vanish: a 42-minute calibration run that paced against the TPM bucket throughout
    emitted zero pacing lines (2026-07-29), and ``/health`` only reports a *dead* drain, not a
    degraded one. One root handler is the whole fix — uvicorn's loggers set ``propagate=False``
    so nothing double-logs, and ``basicConfig`` no-ops when the host already installed handlers.
    """
    logging.basicConfig(
        level=logging.getLogger("uvicorn.error").level or logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
    )


def create_app(
    *,
    config: AppConfig | None = None,
    client: BaseLLMClient | None = None,
    db_pool: object | None = None,
    webhook_secrets: tuple[str, ...] | None = None,
    admin_password_hash: str | None = None,
) -> FastAPI:
    """Build the FastAPI app with its optionally injected dependencies.

    Anything left ``None`` is built at startup from config and secrets; tests inject fakes so no
    build — and no API spend — happens. Everything lands on ``app.state``, so route handlers
    read their dependencies without globals.
    """
    _wire_core_logging()
    cfg = config if config is not None else get_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Build the pool once, only if a test hasn't injected one and a DSN is configured.
        owns_pool = False
        if app.state.db_pool is None:
            dsn = get_secrets().database_url
            if dsn:
                app.state.db_pool = await dbmod.create_pool(
                    dsn,
                    min_size=app.state.config.db.pool_min_size,
                    max_size=app.state.config.db.pool_max_size,
                )
                owns_pool = True
                # Migrations deliberately do NOT run here: on a serverless host the lifespan
                # is a cold start, firing per instance and concurrently, with no release phase
                # to own DDL. The advisory-locked pass lives in the cron drain instead.
        # In the lifespan rather than at import, so importing this module never needs a key.
        if app.state.llm_client is None:
            if os.getenv(_DEV_FAKE_LLM_ENV) == "1":
                from .demo import demo_handler

                logger.warning(
                    "%s=1 — using a zero-spend demo LLM client (no model is called).",
                    _DEV_FAKE_LLM_ENV,
                )
                app.state.llm_client = FakeLLMClient(app.state.config, demo_handler)
            else:
                app.state.llm_client = OpenAILLMClient(app.state.config)
        # `hasattr acquire` guards against test sentinels injected as db_pool.
        worker_stop = asyncio.Event()
        worker_task: asyncio.Task | None = None
        has_pool = app.state.db_pool is not None and hasattr(app.state.db_pool, "acquire")
        if has_pool:
            app.state.llm_client.cache_backend = dbmod.PgCacheBackend(app.state.db_pool)
        if has_pool and os.getenv(_LOCAL_WORKER_ENV) == "1":
            worker_task = asyncio.create_task(
                run_worker(
                    app.state.db_pool,
                    make_grade_fn(app.state.llm_client, app.state.config),
                    poll_seconds=app.state.config.worker.poll_seconds,
                    stop=worker_stop,
                )
            )
        try:
            yield
        finally:
            if worker_task is not None:
                worker_stop.set()  # finish the in-flight row, then exit
                await worker_task
            if owns_pool and app.state.db_pool is not None:
                await app.state.db_pool.close()
                app.state.db_pool = None

    app = FastAPI(
        title="SRIP ATS",
        version="3.0.0",
        summary="Continuous reject-and-rank ATS for SRIP CS-track applications.",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.llm_client = client
    # Tests inject both; production fills the pool in the lifespan and reads secrets from the
    # environment here — no secret ever lives in config.
    app.state.db_pool = db_pool
    if webhook_secrets is not None:
        app.state.webhook_secrets = webhook_secrets
    else:
        env = get_secrets()
        app.state.webhook_secrets = tuple(
            s for s in (env.ats_webhook_secret, env.ats_webhook_secret_previous) if s
        )
    # The cron drain authenticates with a bearer token, so it carries no session — see
    # auth.OPEN_PREFIXES.
    app.state.cron_secret = get_secrets().cron_secret
    register_webhooks(app)
    register_cron(app)
    register_admin_api(app)

    # -- UI shell (before auth, so /login can render) ---------------------------------------
    # Same-origin templates and assets; the browser drives everything by fetch against the
    # JSON API, so no CORS. Paths resolve off this file so CWD doesn't matter.
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.state.templates = templates
    app.mount("/static", _RevalidatedStaticFiles(directory=str(_HERE / "static")), name="static")
    register_pages(app, templates)

    # -- Admin auth (PRD v3 §6) -------------------------------------------------------------
    # Default-deny: every route needs a session except auth.OPEN_PREFIXES.
    app.state.admin_password_hash = (
        admin_password_hash
        if admin_password_hash is not None
        else get_secrets().admin_password_hash
    )
    # The session cookie is signed with the password hash: no separate secret to deploy, and
    # changing the password invalidates every session — a stateless scheme's only revocation.
    app.state.session_secret = app.state.admin_password_hash or ""
    app.state.login_throttle = LoginThrottle(
        max_attempts=cfg.auth.max_attempts,
        lockout_seconds=cfg.auth.lockout_seconds,
        max_attempts_global=cfg.auth.max_attempts_global,
    )

    # One answer to "is this served over https", shared by the Secure cookie flag and HSTS.
    # The demo flag means a local http:// server, which must neither set a Secure cookie it
    # cannot send back nor advertise HSTS — and two copies of that rule is how they diverge.
    https_mode = cfg.auth.cookie_secure and os.getenv(_DEV_FAKE_LLM_ENV) != "1"

    def _client_key(request) -> str:  # type: ignore[no-untyped-def]
        """Throttle bucket for this caller — a salted hash, never an address (see auth)."""
        return client_key(
            request.headers.get("x-forwarded-for"),
            request.client.host if request.client else None,
            app.state.session_secret or "unconfigured",
        )

    # With a database the lockout windows count over `events`, so they hold across serverless
    # instances; the in-memory throttle is the local-dev fallback.
    async def locked_out(actor: str) -> bool:
        pool = app.state.db_pool
        if pool is None:
            return app.state.login_throttle.locked_out(actor)
        mine = await dbmod.count_recent_events(
            pool, "login_failed", cfg.auth.lockout_seconds, actor=actor
        )
        if mine >= cfg.auth.max_attempts:
            return True
        everyone = await dbmod.count_recent_events(
            pool, "login_failed", cfg.auth.lockout_seconds
        )
        return everyone >= cfg.auth.max_attempts_global

    async def record_login_failure(actor: str) -> None:
        pool = app.state.db_pool
        if pool is None:
            app.state.login_throttle.record_failure(actor)
        else:
            await dbmod.add_event(pool, "login_failed", details={"actor": actor})

    @app.middleware("http")
    async def require_admin(request, call_next):  # type: ignore[no-untyped-def]
        """Default-deny session gate, and the one place security headers are stamped — on
        *every* response, so no route added later can quietly miss them."""
        headers = security_headers(https_only=https_mode)
        path = request.url.path
        if is_open_path(path) or valid_session(
            request.cookies.get(SESSION_COOKIE), app.state.session_secret
        ):
            response = await call_next(request)
        elif wants_html(request.headers.get("accept")):
            from fastapi.responses import RedirectResponse

            # Our own routing path, not user content, but it still lands in a URL.
            response = RedirectResponse(
                url=f"/login?next={quote(safe_next_path(path), safe='/')}", status_code=303
            )
        else:
            from fastapi.responses import JSONResponse

            response = JSONResponse(
                status_code=401, content={"detail": "Authentication required."}
            )
        response.headers.update(headers)
        return response

    @app.get("/login", tags=["auth"])
    async def login_page(request: Request, next: str = "/"):  # type: ignore[no-untyped-def]
        from .web import APP_TITLE, BRAND

        return templates.TemplateResponse(
            request,
            "login.html",
            {"brand": BRAND, "app_title": APP_TITLE, "error": "", "next_path": next},
        )

    @app.post("/login", tags=["auth"])
    async def login_submit(request: Request):  # type: ignore[no-untyped-def]
        from fastapi.responses import RedirectResponse

        from .web import APP_TITLE, BRAND

        def _page(error: str, status_code: int):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"brand": BRAND, "app_title": APP_TITLE, "error": error, "next_path": "/"},
                status_code=status_code,
            )

        stored = app.state.admin_password_hash
        if not stored:
            return _page("Login is not configured on this server.", 503)
        actor = _client_key(request)
        if await locked_out(actor):
            return _page("Too many failed attempts. Try again in a few minutes.", 429)

        form = await request.form()
        password = str(form.get("password") or "")
        next_path = safe_next_path(str(form.get("next") or "/"))
        if not verify_password(password, stored):
            await record_login_failure(actor)
            return _page("Incorrect password.", 401)

        app.state.login_throttle.reset()
        token = sign_session(app.state.session_secret, cfg.auth.session_ttl_seconds)
        response = RedirectResponse(url=next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=int(cfg.auth.session_ttl_seconds),
            httponly=True,
            samesite="lax",
            secure=https_mode,
        )
        return response

    @app.post("/logout", tags=["auth"])
    async def logout(request: Request):  # type: ignore[no-untyped-def]
        """Clear the cookie. Stateless sessions cannot be revoked server-side, so a copy taken
        before logout stays valid until it expires; rotating the password kills them all."""
        from fastapi.responses import RedirectResponse

        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/health", response_model=None, tags=["meta"])
    async def health() -> Response:
        """Liveness **and** grading-queue health — the one thing an external monitor watches.

        503 when the oldest ungraded row exceeds ``worker.queue_alert_seconds`` or the database
        is unreachable, because a non-2xx is what makes an off-the-shelf uptime check alert:
        the cron drain failing silently is otherwise invisible until someone opens the
        dashboard. An empty queue is healthy, and it reports an age rather than a count —
        this route is unauthenticated and a count would leak application volume.
        """
        from fastapi.responses import JSONResponse

        pool = app.state.db_pool
        if pool is None or not hasattr(pool, "fetchval"):
            # No database configured: liveness only.
            return JSONResponse(content={"status": "ok", "queue": "not_configured"})
        try:
            age = await dbmod.oldest_pending_seconds(pool)
        except Exception:
            logger.exception("health: queue probe failed")
            return JSONResponse(
                status_code=503, content={"status": "degraded", "reason": "database_unreachable"}
            )

        stalled = age is not None and age > cfg.worker.queue_alert_seconds
        body: dict[str, object] = {
            "status": "degraded" if stalled else "ok",
            "oldest_pending_seconds": round(age, 1) if age is not None else None,
        }
        if stalled:
            body["reason"] = "queue_not_draining"
            logger.error("health: queue not draining; oldest pending row is %.0fs old", age)
        return JSONResponse(status_code=503 if stalled else 200, content=body)

    # -- Cohort assignment (PRD §11) --------------------------------------------------------
    # Capacities are per-request staff knobs, so they ride as query params. The live-DB
    # equivalent is POST /api/cohorts; this one is the offline, durable entry point.

    @app.post(
        "/cohorts",
        response_model=None,
        responses={
            200: {
                "model": CohortResult,
                "description": "Assignment result (JSON, or CSV via ?format=csv)",
            },
            413: {"model": ErrorResponse, "description": "Upload or record count exceeds the cap"},
            422: {"model": ErrorResponse, "description": "Not a readable decisions.jsonl"},
        },
        tags=["cohorts"],
    )
    async def cohorts_from_upload(
        file: Annotated[UploadFile, File()],
        honors: _Capacity = None,
        intensive: _Capacity = None,
        regular: _Capacity = None,
        format: CohortFormat = "json",
        tier: str | None = None,
    ) -> Response:
        """Cohort assignment from a re-uploaded ``decisions.jsonl`` (PRD §11).

        The durable entry point: it still works after a cohort is closed out and purged —
        upload the export and the same assignment recomputes. Malformed input is a 4xx.
        """
        raw = await read_upload_capped(file, cfg.api.max_upload_bytes)
        records = parse_decisions_jsonl(raw, cfg.api.max_rows)
        capacities = CohortCapacities(honors=honors, intensive=intensive, regular=regular)
        return cohort_response(assign_cohorts(records, capacities, cfg), format, tier)

    return app


app = create_app()
