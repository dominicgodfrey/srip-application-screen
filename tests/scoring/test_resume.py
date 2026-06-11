"""Tests for the Stage 6 resume stub (Phase 6.3). Deterministic, no API spend.

The resume bonus is DEFERRED (PRD §7.2): the slot exists but PDF download + parsing is unplanned,
so it contributes 0 for everyone regardless of the ``Resume (optional)`` cell.
"""

from __future__ import annotations

from srip_filter.config import AppConfig, ResumeConfig
from srip_filter.ingest import ApplicantRow
from srip_filter.llm.prompts import task_e as task_e_prompt
from srip_filter.models import TaskEOutput
from srip_filter.scoring.resume import resume_bonus, resume_signal_bonus

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


# ================================================================================================
# Phase 12.4 — Task E prompt shape + pure signal-pricing math (zero spend)
# ================================================================================================


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
    base: dict[str, object] = {"bonus_max": 10.0}  # kill switch off for the math tests
    base.update(overrides)
    return ResumeConfig.model_validate(base)


def test_task_e_prompt_shape() -> None:
    assert "COUNT" in task_e_prompt.SYSTEM
    assert "ONLY JSON" in task_e_prompt.SYSTEM
    rendered = task_e_prompt.user_prompt("Education: Example High School")
    assert rendered.startswith('RESUME_TEXT: """')
    assert "Example High School" in rendered and rendered.endswith('"""')


def test_signal_bonus_composition_uses_config_weights() -> None:
    cfg = _resume_cfg()
    out = _signals(
        relevant_projects=2, relevant_experience=1, relevant_awards=1, skills_relevance=0.5
    )
    # 2*1.5 + 1*2.0 + 1*1.0 + 0.5*2.0 = 7.0
    assert resume_signal_bonus(out, cfg) == 7.0


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
