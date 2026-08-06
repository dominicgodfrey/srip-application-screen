"""Shared API-test wiring.

The default-deny middleware would force a login round-trip into every API test, so an autouse
fixture stamps every session check as valid and ``test_auth.py`` (marked ``real_auth``)
exercises the actual barrier. :func:`raw_asgi_post` is the escape hatch for header bytes
``TestClient`` will not send.
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

    ``TestClient`` cannot express this: httpx rejects a non-ASCII header value client-side, so
    a byte above 0x7F — which a real client sends freely — is unreachable through it. That gap
    is what let an unhandled ``TypeError`` sit in both unauthenticated auth paths.
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
