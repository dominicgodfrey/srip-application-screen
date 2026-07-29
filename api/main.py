"""FastAPI application factory (v3 webhook receiver + session-gated admin UI).

The core stays HTTP-free — everything web lives here. Routes:

  * ``GET  /health``  — liveness probe
  * ``POST /webhooks/applications`` — the partner's per-application delivery (``api.webhooks``)
  * ``/api/*``        — DB-backed admin/review endpoints (``api.admin_api``)
  * ``POST /cohorts`` — what-if cohort assignment from a re-uploaded ``decisions.jsonl``

``create_app`` takes its dependencies as arguments so tests can inject a config and a
``FakeLLMClient`` for a zero-spend suite. In production the LLM client is built once at startup
(lifespan) from config/secrets. The module-level ``app`` is the uvicorn entry point
(``uvicorn api.main:app``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

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
    SessionStore,
    is_open_path,
    verify_password,
    wants_html,
)
from .cohorts import CohortFormat, cohort_response, parse_decisions_jsonl
from .jobs import read_upload_capped
from .schemas import ErrorResponse, HealthResponse
from .web import register_pages
from .webhooks import register_webhooks

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent


class _RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that always revalidates (Cache-Control: no-cache).

    Without a Cache-Control header browsers apply heuristic freshness and keep serving a stale
    app.js/app.css for minutes after a deploy or restart. ``no-cache`` still allows conditional
    (ETag/304) requests, so the cost is one revalidation round-trip per asset per load.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Dev/demo only: launch with SRIP_DEV_FAKE_LLM=1 to wire a zero-spend, no-key FakeLLMClient backed
# by api.demo.demo_handler, so the whole UI can be demoed end-to-end without an OpenAI key. Never
# set this in production — it does not call any model.
_DEV_FAKE_LLM_ENV = "SRIP_DEV_FAKE_LLM"

# Local dev only (P11.6): start the in-process grading loop. Unset on Vercel, where the
# per-minute cron drain (POST /api/cron/drain) is the sole driver — a polling loop per
# serverless instance is constant DB churn for no gain.
_LOCAL_WORKER_ENV = "SRIP_LOCAL_WORKER"

# Per-tier seat cap as a query param (Phase 11.4): omitted/None = unlimited. Module-level so the
# stringified annotation (PEP 563) resolves when FastAPI builds the route signature.
_Capacity = Annotated[int | None, Query(ge=0, description="Seat cap; omit for unlimited.")]


def create_app(
    *,
    config: AppConfig | None = None,
    client: BaseLLMClient | None = None,
    db_pool: object | None = None,
    webhook_secrets: tuple[str, ...] | None = None,
    admin_password_hash: str | None = None,
) -> FastAPI:
    """Build the FastAPI app with its (optional) injected LLM client.

    ``config`` defaults to the project ``config.yaml``. ``client`` is the LLM boundary the
    grading worker uses; when left ``None`` it is built once at startup from config/secrets
    (a real :class:`~srip_filter.llm.client.OpenAILLMClient`). Tests inject a ``FakeLLMClient`` so
    no startup build — and no API spend — happens. Dependencies live on ``app.state`` so route
    handlers read them without globals.
    """
    cfg = config if config is not None else get_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # v3 (P2): build the Postgres pool once, only if a test hasn't injected one and a
        # DSN is configured. Migrations apply at startup — single instance, tiny schema.
        owns_pool = False
        if app.state.db_pool is None:
            dsn = get_secrets().database_url
            if dsn:
                app.state.db_pool = await dbmod.create_pool(
                    dsn,
                    min_size=app.state.config.db.pool_min_size,
                    max_size=app.state.config.db.pool_max_size,
                )
                await dbmod.apply_migrations(app.state.db_pool)
                owns_pool = True
        # Build the client once, only if a test hasn't injected one. Done in the lifespan (not at
        # import) so importing this module never needs an API key. The dev/demo flag swaps in a
        # zero-spend FakeLLMClient; the default path is the real OpenAI client.
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
        # v3 (P4): with a real pool, wire the durable LLM cache. `hasattr acquire` guards
        # against test sentinels injected as db_pool.
        worker_stop = asyncio.Event()
        worker_task: asyncio.Task | None = None
        has_pool = app.state.db_pool is not None and hasattr(app.state.db_pool, "acquire")
        if has_pool:
            app.state.llm_client.cache_backend = dbmod.PgCacheBackend(app.state.db_pool)
        # P11.6: the in-process loop is local-dev only. On Vercel every instance would run
        # its own polling loop; POST /api/cron/drain is the one driver there.
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
                worker_stop.set()  # graceful: finish the in-flight row, then exit
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
    # v3 (P2): DB pool + webhook HMAC secrets. Tests inject both; production fills the pool
    # in the lifespan and reads secrets from the environment here (no secret ever in config).
    app.state.db_pool = db_pool
    if webhook_secrets is not None:
        app.state.webhook_secrets = webhook_secrets
    else:
        env = get_secrets()
        app.state.webhook_secrets = tuple(
            s for s in (env.ats_webhook_secret, env.ats_webhook_secret_previous) if s
        )
    register_webhooks(app)
    register_admin_api(app)  # v3 (P6): DB-backed review endpoints, session-gated by P5

    # -- Server-rendered UI shell (Phase 10; created before auth so /login can render) -----------
    # Same-origin Jinja2 templates + static assets; the browser drives everything via fetch
    # against the JSON API, so no CORS. Paths are resolved off this file so CWD doesn't matter.
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.state.templates = templates
    app.mount("/static", _RevalidatedStaticFiles(directory=str(_HERE / "static")), name="static")
    register_pages(app, templates)

    # -- Admin auth (P5, PRD v3 §6) --------------------------------------------------------------
    # Default-deny: every route needs a session except auth.OPEN_PREFIXES (health, the
    # HMAC-verified webhook, the login page, static assets). Shared-password login → opaque
    # server-side session token in an HttpOnly cookie; global sliding lockout on failures.
    app.state.admin_password_hash = (
        admin_password_hash
        if admin_password_hash is not None
        else get_secrets().admin_password_hash
    )
    app.state.sessions = SessionStore(ttl_seconds=cfg.auth.session_ttl_seconds)
    app.state.login_throttle = LoginThrottle(
        max_attempts=cfg.auth.max_attempts, lockout_seconds=cfg.auth.lockout_seconds
    )

    @app.middleware("http")
    async def require_admin(request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if is_open_path(path):
            return await call_next(request)
        if app.state.sessions.is_valid(request.cookies.get(SESSION_COOKIE)):
            return await call_next(request)
        if wants_html(request.headers.get("accept")):
            from fastapi.responses import RedirectResponse

            return RedirectResponse(url=f"/login?next={path}", status_code=303)
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=401, content={"detail": "Authentication required."})

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
        if app.state.login_throttle.locked_out():
            return _page("Too many failed attempts. Try again in a few minutes.", 429)

        form = await request.form()
        password = str(form.get("password") or "")
        next_path = str(form.get("next") or "/")
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"  # open-redirect guard: same-origin paths only
        if not verify_password(password, stored):
            app.state.login_throttle.record_failure()
            return _page("Incorrect password.", 401)

        app.state.login_throttle.reset()
        token = app.state.sessions.create()
        response = RedirectResponse(url=next_path, status_code=303)
        # The dev/demo flag (never set in production — see its declaration) also drops the
        # Secure cookie flag so the local http:// demo can hold a session.
        dev_mode = os.getenv(_DEV_FAKE_LLM_ENV) == "1"
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=int(cfg.auth.session_ttl_seconds),
            httponly=True,
            samesite="lax",
            secure=cfg.auth.cookie_secure and not dev_mode,
        )
        return response

    @app.post("/logout", tags=["auth"])
    async def logout(request: Request):  # type: ignore[no-untyped-def]
        from fastapi.responses import RedirectResponse

        app.state.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        """Liveness probe. Returns 200 with no dependency on the LLM client or any upload."""
        return HealthResponse()

    # -- Cohort assignment (Phase 11, PRD §11) ---------------------------------------------------
    # Capacities are per-request staff knobs (None/omitted = unlimited), so they ride as query
    # params; synchronous (pure milliseconds-fast core, nothing stored). The live-DB equivalent
    # is POST /api/cohorts (admin_api); this one is the offline/durable entry point.

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

        The durable entry point: works in a later session, or after a cohort has been closed out
        and purged — upload the ``decisions.jsonl`` you exported and the same deterministic
        assignment is recomputed. Malformed input is a graceful 4xx, never a 500.
        """
        raw = await read_upload_capped(file, cfg.api.max_upload_bytes)
        records = parse_decisions_jsonl(raw, cfg.api.max_rows)
        capacities = CohortCapacities(honors=honors, intensive=intensive, regular=regular)
        return cohort_response(assign_cohorts(records, capacities, cfg), format, tier)

    return app


app = create_app()
