"""FastAPI application factory (Phase 9.1 scaffold → 9.2 upload + kickoff).

A thin, stateless shell over :func:`srip_filter.pipeline.grade_batch`. The core stays HTTP-free —
everything web lives here. Routes so far:

  * ``GET  /health``  — liveness probe (9.1)
  * ``POST /jobs``    — upload a CSV, validate at the edge, schedule a background run → 202 (9.2)
  * ``POST /jobs/{id}/cohorts`` — what-if cohort assignment over a completed job (11.4)
  * ``POST /cohorts`` — same, from a re-uploaded ``decisions.jsonl`` (11.4)

``create_app`` takes its dependencies as arguments so tests can inject a config and a
``FakeLLMClient`` for a zero-spend suite. In production the LLM client is built once at startup
(lifespan) from config/secrets. The module-level ``app`` is the uvicorn entry point
(``uvicorn api.main:app``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from srip_filter.cohort import assign_cohorts
from srip_filter.config import AppConfig, get_config
from srip_filter.llm.client import BaseLLMClient, FakeLLMClient, OpenAILLMClient
from srip_filter.models import CohortCapacities, CohortResult
from srip_filter.pipeline import promote_record

from .cohorts import CohortFormat, cohort_response, parse_decisions_jsonl
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
from .web import register_pages

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

# Per-tier seat cap as a query param (Phase 11.4): omitted/None = unlimited. Module-level so the
# stringified annotation (PEP 563) resolves when FastAPI builds the route signature.
_Capacity = Annotated[int | None, Query(ge=0, description="Seat cap; omit for unlimited.")]


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

    # -- Server-rendered UI (Phase 10) -----------------------------------------------------------
    # Same-origin Jinja2 templates + static assets; the browser drives everything via fetch against
    # the JSON API above, so no CORS. Paths are resolved off this file so CWD doesn't matter.
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.state.templates = templates
    app.mount("/static", _RevalidatedStaticFiles(directory=str(_HERE / "static")), name="static")
    register_pages(app, templates)

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

        job = app.state.registry.create(filename=file.filename or "")
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

    @app.post(
        "/jobs/{job_id}/records/{submission_id}/promote",
        response_model=None,
        responses={
            404: {"model": ErrorResponse, "description": "Unknown job or submission id"},
            409: {"model": ErrorResponse, "description": "Results not ready / already ranked"},
            503: {"model": ErrorResponse, "description": "LLM client not configured"},
        },
        tags=["jobs"],
    )
    async def promote_submission(job_id: str, submission_id: str) -> dict:
        """Manually promote a REJECTED/NEEDS_REVIEW applicant into the ranking (PRD §10.2).

        The human-resolution path: re-runs every scoring stage on the applicant's original row
        with gate failures recorded-but-bypassed (``manual_override=true`` in the audit record),
        folds them into the ranking, and rebuilds all artifacts. Spends LLM tokens for the
        re-score. Returns the promoted record and the refreshed summary.
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
        client = app.state.llm_client
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LLM client is not configured.",
            )
        try:
            new_result, promoted = await promote_record(job.result, submission_id, client, cfg)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No applicant with that submission id in this job.",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        job.result = new_result
        return {"record": promoted.model_dump(), "summary": new_result.summary}

    # -- Cohort assignment (Phase 11, PRD §11) ---------------------------------------------------
    # Capacities are per-request staff knobs (None/omitted = unlimited), so they ride as query
    # params on both routes; both are synchronous (pure milliseconds-fast core, nothing stored).

    @app.post(
        "/jobs/{job_id}/cohorts",
        response_model=None,
        responses={
            200: {
                "model": CohortResult,
                "description": "Assignment result (JSON, or CSV via ?format=csv)",
            },
            404: {"model": ErrorResponse, "description": "Unknown or evicted job"},
            409: {"model": ErrorResponse, "description": "Results not ready"},
        },
        tags=["cohorts"],
    )
    async def job_cohorts(
        job_id: str,
        honors: _Capacity = None,
        intensive: _Capacity = None,
        regular: _Capacity = None,
        format: CohortFormat = "json",
        tier: str | None = None,
    ) -> Response:
        """What-if cohort assignment over a completed grading job's records (PRD §11).

        Recomputed from scratch on every call and **non-evicting**, so staff can iterate
        capacities against the same job until they discard it (``DELETE /jobs/{id}``) or the TTL
        sweeper does. 404 unknown/evicted job; 409 while queued/running/failed.
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
        capacities = CohortCapacities(honors=honors, intensive=intensive, regular=regular)
        return cohort_response(assign_cohorts(job.result.records, capacities, cfg), format, tier)

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

        The durable entry point: works in a later session, after the grading job was downloaded/
        evicted, or after a host restart — upload the ``decisions.jsonl`` you saved and the same
        deterministic assignment is recomputed. Malformed input is a graceful 4xx, never a 500.
        """
        raw = await read_upload_capped(file, cfg.api.max_upload_bytes)
        records = parse_decisions_jsonl(raw, cfg.api.max_rows)
        capacities = CohortCapacities(honors=honors, intensive=intensive, regular=regular)
        return cohort_response(assign_cohorts(records, capacities, cfg), format, tier)

    return app


app = create_app()
