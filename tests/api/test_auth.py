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
                 "/healthz", "/webhooksx",
                 # The bulk purge is irreversible and destroys minors' PII: it must never
                 # drift onto the allowlist, and /api/cron/ sitting there makes that a real
                 # hazard rather than a theoretical one.
                 "/api/admin/purge", "/api/admin/purge-preview"):
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


def test_bulk_purge_is_unreachable_without_a_session() -> None:
    """The destructive endpoint has to be stopped by the middleware, before any handler.

    A sentinel pool would raise on use, so reaching the handler at all would surface as a 500
    rather than a quiet 401 — the assertion is that the barrier, not luck, is what stops it.
    """
    client = _client(db_pool=object())

    preview = client.get("/api/admin/purge-preview")
    purge = client.post("/api/admin/purge", json={"expected_count": 0})

    assert preview.status_code == 401
    assert purge.status_code == 401
    assert purge.json() == {"detail": "Authentication required."}


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


def _ledger_backed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Patch the two store calls the throttle uses; return the ledger it writes to."""
    ledger: list[tuple[str, str | None]] = []

    async def add_event(pool, kind, *, submission_id=None, details=None):
        ledger.append((kind, (details or {}).get("actor")))

    async def count_recent_events(pool, kind, within_seconds, *, actor=None):
        return sum(
            1 for k, a in ledger if k == kind and (actor is None or a == actor)
        )

    monkeypatch.setattr(main_mod.dbmod, "add_event", add_event)
    monkeypatch.setattr(main_mod.dbmod, "count_recent_events", count_recent_events)
    return ledger


def test_throttle_counts_failures_from_the_events_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P12.2: with a database the lockout is shared across instances, not per-process."""
    ledger = _ledger_backed(monkeypatch)

    client = _client(db_pool=object())  # sentinel: both store calls are patched
    for _ in range(AppConfig().auth.max_attempts):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert [kind for kind, _ in ledger] == ["login_failed"] * AppConfig().auth.max_attempts
    # In-process throttle untouched — the lockout came from the ledger.
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429


def test_the_ledger_records_a_hashed_actor_never_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`events` is non-PII by law and an IP is personal data."""
    ledger = _ledger_backed(monkeypatch)
    client = _client(db_pool=object())

    client.post(
        "/login", data={"password": "nope"}, headers={"X-Forwarded-For": "198.51.100.9"}
    )

    (_, actor) = ledger[0]
    assert actor and "198.51.100.9" not in actor
    assert len(actor) == 16 and all(c in "0123456789abcdef" for c in actor)


def test_one_abusive_client_cannot_lock_everyone_else_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The availability bug: a global-only counter let anyone hold staff out for free.

    An attacker spending well past the per-client limit must lock out only themselves,
    while a legitimate operator on a different address still logs straight in.
    """
    _ledger_backed(monkeypatch)
    client = _client(db_pool=object())
    attacker = {"X-Forwarded-For": "198.51.100.9"}
    operator = {"X-Forwarded-For": "203.0.113.4"}

    for _ in range(AppConfig().auth.max_attempts * 2):
        client.post("/login", data={"password": "nope"}, headers=attacker)

    assert client.post("/login", data={"password": PASSWORD}, headers=attacker).status_code == 429
    assert client.post("/login", data={"password": PASSWORD}, headers=operator).status_code == 303


