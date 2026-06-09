"""Result download + lifecycle/TTL tests (Phase 9.4).

Drives a job to completion, downloads each of the five Stage-9 artifacts (correct content type +
attachment filename), and checks the lifecycle edges: download-before-done → 409, unknown job →
404, explicit discard (DELETE) then fetch → 404, and the background sweeper evicting an expired
job. Synthetic data, no API spend (rows reject at Stage 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import time
import uuid

from api.jobs import sweeper_loop
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
    return {
        "Submission ID": sid,
        "Student First Name": "Ann",
        "Student Last Name": "Lee",
        "What is your email address?": f"{sid}@example.com",
        "GPA": "3.8",
        _HEADERS[5]: "too short",  # Stage-1 length hard fail → REJECTED, zero LLM spend
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


def _app():
    cfg = AppConfig()
    return create_app(config=cfg, client=FakeLLMClient(cfg))


def _run_to_completion(client: TestClient) -> str:
    """POST a 2-row CSV and poll until the job succeeds; return its id."""
    created = client.post("/jobs", files={"file": ("apps.csv", _csv_bytes([_short_row("a")]))})
    job_id = created.json()["job_id"]
    for _ in range(200):
        if client.get(f"/jobs/{job_id}").json()["state"] == JobState.SUCCEEDED:
            break
    return job_id


# --------------------------------------------------------------------------------------------
# Downloading the five artifacts
# --------------------------------------------------------------------------------------------


def test_download_each_artifact() -> None:
    expected = {
        "decisions": ("decisions.jsonl", "application/x-ndjson"),
        "ranked": ("ranked.csv", "text/csv"),
        "rejected": ("rejected.csv", "text/csv"),
        "needs_review": ("needs_review.csv", "text/csv"),
        "summary": ("summary.json", "application/json"),
    }
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client)
        for artifact, (filename, media_type) in expected.items():
            resp = client.get(f"/jobs/{job_id}/results/{artifact}")
            assert resp.status_code == 200, artifact
            assert resp.headers["content-type"].startswith(media_type), artifact
            assert resp.headers["content-disposition"] == f'attachment; filename="{filename}"'

        # Content sanity: summary parses to the counts dict; decisions has one JSON line per row.
        summary = json.loads(client.get(f"/jobs/{job_id}/results/summary").content)
        assert summary["counts"]["total"] == 1
        decisions = client.get(f"/jobs/{job_id}/results/decisions").text
        assert decisions.count("\n") == 1


def test_unknown_artifact_name_returns_422() -> None:
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client)
        resp = client.get(f"/jobs/{job_id}/results/not_an_artifact")
    assert resp.status_code == 422  # Enum path param rejects unknown names


# --------------------------------------------------------------------------------------------
# Lifecycle edges
# --------------------------------------------------------------------------------------------


def test_download_before_done_returns_409() -> None:
    app = _app()
    job = app.state.registry.create()
    job.state = JobState.RUNNING  # not yet succeeded
    resp = TestClient(app).get(f"/jobs/{job.job_id}/results/ranked")
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"]


def test_download_unknown_job_returns_404() -> None:
    resp = TestClient(_app()).get(f"/jobs/{uuid.uuid4()}/results/ranked")
    assert resp.status_code == 404


def test_delete_evicts_job_then_404() -> None:
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client)
        assert client.get(f"/jobs/{job_id}/results/ranked").status_code == 200

        deleted = client.delete(f"/jobs/{job_id}")
        assert deleted.status_code == 204
        # After discard, both status and result fetches 404; a second delete is also 404.
        assert client.get(f"/jobs/{job_id}").status_code == 404
        assert client.get(f"/jobs/{job_id}/results/ranked").status_code == 404
        assert client.delete(f"/jobs/{job_id}").status_code == 404


# --------------------------------------------------------------------------------------------
# Background TTL sweeper
# --------------------------------------------------------------------------------------------


async def test_sweeper_evicts_expired_job() -> None:
    reg = JobRegistry(ttl_seconds=0)  # ttl=0 → every job is immediately expired
    job = reg.create()
    job.state = JobState.SUCCEEDED
    job.finished_at = time.monotonic() - 1

    task = asyncio.create_task(sweeper_loop(reg, 0.01))
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if reg.get(job.job_id) is None:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert reg.get(job.job_id) is None
