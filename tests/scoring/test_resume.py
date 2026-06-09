"""Tests for the Stage 6 resume stub (Phase 6.3). Deterministic, no API spend.

The resume bonus is DEFERRED (PRD §7.2): the slot exists but PDF download + parsing is unplanned,
so it contributes 0 for everyone regardless of the ``Resume (optional)`` cell.
"""

from __future__ import annotations

from srip_filter.config import AppConfig
from srip_filter.ingest import ApplicantRow
from srip_filter.scoring.resume import resume_bonus

APP = AppConfig()


def _row(resume_url: str = "") -> ApplicantRow:
    return ApplicantRow(submission_id="s1", resume_url=resume_url)


def test_resume_bonus_is_zero_when_blank() -> None:
    assert resume_bonus(_row(""), APP) == 0.0


def test_resume_bonus_is_zero_when_present() -> None:
    # Even a present resume URL contributes nothing in the current (deferred) scope.
    assert resume_bonus(_row("https://s3.example.com/resume.pdf"), APP) == 0.0


def test_resume_bonus_matches_config_max() -> None:
    assert resume_bonus(_row("anything"), APP) == APP.resume.bonus_max
