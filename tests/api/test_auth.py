"""P5 admin-auth tests — the real barrier (marked ``real_auth`` to skip the conftest bypass).

Covers: password hashing, signed-cookie sessions, the global login throttle, the default-deny
middleware (redirect for browsers, 401 for API callers, allowlist for health/webhook/
static), the login/logout flow with cookies, and the open-redirect guard on ``next``.
"""

from __future__ import annotations

import pytest
from api.auth import (
    SESSION_COOKIE,
    LoginThrottle,
    hash_password,
    is_open_path,
    sign_session,
    valid_session,
    verify_password,
)
from api.main import create_app
from fastapi.testclient import TestClient

from api import main as main_mod
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


def test_signed_session_round_trips_and_expires() -> None:
    cookie = sign_session("k", 100, now=0.0)
    assert valid_session(cookie, "k", now=99.0)
    assert not valid_session(cookie, "k", now=100.0)  # expired exactly at TTL


@pytest.mark.parametrize(
    "forged",
    [
        None,
        "",
        "nomac",
        "9999999999.deadbeef",  # right shape, wrong mac
        "9999999999." + sign_session("k", 100, now=0.0).partition(".")[2],  # mac of a
        # different expiry: extending the deadline must invalidate the signature
    ],
)
def test_forged_or_tampered_cookies_are_rejected(forged: str | None) -> None:
    assert not valid_session(forged, "k", now=1.0)


def test_a_cookie_signed_with_another_key_is_worthless() -> None:
    """Rotating the admin password rotates the signing key — the only revocation lever
    a stateless session has (P12.1)."""
    cookie = sign_session("old-password-hash", 100, now=0.0)
    assert not valid_session(cookie, "new-password-hash", now=1.0)


def test_unconfigured_secret_validates_nothing() -> None:
    assert not valid_session(sign_session("", 100, now=0.0), "", now=1.0)


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
                 "/logout", "/favicon.ico", "/api/cron/drain"):
        assert is_open_path(path), path
    for path in ("/", "/api/applications", "/api/exports/decisions", "/audit", "/cohorts",
                 "/healthz", "/webhooksx"):
        assert not is_open_path(path), path


# ------------------------------------------------------------------------------------------------
# Middleware + login flow (TestClient)
# ------------------------------------------------------------------------------------------------


def _client(admin_hash: str | None = HASH, *, db_pool: object | None = None) -> TestClient:
    cfg = AppConfig()
    # Local TestClient speaks http://, so the Secure cookie flag must be off to round-trip.
    cfg = cfg.model_copy(update={"auth": cfg.auth.model_copy(update={"cookie_secure": False})})
    app = create_app(
        config=cfg,
        client=FakeLLMClient(cfg),
        db_pool=db_pool,
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
    resp = client.get("/api/applications")  # fetch-style caller, no text/html accept
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


def test_throttle_counts_failures_from_the_events_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P12.2: with a database the lockout is shared across instances, not per-process."""
    ledger: list[str] = []

    async def add_event(pool, kind, **kwargs):
        ledger.append(kind)

    async def count_recent_events(pool, kind, within_seconds):
        return sum(1 for entry in ledger if entry == kind)

    monkeypatch.setattr(main_mod.dbmod, "add_event", add_event)
    monkeypatch.setattr(main_mod.dbmod, "count_recent_events", count_recent_events)

    client = _client(db_pool=object())  # sentinel: both store calls are patched
    for _ in range(AppConfig().auth.max_attempts):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert ledger == ["login_failed"] * AppConfig().auth.max_attempts
    # In-process throttle untouched — the lockout came from the ledger.
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429


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
    # "" (not None) is how create_app spells *unconfigured*: None means "fall back to the
    # environment", so passing it here made this test silently depend on whether the
    # developer's .env happened to be empty — it started failing the moment
    # ADMIN_PASSWORD_HASH was set locally.
    client = _client(admin_hash="")
    # Login refuses (503) and protected routes stay locked — never silently open.
    assert client.post("/login", data={"password": "anything"}).status_code == 503
    assert client.get("/api/applications").status_code == 401
