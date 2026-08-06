"""Stage 6 — resume bonus (PRD §7.2). Bonus-only: never subtracts, never changes an outcome.

Per applicant: **fetch → extract → Task E → price → discard** — resume bytes and text never
outlive the call and never reach an audit record. Any failure along the way degrades to 0 bonus
plus an audit note, never a block. ``resume.bonus_max: 0`` is the kill switch: zero fetches,
zero tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..applicant import ApplicantRow
from ..config import AppConfig, ResumeConfig
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_e as task_e_prompt
from ..models import ResumeAssessment, TaskEOutput
from ..resume_extract import extract_resume_text
from ..resume_fetch import ResumeFetcher

# --- Pure resume bonus math (no LLM, PRD §7.2) ---
# Task E counts and classifies; config prices — the same split as Task C.


def resume_signal_bonus(out: TaskEOutput, cfg: ResumeConfig) -> float:
    """Price the Task E signal counts from config. Pure, in ``[0, bonus_max]``.

    The count weights are per-item; ``weight_skills`` scales the 0-1 ``skills_relevance``. A
    document that is not a resume prices to 0 — neutral, never a penalty.
    """
    if not out.is_resume:
        return 0.0
    raw = (
        cfg.weight_project * out.relevant_projects
        + cfg.weight_experience * out.relevant_experience
        + cfg.weight_award * out.relevant_awards
        + cfg.weight_skills * out.skills_relevance
    )
    return round(max(0.0, min(cfg.bonus_max, raw)), 4)


# --- Stage 6 aggregator (network + LLM) ---


@dataclass(frozen=True)
class Stage6Result:
    """Reduced Stage-6 outcome. ``error`` is "" normally; on any fetch/extract/Task-E failure it
    carries a note for ``AuditRecord.errors`` while the applicant stays scoreable at bonus 0.
    ``task_e_called`` feeds the ``llm_calls`` audit list, true even when the call failed."""

    bonus: float
    assessment: ResumeAssessment
    error: str
    task_e_called: bool


def _skipped(url: str) -> Stage6Result:
    """No-op result (kill switch / no URL / no fetcher): neutral, no fetch, no token."""
    return Stage6Result(
        bonus=0.0,
        assessment=ResumeAssessment(url_present=bool(url), url=url),
        error="",
        task_e_called=False,
    )


def _failed(
    assessment: ResumeAssessment, reason: str, *, task_e_called: bool = False
) -> Stage6Result:
    """Typed-failure result: 0 bonus + an audit note, never a block (PRD §0.3)."""
    assessment.failure = reason
    return Stage6Result(
        bonus=0.0,
        assessment=assessment,
        error=f"resume: {reason} (bonus neutral)",
        task_e_called=task_e_called,
    )


async def score_resume(
    row: ApplicantRow,
    fetcher: ResumeFetcher | None,
    client: BaseLLMClient,
    cfg: AppConfig,
) -> Stage6Result:
    """Stage 6 end to end: fetch the resume PDF, extract text, run Task E, price the signals.

    Skips at zero cost when the kill switch is on, the URL is blank, or no ``fetcher`` was
    provided. Every failure path returns ``bonus=0`` plus a typed audit note. The PDF bytes and
    extracted text are discarded before this returns; only counted signals reach the audit.
    """
    url = row.resume_url.strip()
    if cfg.resume.bonus_max <= 0 or not url or fetcher is None:
        return _skipped(url)

    assessment = ResumeAssessment(url_present=True, url=url, attempted=True)

    fetched = await fetcher.fetch(url)
    if not fetched.ok:
        return _failed(assessment, fetched.failure)
    assessment.fetched = True

    extracted = extract_resume_text(fetched.content, cfg)
    del fetched  # discard the PDF bytes immediately
    if not extracted.ok:
        return _failed(assessment, extracted.failure)
    assessment.extracted_chars = len(extracted.text)

    try:
        signals = await client.complete(
            "task_e",
            system=task_e_prompt.SYSTEM,
            user=task_e_prompt.user_prompt(extracted.text),
            schema=TaskEOutput,
        )
    except LLMParseFailure:
        return _failed(assessment, "LLM_PARSE_FAILURE", task_e_called=True)

    assessment.signals = signals
    return Stage6Result(
        bonus=resume_signal_bonus(signals, cfg.resume),
        assessment=assessment,
        error="",
        task_e_called=True,
    )
