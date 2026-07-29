"""`GET /health` — liveness plus the grading-queue signal.

This endpoint is the only thing that notices a silently dead grading path (cron stopped
firing, drain erroring, claims failing), so its status code is load-bearing: an
off-the-shelf uptime check alerts on non-2xx and on nothing else.
"""

from __future__ import annotations

import pytest
from api.main import create_app
from fastapi.testclient import TestClient

from api import main as main_mod
from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient


class _FakePool:
    """Stands in for asyncpg's pool — only ``fetchval`` is probed by the health route."""

    async def fetchval(self, *args, **kwargs):  # pragma: no cover - patched per test
        return None


def _client(*, pool: object | None = None, config: AppConfig | None = None) -> TestClient:
    cfg = config or AppConfig()
    app = create_app(
        config=cfg,
        client=FakeLLMClient(cfg, lambda *a, **k: None),
        db_pool=pool,
        webhook_secrets=("x",),
    )
    return TestClient(app)


def _patch_age(monkeypatch: pytest.MonkeyPatch, age: float | None) -> None:
    async def oldest_pending_seconds(pool):
        return age

    monkeypatch.setattr(main_mod.dbmod, "oldest_pending_seconds", oldest_pending_seconds)


def test_without_a_database_it_is_liveness_only() -> None:
    """Local dev and tests run with no pool; that must not read as unhealthy."""
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "queue": "not_configured"}


def test_empty_queue_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing waiting is the normal steady state, not a fault."""
    _patch_age(monkeypatch, None)
    resp = _client(pool=_FakePool()).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "oldest_pending_seconds": None}


def test_a_queue_inside_the_window_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backlog mid-drain is expected — a burst of applications must not page anyone."""
    _patch_age(monkeypatch, AppConfig().worker.queue_alert_seconds - 1)
    resp = _client(pool=_FakePool()).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_a_stalled_queue_is_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this endpoint exists for: rows waiting with nothing draining them."""
    cfg = AppConfig()
    _patch_age(monkeypatch, cfg.worker.queue_alert_seconds + 60)
    resp = _client(pool=_FakePool(), config=cfg).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "queue_not_draining"
    assert body["oldest_pending_seconds"] == pytest.approx(cfg.worker.queue_alert_seconds + 60)


def test_an_unreachable_database_degrades_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(pool):
        raise OSError("connection refused")

    monkeypatch.setattr(main_mod.dbmod, "oldest_pending_seconds", boom)
    resp = _client(pool=_FakePool()).get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "reason": "database_unreachable"}


def test_health_reports_no_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route is unauthenticated, so it exposes an age and never application volume."""
    _patch_age(monkeypatch, 42.0)
    body = _client(pool=_FakePool()).get("/health").json()
    assert set(body) == {"status", "oldest_pending_seconds"}
