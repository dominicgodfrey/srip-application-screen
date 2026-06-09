"""Progress polling + status tests (Phase 9.3): ``GET /jobs/{id}`` and the ``run_job`` failure path.

Covers the lifecycle projection (running progress, succeeded + summary, failed + safe message),
the 404 for an unknown/evicted id, and that a whole-run failure inside ``run_job`` is captured as
a safe message — never surfaced as a 500 or a stack trace. The happy-path job is driven to
completion by polling under the ``TestClient`` with an injected handler-free ``FakeLLMClient`` (the
rows reject at Stage 1, so no API spend).
"""

from __future__ import annotations

import csv
import io
import uuid

from api.jobs import run_job
from api.main import create_app
from api.registry import JobRegistry, JobState
from fastapi.testclient import TestClient

from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient

_HEADERS = [
    "Submission ID",
    "Student First Name",
    "Student Last Name",
    "What is your email address?",
    "GPA",
    "What motivates you to apply to Track 2 of the SRIP program? (100-350 words)",
    "Track 2 is designed as a foundation for future research. (100-350 words)",
    "I affirm that the information provided above is truthful and accurate.",
]


def _short_row(sid: str) -> dict[str, str]:
    # A too-short essay hard-fails the Stage-1 length gate → REJECTED with zero LLM spend, so the
    # run completes deterministically without a scripted handler.
    return {
        "Submission ID": sid,
        "Student First Name": "Ann",
        "Student Last Name": "Lee",
        "What is your email address?": f"{sid}@example.com",
        "GPA": "3.8",
        _HEADERS[5]: "too short",
        _HEADERS[6]: "too short",
        _HEADERS[7]: "I affirm this is truthful.",
    }


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in _HEADERS})
    return buf.getvalue().encode("utf-8")


def _app() -> object:
    cfg = AppConfig()
    return create_app(config=cfg, client=FakeLLMClient(cfg))


# --------------------------------------------------------------------------------------------
# GET /jobs/{id} — projections + 404
# --------------------------------------------------------------------------------------------


def test_unknown_job_returns_404() -> None:
    client = TestClient(_app())
    resp = client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert "id" in resp.json()["detail"]


def test_status_reports_running_progress_without_summary() -> None:
    app = _app()
    job = app.state.registry.create()
    job.state = JobState.RUNNING
    job.rows_done = 2
    job.rows_total = 5

    body = TestClient(app).get(f"/jobs/{job.job_id}").json()
    assert body["state"] == "running"
    assert body["rows_done"] == 2
    assert body["rows_total"] == 5
    assert body["summary"] is None
    assert body["error"] is None


def test_status_reports_failed_with_safe_message() -> None:
    app = _app()
    job = app.state.registry.create()
    job.state = JobState.FAILED
    job.error = "Grading failed due to an internal error."

    body = TestClient(app).get(f"/jobs/{job.job_id}").json()
    assert body["state"] == "failed"
    assert body["error"] == "Grading failed due to an internal error."
    assert body["summary"] is None


def test_job_polled_to_completion_exposes_summary() -> None:
    app = _app()
    with TestClient(app) as client:
        created = client.post(
            "/jobs", files={"file": ("apps.csv", _csv_bytes([_short_row("a"), _short_row("b")]))}
        )
        job_id = created.json()["job_id"]

        body = created.json()
        for _ in range(200):
            body = client.get(f"/jobs/{job_id}").json()
            if body["state"] in (JobState.SUCCEEDED, JobState.FAILED):
                break

    assert body["state"] == JobState.SUCCEEDED
    assert body["rows_done"] == body["rows_total"] == 2
    # Both rows hard-failed the Stage-1 length gate → REJECTED; summary counts reconcile.
    assert body["summary"]["counts"] == {"total": 2, "RANKED": 0, "REJECTED": 2, "NEEDS_REVIEW": 0}


# --------------------------------------------------------------------------------------------
# run_job — whole-run failure is captured, never raised
# --------------------------------------------------------------------------------------------


async def test_run_job_marks_failed_on_unprocessable_input() -> None:
    # grade_batch re-ingests and raises HeaderValidationError on a non-contract CSV; run_job must
    # absorb it into a safe FAILED state (never re-raise), and stamp finished_at for the TTL clock.
    cfg = AppConfig()
    job = JobRegistry(ttl_seconds=3600).create()
    await run_job(job, b"col1,col2\nv1,v2\n", FakeLLMClient(cfg), cfg)

    assert job.state is JobState.FAILED
    assert job.error == "Grading failed due to an internal error."
    assert job.result is None
    assert job.finished_at is not None
