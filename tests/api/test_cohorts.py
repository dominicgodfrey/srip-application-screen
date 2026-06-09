"""Cohort endpoint tests (Phase 11.4). FastAPI ``TestClient``, synthetic data, no API spend.

Covers both entry points: ``POST /jobs/{id}/cohorts`` (chained off a completed job, fabricated
directly in the registry) and ``POST /cohorts`` (a re-uploaded ``decisions.jsonl``), plus the
edge behavior: capacity query params, the ``?format=csv`` download, 404/409 lifecycle codes, and
graceful 413/422 on malformed uploads (never a 500, no applicant content echoed).
"""

from __future__ import annotations

import uuid

from api.main import create_app
from api.registry import JobState
from fastapi.testclient import TestClient

from srip_filter.config import ApiConfig, AppConfig
from srip_filter.ingest import IngestReport
from srip_filter.llm.client import FakeLLMClient
from srip_filter.models import AuditRecord, ProgramChoices
from srip_filter.outputs import (
    build_summary,
    decisions_jsonl,
    needs_review_csv,
    ranked_csv,
    rejected_csv,
)
from srip_filter.pipeline import BatchResult


def _rec(
    sid: str,
    rank: int | None,
    *tiers: str,
    outcome: str = "RANKED",
) -> AuditRecord:
    slots = [f"Summer 2026- {tier.upper()}" for tier in tiers] + [None, None, None]
    return AuditRecord(
        submission_id=sid,
        name=f"Student {sid}",
        outcome=outcome,  # type: ignore[arg-type]
        rank=rank,
        final_score=None if rank is None else 200.0 - rank,
        program_choices=ProgramChoices(first=slots[0], second=slots[1], third=slots[2]),
    )


_RECORDS = [
    _rec("s1", 1, "honors", "intensive"),
    _rec("s2", 2, "honors"),
    _rec("s3", 3, "regular"),
    _rec("rej", None, "honors", outcome="REJECTED"),
]


def _app(cfg: AppConfig | None = None):
    cfg = cfg or AppConfig()
    return create_app(config=cfg, client=FakeLLMClient(cfg))


def _batch_result(records: list[AuditRecord]) -> BatchResult:
    return BatchResult(
        records=records,
        decisions_jsonl=decisions_jsonl(records),
        ranked_csv=ranked_csv(records),
        rejected_csv=rejected_csv(records),
        needs_review_csv=needs_review_csv(records),
        summary=build_summary(records),
        ingest_report=IngestReport(
            total_rows_read=len(records),
            kept_count=len(records),
            identity_dropped=[],
            duplicate_email_dropped=[],
            duplicate_name_flagged=0,
            unrecognized_headers=(),
            missing_optional_roles=(),
        ),
    )


def _succeeded_job(app, records: list[AuditRecord]) -> str:
    """Fabricate a completed job in the registry and return its id."""
    job = app.state.registry.create()
    job.state = JobState.SUCCEEDED
    job.result = _batch_result(records)
    return job.job_id


def _jsonl_upload(records: list[AuditRecord]) -> dict:
    return {"file": ("decisions.jsonl", decisions_jsonl(records).encode("utf-8"))}


# --------------------------------------------------------------------------------------------
# POST /jobs/{id}/cohorts — chained off a completed job
# --------------------------------------------------------------------------------------------


def test_job_cohorts_unbounded_everyone_first_choice() -> None:
    app = _app()
    job_id = _succeeded_job(app, _RECORDS)
    resp = TestClient(app).post(f"/jobs/{job_id}/cohorts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total_ranked"] == 3  # REJECTED never enters the pool
    assert body["summary"]["assigned"] == 3
    assert {a["submission_id"]: a["assigned_tier"] for a in body["assignments"]} == {
        "s1": "honors",
        "s2": "honors",
        "s3": "regular",
    }
    assert all(a["choice_number"] == 1 for a in body["assignments"])


