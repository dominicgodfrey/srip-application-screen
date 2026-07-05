"""P5 admin-auth tests — the real barrier (marked ``real_auth`` to skip the conftest bypass).

Covers: password hashing, session store TTL, the global login throttle, the default-deny
middleware (redirect for browsers, 401 for API callers, allowlist for health/webhook/
static), the login/logout flow with cookies, and the open-redirect guard on ``next``.
"""

from __future__ import annotations

import pytest
from api.auth import (
    SESSION_COOKIE,
    LoginThrottle,
    SessionStore,
    hash_password,
    is_open_path,
    verify_password,
)
from api.main import create_app
from fastapi.testclient import TestClient

from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient

PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD, iterations=1_000)  # low iterations: keep the suite fast

pytestmark = pytest.mark.real_auth


# ------------------------------------------------------------------------------------------------
# Pure pieces
# ------------------------------------------------------------------------------------------------


def test_password_hash_round_trip() -> None:
    stored = hash_password("s3cret", iterations=1_000)
    assert stored.startswith("pbkdf2_sha256$1000$")
    assert verify_password("s3cret", stored)
    assert not verify_password("wrong", stored)


def test_verify_rejects_malformed_or_foreign_hashes() -> None:
    assert not verify_password("x", "")
    assert not verify_password("x", "plaintext-oops")
    assert not verify_password("x", "bcrypt$whatever$salt$hash")
    assert not verify_password("x", "pbkdf2_sha256$notanint$zz$zz")


def test_session_store_ttl_and_revoke() -> None:
    store = SessionStore(ttl_seconds=100)
    token = store.create(now=0.0)
    assert store.is_valid(token, now=50.0)
    assert not store.is_valid(token, now=100.0)  # expired exactly at TTL
    fresh = store.create(now=0.0)
    store.revoke(fresh)
    assert not store.is_valid(fresh, now=1.0)
    assert not store.is_valid(None) and not store.is_valid("unknown")


def test_session_sweep_drops_expired_only() -> None:
    store = SessionStore(ttl_seconds=100)
    stale, live = store.create(now=0.0), store.create(now=50.0)
    assert store.sweep(now=120.0) == 1
    assert not store.is_valid(stale, now=120.0)
    assert store.is_valid(live, now=120.0)


def test_throttle_locks_after_max_and_slides_open() -> None:
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=100)
    for t in (0.0, 1.0, 2.0):
        assert not throttle.locked_out(now=t)
        throttle.record_failure(now=t)
    assert throttle.locked_out(now=3.0)
    assert not throttle.locked_out(now=103.0)  # window slid past the failures
    throttle.record_failure(now=104.0)
    throttle.reset()
    assert not throttle.locked_out(now=104.0)


def test_open_path_allowlist() -> None:
    for path in ("/health", "/webhooks/applications", "/login", "/static/css/app.css",
                 "/logout", "/favicon.ico"):
        assert is_open_path(path), path
    for path in ("/", "/jobs", "/jobs/abc/results/decisions.jsonl", "/audit", "/cohorts",
                 "/healthz", "/webhooksx"):
        assert not is_open_path(path), path


# ------------------------------------------------------------------------------------------------
# Middleware + login flow (TestClient)
# ------------------------------------------------------------------------------------------------


def _client(admin_hash: str | None = HASH) -> TestClient:
    cfg = AppConfig()
    # Local TestClient speaks http://, so the Secure cookie flag must be off to round-trip.
    cfg = cfg.model_copy(update={"auth": cfg.auth.model_copy(update={"cookie_secure": False})})
    app = create_app(
        config=cfg,
        client=FakeLLMClient(cfg),
        admin_password_hash=admin_hash,
    )
    return TestClient(app, follow_redirects=False)


def test_browser_without_session_redirects_to_login() -> None:
    client = _client()
    resp = client.get("/", headers={"Accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/"


def test_api_without_session_gets_401_json() -> None:
    client = _client()
    resp = client.get("/jobs/some-id")  # fetch-style caller, no text/html accept
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Authentication required."}


def test_health_and_login_stay_open() -> None:
    client = _client()
    assert client.get("/health").status_code == 200
    assert client.get("/login", headers={"Accept": "text/html"}).status_code == 200


def test_webhook_stays_hmac_governed_not_session_governed() -> None:
    # No session, no signature: the webhook path must answer with its own 401/503 —
    # never a login redirect (the website is not a browser).
    client = _client()
    resp = client.post("/webhooks/applications", content=b"{}",
                       headers={"Accept": "text/html"})
    assert resp.status_code in (401, 503)
    assert "location" not in resp.headers


def test_login_flow_sets_cookie_and_grants_access() -> None:
    client = _client()
    resp = client.post("/login", data={"password": PASSWORD, "next": "/audit"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/audit"
    assert SESSION_COOKIE in resp.cookies
    # Cookie persists on the client; a protected page now renders.
    page = client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 200


def test_wrong_password_401_then_lockout_429() -> None:
    client = _client()
    for _ in range(AppConfig().auth.max_attempts):
        resp = client.post("/login", data={"password": "nope"})
        assert resp.status_code == 401
    locked = client.post("/login", data={"password": PASSWORD})  # right pw, still locked
    assert locked.status_code == 429


def test_open_redirect_guard_on_next() -> None:
    client = _client()
    for evil in ("https://evil.example", "//evil.example", "javascript:alert(1)"):
        resp = client.post("/login", data={"password": PASSWORD, "next": evil})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


def test_logout_revokes_session() -> None:
    client = _client()
    client.post("/login", data={"password": PASSWORD})
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 200
    out = client.post("/logout")
    assert out.status_code == 303
    resp = client.get("/", headers={"Accept": "text/html"})
    assert resp.status_code == 303  # back to the login redirect


def test_unconfigured_hash_fails_closed() -> None:
    client = _client(admin_hash=None)
    # Login refuses (503) and protected routes stay locked — never silently open.
    assert client.post("/login", data={"password": "anything"}).status_code == 503
    assert client.get("/jobs/x").status_code == 401
