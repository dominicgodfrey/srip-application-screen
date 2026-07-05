"""P6a — DB-backed admin API tests (list/detail, promote/demote, delete, exports, what-if).

The store boundary is an in-memory fake (same pattern as the worker tests): SKIP LOCKED
and upsert semantics were proven in test_db.py; here we prove the endpoints' behavior on
top of them — read-time per-cohort ranks, override persistence with decided_by events,
and honest 404/409s. Auth is bypassed by the conftest fixture; the barrier itself is
proven in test_auth.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from api.main import create_app
from fastapi.testclient import TestClient

from api import admin_api as admin_mod
from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient
from srip_filter.models import TaskDOutput, TaskFOutput

_WORDS_150 = " ".join(f"idea{i}" for i in range(150))


def _essays_payload(sid: str) -> dict:
    return {
        "ats_mode": "essays",
        "submission_id": sid,
        "user_email": "syn@example.com",
        "student_name": "Syn Thetic",
        "cohort_name": "su26-cs",
        "gpa": {"unweighted": "3.9 / 4.0", "weighted": None},
        "required_essays": [
            {"question": "Why?", "answer": _WORDS_150, "min_words": 100, "max_words": 350},
            {"question": "Future?", "answer": _WORDS_150 + " more",
             "min_words": 100, "max_words": 350},
        ],
        "optional_essays": [],
    }


def _audit(sid: str, *, outcome: str, score: float | None, cohort: str = "su26-cs") -> dict:
    return {
        "submission_id": sid,
        "name": "Syn Thetic",
        "email": "syn@example.com",
        "cohort_name": cohort,
        "outcome": outcome,
        "final_score": score,
        "primary_reason": "Survived all gates" if outcome == "RANKED" else "some gate",
        "scores": {"gpa_points": 30.0, "essay": {"e1": 10, "e2": 10, "total": 20.0}},
    }


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[tuple[str, str | None, dict | None]] = []

    def add(self, sid: str, **overrides) -> dict:
        row = {
            "submission_id": sid,
            "cohort_name": "su26-cs",
            "user_email": "syn@example.com",
            "student_name": "Syn Thetic",
            "sub_track": "cs",
            "status": "graded",
            "essays_payload": _essays_payload(sid),
            "resume_payload": None,
            "audit_record": None,
            "submitted_at": datetime(2026, 7, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 7, 2, tzinfo=UTC),
        }
        row.update(overrides)
        self.rows[sid] = row
        return row

    async def list_applications(self, pool, *, cohort_name=None):
        rows = list(self.rows.values())
        if cohort_name is not None:
            rows = [r for r in rows if r["cohort_name"] == cohort_name]
        return rows

    async def get_application(self, pool, sid):
        return self.rows.get(sid)

    async def finish_graded(self, pool, sid, *, audit_record, outcome, final_score):
        row = self.rows[sid]
        row["audit_record"] = audit_record
        row["status"] = "graded"

    async def delete_submission(self, pool, sid):
        return self.rows.pop(sid, None) is not None

    async def add_event(self, pool, kind, *, submission_id=None, details=None):
        self.events.append((kind, submission_id, details))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    for name in (
        "list_applications", "get_application", "finish_graded",
        "delete_submission", "add_event",
    ):
        monkeypatch.setattr(admin_mod.dbmod, name, getattr(s, name))
    return s


def _handler(task, user, schema):  # type: ignore[no-untyped-def]
    if task == "task_d":
        return TaskDOutput(
            is_gibberish=False, on_topic=True, relevance_confidence=0.9,
            quality_score=12, grammar_spelling_penalty=0, saliency_notes="", rationale="",
        )
    if task == "task_f":
        return TaskFOutput(on_topic=True, gibberish=False, technical_depth_0_10=5,
                           exploration_level_0_10=5, impact_0_10=5, rationale="")
    raise AssertionError(f"unexpected task {task}")


@pytest.fixture
def client() -> TestClient:
    cfg = AppConfig()
    app = create_app(
        config=cfg,
        client=FakeLLMClient(cfg, handler=_handler),
        db_pool=object(),  # sentinel; store functions are monkeypatched
        webhook_secrets=("s",),
    )
    return TestClient(app)


# ------------------------------------------------------------------------------------------------
# Listing + read-time ranks
# ------------------------------------------------------------------------------------------------


def test_listing_assigns_per_cohort_ranks(client: TestClient, store: _FakeStore) -> None:
    a, b, c, d = (str(uuid.uuid4()) for _ in range(4))
    store.add(a, audit_record=_audit(a, outcome="RANKED", score=120.0))
    store.add(b, audit_record=_audit(b, outcome="RANKED", score=140.0))
    # Different cohort: its own rank sequence starting at 1.
    store.add(c, audit_record=_audit(c, outcome="RANKED", score=90.0, cohort="su27-cs"),
              cohort_name="su27-cs")
    store.add(d, audit_record=_audit(d, outcome="REJECTED", score=None))

    body = client.get("/api/applications").json()
    by_id = {e["submission_id"]: e for e in body["applications"]}
    assert by_id[b]["rank"] == 1 and by_id[a]["rank"] == 2  # su26 by score desc
    assert by_id[c]["rank"] == 1  # su27 ranks independently
    assert by_id[d]["rank"] is None and by_id[d]["outcome"] == "REJECTED"
    assert body["counts"]["RANKED"] == 3 and body["counts"]["REJECTED"] == 1
    assert body["cohorts"] == ["su26-cs", "su27-cs"]


def test_listing_includes_ungraded_rows_by_status(client: TestClient,
                                                  store: _FakeStore) -> None:
    sid = str(uuid.uuid4())
    store.add(sid, status="received", audit_record=None)
    body = client.get("/api/applications").json()
    entry = body["applications"][0]
    assert entry["status"] == "received" and entry["outcome"] is None
    assert body["counts"]["received"] == 1


def test_detail_404_and_full_record(client: TestClient, store: _FakeStore) -> None:
    assert client.get(f"/api/applications/{uuid.uuid4()}").status_code == 404
    sid = str(uuid.uuid4())
    store.add(sid, audit_record=_audit(sid, outcome="RANKED", score=100.0))
    body = client.get(f"/api/applications/{sid}").json()
    assert body["audit_record"]["outcome"] == "RANKED"
    assert body["audit_record"]["rank"] == 1  # read-time rank present on detail too
    assert body["has_essays_payload"] is True


# ------------------------------------------------------------------------------------------------
# Promote / demote
# ------------------------------------------------------------------------------------------------


def test_promote_rescores_with_gates_bypassed_and_tombstones(
    client: TestClient, store: _FakeStore
) -> None:
    sid = str(uuid.uuid4())
    store.add(sid, audit_record=_audit(sid, outcome="REJECTED", score=None))
    body = client.post(f"/api/applications/{sid}/promote").json()
    record = body["record"]
    assert record["outcome"] == "RANKED"
    assert record["manual_override"] is True
    assert record["final_score"] is not None and record["final_score"] > 0
    # Persisted + decided_by tombstoned.
    assert store.rows[sid]["audit_record"]["outcome"] == "RANKED"
    kinds = [(k, d) for k, _, d in store.events]
    assert ("manual_promote", {"decided_by": "admin",
                               "final_score": record["final_score"]}) in kinds


def test_promote_conflicts(client: TestClient, store: _FakeStore) -> None:
    ranked = str(uuid.uuid4())
    store.add(ranked, audit_record=_audit(ranked, outcome="RANKED", score=100.0))
    assert client.post(f"/api/applications/{ranked}/promote").status_code == 409
    ungraded = str(uuid.uuid4())
    store.add(ungraded, status="received", audit_record=None)
    assert client.post(f"/api/applications/{ungraded}/promote").status_code == 409
    resume_only = str(uuid.uuid4())
    store.add(resume_only, essays_payload=None,
              audit_record=_audit(resume_only, outcome="NEEDS_REVIEW", score=None))
    assert client.post(f"/api/applications/{resume_only}/promote").status_code == 409


def test_demote_is_deterministic_and_reversible_shape(
    client: TestClient, store: _FakeStore
) -> None:
    sid = str(uuid.uuid4())
    store.add(sid, audit_record=_audit(sid, outcome="RANKED", score=100.0))
    body = client.post(f"/api/applications/{sid}/demote").json()
    record = body["record"]
    assert record["outcome"] == "REJECTED"
    assert record["manual_override"] is True
    assert record["final_score"] is None
    assert store.rows[sid]["audit_record"]["outcome"] == "REJECTED"
    assert any(k == "manual_demote" and d == {"decided_by": "admin"}
               for k, _, d in store.events)
    # Subscores survive on the record (reversible via promote).
    assert record["scores"]["gpa_points"] == 30.0
    # Demoting again: no longer ranked -> 409.
    assert client.post(f"/api/applications/{sid}/demote").status_code == 409


def test_demote_requires_ranked(client: TestClient, store: _FakeStore) -> None:
    sid = str(uuid.uuid4())
    store.add(sid, audit_record=_audit(sid, outcome="NEEDS_REVIEW", score=None))
    assert client.post(f"/api/applications/{sid}/demote").status_code == 409


# ------------------------------------------------------------------------------------------------
# Delete + exports + summary
# ------------------------------------------------------------------------------------------------


def test_delete_204_then_404(client: TestClient, store: _FakeStore) -> None:
    sid = str(uuid.uuid4())
    store.add(sid)
    assert client.delete(f"/api/applications/{sid}").status_code == 204
    assert sid not in store.rows
    assert client.delete(f"/api/applications/{sid}").status_code == 404


def test_exports_serve_live_artifacts(client: TestClient, store: _FakeStore) -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    store.add(a, audit_record=_audit(a, outcome="RANKED", score=120.0))
    store.add(b, audit_record=_audit(b, outcome="REJECTED", score=None))

    decisions = client.get("/api/exports/decisions")
    assert decisions.status_code == 200
    assert decisions.headers["content-disposition"].endswith('"decisions.jsonl"')
    lines = [ln for ln in decisions.text.splitlines() if ln.strip()]
    assert len(lines) == 2

    ranked = client.get("/api/exports/ranked")
    assert a in ranked.text and b not in ranked.text

    summary = client.get("/api/summary").json()
    assert summary["counts"]["RANKED"] == 1
    assert summary["ungraded"] == 0


def test_exports_scope_by_cohort(client: TestClient, store: _FakeStore) -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    store.add(a, audit_record=_audit(a, outcome="RANKED", score=120.0))
    store.add(b, cohort_name="su27-cs",
              audit_record=_audit(b, outcome="RANKED", score=90.0, cohort="su27-cs"))
    ranked = client.get("/api/exports/ranked", params={"cohort": "su27-cs"})
    assert b in ranked.text and a not in ranked.text


def test_cohort_whatif_runs_over_live_ranking(client: TestClient,
                                              store: _FakeStore) -> None:
    sid = str(uuid.uuid4())
    audit = _audit(sid, outcome="RANKED", score=120.0)
    audit["program_choices"] = {"first": "Summer 2026 - INTENSIVE", "second": None,
                                "third": None}
    store.add(sid, audit_record=audit)
    body = client.post("/api/cohorts", params={"intensive": 5}).json()
    assert body["summary"]["assigned"] == 1
    assert body["summary"]["total_ranked"] == 1
