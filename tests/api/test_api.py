"""API scaffold tests (Phase 9.1): the in-memory job registry + the health probe.

Deterministic, no LLM spend — the grading routes (and their injected ``FakeLLMClient``) arrive in
9.2+. TTL/eviction is exercised with an explicit ``now`` clock so the suite never sleeps.
"""

from __future__ import annotations

from api.main import create_app
from api.registry import JobRegistry, JobState
from api.schemas import JobStatus
from fastapi.testclient import TestClient

from srip_filter.config import AppConfig

# --------------------------------------------------------------------------------------------
# JobRegistry — lifecycle, eviction, TTL sweep
# --------------------------------------------------------------------------------------------


def test_create_registers_queued_job_with_unique_id() -> None:
    reg = JobRegistry(ttl_seconds=3600)
    a = reg.create(now=0.0)
    b = reg.create(now=0.0)

    assert a.state is JobState.QUEUED
    assert a.rows_done == 0
    assert a.rows_total is None
    assert a.job_id != b.job_id
    assert len(reg) == 2


def test_get_returns_job_then_none_after_evict() -> None:
    reg = JobRegistry(ttl_seconds=3600)
    job = reg.create(now=0.0)

    assert reg.get(job.job_id) is job
    reg.evict(job.job_id)
    assert reg.get(job.job_id) is None
    reg.evict(job.job_id)  # idempotent — evicting an unknown id is a no-op
    assert reg.get("not-a-real-id") is None


def test_sweep_evicts_only_expired_finished_jobs() -> None:
    reg = JobRegistry(ttl_seconds=100)
    fresh = reg.create(now=0.0)
    fresh.state = JobState.SUCCEEDED
    fresh.finished_at = 1000.0  # finished "now-ish"

    stale = reg.create(now=0.0)
    stale.state = JobState.SUCCEEDED
    stale.finished_at = 800.0  # finished > ttl before the sweep clock

    dropped = reg.sweep(now=1000.0)

    assert dropped == 1
    assert reg.get(stale.job_id) is None
    assert reg.get(fresh.job_id) is fresh


def test_sweep_reaps_wedged_unfinished_job_by_created_at() -> None:
    # An unfinished job expires off created_at so a stuck run can't pin PII forever.
    reg = JobRegistry(ttl_seconds=100)
    wedged = reg.create(now=0.0)
    wedged.state = JobState.RUNNING

    assert reg.sweep(now=50.0) == 0  # not yet past ttl
    assert reg.get(wedged.job_id) is wedged
    assert reg.sweep(now=150.0) == 1  # past ttl from creation
    assert reg.get(wedged.job_id) is None


def test_job_is_expired_boundary_is_inclusive() -> None:
    reg = JobRegistry(ttl_seconds=100)
    job = reg.create(now=0.0)
    assert not job.is_expired(100, now=99.0)
    assert job.is_expired(100, now=100.0)


# --------------------------------------------------------------------------------------------
# JobStatus projection
# --------------------------------------------------------------------------------------------


def test_job_status_hides_summary_until_succeeded() -> None:
    reg = JobRegistry(ttl_seconds=3600)
    job = reg.create(now=0.0)
    job.state = JobState.RUNNING
    job.rows_total = 10
    job.rows_done = 4

    status = JobStatus.from_job(job)
    assert status.state is JobState.RUNNING
    assert status.rows_done == 4
    assert status.rows_total == 10
    assert status.summary is None  # no result yet
    assert status.error is None


def test_job_status_surfaces_failure_message() -> None:
    reg = JobRegistry(ttl_seconds=3600)
    job = reg.create(now=0.0)
    job.state = JobState.FAILED
    job.error = "could not read uploaded CSV"

    status = JobStatus.from_job(job)
    assert status.state is JobState.FAILED
    assert status.error == "could not read uploaded CSV"
    assert status.summary is None


# --------------------------------------------------------------------------------------------
# App scaffold — health + wiring
# --------------------------------------------------------------------------------------------


def test_health_endpoint_ok() -> None:
    client = TestClient(create_app(config=AppConfig()))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_app_wires_registry_from_config_ttl() -> None:
    cfg = AppConfig()
    cfg = cfg.model_copy(update={"api": cfg.api.model_copy(update={"job_ttl_seconds": 123.0})})
    app = create_app(config=cfg)
    registry = app.state.registry
    assert isinstance(registry, JobRegistry)
    assert registry.ttl_seconds == 123.0
    assert app.state.config is cfg
    assert app.state.llm_client is None
