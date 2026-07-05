"""Shared API-test wiring (P5).

The admin-auth middleware (default-deny) would force a login round-trip into every
pre-existing API test. Instead — mirroring the FakeLLMClient pattern of not re-testing a
boundary everywhere — an autouse fixture stamps every session check as valid, and the
dedicated ``tests/api/test_auth.py`` suite (marked ``real_auth``) exercises the actual
barrier: redirects, 401s, throttling, cookies, logout.
"""

from __future__ import annotations

import pytest
from api.auth import SessionStore


@pytest.fixture(autouse=True)
def _bypass_admin_auth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Treat every session as valid unless the test opts into the real barrier."""
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    monkeypatch.setattr(SessionStore, "is_valid", lambda self, token, now=None: True)
    yield
