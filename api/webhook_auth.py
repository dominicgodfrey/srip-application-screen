"""Webhook authentication — the partner's dispatcher sends a static ``X-ATS-Secret`` header.

Compared in constant time against any configured secret, current or previous, which is the
zero-downtime rotation path. A failure raises :class:`WebhookAuthError` with a machine reason
for the log while the response stays a generic 401, so probes learn nothing.

**Fail closed:** with no secrets configured every request is rejected. Their dispatcher omits
the header entirely when its env var is unset, so an unset secret on either side looks like a
healthy deploy that authenticates nothing.

HMAC (timestamp, body binding, replay window) is the intended pre-production hardening and
lives in git history; this module is the seam to restore it. A static secret over HTTPS is
replayable and does not bind the body — accepted deliberately, revisit before go-live.

Pure functions, no FastAPI imports, so the replay tool shares them.
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
    """True when ``provided`` equals any configured secret, compared in constant time. The one
    primitive behind every header-secret check here, so the encoding rule lives in one place.

    **Encoding both sides to bytes first is load-bearing.** ``compare_digest`` refuses a
    non-ASCII ``str`` with a ``TypeError``, and ASGI servers hand header values over latin-1
    decoded — so one byte above 0x7F in an attacker-controlled header turned a 401 into an
    unhandled 500 on two unauthenticated endpoints. Never a bypass, but a 500 is a stack trace
    in the platform log and breaks the §2.1 "never a 500 on bad input" rule. latin-1 round-trips
    exactly what the server read, and ``replace`` degrades anything exotic to a non-match.
    """
    if not provided or not secrets:
        return False
    candidate = provided.encode("latin-1", "replace")
    # Not short-circuited: every secret is compared, so timing cannot reveal a rotation.
    matched = False
    for secret in secrets:
        matched |= hmac.compare_digest(secret.encode("utf-8"), candidate)
    return matched


def verify_webhook(secret_header: str | None, secrets: tuple[str, ...]) -> None:
    """Raise :class:`WebhookAuthError` unless the header matches a configured secret — either
    current or previous, so a rotation window passes both."""
    if not secrets:
        raise WebhookAuthError("no_secrets_configured")
    if not secret_header:
        raise WebhookAuthError("missing_header")
    if not constant_time_match(secret_header, secrets):
        raise WebhookAuthError("bad_secret")
