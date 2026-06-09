"""Upload validation + the background grading task (Phase 9.2).

The edge does the cheap, deterministic gatekeeping before any LLM spend or background work:
enforce the byte-size cap while streaming the upload, then parse + header-validate + row-cap the
CSV. Every rejection is a graceful 4xx (413 too-large, 422 unprocessable) — **never a 500** (PRD
§Privacy: "reject malformed uploads gracefully, not a 500"). A clean upload schedules
:func:`~srip_filter.pipeline.grade_batch` as a fire-and-forget ``asyncio`` task; the run is held
only in the in-memory :class:`~api.registry.Job`.

The CSV is parsed once here for validation and again inside ``grade_batch`` (Stage 0). That double
parse is deliberate: it keeps the core untouched (the only core change in Phase 9 is the 9.3
progress callback) and a re-parse of a ≤25 MiB blob is cheap next to the LLM grading that follows.
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException, UploadFile

from srip_filter.config import AppConfig
from srip_filter.ingest import HeaderValidationError, read_csv_records, validate_headers
from srip_filter.llm.client import BaseLLMClient
from srip_filter.pipeline import grade_batch

from .registry import Job, JobState

logger = logging.getLogger(__name__)

_READ_CHUNK = 1 << 20  # 1 MiB streaming chunk — bounds memory to max_bytes + one chunk

# Status codes as plain ints: Starlette renamed its 413/422 constants across versions and the old
# names warn on access, so literals stay correct across the whole supported FastAPI range.
_HTTP_413_TOO_LARGE = 413  # Content Too Large
_HTTP_422_UNPROCESSABLE = 422  # Unprocessable Content

# pandas raises these for an empty/garbled CSV; both subclass ValueError. UnicodeDecodeError can't
# actually fire (read_csv_records' latin-1 fallback decodes any byte) but is caught for safety.
_UNREADABLE_CSV: tuple[type[Exception], ...] = (ValueError, UnicodeDecodeError)


async def read_upload_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Stream the upload into memory, aborting with 413 the moment it exceeds ``max_bytes``.

    Reads in chunks so an oversize body is rejected without buffering the whole thing — peak
    memory is ``max_bytes`` plus one chunk.
    """
    buffer = bytearray()
    while chunk := await upload.read(_READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=_HTTP_413_TOO_LARGE,
                detail=f"Uploaded file exceeds the maximum size of {max_bytes} bytes.",
            )
    return bytes(buffer)


def validate_csv(raw: bytes, cfg: AppConfig) -> None:
    """Validate an uploaded CSV at the edge before scheduling work. Raises HTTP 4xx on failure.

    Three checks, in cost order: parseability (→ 422), §2 header contract (→ 422), and the row cap
    (→ 413). Header/row checks reuse the Stage-0 contract so the edge and the core agree on what a
    valid upload is. No applicant content is echoed in any error message.
    """
    try:
        headers, records = read_csv_records(raw)
    except _UNREADABLE_CSV as exc:
        raise HTTPException(
            status_code=_HTTP_422_UNPROCESSABLE,
            detail="Uploaded file could not be read as CSV.",
        ) from exc

    try:
        validate_headers(headers)
    except HeaderValidationError as exc:
        raise HTTPException(
            status_code=_HTTP_422_UNPROCESSABLE,
            detail=f"CSV headers do not satisfy the required data contract: {exc}",
        ) from exc

    if len(records) > cfg.api.max_rows:
        raise HTTPException(
            status_code=_HTTP_413_TOO_LARGE,
            detail=f"CSV has {len(records)} rows; the maximum is {cfg.api.max_rows}.",
        )


async def run_job(job: Job, raw: bytes, client: BaseLLMClient, cfg: AppConfig) -> None:
    """Background task: run the whole pipeline over ``raw`` and record the result on ``job``.

    Marks the job ``RUNNING``, awaits :func:`~srip_filter.pipeline.grade_batch`, and on success
    stashes the in-memory :class:`~srip_filter.pipeline.BatchResult` and marks ``SUCCEEDED``. Any
    failure is captured as a **safe** message (never PII, never a stack trace) and marked
    ``FAILED`` — the batch never aborts the process; per-row errors are already absorbed inside the
    pipeline ("when grading begins, it finishes"), so reaching here means an unexpected whole-run
    failure. ``finished_at`` is always stamped so the TTL clock starts.
    """
    job.state = JobState.RUNNING

    def _progress(rows_done: int, rows_total: int) -> None:
        # Called by grade_batch after ingest (0, total) and after each row; drives the poll.
        job.rows_done = rows_done
        job.rows_total = rows_total

    try:
        result = await grade_batch(raw, client, cfg, progress=_progress)
        job.result = result
        job.state = JobState.SUCCEEDED
    except Exception:  # safety net — grade_batch isolates per-row errors, so this is unexpected
        logger.exception("grading job %s failed", job.job_id)
        job.error = "Grading failed due to an internal error."
        job.state = JobState.FAILED
    finally:
        job.finished_at = time.monotonic()
