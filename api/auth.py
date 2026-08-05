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
* **Throttling (two tiers):** a sliding lockout counted from the ``events`` ledger when a
  database is configured (so it holds across serverless instances); the in-memory
  :class:`LoginThrottle` is the local-dev fallback.

  - **Per-client**, at ``max_attempts``: the tier that actually runs. It was originally
    left out on the reasoning that per-IP granularity "buys nothing against a distributed
    guesser" — true of *guessing*, but it misses the availability side. With only a global
    counter, any anonymous caller could send five wrong passwords every five minutes and
    keep every staff member locked out indefinitely, forever, for free. A per-client tier
    means an attacker locks out **themselves**.
  - **Global**, at the much higher ``max_attempts_global``: the distributed-guesser
    backstop the original reasoning was about, kept but priced so ordinary abuse cannot
    reach it. **Honest limit:** a determined attacker who rotates client keys can still
    reach it and force a global lockout — that is now a sustained, ledger-visible attack
    rather than five idle requests. Online guessing was never the live risk anyway
    (600k-iteration PBKDF2 over a strong shared password); availability was.

  The client key is a **salted hash**, never an address: ``events`` is non-PII by law and
  an IP is personal data. The salt is the admin password hash, so there is no new secret
  to deploy and a password rotation re-keys the throttle too. A spoofed forwarding header
  only changes which bucket the attacker fills, never the operator's own — which is the
  property that matters here.
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


ANY_CLIENT = "*"  # bucket used when no client can be identified at all


def client_key(forwarded_for: str | None, peer: str | None, salt: str) -> str:
    """Opaque, stable per-client bucket id for the throttle — never an address.

    An IP is personal data and ``events`` is a non-PII ledger, so what is stored is a
    truncated HMAC of the address under the admin password hash. That is enough to count
    attempts per client and useless for identifying one; rotating the password rotates the
    salt along with every live session.

    The leftmost ``X-Forwarded-For`` entry is preferred (behind Vercel's edge, ``peer`` is
    the proxy) and is client-spoofable — which is acceptable here by design. Spoofing
    scatters an attacker's failures across buckets, so they never trip the per-client tier;
    it can never move failures *into* a legitimate operator's bucket, which is the only
    thing the tier has to guarantee. The global tier remains the backstop for the scattered
    case.
    """
    address = (forwarded_for or "").split(",")[0].strip() or (peer or "")
    if not address:
        return ANY_CLIENT
    return hmac.new(salt.encode("utf-8"), address.encode("utf-8"), "sha256").hexdigest()[:16]


@dataclass
class LoginThrottle:
    """Sliding-window lockout for the single shared credential, per client and overall.

    In-memory, therefore per-instance: the local-dev fallback for when no database is
    configured. With a pool the same windows are counted over ``events`` (P12.2).
    """

    max_attempts: int
    lockout_seconds: float
    max_attempts_global: int = 0  # 0 ⇒ no global tier (the in-memory fallback's default)
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _live(self, now: float) -> dict[str, list[float]]:
        """Drop everything outside the window, then return what is left (per bucket)."""
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
    # The UI is same-origin fetch + inline-free scripts, so 'self' costs nothing and is the
    # backstop for the innerHTML-heavy audit browser. The two font hosts are the one
    # external dependency (base.html); style-src needs 'unsafe-inline' only because
    # Google's stylesheet is fetched, not for any inline <style> of ours.
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
    # frame-ancestors covers modern browsers; this is the belt for anything older. The
    # bulk-purge control is one click behind a dialog, so framing is not theoretical.
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    # Applicant ids ride in admin URLs; no referrer should ever leave with them — including
    # to the font CDN.
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)

# HSTS is separate: it must never be sent over plaintext http, which is what local
# development speaks. Gated on the same config flag as the Secure cookie.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def security_headers(*, https_only: bool) -> dict[str, str]:
    """The header set stamped on every response, HSTS included only under ``https_only``.

    This service renders minors' PII and hosts an irreversible purge control, and it
    previously sent no security headers at all. ``SameSite=Lax`` already covered most CSRF
    and framing; these are the defence in depth around it, and CSP in particular is the
    backstop for a missed escape in the audit browser's HTML assembly.
    """
    headers = dict(_SECURITY_HEADERS)
    if https_only:
        headers.update((HSTS_HEADER,))
    return headers


def safe_next_path(candidate: str) -> str:
    """Reduce a post-login ``next`` to a same-origin path, or ``/``.

    Rejecting only ``//`` is not enough: browsers follow the WHATWG URL parser, which
    treats a backslash as a slash for special schemes, so ``Location: /\\evil.example``
    resolves to ``https://evil.example``. Anything that is not a single leading slash
    followed by a non-separator is refused outright.
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
