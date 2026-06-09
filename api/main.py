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
import contextlib
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Response, UploadFile, status

from srip_filter.config import AppConfig, get_config
from srip_filter.llm.client import BaseLLMClient, OpenAILLMClient

from .jobs import (
    ArtifactName,
    artifact_response,
    read_upload_capped,
    run_job,
    sweeper_loop,
    validate_csv,
)
from .registry import JobRegistry, JobState
from .schemas import ErrorResponse, HealthResponse, JobCreated, JobStatus


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
        # Background TTL sweeper drops expired jobs so PII-bearing results aren't held.
        sweeper = asyncio.create_task(
            sweeper_loop(app.state.registry, app.state.config.api.job_sweep_seconds)
        )
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

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

    @app.get(
        "/jobs/{job_id}",
        response_model=JobStatus,
        responses={404: {"model": ErrorResponse, "description": "Unknown or evicted job"}},
        tags=["jobs"],
    )
    async def get_job(job_id: str) -> JobStatus:
        """Poll a job's lifecycle + progress; once succeeded, the run ``summary`` is included.

        An unknown id — never created, or already evicted on download / past TTL — is a 404. A
        failed job reports ``state="failed"`` with a safe one-line message (never PII or a stack
        trace). Progress (``rows_done``/``rows_total``) is updated live by the grading callback.
        """
        job = app.state.registry.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No job with that id; it may have expired or its results were downloaded.",
            )
        return JobStatus.from_job(job)

    @app.get(
        "/jobs/{job_id}/results/{artifact}",
        responses={
            404: {"model": ErrorResponse, "description": "Unknown or evicted job"},
            409: {"model": ErrorResponse, "description": "Results not ready"},
        },
        tags=["jobs"],
    )
    async def download_artifact(job_id: str, artifact: ArtifactName) -> Response:
        """Stream one of the five in-memory result artifacts (PRD §12) with its content type.

        404 if the job is unknown/evicted; 409 if it hasn't succeeded yet (queued/running) or
        failed (no results). Downloads are non-evicting so all five files can be fetched; the
        client calls ``DELETE /jobs/{id}`` to discard, and the TTL sweeper is the backstop.
        """
        job = app.state.registry.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No job with that id; it may have expired or been discarded.",
            )
        if job.state is not JobState.SUCCEEDED or job.result is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Results are not available; job state is '{job.state}'.",
            )
        return artifact_response(job.result, artifact)

    @app.delete(
        "/jobs/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={404: {"model": ErrorResponse, "description": "Unknown or evicted job"}},
        tags=["jobs"],
    )
    async def delete_job(job_id: str) -> Response:
        """Discard a job and its in-memory results immediately (discard-after-download).

        404 if the job is already unknown/evicted, so a double-discard is reported honestly rather
        than silently succeeding.
        """
        if app.state.registry.get(job_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No job with that id; it may have expired or already been discarded.",
            )
        app.state.registry.evict(job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_app()
