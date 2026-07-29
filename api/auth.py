"""Admin authentication (P5, PRD v3 §6) — shared password → server-side session.

Owner decision (2026-07-04): a single strong shared admin password is the *permanent*
solution; the seam for any future SSO is exactly one function (:func:`is_authenticated`)
plus the middleware below. Mechanics:

* **Password storage:** only a PBKDF2-SHA256 hash lives in the environment
  (``ADMIN_PASSWORD_HASH``, format ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``).
  Generate one with ``python -m api.auth 'the-password'``. Never the plaintext.
* **Sessions (P12, decision D2):** a stateless HMAC-signed cookie — ``<expiry>.<mac>``,
  signed with the admin password hash as the key. No server-side store, because there is
  no single server any more: on Vercel every request may hit a different instance, and an
  in-memory token store would log staff out at random. Cookie: HttpOnly, SameSite=Lax,
  Secure by config.
  **Honest tradeoff: no server-side revocation.** Logout clears the cookie, but a stolen
  cookie stays valid until it expires — hence the short TTL. Changing the admin password
  changes the signing key, which invalidates every outstanding session at once; that is
  the revocation lever, and it is why the key is derived rather than a separate secret.
* **Throttling:** a global sliding lockout — after ``max_attempts`` failed logins within
  the window, ALL logins are refused for ``lockout_seconds`` (single shared credential,
  so per-IP granularity buys nothing against a distributed guesser and complicates ops).
  Counted from the ``events`` ledger when a database is configured (shared across
  instances); the in-memory :class:`LoginThrottle` is the local-dev fallback.
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
    "/webhooks/",  # static-secret verified separately (P10)
    "/api/cron/",  # bearer-token verified separately (P11.1) — cron has no cookie
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
# Sessions — stateless signed cookies (P12.1; no shared store, no per-request DB read)
# ================================================================================================


def _mac(secret: str, expires_at: str) -> str:
    return hmac.new(secret.encode("utf-8"), expires_at.encode("utf-8"), "sha256").hexdigest()


def sign_session(secret: str, ttl_seconds: float, *, now: float | None = None) -> str:
    """Mint a session cookie value: the expiry, and an HMAC over it."""
    expires_at = str(int((time.time() if now is None else now) + ttl_seconds))
    return f"{expires_at}.{_mac(secret, expires_at)}"


def valid_session(cookie: str | None, secret: str, *, now: float | None = None) -> bool:
    """True when ``cookie`` carries our signature and has not expired.

    Fails closed on an empty secret (an unconfigured server grants nothing) and compares
    the MAC in constant time before trusting any part of the value.
    """
    if not cookie or not secret:
        return False
    expires_at, _, mac = cookie.partition(".")
    if not mac or not hmac.compare_digest(mac, _mac(secret, expires_at)):
        return False
    return (time.time() if now is None else now) < int(expires_at)


@dataclass
class LoginThrottle:
    """Global sliding-window lockout for the single shared credential.

    In-memory, therefore per-instance: the local-dev fallback for when no database is
    configured. With a pool the same window is counted over ``events`` (P12.2).
    """

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
