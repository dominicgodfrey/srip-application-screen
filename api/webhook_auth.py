"""Webhook HMAC verification (P2, PRD v3 §2.1).

Scheme (mirrored by the website's ``sendWebhook`` per WEBSITE_ASKS #1, and by
``scripts/replay.py``):

    X-ATS-Timestamp: <unix seconds>
    X-ATS-Signature: hex(HMAC_SHA256(secret, f"{timestamp}.{raw_body}"))

Rules: constant-time comparison; the timestamp must parse and sit within
``max_skew_seconds`` of the server clock (replay window); any configured secret
(current or previous) may sign — that is the zero-downtime rotation path. A failure
raises :class:`WebhookAuthError` with a machine reason for the server log; the HTTP
response stays a generic 401 so probes learn nothing (CLAUDE.md security rules).

Pure functions, no FastAPI imports — fully unit-testable and reusable by the replay tool.
"""

from __future__ import annotations

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-ATS-Timestamp"
SIGNATURE_HEADER = "X-ATS-Signature"


class WebhookAuthError(Exception):
    """Signature verification failed. ``reason`` is for server logs, never the response."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """Compute the hex signature for a payload — the single source of the signing rule."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook(
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    secrets: tuple[str, ...],
    *,
    max_skew_seconds: float,
    now: float | None = None,
) -> None:
    """Raise :class:`WebhookAuthError` unless the request is authentically signed and fresh.

    ``secrets`` is (current,) or (current, previous) — any match passes. Every branch uses
    ``hmac.compare_digest`` so timing reveals nothing about how close a forgery got.
    """
    if not secrets:
        raise WebhookAuthError("no_secrets_configured")
    if not timestamp_header or not signature_header:
        raise WebhookAuthError("missing_headers")

    try:
        ts = float(timestamp_header)
    except ValueError:
        raise WebhookAuthError("bad_timestamp") from None

    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        raise WebhookAuthError("stale_timestamp")

    provided = signature_header.strip().lower()
    for secret in secrets:
        expected = sign(secret, timestamp_header, body)
        if hmac.compare_digest(expected, provided):
            return
    raise WebhookAuthError("bad_signature")
