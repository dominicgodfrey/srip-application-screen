"""Tests for the Stage 6 resume bonus (Phase 12). Zero spend, zero real network.

The aggregator tests drive ``score_resume`` with a MockTransport :class:`ResumeFetcher` and a
scripted :class:`FakeLLMClient`. Hard line throughout (PRD §0.3): every failure path yields a
0 bonus + an audit note — never ``NEEDS_REVIEW``/``REJECTED`` — and absence is neutral.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from srip_filter.config import AppConfig, ResumeConfig
from srip_filter.ingest import ApplicantRow
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.llm.prompts import task_e as task_e_prompt
from srip_filter.models import TaskEOutput
from srip_filter.resume_fetch import ResumeFetcher
from srip_filter.scoring.resume import resume_signal_bonus, score_resume

ALLOWED_HOST = "files.example-bucket.test"
GOOD_URL = f"https://{ALLOWED_HOST}/applicant/resume.pdf"


def make_config(**resume_overrides: object) -> AppConfig:
    base: dict[str, object] = {"bonus_max": 10.0, "allowed_url_hosts": [ALLOWED_HOST]}
    base.update(resume_overrides)
    return AppConfig.model_validate({"resume": base})


def _row(resume_url: str = "") -> ApplicantRow:
    return ApplicantRow(submission_id="s1", resume_url=resume_url)


def _signals(**overrides: object) -> TaskEOutput:
    base: dict[str, object] = {
        "is_resume": True,
        "relevant_projects": 0,
        "relevant_experience": 0,
        "relevant_awards": 0,
        "skills_relevance": 0.0,
        "highlights": "synthetic",
        "rationale": "synthetic",
    }
    base.update(overrides)
    return TaskEOutput.model_validate(base)


def _resume_cfg(**overrides: object) -> ResumeConfig:
    base: dict[str, object] = {"bonus_max": 10.0}
    base.update(overrides)
    return ResumeConfig.model_validate(base)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_text_pdf(text: str) -> bytes:
    """Minimal one-page PDF whose content stream draws ``text`` (correct xref offsets)."""
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = f"BT /F1 12 Tf 72 712 Td ({_escape(text)}) Tj ET".encode("latin-1")
    objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


PDF_BYTES = build_text_pdf("Built two web apps in Python; USACO silver; intern at a startup")


class CountingHandler:
    def __init__(self, responder) -> None:
        self.calls = 0
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._responder(request)


def make_fetcher(cfg: AppConfig, responder=None) -> tuple[ResumeFetcher, CountingHandler]:
    handler = CountingHandler(responder or (lambda r: httpx.Response(200, content=PDF_BYTES)))
    return ResumeFetcher(cfg, transport=httpx.MockTransport(handler)), handler


def task_e_client(cfg: AppConfig, signals: TaskEOutput | None = None) -> FakeLLMClient:
    def handler(task: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "task_e"
        if signals is None:
            raise LLMParseFailure(task, "scripted failure")
        return signals

    return FakeLLMClient(cfg, handler)


# ================================================================================================
# 12.4 — Task E prompt shape + pure signal-pricing math (zero spend)
# ================================================================================================


def test_task_e_prompt_shape() -> None:
    assert "COUNT" in task_e_prompt.SYSTEM
    assert "ONLY JSON" in task_e_prompt.SYSTEM
    rendered = task_e_prompt.user_prompt("Education: Example High School")
    assert rendered.startswith('RESUME_TEXT: """')
    assert "Example High School" in rendered and rendered.endswith('"""')


def test_signal_bonus_composition_uses_config_weights() -> None:
    out = _signals(
        relevant_projects=2, relevant_experience=1, relevant_awards=1, skills_relevance=0.5
    )
    # 2*1.5 + 1*2.0 + 1*1.0 + 0.5*2.0 = 7.0
    assert resume_signal_bonus(out, _resume_cfg()) == 7.0


def test_signal_bonus_capped_at_bonus_max() -> None:
    out = _signals(
        relevant_projects=10, relevant_experience=10, relevant_awards=10, skills_relevance=1.0
    )
    assert resume_signal_bonus(out, _resume_cfg()) == 10.0


def test_signal_bonus_never_negative_and_zero_signals_zero() -> None:
    assert resume_signal_bonus(_signals(), _resume_cfg()) == 0.0
    # Even with (hypothetical) negative weights config, the floor holds.
    cfg = _resume_cfg(weight_skills=-5.0)
    assert resume_signal_bonus(_signals(skills_relevance=1.0), cfg) == 0.0


def test_signal_bonus_not_a_resume_prices_to_zero() -> None:
    out = _signals(
        is_resume=False,
        relevant_projects=5,
        relevant_experience=5,
        relevant_awards=5,
        skills_relevance=1.0,
    )
    assert resume_signal_bonus(out, _resume_cfg()) == 0.0


