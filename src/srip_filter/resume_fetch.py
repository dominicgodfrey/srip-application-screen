"""Stage 6 resume download layer (PRD §7.2) — the SSRF guard, kept separate from the LLM client
and the scoring math.

Only https URLs whose hostname is pinned in ``resume.allowed_url_hosts`` are fetched, redirects
are never followed (one could escape the allowlist), and the body streams against
``max_download_bytes`` so an oversized file aborts early. The fetcher holds its own semaphore,
which bounds peak transient memory at ``download_concurrency × max_download_bytes``.

:meth:`ResumeFetcher.fetch` **never raises**: every failure is a typed reason the aggregator
turns into a 0 bonus plus an audit note. Nothing here logs a URL — they embed applicant names.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import AppConfig

logger = logging.getLogger(__name__)

# Typed failure reasons (audit-facing). HTTP failures use the dynamic "http_status_<code>".
FAIL_INVALID_URL = "invalid_url"
FAIL_NOT_HTTPS = "url_not_https"
FAIL_HOST_NOT_ALLOWED = "host_not_allowed"
FAIL_REDIRECT = "redirect_not_followed"
FAIL_TOO_LARGE = "download_too_large"
FAIL_TIMEOUT = "download_timeout"
FAIL_NETWORK = "network_error"

_HTTP_STATUS_PREFIX = "http_status_"
_RETRYABLE_5XX_PREFIX = f"{_HTTP_STATUS_PREFIX}5"


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one resume download. ``content`` is the PDF bytes, which the caller extracts
    from and **discards immediately**; ``failure`` is a typed reason for the audit record."""

    ok: bool
    content: bytes
    failure: str


def _ok(content: bytes) -> FetchResult:
    return FetchResult(ok=True, content=content, failure="")


def _fail(reason: str) -> FetchResult:
    return FetchResult(ok=False, content=b"", failure=reason)


def validate_resume_url(url: str, cfg: AppConfig) -> str:
    """Apply the SSRF policy to a URL: ``""`` if fetchable, else the typed reason.

    https only, default port only, and the hostname must match the allowlist *exactly* — no
    suffix matching, so ``evil-prod-fillout...com`` cannot sneak by. ``urlsplit().hostname``
    strips userinfo, so ``https://allowed-host@evil.com/`` resolves to ``evil.com`` and fails.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # can raise ValueError on a malformed port
    except ValueError:
        return FAIL_INVALID_URL
    if parts.scheme.lower() != "https":
        return FAIL_NOT_HTTPS
    host = (parts.hostname or "").lower()
    if not host:
        return FAIL_INVALID_URL
    if port not in (None, 443):
        return FAIL_HOST_NOT_ALLOWED
    if host not in {h.lower() for h in cfg.resume.allowed_url_hosts}:
        return FAIL_HOST_NOT_ALLOWED
    return ""


class ResumeFetcher:
    """Run-scoped resume downloader owning the semaphore and one redirect-disabled
    ``httpx.AsyncClient``. Use as an async context manager so the client closes with the run.

    ``transport`` is a test seam — no real network in unit tests.
    """

    def __init__(
        self, cfg: AppConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._cfg = cfg
        self._semaphore = asyncio.Semaphore(cfg.resume.download_concurrency)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.resume.download_timeout_s),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ResumeFetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def fetch(self, url: str) -> FetchResult:
        """Download one resume under the SSRF policy; never raises. The URL is validated before
        any network call, and transient failures are retried once — everything else fails fast."""
        reason = validate_resume_url(url, self._cfg)
        if reason:
            return _fail(reason)
        async with self._semaphore:
            result = await self._fetch_once(url)
            if self._is_transient(result.failure):
                result = await self._fetch_once(url)
            return result

    @staticmethod
    def _is_transient(failure: str) -> bool:
        return failure in (FAIL_TIMEOUT, FAIL_NETWORK) or failure.startswith(
            _RETRYABLE_5XX_PREFIX
        )

    async def _fetch_once(self, url: str) -> FetchResult:
        max_bytes = self._cfg.resume.max_download_bytes
        try:
            async with self._client.stream("GET", url) as response:
                if response.is_redirect:
                    return _fail(FAIL_REDIRECT)
                if response.status_code != 200:
                    return _fail(f"{_HTTP_STATUS_PREFIX}{response.status_code}")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    return _fail(FAIL_TOO_LARGE)  # abort before reading the body
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        return _fail(FAIL_TOO_LARGE)
                return _ok(bytes(buffer))
        except httpx.TimeoutException:
            return _fail(FAIL_TIMEOUT)
        except Exception as error:  # boundary: any failure degrades to a typed reason
            logger.warning("resume fetch failed: %s", type(error).__name__)  # never the URL
            return _fail(FAIL_NETWORK)
