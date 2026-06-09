"""FastAPI application factory (Phase 9.1 scaffold → 9.2 upload + kickoff).

A thin, stateless shell over :func:`srip_filter.pipeline.grade_batch`. The core stays HTTP-free —
everything web lives here. Routes so far:

  * ``GET  /health``  — liveness probe (9.1)
  * ``POST /jobs``    — upload a CSV, validate at the edge, schedule a background run → 202 (9.2)

``create_app`` takes its dependencies as arguments so tests can inject a config and a
``FakeLLMClient`` for a zero-spend suite. In production the LLM client is built once at startup
(lifespan) from config/secrets. The module-level ``app`` is the uvicorn entry point
(``uvicorn api.main:app``).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, UploadFile, status

from srip_filter.config import AppConfig, get_config
from srip_filter.llm.client import BaseLLMClient, OpenAILLMClient

from .jobs import read_upload_capped, run_job, validate_csv
from .registry import JobRegistry
from .schemas import ErrorResponse, HealthResponse, JobCreated


def create_app(
    *,
    config: AppConfig | None = None,
    client: BaseLLMClient | None = None,
) -> FastAPI:
    """Build the FastAPI app with its registry and (optional) injected LLM client.

    ``config`` defaults to the project ``config.yaml``. ``client`` is the LLM boundary the
    background grading job uses; when left ``None`` it is built once at startup from config/secrets
    (a real :class:`~srip_filter.llm.client.OpenAILLMClient`). Tests inject a ``FakeLLMClient`` so
    no startup build — and no API spend — happens. Dependencies live on ``app.state`` so route
    handlers read them without globals.
    """
    cfg = config if config is not None else get_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Build the real client once, only if a test hasn't injected one. Done in the lifespan
        # (not at import) so importing this module never needs an API key.
        if app.state.llm_client is None:
            app.state.llm_client = OpenAILLMClient(app.state.config)
        yield

    app = FastAPI(
        title="SRIP Track 2 Application Filter",
        version="0.1.0",
        summary="Stateless reject-and-rank filtering for SRIP Track 2 applications.",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.llm_client = client
    app.state.registry = JobRegistry(ttl_seconds=cfg.api.job_ttl_seconds)
    # Hold strong refs to in-flight background tasks so they're not garbage-collected mid-run.
    app.state.background_tasks = set()

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        """Liveness probe. Returns 200 with no dependency on the LLM client or any upload."""
        return HealthResponse()

    @app.post(
        "/jobs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=JobCreated,
        responses={
            413: {"model": ErrorResponse, "description": "Upload or row count exceeds the cap"},
            422: {"model": ErrorResponse, "description": "Unreadable CSV or invalid headers"},
            503: {"model": ErrorResponse, "description": "LLM client not configured"},
        },
        tags=["jobs"],
    )
    async def create_job(file: Annotated[UploadFile, File()]) -> JobCreated:
        """Accept a CSV upload, validate it at the edge, and schedule a background grading run.

        Enforces the byte-size cap (413), parseability + §2 header contract (422), and the row cap
        (413) before any work. On success a :class:`~api.registry.Job` is created and
        :func:`~api.jobs.run_job` is scheduled; the response is 202 with the ``job_id`` to poll.
        """
        client = app.state.llm_client
        if client is None:  # only reachable if startup was skipped without an injected client
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LLM client is not configured.",
            )

        raw = await read_upload_capped(file, cfg.api.max_upload_bytes)
        validate_csv(raw, cfg)

        job = app.state.registry.create()
        task = asyncio.create_task(run_job(job, raw, client, cfg))
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)
        return JobCreated(job_id=job.job_id, state=job.state)

    return app


app = create_app()
