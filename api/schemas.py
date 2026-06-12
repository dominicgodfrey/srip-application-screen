"""Request/response models for the API shell (Phase 9.1).

Thin pydantic envelopes over the job lifecycle. They carry only structural, non-PII facts —
ids, states, progress counts, and the run's outcome *counts* (from ``summary``) — never essay or
GPA content. The audit records themselves are downloaded as artifacts (Phase 9.4), not embedded
in a status payload.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .registry import Job, JobState


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    status: str = "ok"


class JobCreated(BaseModel):
    """Returned by ``POST /jobs`` (202) once a run is scheduled."""

    job_id: str
    state: JobState


class JobStatus(BaseModel):
    """Returned by ``GET /jobs/{id}`` — lifecycle + progress, plus the run summary once done.

    ``summary`` is the Stage-9 counts/histogram dict (outcome counts, ``RANKED`` score histogram,
    ``NEEDS_REVIEW`` reasons) and is present only when the job ``succeeded``. ``error`` is a safe
    one-line message present only when the job ``failed``.
    """

    job_id: str
    state: JobState
    filename: str = ""
    rows_done: int
    rows_total: int | None = None
    summary: dict | None = None
    error: str | None = None

    @classmethod
    def from_job(cls, job: Job) -> JobStatus:
        """Project a :class:`~api.registry.Job` into its public status view."""
        summary = (
            job.result.summary
            if job.state is JobState.SUCCEEDED and job.result is not None
            else None
        )
        return cls(
            job_id=job.job_id,
            state=job.state,
            filename=job.filename,
            rows_done=job.rows_done,
            rows_total=job.rows_total,
            summary=summary,
            error=job.error,
        )


class ErrorResponse(BaseModel):
    """Uniform error envelope for graceful 4xx responses (never a 500 / stack trace)."""

    detail: str = Field(description="Safe, human-readable reason; never PII or a stack trace.")
