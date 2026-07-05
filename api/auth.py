"""Admin authentication (P5, PRD v3 §6) — shared password → server-side session.

Owner decision (2026-07-04): a single strong shared admin password is the *permanent*
solution; the seam for any future SSO is exactly one function (:func:`is_authenticated`)
plus the middleware below. Mechanics:

* **Password storage:** only a PBKDF2-SHA256 hash lives in the environment
  (``ADMIN_PASSWORD_HASH``, format ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``).
  Generate one with ``python -m api.auth 'the-password'``. Never the plaintext.
* **Sessions:** opaque random tokens in a server-side in-memory store with a TTL —
  nothing user-derived in the cookie, nothing to forge; a restart simply logs everyone
  out (fine for a small staff tool). Cookie: HttpOnly, SameSite=Lax, Secure by config.
* **Throttling:** a global sliding lockout — after ``max_attempts`` failed logins within
  the window, ALL logins are refused for ``lockout_seconds`` (single shared credential,
  so per-IP granularity buys nothing against a distributed guesser and complicates ops).
* **Default-deny middleware:** every route requires a session except the explicit
  ``OPEN_PREFIXES`` allowlist (health, HMAC-verified webhook, the login page itself,
  static assets). Browsers get a redirect to ``/login``; API calls get a plain 401.

No PII, no secrets, and no session tokens are ever logged.
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
    "/webhooks/",  # HMAC-verified separately (PRD v3 §2.1)
    "/login",
    "/logout",
    "/static/",
    "/favicon.ico",
)


# ================================================================================================
# Password hashing (stdlib only — no new dependency for one credential)
# ================================================================================================


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


# ================================================================================================
# Session store + login throttle (in-memory; single instance by design)
# ================================================================================================


@dataclass
class SessionStore:
    """Opaque-token sessions with TTL. All methods take ``now`` for clock-free tests."""

    ttl_seconds: float
    _sessions: dict[str, float] = field(default_factory=dict)

    def create(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        self._sessions[token] = now + self.ttl_seconds
        return token

    def is_valid(self, token: str | None, now: float | None = None) -> bool:
        if not token:
            return False
        now = time.time() if now is None else now
        expires = self._sessions.get(token)
        if expires is None:
            return False
        if now >= expires:
            del self._sessions[token]
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def sweep(self, now: float | None = None) -> int:
        """Drop expired sessions; returns how many were dropped."""
        now = time.time() if now is None else now
        dead = [t for t, exp in self._sessions.items() if now >= exp]
        for token in dead:
            del self._sessions[token]
        return len(dead)


@dataclass
class LoginThrottle:
    """Global sliding-window lockout for the single shared credential."""

    max_attempts: int
    lockout_seconds: float
    _failures: list[float] = field(default_factory=list)

    def locked_out(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        cutoff = now - self.lockout_seconds
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= self.max_attempts

    def record_failure(self, now: float | None = None) -> None:
        self._failures.append(time.time() if now is None else now)

    def reset(self) -> None:
        self._failures.clear()


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
