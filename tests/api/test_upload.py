"""Upload + validation + kickoff tests (Phase 9.2).

Drives ``POST /jobs`` through the FastAPI ``TestClient`` with an injected ``FakeLLMClient`` (no
API spend). Asserts the HTTP contract at the edge: a clean CSV → 202 + a registered job; oversize
body → 413; row cap exceeded → 413; bad headers / unreadable CSV → 422; missing client → 503.
Job *completion* (the background run finishing) is Phase 9.3's concern — here we only check that a
job is scheduled.
"""

from __future__ import annotations

import csv
import io

from api.main import create_app
from api.registry import JobState
from fastapi.testclient import TestClient

from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient

# Minimal header set that resolves every required role (+ affirmation) per the §2 contract.
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


def _row(sid: str) -> dict[str, str]:
    return {
        "Submission ID": sid,
        "Student First Name": "Ann",
        "Student Last Name": "Lee",
        "What is your email address?": f"{sid}@example.com",
        "GPA": "3.8",
        _HEADERS[5]: "essay one text",
        _HEADERS[6]: "essay two text",
        _HEADERS[7]: "I affirm this is truthful.",
    }


def _csv_bytes(rows: list[dict[str, str]], *, headers: list[str] = _HEADERS) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buf.getvalue().encode("utf-8")


def _cfg(**api_overrides) -> AppConfig:
    base = AppConfig()
    return base.model_copy(update={"api": base.api.model_copy(update=api_overrides)})


def _client(cfg: AppConfig | None = None) -> TestClient:
    cfg = cfg or AppConfig()
    app = create_app(config=cfg, client=FakeLLMClient(cfg))
    return TestClient(app)


def _post(client: TestClient, raw: bytes, *, filename: str = "apps.csv"):
    return client.post("/jobs", files={"file": (filename, raw, "text/csv")})


# --------------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------------


def test_good_csv_returns_202_and_registers_job() -> None:
    app = create_app(config=AppConfig(), client=FakeLLMClient(AppConfig()))
    with TestClient(app) as client:
        resp = _post(client, _csv_bytes([_row("a"), _row("b")]))

    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == JobState.QUEUED
    job = app.state.registry.get(body["job_id"])
    assert job is not None  # a real job was scheduled


# --------------------------------------------------------------------------------------------
# Size + row caps → 413
# --------------------------------------------------------------------------------------------


def test_oversize_upload_returns_413() -> None:
    raw = _csv_bytes([_row("a")])
    client = _client(_cfg(max_upload_bytes=len(raw) - 1))
    resp = _post(client, raw)
    assert resp.status_code == 413
    assert "maximum size" in resp.json()["detail"]


def test_too_many_rows_returns_413() -> None:
    client = _client(_cfg(max_rows=1))
    resp = _post(client, _csv_bytes([_row("a"), _row("b")]))
    assert resp.status_code == 413
    assert "rows" in resp.json()["detail"]


# --------------------------------------------------------------------------------------------
# Unprocessable input → 422
# --------------------------------------------------------------------------------------------


def test_missing_required_header_returns_422() -> None:
    headers = [h for h in _HEADERS if h != "GPA"]  # drop a required column
    raw = _csv_bytes([_row("a")], headers=headers)
    resp = _post(_client(), raw)
    assert resp.status_code == 422
    assert "data contract" in resp.json()["detail"]


def test_empty_file_returns_422() -> None:
    resp = _post(_client(), b"")
    assert resp.status_code == 422
    assert "CSV" in resp.json()["detail"]


# --------------------------------------------------------------------------------------------
# Misconfiguration → 503
# --------------------------------------------------------------------------------------------


def test_missing_client_returns_503() -> None:
    # No injected client and lifespan not entered (no `with`), so app.state.llm_client stays None.
    client = TestClient(create_app(config=AppConfig(), client=None))
    resp = _post(client, _csv_bytes([_row("a")]))
    assert resp.status_code == 503
