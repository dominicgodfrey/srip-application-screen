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
pre-production hardening; it lives in git history and this module is the
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


def constant_time_match(provided: str | None, secrets: tuple[str, ...]) -> bool:
    """True when ``provided`` equals any configured secret, compared in constant time.

    The shared primitive behind every header-secret check in the service (the webhook's
    ``X-ATS-Secret`` and the cron drain's bearer token), so the encoding rule below lives
    in exactly one place.

    **Both sides are encoded to bytes first, and that is load-bearing.**
    ``hmac.compare_digest`` refuses a non-ASCII ``str`` with a ``TypeError``, and ASGI
    servers hand header values over latin-1 decoded — so a single byte above 0x7F in an
    attacker-controlled header turned a 401 into an unhandled 500 on two unauthenticated
    endpoints (never a bypass: the request still failed closed, but a 500 is a stack trace
    in the platform log and violates the §2.1 "never a 500 on bad input" rule).

    latin-1 round-trips the exact bytes the server read, and ``replace`` means even an
    exotic codepoint from a non-conforming server degrades to a byte that matches nothing.
    Configured secrets are encoded utf-8, which is how the environment delivers them; for
    the ASCII secrets this service actually uses the two encodings are identical.
    """
    if not provided or not secrets:
        return False
    candidate = provided.encode("latin-1", "replace")
    # Not short-circuited on the first match: every configured secret is compared so a
    # rotation window cannot be distinguished by timing.
    matched = False
    for secret in secrets:
        matched |= hmac.compare_digest(secret.encode("utf-8"), candidate)
    return matched


def verify_webhook(secret_header: str | None, secrets: tuple[str, ...]) -> None:
    """Raise :class:`WebhookAuthError` unless the header matches a configured secret.

    ``secrets`` is (current,) or (current, previous) — any match passes. The comparison is
    :func:`constant_time_match`, so timing reveals nothing about how close a guess got.
    """
    if not secrets:
        raise WebhookAuthError("no_secrets_configured")
    if not secret_header:
        raise WebhookAuthError("missing_header")
    if not constant_time_match(secret_header, secrets):
        raise WebhookAuthError("bad_secret")
