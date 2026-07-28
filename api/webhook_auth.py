"""Webhook authentication (P10, live scheme).

The partner's dispatcher sends a single static header (`thinkNeuroWebsite/lib/ats.ts`)::

    X-ATS-Secret: <ATS_WEBHOOK_SECRET>

Rules: constant-time comparison; any configured secret (current or previous) may match —
that is the zero-downtime rotation path. A failure raises :class:`WebhookAuthError` with a
machine reason for the server log; the HTTP response stays a generic 401 so probes learn
nothing (CLAUDE.md security rules).

**Fail closed:** with no secrets configured every request is rejected. Their dispatcher
omits the header entirely when its env var is unset, so an unset secret on either side
looks like a healthy deploy that authenticates nothing — rejecting is the safe read.

HMAC (timestamp + body binding + replay window) was the v3 design and is the intended
pre-production hardening (WEBSITE_ASKS #1); it lives in git history and this module is the
seam to restore it. A static bearer secret over HTTPS is replayable and does not bind the
body — accepted deliberately, revisit before go-live.

Pure functions, no FastAPI imports — unit-testable and reusable by the replay tool.
"""

from __future__ import annotations

import hmac

SECRET_HEADER = "X-ATS-Secret"


class WebhookAuthError(Exception):
    """Authentication failed. ``reason`` is for server logs, never the response."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_webhook(secret_header: str | None, secrets: tuple[str, ...]) -> None:
    """Raise :class:`WebhookAuthError` unless the header matches a configured secret.

    ``secrets`` is (current,) or (current, previous) — any match passes. Every branch uses
    ``hmac.compare_digest`` so timing reveals nothing about how close a guess got.
    """
    if not secrets:
        raise WebhookAuthError("no_secrets_configured")
    if not secret_header:
        raise WebhookAuthError("missing_header")

    for secret in secrets:
        if hmac.compare_digest(secret, secret_header):
            return
    raise WebhookAuthError("bad_secret")
