"""Request/response models for the API shell.

Thin pydantic envelopes carrying only structural, non-PII facts — never essay or GPA content.
Audit records and exports are served by ``api.admin_api``, not embedded in these payloads.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Uniform error envelope for graceful 4xx responses (never a 500 / stack trace)."""

    detail: str = Field(description="Safe, human-readable reason; never PII or a stack trace.")


class PurgeRequest(BaseModel):
    """Body of ``POST /api/admin/purge`` (PRD v3 §9). Irreversible — hence the count guard."""

    cohort: str | None = Field(
        default=None,
        description="Cohort to purge; omit/null purges EVERY cohort and clears llm_cache.",
    )
    expected_count: int = Field(
        ge=0,
        description=(
            "Application count the operator was shown and consented to. The purge 409s "
            "without deleting if the live count has moved (deliveries arrive continuously)."
        ),
    )
