"""Admin authentication (PRD v3 §6) — a shared password and a signed session cookie.

A single strong shared password is the *permanent* solution (owner, 2026-07-04). Only its
PBKDF2-SHA256 hash lives in the environment; generate one with ``python -m api.auth '<pw>'``.

**Sessions are stateless** — ``<expiry>.<mac>``, signed with the password hash — because there
is no single server any more: on Vercel every request may hit a different instance, and an
in-memory store would log staff out at random. The honest tradeoff is no server-side
revocation: a stolen cookie stays valid until it expires, hence the short TTL. Changing the
password changes the signing key and kills every session at once, which is why the key is
derived rather than a separate secret.

**Throttling has two tiers**, counted from the ``events`` ledger so they hold across instances.
The per-client tier at ``max_attempts`` is the one that matters for availability: with only a
global counter, any anonymous caller could send five wrong passwords per window and hold staff
out forever, for free. Per-client, an attacker locks out *themselves*. The global tier is the
distributed-guesser backstop, priced so ordinary abuse cannot reach it — someone rotating
client keys still can, but that is a sustained, ledger-visible attack rather than five idle
requests. Online guessing was never the live risk (600k-iteration PBKDF2); availability was.

The client key is a **salted hash, never an address**: ``events`` is non-PII and an IP is
personal data. The salt is the password hash, so a rotation re-keys the throttle too, and a
spoofed forwarding header only changes which bucket the attacker fills, never the operator's.

No PII, secrets, or session tokens are ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
import time
from dataclasses import dataclass, field

_PBKDF2_ITERATIONS = 600_000
_SCHEME = "pbkdf2_sha256"

SESSION_COOKIE = "srip_session"

# Default-deny allowlist: exact paths or prefixes (trailing "/") open without a session.
OPEN_PREFIXES: tuple[str, ...] = (
    "/health",
    "/webhooks/",  # static-secret verified separately
    "/api/cron/",  # bearer-token verified separately — cron carries no cookie
    "/login",
    "/logout",
    "/static/",
    "/favicon.ico",
)


# --- Password hashing (stdlib only — no dependency for one credential) ---


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Produce the env-storable hash string for a password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification against the stored hash; malformed hash ⇒ False."""
    try:
        scheme, iter_s, salt_hex, hash_hex = stored.split("$")
        if scheme != _SCHEME:
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# --- Sessions: stateless signed cookies, no shared store, no per-request DB read ---


def _mac(secret: str, expires_at: str) -> str:
    return hmac.new(secret.encode("utf-8"), expires_at.encode("utf-8"), "sha256").hexdigest()


def sign_session(secret: str, ttl_seconds: float, *, now: float | None = None) -> str:
    """Mint a session cookie value: the expiry, and an HMAC over it."""
    expires_at = str(int((time.time() if now is None else now) + ttl_seconds))
    return f"{expires_at}.{_mac(secret, expires_at)}"


def valid_session(cookie: str | None, secret: str, *, now: float | None = None) -> bool:
    """True when ``cookie`` carries our signature and has not expired. Fails closed on an empty
    secret, and compares the MAC in constant time before trusting any part of the value."""
    if not cookie or not secret:
        return False
    expires_at, _, mac = cookie.partition(".")
    if not mac or not hmac.compare_digest(mac, _mac(secret, expires_at)):
        return False
    return (time.time() if now is None else now) < int(expires_at)


ANY_CLIENT = "*"  # bucket used when no client can be identified at all


def client_key(forwarded_for: str | None, peer: str | None, salt: str) -> str:
    """Opaque, stable per-client bucket id for the throttle — a truncated HMAC of the address,
    never the address itself, since ``events`` is a non-PII ledger and an IP is personal data.

    The leftmost ``X-Forwarded-For`` entry wins (behind an edge, ``peer`` is the proxy) and is
    spoofable by design: spoofing only scatters an attacker's own failures across buckets, and
    can never move them *into* an operator's — the one thing this tier must guarantee.
    """
    address = (forwarded_for or "").split(",")[0].strip() or (peer or "")
    if not address:
        return ANY_CLIENT
    return hmac.new(salt.encode("utf-8"), address.encode("utf-8"), "sha256").hexdigest()[:16]


