"""API tests for the manual promote-to-RANKED endpoint (PRD §10.2 human-resolution path).

Synthetic data only; `FakeLLMClient` + the demo handler, so zero API spend.
"""

from __future__ import annotations

import csv
import io

from api.demo import demo_handler
from api.main import create_app
from api.registry import JobState
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

_GOOD_ESSAY = (
    "Software engineering motivates me because it turns abstract ideas into tools that people "
    "can actually use, and I find that deeply rewarding. Over the past two years I taught myself "
    "Python and JavaScript, built a small web application for my school newspaper, and helped "
    "organize a coding club for younger students who had never written a single line of code "
    "before joining us. I am applying to this track because I want a rigorous foundation in "
    "computer science and the chance to work alongside mentors who care about doing careful, "
    "honest research that matters. In the long run I hope to study how machine learning systems "
    "can be made more transparent, reliable, and genuinely fair for the communities that "
    "increasingly depend on them every day."
)


def _row(sid: str, *, essay1: str | None = None) -> dict[str, str]:
    return {
        _HEADERS[0]: sid,
        _HEADERS[1]: "Ann",
        _HEADERS[2]: "Lee",
        _HEADERS[3]: f"{sid}@example.com",
        _HEADERS[4]: "3.8",
        _HEADERS[5]: essay1 if essay1 is not None else _GOOD_ESSAY,
        _HEADERS[6]: _GOOD_ESSAY + " I also enjoy collaborating with curious teammates.",
        _HEADERS[7]: "I affirm this is truthful.",
    }


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _app():
    cfg = AppConfig()
    return create_app(config=cfg, client=FakeLLMClient(cfg, demo_handler))


def _run_to_completion(client: TestClient, rows: list[dict[str, str]]) -> str:
    created = client.post("/jobs", files={"file": ("apps.csv", _csv_bytes(rows))})
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    for _ in range(200):
        if client.get(f"/jobs/{job_id}").json()["state"] == JobState.SUCCEEDED:
            break
    return job_id


def test_promote_rejected_applicant_into_ranking() -> None:
    rows = [_row("s-good"), _row("s-short", essay1="too short")]  # second REJECTED at Stage 1
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, rows)
        resp = client.post(f"/jobs/{job_id}/records/s-short/promote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["record"]["outcome"] == "RANKED"
        assert body["record"]["manual_override"] is True
        assert body["record"]["rank"] is not None
        assert body["summary"]["counts"] == {
            "total": 2,
            "RANKED": 2,
            "REJECTED": 0,
            "NEEDS_REVIEW": 0,
        }
        # The job's artifacts were rebuilt: the decisions download reflects the promotion.
        decisions = client.get(f"/jobs/{job_id}/results/decisions").text
        assert '"manual_override":true' in decisions


def test_promote_unknown_submission_404() -> None:
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, [_row("s-good")])
        resp = client.post(f"/jobs/{job_id}/records/nope/promote")
        assert resp.status_code == 404


def test_promote_already_ranked_409() -> None:
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, [_row("s-good")])
        resp = client.post(f"/jobs/{job_id}/records/s-good/promote")
        assert resp.status_code == 409


def test_promote_unknown_job_404() -> None:
    with TestClient(_app()) as client:
        resp = client.post("/jobs/no-such-job/records/x/promote")
        assert resp.status_code == 404


# --------------------------------------------------------------------------------------------
# Demote — the mirror override: RANKED → REJECTED, no LLM spend
# --------------------------------------------------------------------------------------------


def test_demote_ranked_applicant_out_of_ranking() -> None:
    rows = [_row("s-good"), _row("s-good-2")]
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, rows)
        resp = client.post(f"/jobs/{job_id}/records/s-good/demote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["record"]["outcome"] == "REJECTED"
        assert body["record"]["manual_override"] is True
        assert body["record"]["rank"] is None
        assert body["record"]["final_score"] is None
        assert "human reviewer" in body["record"]["primary_reason"]
        assert body["summary"]["counts"] == {
            "total": 2,
            "RANKED": 1,
            "REJECTED": 1,
            "NEEDS_REVIEW": 0,
        }
        # The survivor moved up and the artifacts were rebuilt.
        ranked = client.get(f"/jobs/{job_id}/results/ranked").text
        assert "s-good-2" in ranked
        assert "s-good," not in ranked
        rejected = client.get(f"/jobs/{job_id}/results/rejected").text
        assert "s-good" in rejected


def test_demote_then_promote_back_is_reversible() -> None:
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, [_row("s-good")])
        assert client.post(f"/jobs/{job_id}/records/s-good/demote").status_code == 200
        resp = client.post(f"/jobs/{job_id}/records/s-good/promote")
        assert resp.status_code == 200
        assert resp.json()["record"]["outcome"] == "RANKED"


def test_demote_not_ranked_409() -> None:
    rows = [_row("s-good"), _row("s-short", essay1="too short")]  # second REJECTED at Stage 1
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, rows)
        resp = client.post(f"/jobs/{job_id}/records/s-short/demote")
        assert resp.status_code == 409


def test_demote_unknown_submission_404() -> None:
    with TestClient(_app()) as client:
        job_id = _run_to_completion(client, [_row("s-good")])
        resp = client.post(f"/jobs/{job_id}/records/nope/demote")
        assert resp.status_code == 404


def test_demote_unknown_job_404() -> None:
    with TestClient(_app()) as client:
        resp = client.post("/jobs/no-such-job/records/x/demote")
        assert resp.status_code == 404
