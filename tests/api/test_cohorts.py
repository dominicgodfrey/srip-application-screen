"""``POST /cohorts`` tests. FastAPI ``TestClient``, synthetic data, no API spend.

The offline/durable entry point: cohort assignment from a re-uploaded ``decisions.jsonl``.
Covers capacity query params, the ``?format=csv`` download and per-tier rosters, and graceful
413/422 on malformed uploads (never a 500, no applicant content echoed). The live-DB twin
(``POST /api/cohorts``) is covered in ``test_admin_api.py``.
"""

from __future__ import annotations

from api.main import create_app
from fastapi.testclient import TestClient

from srip_filter.config import ApiConfig, AppConfig
from srip_filter.llm.client import FakeLLMClient
from srip_filter.models import AuditRecord, ProgramChoices
from srip_filter.outputs import decisions_jsonl


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


def _jsonl_upload(records: list[AuditRecord]) -> dict:
    return {"file": ("decisions.jsonl", decisions_jsonl(records).encode("utf-8"))}


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
    assert tiers == {"s1": "honors", "s3": "regular"}
    assert [w["submission_id"] for w in body["waitlist"]] == ["s2"]
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
    assert lines[0].startswith("assigned_tier,rank,submission_id,name,email,phone")
    assert len(lines) == 1 + 3  # one row per RANKED record


def test_upload_cohorts_tier_roster_csv_download() -> None:
    resp = TestClient(_app()).post(
        "/cohorts", params={"format": "csv", "tier": "honors"}, files=_jsonl_upload(_RECORDS)
    )
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="cohort_honors.csv"'
    lines = resp.text.strip().split("\n")
    assert lines[0] == "rank,submission_id,name,email,phone,final_score"
    # honors members only (s1, s2 by rank); s3 chose regular, "rej" never enters the pool
    assert [line.split(",")[1] for line in lines[1:]] == ["s1", "s2"]


def test_upload_cohorts_unknown_tier_422() -> None:
    resp = TestClient(_app()).post(
        "/cohorts", params={"format": "csv", "tier": "platinum"}, files=_jsonl_upload(_RECORDS)
    )
    assert resp.status_code == 422
    assert "Unknown tier" in resp.json()["detail"]


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