def test_signal_bonus_kill_switch_prices_everything_to_zero() -> None:
    out = _signals(relevant_projects=4, skills_relevance=1.0)
    assert resume_signal_bonus(out, _resume_cfg(bonus_max=0.0)) == 0.0


# ================================================================================================
# 12.5 — Stage 6 aggregator (MockTransport fetcher + FakeLLMClient; no spend, no network)
# ================================================================================================


async def test_score_resume_happy_path() -> None:
    cfg = make_config()
    fetcher, handler = make_fetcher(cfg)
    signals = _signals(relevant_projects=2, relevant_experience=1, skills_relevance=0.5)
    async with fetcher:
        result = await score_resume(_row(GOOD_URL), fetcher, task_e_client(cfg, signals), cfg)
    assert result.bonus == 6.0  # 2*1.5 + 1*2.0 + 0.5*2.0
    assert result.error == "" and result.task_e_called
    a = result.assessment
    assert a.url_present and a.attempted and a.fetched
    assert a.extracted_chars > 0 and a.failure == ""
    assert a.signals == signals
    assert handler.calls == 1


async def test_blank_url_skips_with_no_fetch_and_no_token() -> None:
    cfg = make_config()
    fetcher, handler = make_fetcher(cfg)
    client = task_e_client(cfg, _signals())
    async with fetcher:
        result = await score_resume(_row(""), fetcher, client, cfg)
    assert result.bonus == 0.0 and result.error == "" and not result.task_e_called
    assert not result.assessment.url_present and not result.assessment.attempted
    assert handler.calls == 0 and client.calls == []


async def test_kill_switch_skips_with_no_fetch_and_no_token() -> None:
    cfg = make_config(bonus_max=0.0)
    fetcher, handler = make_fetcher(cfg)
    client = task_e_client(cfg, _signals())
    async with fetcher:
        result = await score_resume(_row(GOOD_URL), fetcher, client, cfg)
    assert result.bonus == 0.0 and result.error == ""
    assert result.assessment.url_present and not result.assessment.attempted
    assert handler.calls == 0 and client.calls == []


async def test_no_fetcher_skips_neutrally() -> None:
    cfg = make_config()
    client = task_e_client(cfg, _signals())
    result = await score_resume(_row(GOOD_URL), None, client, cfg)
    assert result.bonus == 0.0 and result.error == ""
    assert not result.assessment.attempted and client.calls == []


async def test_fetch_failure_is_neutral_with_audit_note() -> None:
    cfg = make_config()
    fetcher, _ = make_fetcher(cfg, lambda r: httpx.Response(404))
    client = task_e_client(cfg, _signals())
    async with fetcher:
        result = await score_resume(_row(GOOD_URL), fetcher, client, cfg)
    assert result.bonus == 0.0
    assert result.assessment.failure == "http_status_404"
    assert "http_status_404" in result.error and "neutral" in result.error
    assert not result.assessment.fetched and client.calls == []


async def test_disallowed_host_is_neutral_with_audit_note() -> None:
    cfg = make_config()
    fetcher, handler = make_fetcher(cfg)
    async with fetcher:
        result = await score_resume(
            _row("https://evil.test/resume.pdf"), fetcher, task_e_client(cfg, _signals()), cfg
        )
    assert result.bonus == 0.0 and result.assessment.failure == "host_not_allowed"
    assert handler.calls == 0  # SSRF guard fires before any request


async def test_non_pdf_body_is_neutral_with_audit_note() -> None:
    cfg = make_config()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # e.g. the screenshot-as-resume upload
    fetcher, _ = make_fetcher(cfg, lambda r: httpx.Response(200, content=png))
    client = task_e_client(cfg, _signals())
    async with fetcher:
        result = await score_resume(_row(GOOD_URL), fetcher, client, cfg)
    assert result.bonus == 0.0
    assert result.assessment.fetched and result.assessment.failure == "not_a_pdf"
    assert client.calls == []  # extraction failed -> no token spent


async def test_task_e_parse_failure_is_neutral_never_a_block() -> None:
    cfg = make_config()
    fetcher, _ = make_fetcher(cfg)
    async with fetcher:
        result = await score_resume(_row(GOOD_URL), fetcher, task_e_client(cfg, None), cfg)
    assert result.bonus == 0.0 and result.task_e_called
    assert result.assessment.failure == "LLM_PARSE_FAILURE"
    assert "neutral" in result.error
    assert result.assessment.signals is None


async def test_no_resume_bytes_or_text_on_assessment() -> None:
    """The fetch->extract->discard memory rule: the audit block carries counts, never content."""
    cfg = make_config()
    fetcher, _ = make_fetcher(cfg)
    async with fetcher:
        result = await score_resume(
            _row(GOOD_URL), fetcher, task_e_client(cfg, _signals(relevant_projects=1)), cfg
        )
    dumped = result.assessment.model_dump_json()
    assert "USACO" not in dumped  # extracted text never serializes into the audit record
    assert result.assessment.extracted_chars > 0
