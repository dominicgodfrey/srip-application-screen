"""Configuration loading. Two deliberately separate sources: ``config.yaml`` for tunable knobs
and pinned model IDs (committed), the environment for secrets (never committed or logged).

Every magic number the pipeline uses comes from ``AppConfig``; nothing is hard-coded in the
business logic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def project_root() -> Path:
    """Where config.yaml, db/migrations, and resources live: normally two levels up, but a host
    that *installs* the package puts the source in site-packages, so fall back to cwd (the
    project base on Vercel)."""
    here = Path(__file__).resolve().parents[2]
    return here if (here / "config.yaml").exists() else Path.cwd()


_PROJECT_ROOT = project_root()
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


class _Strict(BaseModel):
    """Config-section base: unknown keys in config.yaml are an error, not dropped."""

    model_config = ConfigDict(extra="forbid")


class GibberishConfig(_Strict):
    """Cheap deterministic gibberish heuristics (PRD §4.2). ESL-safe: a hit requires
    ``min_signals`` independent signals to trip, so ordinary awkward/ESL prose passes."""

    min_signals: int = 2  # signals that must fire together to call it gibberish
    max_consonant_run: int = 7  # consecutive consonants ABOVE this -> signal
    min_char_entropy: float = 2.5  # Shannon entropy of letters BELOW this -> signal
    max_repeat_run: int = 5  # run of one identical char AT/ABOVE this -> signal
    min_unique_word_ratio: float = 0.3  # unique/total words BELOW this -> signal
    min_words_for_ratio: int = 20  # fewer words than this: skip the ratio signal
    min_chars: int = 20  # fewer letters than this: too little signal, skip detection


class GpaPercentageBand(_Strict):
    """One row of the PRD §6.1 percentage→4.0 table: a percentage at or above ``min_pct`` maps
    to ``gpa``, and below the lowest band the normalizer scales linearly toward 0."""

    min_pct: float
    gpa: float


# PRD §6.1 default table; the 87-89 → 3.3 row is the gate threshold.
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
    """Deterministic GPA-normalization knobs (PRD §6.1). The table and the ceiling are the only
    magic numbers in the Stage-2 path — a fraction's scale follows from its denominator."""

    gpa_max: float = 4.0  # clean-scale ceiling + final cap; bare values above this -> Task A
    percentage_max: float = 100.0  # above this a percentage is invalid -> Task A
    percentage_table: list[GpaPercentageBand] = Field(
        default_factory=lambda: list(_DEFAULT_PERCENTAGE_TABLE)
    )


class GpaConfig(_Strict):
    threshold: float = 3.3
    hard_floor: float = 2.0  # below this no explanation can rescue -> REJECTED
    score_max: float = 40.0
    normalization: GpaNormalizationConfig = Field(default_factory=GpaNormalizationConfig)


class EssayScoringConfig(_Strict):
    quality_max_each: int = 15  # 30 across the two required essays
    grammar_penalty_max: int = 3


class TechnicalEssayConfig(_Strict):
    """Stage 4b Task F pricing: the model judges 0-10 signals, this prices them as
    ``bonus_max * Σ(w·signal) / (10·Σw)``."""

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
    bonus_us_top20: float = 20.0
    bonus_intl_top50: float = 16.0
    fuzzy_match_threshold: int = 88


class ResumeConfig(_Strict):
    """Stage 6 resume bonus (PRD §7.2). ``allowed_url_hosts`` is the https-only SSRF allowlist —
    only pinned hosts are ever fetched. Peak transient memory is
    ``download_concurrency × max_download_bytes``."""

    # Kill switch: 0 means zero fetches and zero LLM calls. Raising it to 25 needs only the R2
    # host pinned below; the weights are already priced for that scale.
    bonus_max: float = 0.0
    max_download_bytes: int = 10_485_760  # 10 MiB streaming cap per resume
    download_timeout_s: float = 20.0
    download_concurrency: int = 4  # own semaphore, separate from the LLM one
    allowed_url_hosts: list[str] = Field(
        # Retired v2 value (the Fillout S3 bucket); re-pin to the R2 host before enabling.
        default_factory=lambda: ["prod-fillout-oregon-s3.s3.us-west-2.amazonaws.com"]
    )
    max_text_chars: int = 15_000  # extracted-text cap; bounds Task E token spend
    # Priced for the 0-25 band (owner, 2026-07-30): the v2 values x2.5, which moves the approved
    # shape onto the new maximum rather than re-deciding it. test_resume.py pins the landings.
    weight_project: float = 3.75  # per relevant project
    weight_experience: float = 5.0  # per relevant internship/job/research entry
    weight_award: float = 2.5  # per relevant award/competition
    weight_skills: float = 5.0  # × skills_relevance (0-1)


class CohortConfig(_Strict):
    """Cohort assignment (PRD §11). ``tiers`` are the canonical program tokens, matched by
    case-insensitive containment because the form emits inconsistent strings.

    **List order is load-bearing:** most expensive first, since the cost ceiling ("never place a
    student above their first choice") is computed from list position. Per-tier capacities are a
    per-request staff input, not config.
    """

    tiers: list[str] = Field(default_factory=lambda: ["honors", "intensive", "regular"])


