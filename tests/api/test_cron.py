"""Cron-drain tests — auth, ordering, and the row cap.

No database and no pipeline: the drain-time calls are monkeypatched, so what is under test is
the endpoint's own contract. Per-row isolation is already covered in ``test_worker.py``,
against the same unmodified ``process_one``.
"""

from __future__ import annotations

import pytest
from api.auth import is_open_path
from api.main import create_app
from fastapi.testclient import TestClient

from api import cron as cron_mod
from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient
from tests.api.conftest import raw_asgi_post

SECRET = "test-cron-secret"


class _Calls:
    def __init__(self) -> None:
        self.migrated = 0
        self.reaped_with: list[float] = []
        self.processed = 0
        self.queue_depth = 0


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    c = _Calls()

    async def apply_migrations(pool, *a, **k):
        c.migrated += 1
        return []

    async def reap_stale_claims(pool, stale_seconds):
        assert c.migrated == 1, "reap must run after migrations"
        c.reaped_with.append(stale_seconds)
        return 2

    async def process_one(pool, grade_fn):
        assert c.reaped_with, "draining must not start before the reaper"
        if c.queue_depth <= 0:
            return False
        c.queue_depth -= 1
        c.processed += 1
        return True

    monkeypatch.setattr(cron_mod.dbmod, "apply_migrations", apply_migrations)
    monkeypatch.setattr(cron_mod.dbmod, "reap_stale_claims", reap_stale_claims)
    monkeypatch.setattr(cron_mod, "process_one", process_one)
    return c


def _client(*, secret: str | None = SECRET, config: AppConfig | None = None) -> TestClient:
    cfg = config or AppConfig()
    app = create_app(
        config=cfg,
        client=FakeLLMClient(cfg, lambda *a, **k: None),
        db_pool=object(),  # sentinel — every store call is monkeypatched
        webhook_secrets=("x",),
    )
    app.state.cron_secret = secret
    return TestClient(app)


def _drain(client: TestClient, *, secret: str | None = SECRET):
    headers = {"Authorization": f"Bearer {secret}"} if secret is not None else {}
    return client.post(cron_mod.DRAIN_PATH, headers=headers)


def test_drain_path_is_open_to_the_session_middleware() -> None:
    """Cron carries a bearer token, never a cookie — it must not be redirected to /login."""
    assert is_open_path(cron_mod.DRAIN_PATH)


def test_unconfigured_secret_fails_closed(calls: _Calls) -> None:
    resp = _drain(_client(secret=None))
    assert resp.status_code == 503
    assert (calls.migrated, calls.processed) == (0, 0)


@pytest.mark.parametrize("bad", [None, "", "wrong-secret"])
def test_bad_or_missing_bearer_touches_nothing(calls: _Calls, bad: str | None) -> None:
    resp = _drain(_client(), secret=bad)
    assert resp.status_code == 401
    assert (calls.migrated, calls.reaped_with, calls.processed) == (0, [], 0)


def test_hostile_bearer_bytes_401_not_500(calls: _Calls) -> None:
    """A raw high byte in the Authorization header is a 401, never an unhandled 500.

    Same defect as the webhook's secret header and the same fix — the drain now shares
    ``webhook_auth.constant_time_match`` rather than calling ``compare_digest`` on a str.
    Driven at the ASGI layer because httpx refuses to send the header (see raw_asgi_post).
    """
    status = raw_asgi_post(
        _client().app, cron_mod.DRAIN_PATH, [(b"authorization", b"Bearer caf\xe9")]
    )
    assert status == 401
    assert (calls.migrated, calls.reaped_with, calls.processed) == (0, [], 0)


def test_drain_migrates_reaps_then_empties_the_queue(calls: _Calls) -> None:
    calls.queue_depth = 3
    client = _client()
    resp = _drain(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["processed"] == 3
    assert body["reaped"] == 2
    assert calls.migrated == 1
    assert calls.reaped_with == [client.app.state.config.worker.stale_grading_seconds]


def test_drain_stops_at_the_row_cap(calls: _Calls) -> None:
    """A long queue is spread across invocations rather than run to the timeout."""
    cfg = AppConfig()
    cfg.worker.drain_max_rows = 2
    calls.queue_depth = 10
    resp = _drain(_client(config=cfg))
    assert resp.json()["processed"] == 2
    assert calls.queue_depth == 8


def test_drain_without_a_database_is_a_clean_503(calls: _Calls) -> None:
    client = _client()
    client.app.state.db_pool = None
    assert _drain(client).status_code == 503
    assert calls.migrated == 0
