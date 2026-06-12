"""Tests for configuration loading (Phase 0.2)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from srip_filter.config import AppConfig, Secrets, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_defaults_match_prd() -> None:
    """AppConfig() with no file must equal the PRD §10.3 defaults."""
    cfg = AppConfig()
    assert cfg.gpa.threshold == 3.0
    assert cfg.gpa.score_max == 40.0
    assert cfg.essay_length.target_min == 100
    assert cfg.essay_length.target_max == 350
    assert cfg.essay_length.hard_min == 60
    assert cfg.essay_length.hard_max == 500
    assert cfg.essay_length.len_penalty_max == 5
    assert cfg.essay_scoring.quality_max_each == 20
    assert cfg.essay_scoring.grammar_penalty_max == 3
    assert cfg.coursework.bonus_max == 15.0
    assert cfg.coursework.min_grade_pct == 85.0  # B; explicit grade below this excludes
    assert cfg.school.bonus_us_top20 == 15.0
    assert cfg.school.bonus_intl_top50 == 12.0
    assert cfg.school.fuzzy_match_threshold == 88
    assert cfg.resume.bonus_max == 10.0  # PRD §10.1; 0 is the Stage 6 kill switch


def test_resume_config_defaults() -> None:
    """Phase 12.1: the Stage-6 download/extraction/pricing knobs (PLAN.md Phase 12)."""
    cfg = AppConfig()
    assert cfg.resume.max_download_bytes == 10_485_760
    assert cfg.resume.download_timeout_s == 20.0
    assert cfg.resume.download_concurrency == 4
    # SSRF allowlist: the pinned Fillout S3 bucket host (openissue #5), https-only.
    assert cfg.resume.allowed_url_hosts == ["prod-fillout-oregon-s3.s3.us-west-2.amazonaws.com"]
    assert cfg.resume.max_text_chars == 15_000
    # Signal pricing is config-owned (the model never prices) and never negative.
    for weight in (
        cfg.resume.weight_project,
        cfg.resume.weight_experience,
        cfg.resume.weight_award,
        cfg.resume.weight_skills,
    ):
        assert weight >= 0.0


def test_loads_shipped_config_yaml() -> None:
    cfg = load_config(PROJECT_ROOT / "config.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.gpa.threshold == 3.0
    assert cfg.llm.models.task_a == "gpt-4.1-mini"
    assert cfg.llm.models.task_b == "gpt-4.1"
    assert cfg.llm.models.task_c == "gpt-4.1-mini"
    assert cfg.llm.models.task_d == "gpt-4.1"
    assert cfg.llm.models.task_e == "gpt-4.1-mini"  # mechanical extraction -> mini tier
    assert cfg.llm.temperature <= 0.2


def test_shipped_yaml_matches_defaults() -> None:
    """The committed config.yaml must not drift from the PRD defaults baked into the models."""
    assert load_config(PROJECT_ROOT / "config.yaml") == AppConfig()


def test_default_path_load() -> None:
    """load_config() with no arg resolves to the project-root config.yaml regardless of CWD."""
    cfg = load_config()
    assert cfg.gpa.threshold == 3.0


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"gpa": {"threshold": 3.0, "bogus": 1}})


def test_missing_explicit_path_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_config(PROJECT_ROOT / "does_not_exist.yaml")


def test_secrets_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    secrets = Secrets(_env_file=None)  # type: ignore[call-arg]
    assert secrets.openai_api_key is None
