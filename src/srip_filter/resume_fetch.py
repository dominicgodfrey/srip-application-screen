"""Stage 6 resume download layer (Phase 12.2, PRD §7.2).

A network I/O boundary, deliberately separate from both the LLM client and the scoring math.
Resume URLs arrive inside an **uploaded CSV**, so this module is the SSRF guard: only https
URLs whose hostname is pinned in ``resume.allowed_url_hosts`` are ever fetched, redirects are
not followed (a redirect could escape the allowlist), and the body is streamed against
``max_download_bytes`` so an oversized file aborts early.

Bonus-only discipline (PRD §0.3): :meth:`ResumeFetcher.fetch` **never raises** — every failure
becomes a typed reason in :class:`FetchResult` that the Stage 6 aggregator turns into a 0 bonus
plus an audit note, never a block. Transient failures (timeout / network / 5xx) are retried
once; 4xx are not (not transient).

Memory rule (PLAN.md Phase 12 hosting analysis): the fetcher holds its **own semaphore**
(``download_concurrency``, separate from the LLM one), so peak transient memory is
``download_concurrency × max_download_bytes`` regardless of batch size. The caller must
fetch → extract → discard per applicant; resume bytes never land on an audit record.

Privacy: resume URLs embed applicant names, so nothing here logs a URL — only failure types.
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
    """Outcome of one resume download.

    ``content`` is the PDF bytes on success and ``b""`` on failure; the caller extracts text
    and **discards it immediately** (the per-applicant memory rule). ``failure`` is ``""`` on
    success, else a typed reason for ``AuditRecord.resume.failure``.
    """

    ok: bool
    content: bytes
    failure: str


def _ok(content: bytes) -> FetchResult:
    return FetchResult(ok=True, content=content, failure="")


def _fail(reason: str) -> FetchResult:
    return FetchResult(ok=False, content=b"", failure=reason)


def validate_resume_url(url: str, cfg: AppConfig) -> str:
    """Apply the SSRF policy to a URL: return ``""`` if fetchable, else the typed reason.

    Policy: https only; hostname must match ``resume.allowed_url_hosts`` exactly
    (case-insensitive — no wildcard/suffix matching, so ``evil-prod-fillout...com`` can't
    sneak by); only the default port (an explicit ``:443`` is fine). ``urlsplit().hostname``
    strips any userinfo, so ``https://allowed-host@evil.com/`` resolves to ``evil.com`` and
    fails the allowlist.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # property access can raise ValueError on a malformed port
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
    """Batch-scoped resume downloader: one per ``grade_batch`` run.

    Owns the download semaphore and one ``httpx.AsyncClient`` (redirects disabled, timeout
    from config). Use as an async context manager so the client is closed with the run::

        async with ResumeFetcher(cfg) as fetcher:
            result = await fetcher.fetch(url)

    ``transport`` is a test seam (``httpx.MockTransport``) — no real network in unit tests.
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
        """Download one resume under the SSRF policy. Never raises.

        Validates the URL first (no network for a disallowed host), then streams the body
        under the size cap, holding the download semaphore. Transient failures (timeout,
        network error, 5xx) are retried once; everything else fails fast.
        """
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
