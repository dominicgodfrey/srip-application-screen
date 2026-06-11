"""Tests for the resume download layer (Phase 12.2). httpx.MockTransport — zero real network."""

from __future__ import annotations

import httpx
import pytest

from srip_filter.config import AppConfig
from srip_filter.resume_fetch import (
    FAIL_HOST_NOT_ALLOWED,
    FAIL_INVALID_URL,
    FAIL_NETWORK,
    FAIL_NOT_HTTPS,
    FAIL_REDIRECT,
    FAIL_TIMEOUT,
    FAIL_TOO_LARGE,
    ResumeFetcher,
    validate_resume_url,
)

ALLOWED_HOST = "files.example-bucket.test"
GOOD_URL = f"https://{ALLOWED_HOST}/applicant/resume.pdf"
PDF_BYTES = b"%PDF-1.7 tiny but plausible body"


def make_config(**resume_overrides: object) -> AppConfig:
    base: dict[str, object] = {"allowed_url_hosts": [ALLOWED_HOST]}
    base.update(resume_overrides)
    return AppConfig.model_validate({"resume": base})


class CountingHandler:
    """MockTransport handler that counts calls and delegates to a per-call responder."""

    def __init__(self, responder) -> None:
        self.calls = 0
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._responder(request, self.calls)


def make_fetcher(cfg: AppConfig, responder) -> tuple[ResumeFetcher, CountingHandler]:
    handler = CountingHandler(responder)
    return ResumeFetcher(cfg, transport=httpx.MockTransport(handler)), handler


# ------------------------------------------------------------------------------------------
# URL validation (the SSRF policy) — pure, no transport involved
# ------------------------------------------------------------------------------------------


def test_validate_accepts_allowed_https_url() -> None:
    assert validate_resume_url(GOOD_URL, make_config()) == ""


def test_validate_accepts_explicit_default_port_and_mixed_case_host() -> None:
    cfg = make_config()
    assert validate_resume_url(f"https://{ALLOWED_HOST.upper()}:443/r.pdf", cfg) == ""


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"http://{ALLOWED_HOST}/resume.pdf", FAIL_NOT_HTTPS),
        ("ftp://files.example-bucket.test/r.pdf", FAIL_NOT_HTTPS),
        ("https://evil.test/resume.pdf", FAIL_HOST_NOT_ALLOWED),
        # Suffix/prefix look-alikes must not match the exact-host allowlist.
        (f"https://evil-{ALLOWED_HOST}/r.pdf", FAIL_HOST_NOT_ALLOWED),
        (f"https://{ALLOWED_HOST}.evil.test/r.pdf", FAIL_HOST_NOT_ALLOWED),
        # Userinfo trick: the real host is evil.test.
        (f"https://{ALLOWED_HOST}@evil.test/r.pdf", FAIL_HOST_NOT_ALLOWED),
        # Non-default port is refused even on the allowed host.
        (f"https://{ALLOWED_HOST}:8443/r.pdf", FAIL_HOST_NOT_ALLOWED),
        ("https:///no-host.pdf", FAIL_INVALID_URL),
        (f"https://{ALLOWED_HOST}:notaport/r.pdf", FAIL_INVALID_URL),
    ],
)
def test_validate_rejects_disallowed_urls(url: str, reason: str) -> None:
    assert validate_resume_url(url, make_config()) == reason


def test_empty_allowlist_means_nothing_fetchable() -> None:
    cfg = make_config(allowed_url_hosts=[])
    assert validate_resume_url(GOOD_URL, cfg) == FAIL_HOST_NOT_ALLOWED


# ------------------------------------------------------------------------------------------
# Fetching — MockTransport
# ------------------------------------------------------------------------------------------


async def test_fetch_happy_path_returns_bytes() -> None:
    fetcher, handler = make_fetcher(
        make_config(), lambda request, call: httpx.Response(200, content=PDF_BYTES)
    )
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert result.ok and result.content == PDF_BYTES and result.failure == ""
    assert handler.calls == 1


async def test_fetch_disallowed_host_makes_no_network_call() -> None:
    fetcher, handler = make_fetcher(
        make_config(), lambda request, call: httpx.Response(200, content=PDF_BYTES)
    )
    async with fetcher:
        result = await fetcher.fetch("https://evil.test/resume.pdf")
    assert not result.ok and result.failure == FAIL_HOST_NOT_ALLOWED
    assert handler.calls == 0  # validation fails before any request is built


async def test_fetch_404_fails_without_retry() -> None:
    fetcher, handler = make_fetcher(make_config(), lambda request, call: httpx.Response(404))
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == "http_status_404"
    assert handler.calls == 1  # 4xx is not transient


async def test_fetch_5xx_retries_once_then_fails() -> None:
    fetcher, handler = make_fetcher(make_config(), lambda request, call: httpx.Response(503))
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == "http_status_503"
    assert handler.calls == 2


async def test_fetch_5xx_then_success_recovers_on_retry() -> None:
    def flaky(request: httpx.Request, call: int) -> httpx.Response:
        return httpx.Response(500) if call == 1 else httpx.Response(200, content=PDF_BYTES)

    fetcher, handler = make_fetcher(make_config(), flaky)
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert result.ok and result.content == PDF_BYTES
    assert handler.calls == 2


async def test_fetch_timeout_is_typed_and_retried() -> None:
    def time_out(request: httpx.Request, call: int) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    fetcher, handler = make_fetcher(make_config(), time_out)
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == FAIL_TIMEOUT
    assert handler.calls == 2


async def test_fetch_network_error_never_raises() -> None:
    def explode(request: httpx.Request, call: int) -> httpx.Response:
        raise httpx.ConnectError("simulated connection refused")

    fetcher, _ = make_fetcher(make_config(), explode)
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == FAIL_NETWORK


async def test_fetch_redirect_is_not_followed() -> None:
    fetcher, handler = make_fetcher(
        make_config(),
        lambda request, call: httpx.Response(302, headers={"location": "https://evil.test/x"}),
    )
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == FAIL_REDIRECT
    assert handler.calls == 1  # the redirect target is never requested


async def test_fetch_oversize_content_length_aborts_before_body() -> None:
    cfg = make_config(max_download_bytes=10)
    fetcher, _ = make_fetcher(
        cfg,
        lambda request, call: httpx.Response(
            200, headers={"content-length": "11"}, content=b"x" * 11
        ),
    )
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == FAIL_TOO_LARGE


async def test_fetch_oversize_streamed_body_aborts_mid_stream() -> None:
    cfg = make_config(max_download_bytes=10)

    def chunky(request: httpx.Request, call: int) -> httpx.Response:
        # No content-length header — the cap must trip while streaming.
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 64))

    fetcher, _ = make_fetcher(cfg, chunky)
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert not result.ok and result.failure == FAIL_TOO_LARGE


async def test_fetch_at_exact_cap_succeeds() -> None:
    cfg = make_config(max_download_bytes=10)
    fetcher, _ = make_fetcher(cfg, lambda request, call: httpx.Response(200, content=b"x" * 10))
    async with fetcher:
        result = await fetcher.fetch(GOOD_URL)
    assert result.ok and len(result.content) == 10


def test_fetcher_semaphore_sized_from_config() -> None:
    cfg = make_config(download_concurrency=2)
    fetcher = ResumeFetcher(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert fetcher._semaphore._value == 2  # noqa: SLF001 — sizing is the contract under test