@dataclass
class LoginThrottle:
    """Sliding-window lockout, per client and overall. In-memory and therefore per-instance —
    the local-dev fallback; with a pool the same windows are counted over ``events``."""

    max_attempts: int
    lockout_seconds: float
    max_attempts_global: int = 0  # 0 ⇒ no global tier
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _live(self, now: float) -> dict[str, list[float]]:
        """Drop everything outside the window, then return what is left per bucket."""
        cutoff = now - self.lockout_seconds
        self._failures = {
            actor: fresh
            for actor, times in self._failures.items()
            if (fresh := [t for t in times if t > cutoff])
        }
        return self._failures

    def locked_out(self, actor: str = ANY_CLIENT, now: float | None = None) -> bool:
        live = self._live(time.time() if now is None else now)
        if len(live.get(actor, ())) >= self.max_attempts:
            return True
        return bool(self.max_attempts_global) and (
            sum(len(times) for times in live.values()) >= self.max_attempts_global
        )

    def record_failure(self, actor: str = ANY_CLIENT, now: float | None = None) -> None:
        self._failures.setdefault(actor, []).append(time.time() if now is None else now)

    def reset(self) -> None:
        self._failures.clear()


# Response headers stamped on every response (see :func:`security_headers`).
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    # The UI is same-origin fetch with no inline scripts, so 'self' costs nothing and is the
    # backstop for the innerHTML-heavy audit browser. The font hosts are the one external
    # dependency; 'unsafe-inline' is for Google's stylesheet, not any inline <style> of ours.
    (
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "object-src 'none'",
    ),
    # The belt for browsers older than frame-ancestors. The bulk-purge control is one click
    # behind a dialog, so framing is not theoretical.
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    # Applicant ids ride in admin URLs; no referrer leaves with them, font CDN included.
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)

# Separate because it must never go over the plaintext http local development speaks; gated
# on the same flag as the Secure cookie.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def security_headers(*, https_only: bool) -> dict[str, str]:
    """The header set stamped on every response, HSTS included only under ``https_only``.

    Defence in depth around ``SameSite=Lax`` for a service that renders minors' PII and hosts
    an irreversible purge control; CSP backstops a missed escape in the audit browser.
    """
    headers = dict(_SECURITY_HEADERS)
    if https_only:
        headers.update((HSTS_HEADER,))
    return headers


def safe_next_path(candidate: str) -> str:
    """Reduce a post-login ``next`` to a same-origin path, or ``/``.

    Rejecting only ``//`` is not enough: the WHATWG parser treats a backslash as a slash for
    special schemes, so ``/\\evil.example`` resolves off-origin. Anything but a single leading
    slash followed by a non-separator is refused.
    """
    if not candidate.startswith("/"):
        return "/"
    if candidate[1:2] in ("/", "\\"):
        return "/"
    if "\\" in candidate:
        return "/"
    return candidate


def is_open_path(path: str) -> bool:
    """True when ``path`` is on the no-session allowlist (exact or prefix match)."""
    for entry in OPEN_PREFIXES:
        if entry.endswith("/"):
            if path.startswith(entry) or path == entry.rstrip("/"):
                return True
        elif path == entry:
            return True
    return False


def wants_html(accept_header: str | None) -> bool:
    """Crude but sufficient: browsers send Accept: text/html; fetch/API callers don't."""
    return bool(accept_header) and "text/html" in accept_header


if __name__ == "__main__":  # pragma: no cover - operator utility
    # Usage: uv run python -m api.auth 'the-strong-password'  → prints the env value.
    if len(sys.argv) != 2:
        print("usage: python -m api.auth '<password>'", file=sys.stderr)
        raise SystemExit(2)
    print(hash_password(sys.argv[1]))
