"""Request/response models for the API shell.

Thin pydantic envelopes carrying only structural, non-PII facts — never essay or GPA content.
Audit records and exports are served by ``api.admin_api``, not embedded in these payloads.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    status: str = "ok"


class ErrorResponse(BaseModel):
    """Uniform error envelope for graceful 4xx responses (never a 500 / stack trace)."""

    detail: str = Field(description="Safe, human-readable reason; never PII or a stack trace.")
