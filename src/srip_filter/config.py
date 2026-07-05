"""Configuration loading for the SRIP filter (Phase 0.2).

Two sources, deliberately separated:
  * config.yaml -> tunable knobs (PRD §10.3) + pinned model IDs. Non-secret, committed.
  * .env / env  -> secrets (OPENAI_API_KEY). Never committed, never logged.

Every magic number used by the pipeline must come from ``AppConfig``; nothing is hard-coded
in business logic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


class _Strict(BaseModel):
    """Config-section base: unknown keys in config.yaml are an error, not dropped."""

    model_config = ConfigDict(extra="forbid")


class EssayLengthConfig(_Strict):
    target_min: int = 100
    target_max: int = 350
    hard_min: int = 60
    hard_max: int = 500
    len_penalty_max: int = 5


class GibberishConfig(_Strict):
    """Cheap deterministic gibberish heuristics (PRD §4.2). ESL-safe: a hit requires
    ``min_signals`` independent signals to trip, so ordinary awkward/ESL prose passes."""

    min_signals: int = 2  # number of signals that must fire to call it gibberish
    max_consonant_run: int = 7  # longest run of consecutive consonants ABOVE this -> signal
    min_char_entropy: float = 2.5  # Shannon entropy of letters BELOW this -> signal
    max_repeat_run: int = 5  # run of one identical char AT/ABOVE this (aaaaa) -> signal
    min_unique_word_ratio: float = 0.3  # unique/total words BELOW this -> signal
    min_words_for_ratio: int = 20  # only evaluate the unique-word ratio with at least this many
    min_chars: int = 20  # below this many letters, skip detection (too little signal)


class GpaPercentageBand(_Strict):
    """One row of the PRD §6.1 percentage→4.0 table.

    A percentage at or above ``min_pct`` (and below the next-higher band's ``min_pct``) maps to
    ``gpa``. Below the lowest band the normalizer scales linearly toward 0 (§6.1: "< 73 → scale
    linearly toward 0"), anchored on the lowest band's ``(min_pct, gpa)`` point.
    """

    min_pct: float
    gpa: float


# PRD §6.1 default table (the 87-89 → 3.3 row is the gate threshold; 83-86 → 3.0 is a B average).
_DEFAULT_PERCENTAGE_TABLE: list[GpaPercentageBand] = [
    GpaPercentageBand(min_pct=93, gpa=4.0),
    GpaPercentageBand(min_pct=90, gpa=3.7),
    GpaPercentageBand(min_pct=87, gpa=3.3),
    GpaPercentageBand(min_pct=83, gpa=3.0),
    GpaPercentageBand(min_pct=80, gpa=2.7),
    GpaPercentageBand(min_pct=77, gpa=2.3),
    GpaPercentageBand(min_pct=73, gpa=2.0),
]


class GpaNormalizationConfig(_Strict):
    """Deterministic GPA-normalization knobs (PRD §6.1).

    The percentage table and the clean-scale ceiling are the only magic numbers in the Stage-2
    deterministic path; everything else (which scale a ``a/b`` fraction is) follows from the
    denominator. A bare value above ``gpa_max`` is treated as weighted/out-of-scale and routed
    to LLM Task A rather than resolved deterministically.
    """

    gpa_max: float = 4.0  # clean-scale ceiling + final cap; bare values above this -> Task A
    percentage_max: float = 100.0  # a percentage above this is invalid -> Task A
    percentage_table: list[GpaPercentageBand] = Field(
        default_factory=lambda: list(_DEFAULT_PERCENTAGE_TABLE)
    )


class GpaConfig(_Strict):
    threshold: float = 3.3
    hard_floor: float = 2.0  # below this, no explanation can rescue -> REJECTED outright
    score_max: float = 40.0
    normalization: GpaNormalizationConfig = Field(default_factory=GpaNormalizationConfig)


class EssayScoringConfig(_Strict):
    quality_max_each: int = 15  # v3 (SCORING.md): 15 per required essay, 30 total
    grammar_penalty_max: int = 3


class TechnicalEssayConfig(_Strict):
    """Stage 4b Task F bonus pricing (v3, SCORING.md). Model judges 0-10 signals; this
    prices them: ``bonus_max * Σ(w·signal) / (10·Σw)``. Bonus-only — never rejects."""

    bonus_max: float = 20.0
    weight_depth: float = 1.0
    weight_exploration: float = 1.0
    weight_impact: float = 1.0


class CourseworkConfig(_Strict):
    bonus_max: float = 15.0
    weight_cs: float = 1.0
    weight_math: float = 0.8
    weight_data: float = 0.6
    weight_other: float = 0.0
    min_grade_pct: float = 80.0  # an explicit grade below this excludes the course
    unit: float = 3.0


class SchoolConfig(_Strict):
    bonus_us_top20: float = 20.0  # v3 (SCORING.md)
    bonus_intl_top50: float = 16.0
    fuzzy_match_threshold: int = 88


class ResumeConfig(_Strict):
    """Stage 6 resume bonus (Phase 12, PRD §7.2 — in scope).

    ``bonus_max`` is the kill switch: at 0 the stage performs **zero fetches and zero LLM
    calls** (exact stub behavior). The download knobs implement the hosting design rules
    (PLAN.md Phase 12): peak transient memory = ``download_concurrency × max_download_bytes``,
    and ``allowed_url_hosts`` is the https-only SSRF allowlist — resume URLs arrive in an
    uploaded CSV, so only pinned hosts are ever fetched (empty list = nothing fetchable).
    The ``weight_*`` knobs price the Task E signals deterministically — the model counts and
    classifies, config prices (the Task C pattern).
    """

    # v3: 0 = shipping default until the engine decision (WEBSITE_ASKS #11); 25 once
    # decided (SCORING.md). At 0 the stage performs zero fetches and zero LLM calls.
    bonus_max: float = 0.0
    max_download_bytes: int = 10_485_760  # 10 MiB streaming cap per resume; abort above this
    download_timeout_s: float = 20.0
    download_concurrency: int = 4  # own semaphore, separate from the LLM one
    allowed_url_hosts: list[str] = Field(
        # The Fillout S3 bucket host observed in the real export (openissue #5).
        default_factory=lambda: ["prod-fillout-oregon-s3.s3.us-west-2.amazonaws.com"]
    )
    max_text_chars: int = 15_000  # extracted-text cap; bounds Task E token spend
    weight_project: float = 1.5  # per relevant project
    weight_experience: float = 2.0  # per relevant internship/job/research entry
    weight_award: float = 1.0  # per relevant award/competition
    weight_skills: float = 2.0  # × skills_relevance (0-1)


class CohortConfig(_Strict):
    """Cohort assignment (PRD §11, Phase 11; tiered cost model since 11.5).

    ``tiers`` are the canonical program tokens, matched case-insensitively by containment inside
    the free-text choice strings (the form emits inconsistent values like ``Summer 2026- HONORS``
    vs ``Summer 2026 - HONORS``). **List order is load-bearing:** it is the competitiveness/cost
    order, most expensive first — the cost ceiling ("never place a student above their first
    choice") is computed from list position. Per-tier capacities are NOT config — they are a
    per-request staff input (:class:`~srip_filter.models.CohortCapacities`).
    """

    tiers: list[str] = Field(default_factory=lambda: ["honors", "intensive", "regular"])


class TaskModels(_Strict):
    task_a: str = "gpt-4.1-mini"
    task_b: str = "gpt-4.1"
    task_c: str = "gpt-4.1-mini"
    task_d: str = "gpt-4.1"
    task_e: str = "gpt-4.1-mini"  # E: resume signal extraction (mechanical, Phase 12)
    task_f: str = "gpt-4.1"  # F: technical-essay bonus (judgment, bonus-only — v3)


class LlmConfig(_Strict):
    models: TaskModels = Field(default_factory=TaskModels)
    temperature: float = 0.2
    max_concurrency: int = 8
    max_retries: int = 2
    request_timeout_s: float = 60.0


class ApiConfig(_Strict):
    """Edge caps for the stateless FastAPI shell (Phase 9).

    These are magic numbers and belong in config, not the request handlers. ``max_upload_bytes``
    bounds the multipart body; ``max_rows`` caps the parsed CSV (~2000, PRD §12 scale target);
    ``job_ttl_seconds`` is how long a finished job (with its in-memory PII-bearing results) lives
    before the sweeper evicts it — a download evicts it immediately regardless.
    """

    max_upload_bytes: int = 26_214_400  # 25 MiB — comfortably fits ~2000 rows with long essays
    max_rows: int = 2000
    job_ttl_seconds: float = 3600.0  # 1 hour; results are discarded on download or at TTL
    job_sweep_seconds: float = 300.0  # how often the background sweeper evicts expired jobs


class DbConfig(_Strict):
    """asyncpg pool sizing (P1). The DSN itself is a secret and lives in the env, not here."""

    pool_min_size: int = 1
    pool_max_size: int = 5


class AuthConfig(_Strict):
    """Admin-session knobs (P5, PRD v3 §6). The password hash itself is a secret (env)."""

    session_ttl_seconds: float = 43_200.0  # 12 h — one working day, then re-login
    max_attempts: int = 5  # failed logins within the window before lockout
    lockout_seconds: float = 300.0  # sliding lockout window
    cookie_secure: bool = True  # set False only for local http:// development


class WorkerConfig(_Strict):
    """Grading-worker loop knobs (P3)."""

    poll_seconds: float = 2.0  # idle sleep between queue polls (stop wakes it immediately)


class WebhookConfig(_Strict):
    """Webhook edge knobs (P2, PRD v3 §2.1).

    ``max_body_bytes`` bounds a single application payload (a few KB in practice — 1 MiB is
    generous); ``max_skew_seconds`` is the HMAC replay window (|now − X-ATS-Timestamp|).
    """

    max_body_bytes: int = 1_048_576  # 1 MiB
    max_skew_seconds: float = 300.0  # ±5 min


class AppConfig(_Strict):
    """All tunable knobs. Defaults mirror PRD §10.3 exactly."""

    essay_length: EssayLengthConfig = Field(default_factory=EssayLengthConfig)
    gibberish: GibberishConfig = Field(default_factory=GibberishConfig)
    gpa: GpaConfig = Field(default_factory=GpaConfig)
    essay_scoring: EssayScoringConfig = Field(default_factory=EssayScoringConfig)
    technical_essay: TechnicalEssayConfig = Field(default_factory=TechnicalEssayConfig)
    coursework: CourseworkConfig = Field(default_factory=CourseworkConfig)
    school: SchoolConfig = Field(default_factory=SchoolConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    cohort: CohortConfig = Field(default_factory=CohortConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)


class Secrets(BaseSettings):
    """Secrets from environment / .env. Never written to outputs or logs.

    v3 additions: ``database_url`` (the ATS's own Neon Postgres — credentials inside, so
    env-only), ``database_url_test`` (dev Neon branch for the P1 test suite; tests skip
    cleanly when unset).
    """

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str | None = None
    database_url: str | None = None
    database_url_test: str | None = None
    # Webhook HMAC secrets (PRD v3 §2.1). "previous" enables zero-downtime rotation:
    # both are accepted while the website flips to a new value, then previous is cleared.
    ats_webhook_secret: str | None = None
    ats_webhook_secret_previous: str | None = None
    # Admin login (P5): PBKDF2 hash only, never plaintext. Generate:
    #   uv run python -m api.auth '<password>'
    admin_password_hash: str | None = None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate config.yaml.

    With no argument, loads the project-root ``config.yaml``; if that default file is absent,
    falls back to the PRD-default values. An explicitly supplied path that does not exist is an
    error (so a typo fails loudly rather than silently using defaults).
    """
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
    else:
        cfg_path = DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            return AppConfig()
    with cfg_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(data)


@lru_cache
def get_config() -> AppConfig:
    """Cached singleton config for application use."""
    return load_config()


@lru_cache
def get_secrets() -> Secrets:
    """Cached singleton secrets."""
    return Secrets()


def require_openai_key() -> str:
    """Return the OpenAI key, or raise if missing (used by the LLM client in later phases)."""
    key = get_secrets().openai_api_key
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to .env (see .env.example) to run LLM stages."
        )
    return key
