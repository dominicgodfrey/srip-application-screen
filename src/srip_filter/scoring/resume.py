"""Stage 6 — resume bonus (Phase 6.3, PRD §7.2) — DEFERRED, INERT STUB.

A **bonus-only** slot that currently contributes 0 for everyone. The resume is a PDF URL;
scoring it needs download + text extraction, which is **not yet designed or built**. The slot
exists so the score composition (PRD §10.1) is stable, but absence of a resume is neutral anyway
(148 of 466 applicants leave it blank).

No magic numbers: the (zero) bonus comes from ``AppConfig.resume.bonus_max``.
"""

from __future__ import annotations

from ..config import AppConfig
from ..ingest import ApplicantRow


def resume_bonus(row: ApplicantRow, cfg: AppConfig) -> float:
    """Stage 6 resume bonus — **inert stub, always returns 0** (PRD §7.2, DEFERRED).

    Returns ``resume.bonus_max`` (pinned at 0 in config) regardless of the ``Resume (optional)``
    cell. Never negative; can never change an outcome.

    TODO(resume): once PDF parsing is built, download ``row.resume_url``, extract text, and run
    an LLM relevance score (projects/internships/languages/repos) → 0..``resume.bonus_max``,
    relevance-only. Bump ``resume.bonus_max`` above 0 in config only then.
    """
    return cfg.resume.bonus_max
