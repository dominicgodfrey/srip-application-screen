"""Stage 6 — resume bonus (Phase 12, PRD §7.2 — in scope; supersedes the deferred stub).

A **bonus-only** stage (PRD §0.3): it can add up to ``resume.bonus_max`` to ``final_score``,
never subtracts, and can never change a ``REJECTED``/``NEEDS_REVIEW`` outcome. It runs only on
gate-survivors inside ``grade_one``, so rejected rows cost zero downloads and zero tokens.

Per applicant the flow is **fetch → extract → Task E → price → discard** (the hosting memory
rule: resume bytes/text never outlive the call and never land on an audit record). Any failure
at any step — disallowed/missing URL, download error, non-PDF, scanned PDF, Task E parse
failure — degrades to a **0 bonus plus an audit note**, never a block (the Task C precedent:
a bonus-only signal that cannot be extracted is neutral).

Kill switch: ``resume.bonus_max: 0`` restores exact stub behavior — zero fetches, zero tokens.

Split (the Phases 3-5 isolate-the-LLM pattern):

  * 12.4 pure pricing math   — :func:`resume_signal_bonus` (pure, no LLM)
  * 12.5 Stage 6 aggregator  — :func:`score_resume`        (network + LLM)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig, ResumeConfig
from ..ingest import ApplicantRow
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_e as task_e_prompt
from ..models import ResumeAssessment, TaskEOutput
from ..resume_extract import extract_resume_text
from ..resume_fetch import ResumeFetcher

# ================================================================================================
# 12.4 — Pure resume bonus math (no LLM, PRD §7.2)
# ================================================================================================
# Task E counts and classifies; config prices (the Task C "model classifies, config prices"
# pattern). The sum is capped at bonus_max and floored at 0 — bonus-only, never negative
# (PRD §0.3). With bonus_max = 0 (the kill switch) every input prices to 0.


def resume_signal_bonus(out: TaskEOutput, cfg: ResumeConfig) -> float:
    """Price the Task E signal counts from config. Pure function, in ``[0, bonus_max]``.

    ``weight_project``/``weight_experience``/``weight_award`` are per-item prices on the
    counts; ``weight_skills`` scales the 0-1 ``skills_relevance``. A document that is not
    actually a resume (``is_resume`` false) prices to 0 — neutral, never a penalty.
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


# ================================================================================================
# 12.5 — Stage 6 aggregator (network + LLM)
# ================================================================================================


@dataclass(frozen=True)
class Stage6Result:
    """Reduced outcome of Stage 6 for one application.

    ``bonus`` drops into ``Scores.resume_bonus`` and ``assessment`` into
    ``AuditRecord.resume``. ``error`` is "" normally; on any fetch/extract/Task-E failure it
    carries a note for ``AuditRecord.errors`` while the applicant stays scoreable (bonus 0).
    ``task_e_called`` feeds the ``llm_calls`` audit list (true even when the call failed).
    """

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

    Skips with zero cost when the kill switch is on (``bonus_max <= 0``), the ``Resume
    (optional)`` cell is blank (148 applicants — absence is neutral), or no ``fetcher`` was
    provided. Every failure path returns ``bonus=0`` plus a typed audit note — **never**
    ``NEEDS_REVIEW``/``REJECTED``. The PDF bytes and extracted text are discarded before this
    returns; only counted signals reach the audit record.
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
    del fetched  # discard the PDF bytes immediately (per-applicant memory rule)
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