def test_job_cohorts_capacity_params_bind() -> None:
    app = _app()
    job_id = _succeeded_job(app, _RECORDS)
    resp = TestClient(app).post(f"/jobs/{job_id}/cohorts", params={"honors": 1})

    assert resp.status_code == 200
    body = resp.json()
    tiers = {a["submission_id"]: a["assigned_tier"] for a in body["assignments"]}
    # s2 is honors-only: the displacement chain moves flexible s1 to intensive.
    assert tiers == {"s1": "intensive", "s2": "honors", "s3": "regular"}
    assert body["summary"]["displaced"] == 1
    assert body["summary"]["tiers"]["honors"]["open_seats"] == 0


def test_job_cohorts_is_non_evicting_for_what_if_iteration() -> None:
    app = _app()
    job_id = _succeeded_job(app, _RECORDS)
    with TestClient(app) as client:
        first = client.post(f"/jobs/{job_id}/cohorts", params={"honors": 1})
        second = client.post(f"/jobs/{job_id}/cohorts", params={"honors": 2})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["summary"]["displaced"] == 0  # honors=2 seats both honors-listers


def test_job_cohorts_unknown_job_404() -> None:
    resp = TestClient(_app()).post(f"/jobs/{uuid.uuid4()}/cohorts")
    assert resp.status_code == 404


def test_job_cohorts_before_done_409() -> None:
    app = _app()
    job = app.state.registry.create()
    job.state = JobState.RUNNING
    resp = TestClient(app).post(f"/jobs/{job.job_id}/cohorts")
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"]


def test_job_cohorts_negative_capacity_422() -> None:
    app = _app()
    job_id = _succeeded_job(app, _RECORDS)
    resp = TestClient(app).post(f"/jobs/{job_id}/cohorts", params={"honors": -1})
    assert resp.status_code == 422


def test_job_cohorts_bad_format_422() -> None:
    app = _app()
    job_id = _succeeded_job(app, _RECORDS)
    resp = TestClient(app).post(f"/jobs/{job_id}/cohorts", params={"format": "xml"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------------------------
# POST /cohorts — re-uploaded decisions.jsonl (the durable entry point)
# --------------------------------------------------------------------------------------------


def test_upload_cohorts_round_trips_decisions_jsonl() -> None:
    resp = TestClient(_app()).post(
        "/cohorts", params={"honors": 1}, files=_jsonl_upload(_RECORDS)
    )
    assert resp.status_code == 200
    body = resp.json()
    tiers = {a["submission_id"]: a["assigned_tier"] for a in body["assignments"]}
    assert tiers == {"s1": "intensive", "s2": "honors", "s3": "regular"}
    assert body["summary"]["total_ranked"] == 3


def test_upload_cohorts_csv_format_download() -> None:
    resp = TestClient(_app()).post(
        "/cohorts", params={"format": "csv"}, files=_jsonl_upload(_RECORDS)
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert (
        resp.headers["content-disposition"] == 'attachment; filename="cohort_assignments.csv"'
    )
    lines = resp.text.strip().split("\n")
    assert lines[0].startswith("rank,submission_id,name,final_score,status,assigned_tier")
    assert len(lines) == 1 + 3  # one row per RANKED record


def test_upload_cohorts_garbage_line_422_names_line_not_content() -> None:
    payload = decisions_jsonl(_RECORDS[:1]) + '{"this": "is not an audit record"}\n'
    resp = TestClient(_app()).post(
        "/cohorts", files={"file": ("decisions.jsonl", payload.encode("utf-8"))}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Line 2" in detail
    assert "audit record" in detail
    assert "Student" not in detail  # no applicant content echoed


def test_upload_cohorts_empty_file_422() -> None:
    resp = TestClient(_app()).post("/cohorts", files={"file": ("decisions.jsonl", b"")})
    assert resp.status_code == 422


def test_upload_cohorts_not_utf8_422() -> None:
    resp = TestClient(_app()).post(
        "/cohorts", files={"file": ("decisions.jsonl", b"\xff\xfe\x00garbage")}
    )
    assert resp.status_code == 422


def test_upload_cohorts_row_cap_413() -> None:
    cfg = AppConfig(api=ApiConfig(max_rows=2))
    resp = TestClient(_app(cfg)).post("/cohorts", files=_jsonl_upload(_RECORDS))
    assert resp.status_code == 413
