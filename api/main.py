"""FastAPI application factory (Phase 9.1 scaffold).

A thin, stateless shell over :func:`srip_filter.pipeline.grade_batch`. This commit stands up the
app, an in-memory :class:`~api.registry.JobRegistry`, and a health probe; the upload, polling, and
download routes land in 9.2–9.4. The core stays HTTP-free — everything web lives here.

``create_app`` takes its dependencies as arguments so tests can inject a config and (later) a
``FakeLLMClient`` for a zero-spend suite. The module-level ``app`` is the uvicorn entry point
(``uvicorn api.main:app``).
"""

from __future__ import annotations

from fastapi import FastAPI

from srip_filter.config import AppConfig, get_config
from srip_filter.llm.client import BaseLLMClient

from .registry import JobRegistry
from .schemas import HealthResponse


def create_app(
    *,
    config: AppConfig | None = None,
    client: BaseLLMClient | None = None,
) -> FastAPI:
    """Build the FastAPI app with its registry and (optional) injected LLM client.

    ``config`` defaults to the project ``config.yaml``. ``client`` is the LLM boundary the
    background grading job will use; it is left ``None`` here (no grading endpoint yet) and wired
    at startup in 9.2, with tests injecting a ``FakeLLMClient``. Dependencies are stashed on
    ``app.state`` so route handlers can read them without globals.
    """
    cfg = config if config is not None else get_config()

    app = FastAPI(
        title="SRIP Track 2 Application Filter",
        version="0.1.0",
        summary="Stateless reject-and-rank filtering for SRIP Track 2 applications.",
    )
    app.state.config = cfg
    app.state.llm_client = client
    app.state.registry = JobRegistry(ttl_seconds=cfg.api.job_ttl_seconds)

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        """Liveness probe. Returns 200 with no dependency on the LLM client or any upload."""
        return HealthResponse()

    return app


app = create_app()
