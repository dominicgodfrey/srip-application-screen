"""Stage 6 — resume bonus (Phase 6.3, PRD §7.2) — DEFERRED, INERT STUB.

A **bonus-only** slot that currently contributes 0 for everyone. The resume is a PDF URL;
scoring it needs download + text extraction, which is **not yet designed or built**. The slot
exists so the score composition (PRD §10.1) is stable, but absence of a resume is neutral anyway
(148 of 466 applicants leave it blank).

No magic numbers: the (zero) bonus comes from ``AppConfig.resume.bonus_max``.
"""

from __future__ import annotations

from ..config import AppConfig, ResumeConfig
from ..ingest import ApplicantRow
from ..models import TaskEOutput

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


def resume_bonus(row: ApplicantRow, cfg: AppConfig) -> float:
    """Stage 6 resume bonus — **inert stub, always returns 0** (PRD §7.2, DEFERRED).

    Returns ``resume.bonus_max`` (pinned at 0 in config) regardless of the ``Resume (optional)``
    cell. Never negative; can never change an outcome.

    TODO(resume): once PDF parsing is built, download ``row.resume_url``, extract text, and run
    an LLM relevance score (projects/internships/languages/repos) → 0..``resume.bonus_max``,
    relevance-only. Bump ``resume.bonus_max`` above 0 in config only then.
    """
    return cfg.resume.bonus_max