def test_the_global_tier_still_backstops_a_distributed_guesser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-client throttling must not have retired the distributed-attack backstop."""
    _ledger_backed(monkeypatch)
    cfg = AppConfig()
    client = _client(db_pool=object())

    # One failure each from enough distinct clients to cross the global threshold, so no
    # single per-client bucket is ever close to its own limit.
    for n in range(cfg.auth.max_attempts_global):
        client.post(
            "/login", data={"password": "nope"}, headers={"X-Forwarded-For": f"198.51.100.{n}"}
        )

    fresh = {"X-Forwarded-For": "203.0.113.4"}
    assert client.post("/login", data={"password": PASSWORD}, headers=fresh).status_code == 429


def test_open_redirect_guard_on_next() -> None:
    client = _client()
    evils = (
        "https://evil.example",
        "//evil.example",
        "javascript:alert(1)",
        # Browsers follow the WHATWG parser, which folds a backslash to a slash for special
        # schemes — so this resolves to https://evil.example despite the leading slash.
        "/\\evil.example",
        "/\\/evil.example",
        "/legit/../\\evil.example",
    )
    for evil in evils:
        resp = client.post("/login", data={"password": PASSWORD, "next": evil})
        assert resp.status_code == 303, evil
        assert resp.headers["location"] == "/", evil


def test_a_legitimate_next_path_still_round_trips() -> None:
    """The tightened guard must not have broken the case it exists to serve."""
    client = _client()
    resp = client.post("/login", data={"password": PASSWORD, "next": "/audit?cohort=su26-cs"})
    assert resp.headers["location"] == "/audit?cohort=su26-cs"


# ------------------------------------------------------------------------------------------------
# Security response headers
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/health", {}),                                        # open, no session
        ("/login", {"headers": {"Accept": "text/html"}}),       # open, HTML
        ("/api/applications", {}),                              # denied, JSON 401
        ("/", {"headers": {"Accept": "text/html"}}),            # denied, redirect
        ("/static/css/app.css", {}),                            # mounted sub-app
    ],
)
def test_security_headers_are_on_every_response(path: str, kwargs: dict) -> None:
    """Stamped in the middleware, so no route — present or future — can miss them.

    This service renders minors' PII and hosts an irreversible purge control, and shipped
    with none of these set. Error pages and redirects count: a 401 that can be framed is
    still a framed page.
    """
    resp = _client().get(path, **kwargs)
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp  # the backstop for a missed escape in the audit UI
    assert "object-src 'none'" in csp and "base-uri 'none'" in csp


def _headers_for(config) -> dict:
    app = create_app(config=config, client=FakeLLMClient(config), admin_password_hash=HASH)
    return TestClient(app, follow_redirects=False).get("/health").headers


def test_hsts_only_when_the_deployment_is_https() -> None:
    """Sending HSTS over plaintext is wrong, and local development speaks http://."""
    cfg = AppConfig()
    http_only = cfg.model_copy(
        update={"auth": cfg.auth.model_copy(update={"cookie_secure": False})}
    )
    https = cfg.model_copy(update={"auth": cfg.auth.model_copy(update={"cookie_secure": True})})

    assert "Strict-Transport-Security" not in _headers_for(http_only)
    assert "max-age=31536000" in _headers_for(https)["Strict-Transport-Security"]


def test_the_dev_flag_drops_hsts_the_same_way_it_drops_the_secure_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One rule for "is this https", so the two cannot drift apart.

    The demo flag means a local http:// server. It already dropped the Secure cookie flag
    (otherwise the session cannot round-trip); HSTS was still being advertised, which
    browsers ignore over plaintext but which contradicted the cookie's own answer.
    """
    monkeypatch.setenv("SRIP_DEV_FAKE_LLM", "1")
    cfg = AppConfig()  # cookie_secure stays True — the flag is what overrides it
    assert cfg.auth.cookie_secure is True
    assert "Strict-Transport-Security" not in _headers_for(cfg)


def test_the_font_hosts_base_html_uses_are_the_only_external_origins_allowed() -> None:
    """The CSP has to actually permit the one external dependency the UI ships with.

    A CSP that breaks the page gets removed, so this pins the pairing: if base.html stops
    loading Google Fonts, these entries should go with it.
    """
    csp = _client().get("/health").headers["Content-Security-Policy"]
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "font-src 'self' https://fonts.gstatic.com" in csp
    assert "default-src 'self'" in csp


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
