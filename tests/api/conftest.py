"""Shared API-test wiring (P5).

The admin-auth middleware (default-deny) would force a login round-trip into every
pre-existing API test. Instead — mirroring the FakeLLMClient pattern of not re-testing a
boundary everywhere — an autouse fixture stamps every session check as valid, and the
dedicated ``tests/api/test_auth.py`` suite (marked ``real_auth``) exercises the actual
barrier: redirects, 401s, throttling, cookies, logout.

:func:`raw_asgi_post` is the escape hatch for header bytes ``TestClient`` will not send.
"""

from __future__ import annotations

import asyncio

import api.main
import pytest


@pytest.fixture(autouse=True)
def _bypass_admin_auth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Treat every session as valid unless the test opts into the real barrier."""
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    # The middleware resolves this off the module at call time (P12.1 replaced the
    # SessionStore with the pure `valid_session` verifier).
    monkeypatch.setattr(api.main, "valid_session", lambda cookie, secret, **kw: True)
    yield


def raw_asgi_post(app, path: str, headers: list[tuple[bytes, bytes]], body: bytes = b"{}") -> int:
    """POST to ``app`` at the ASGI layer with raw header BYTES; return the status code.

    ``TestClient`` cannot express this case: httpx rejects a non-ASCII header value on the
    client side, so a header byte above 0x7F — which a real HTTP client sends freely, and
    which an ASGI server hands us latin-1 decoded — is unreachable through it. That gap is
    what let an unhandled ``TypeError`` sit in both unauthenticated auth paths. Any
    exception escaping the app is re-raised here so the test sees the 500 for what it is.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("203.0.113.7", 45678),
        "server": ("testserver", 443),
    }
    started: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            started.append(message)

    asyncio.run(app(scope, receive, send))
    return started[0]["status"]