class TaskModels(_Strict):
    task_a: str = "gpt-4.1-mini"
    task_b: str = "gpt-4.1"
    task_c: str = "gpt-4.1-mini"
    task_d: str = "gpt-4.1"
    task_e: str = "gpt-4.1-mini"  # resume signal extraction (mechanical)
    task_f: str = "gpt-4.1"  # technical-essay bonus (judgment, bonus-only)


class LlmConfig(_Strict):
    models: TaskModels = Field(default_factory=TaskModels)
    temperature: float = 0.2
    # Transient-failure budget (429 / timeout / connection / 5xx), backoff capped at
    # backoff_max_s. Sized to ride out a sustained rate limit rather than dump healthy
    # applications into NEEDS_REVIEW — at 2 attempts a 30k-TPM ceiling failed 307 of 466 rows
    # (2026-07-29). Terminal failures still get exactly one retry, per PRD §8.
    max_attempts: int = 6
    backoff_max_s: float = 30.0
    # ⚠️ RAISE to the deploying account's real TPM limit for the judgment models (gpt-4.1 is
    # the binding one); 0 disables pacing. The default is OpenAI's tier-1 figure — being wrong
    # low only makes a batch slower, being wrong high wastes calls on 429s.
    tokens_per_minute: int = 30_000
    estimated_output_tokens: int = 400  # per-call allowance for the pacing estimate
    max_concurrency: int = 8
    max_retries: int = 2
    request_timeout_s: float = 60.0


class ApiConfig(_Strict):
    """Edge caps for the shell's one upload route (``POST /cohorts``)."""

    max_upload_bytes: int = 26_214_400  # 25 MiB — comfortably fits ~2000 records
    max_rows: int = 2000


class DbConfig(_Strict):
    """asyncpg pool sizing; the DSN itself is a secret and lives in the env. Sized for
    serverless — many short-lived instances, and ``min_size: 0`` so a cold start pays for
    no connection it never uses."""

    pool_min_size: int = 0
    pool_max_size: int = 2


class AuthConfig(_Strict):
    """Admin-session knobs (PRD v3 §6). The password hash itself is a secret (env)."""

    # Short because sessions are stateless: with no server-side revocation, expiry is the only
    # thing that retires a stolen cookie on its own.
    session_ttl_seconds: float = 7_200.0
    max_attempts: int = 5  # failed logins from ONE client per window before lockout
    # Failed logins across ALL clients — the distributed-guesser backstop. Deliberately far
    # above max_attempts: as the only tier, it let any anonymous caller hold staff out.
    max_attempts_global: int = 50
    lockout_seconds: float = 300.0  # sliding lockout window
    cookie_secure: bool = True  # set False only for local http:// development


class WorkerConfig(_Strict):
    """Grading-worker knobs: the local loop and the cron drain."""

    poll_seconds: float = 2.0  # idle sleep between queue polls (stop wakes it immediately)
    # The budget sits well inside Vercel's 800 s maxDuration; stale_grading_seconds must
    # exceed the slowest realistic single-row grade.
    drain_budget_seconds: float = 600.0
    drain_max_rows: int = 50
    stale_grading_seconds: float = 900.0
    # /health goes "degraded" once the oldest ungraded row is older than this. Sized off drain
    # throughput: 50 rows/min clears a ~2,000-application burst in ~40 min, so anything under
    # an hour cries wolf at the busiest moment of the cycle. Raise it if drain_max_rows drops.
    queue_alert_seconds: float = 3600.0


class WebhookConfig(_Strict):
    """Webhook edge knobs. A real payload is a few KB, so the cap is generous."""

    max_body_bytes: int = 1_048_576  # 1 MiB


class AppConfig(_Strict):
    """All tunable knobs. Defaults mirror PRD §10.3 exactly."""

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
    """Secrets from environment / .env. Never written to outputs or logs."""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str | None = None
    database_url: str | None = None
    database_url_test: str | None = None  # separate Neon branch; DB tests skip when unset
    # The website's X-ATS-Secret. "previous" enables zero-downtime rotation: both are
    # accepted while the website flips over, then previous is cleared.
    ats_webhook_secret: str | None = None
    ats_webhook_secret_previous: str | None = None
    # PBKDF2 hash only, never plaintext. Generate: uv run python -m api.auth '<password>'
    admin_password_hash: str | None = None
    # Vercel sends this as `Authorization: Bearer …` on cron invocations; unset ⇒ 503.
    cron_secret: str | None = None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate config.yaml, falling back to defaults when the project-root file is
    absent. An explicitly supplied path that does not exist raises, so a typo fails loudly."""
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
    """Return the OpenAI key, or raise if missing."""
    key = get_secrets().openai_api_key
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to .env (see .env.example) to run LLM stages."
        )
    return key
