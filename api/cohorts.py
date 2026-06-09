"""Cohort endpoint helpers (Phase 11.4, PRD §11).

Both cohort routes are thin, synchronous shells over the pure
:func:`srip_filter.cohort.assign_cohorts`: assignment over ≤2000 records takes milliseconds, so
there is no background job, no registry entry, and nothing stored — the response *is* the whole
result, and every call recomputes from scratch. That statelessness is the feature: staff can
iterate capacities ("what if honors takes 40?") and watch the assignment move live.

Validation mirrors the upload edge in :mod:`api.jobs`: every rejection is a graceful 4xx (413
too-large via :func:`api.jobs.read_upload_capped`, 422 unparseable) — never a 500 — and error
messages carry line numbers, never applicant content.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import ValidationError

from srip_filter.cohort import COHORT_ASSIGNMENTS_FILE, cohort_assignments_csv
from srip_filter.models import AuditRecord, CohortResult

from .jobs import _HTTP_413_TOO_LARGE, _HTTP_422_UNPROCESSABLE

CohortFormat = Literal["json", "csv"]


def parse_decisions_jsonl(raw: bytes, max_rows: int) -> list[AuditRecord]:
    """Parse an uploaded ``decisions.jsonl`` back into audit records. Raises HTTP 4xx, never 500.

    Accepts exactly what :func:`srip_filter.outputs.decisions_jsonl` emits (UTF-8, one record per
    line; blank lines tolerated). Errors echo a line number only — no applicant content ever
    appears in a response body.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:  # decisions.jsonl is always UTF-8; anything else is garbage
        raise HTTPException(
            status_code=_HTTP_422_UNPROCESSABLE,
            detail="Uploaded file is not UTF-8 text (expected a decisions.jsonl).",
        ) from exc

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(
            status_code=_HTTP_422_UNPROCESSABLE,
            detail="Uploaded file contains no records.",
        )
    if len(lines) > max_rows:
        raise HTTPException(
            status_code=_HTTP_413_TOO_LARGE,
            detail=f"File has {len(lines)} records; the maximum is {max_rows}.",
        )

    records: list[AuditRecord] = []
    for number, line in enumerate(lines, start=1):
        try:
            records.append(AuditRecord.model_validate_json(line))
        except ValidationError as exc:
            raise HTTPException(
                status_code=_HTTP_422_UNPROCESSABLE,
                detail=f"Line {number} is not a valid audit record.",
            ) from exc
    return records


def cohort_response(result: CohortResult, format: CohortFormat) -> Response:
    """Render an assignment result in the requested form: JSON (default) or the CSV download."""
    if format == "csv":
        return Response(
            content=cohort_assignments_csv(result),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{COHORT_ASSIGNMENTS_FILE}"'
            },
        )
    return Response(content=result.model_dump_json(), media_type="application/json")
