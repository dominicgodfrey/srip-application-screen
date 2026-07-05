"""P4 — v3 webhook pipeline end-to-end tests (PRD v3 §4 + §10 invariants, zero spend).

Drives ``grade_webhook_applicant`` / ``make_grade_fn`` with synthetic payloads and a
scripted ``FakeLLMClient``. The §10 invariants this file owns: (1) essay-3/coursework/
school absence is neutral, (2) no bonus rescues a rejection, (3) every REJECTED names its
gate, plus the v3 essay-model rules (profanity in the optional essay rejects; gibberish/
off-topic there only zeroes the bonus; strict exact word bounds).
"""

from __future__ import annotations

import uuid

import pytest

from srip_filter.config import AppConfig
from srip_filter.ingest_webhook import map_essays_payload
from srip_filter.llm.client import FakeLLMClient
from srip_filter.models import EssaysModePayload, TaskCOutput, TaskDOutput, TaskFOutput
from srip_filter.pipeline import grade_webhook_applicant, make_grade_fn

APP = AppConfig()

_WORDS_150 = " ".join(["insight"] * 150)
_TECH_ESSAY = " ".join(["project"] * 200)


def _payload_dict(**overrides) -> dict:
    base = {
        "ats_mode": "essays",
        "submission_id": str(uuid.uuid4()),
        "user_email": "syn@example.com",
        "student_name": "Syn Thetic",
        "cohort_name": "su26-cs",
        "gpa": {"unweighted": "3.8 / 4.0", "weighted": None},
        "gpa_explanation": "",
        "relevant_coursework": "",
        "institution": "High School",
        "state_of_residence": "California",
        "required_essays": [
            {"question": "Why apply?", "answer": _WORDS_150,
             "min_words": 100, "max_words": 350},
            {"question": "Research future?", "answer": _WORDS_150 + " indeed",
             "min_words": 100, "max_words": 350},
        ],
        "optional_essays": [],
    }
    base.update(overrides)
    return base


def _applicant(**overrides):
    return map_essays_payload(EssaysModePayload.model_validate(_payload_dict(**overrides)))


def _task_d(*, on_topic: bool = True, gibberish: bool = False) -> TaskDOutput:
    return TaskDOutput(
        is_gibberish=gibberish,
        on_topic=on_topic,
        relevance_confidence=0.9,
        quality_score=13,
        grammar_spelling_penalty=0,
        saliency_notes="",
        rationale="",
    )


def _task_f(*, on_topic: bool = True, gibberish: bool = False) -> TaskFOutput:
    return TaskFOutput(
        on_topic=on_topic,
        gibberish=gibberish,
        technical_depth_0_10=8,
        exploration_level_0_10=6,
        impact_0_10=4,
        rationale="",
    )


def _handler(task, user, schema):  # type: ignore[no-untyped-def]
    if task == "task_d":
        return _task_d()
    if task == "task_f":
        return _task_f()
    if task == "task_c":
        return TaskCOutput(courses=[], rationale="")
    raise AssertionError(f"unexpected task {task}")


def _client(handler=_handler) -> FakeLLMClient:
    return FakeLLMClient(APP, handler=handler)


# ------------------------------------------------------------------------------------------------
# Survivor path + composition
# ------------------------------------------------------------------------------------------------


async def test_survivor_ranked_with_composed_score_and_metadata() -> None:
    optional = [{"question": "Tech topic?", "answer": _TECH_ESSAY, "max_words": 500}]
    rec = await grade_webhook_applicant(
        _applicant(optional_essays=optional), _client(), APP
    )
    assert rec.outcome == "RANKED"
    assert rec.cohort_name == "su26-cs"
    assert rec.international is False
    gpa_expected = 40 * (3.8 - 3.3) / (4.0 - 3.3)
    assert rec.scores.gpa_points == pytest.approx(gpa_expected, abs=1e-3)
    assert rec.scores.essay.total == pytest.approx(26.0)  # 13 + 13
    assert rec.scores.technical_essay_bonus == pytest.approx(20 * (8 + 6 + 4) / 30)
    # v3: the worker stores the composed score immediately; rank stays read-time.
    assert rec.final_score == pytest.approx(
        gpa_expected + 26.0 + rec.scores.technical_essay_bonus, abs=1e-3
    )
    assert rec.rank is None
    assert "task_f" in rec.llm_calls


async def test_essay3_absence_is_neutral_and_free() -> None:
    """Invariant #1 for the new bonus: no essay 3 ⇒ same required-signal total, no Task F."""
    client = _client()
    rec = await grade_webhook_applicant(_applicant(), client, APP)
    assert rec.outcome == "RANKED"
    assert rec.scores.technical_essay_bonus == 0.0
    assert rec.technical_essay.skipped_reason == "absent"
    assert rec.final_score == pytest.approx(
        rec.scores.gpa_points + rec.scores.essay.total, abs=1e-6
    )
    assert all(call[0] != "task_f" for call in client.calls)


# ------------------------------------------------------------------------------------------------
# Strict word bounds (v3 Stage 1)
# ------------------------------------------------------------------------------------------------


async def test_required_essay_outside_exact_bounds_rejects_as_contract_drift() -> None:
    client = _client()
    short = {"question": "Why apply?", "answer": "ninety nine words missing",
             "min_words": 100, "max_words": 350}
    good = {"question": "Research future?", "answer": _WORDS_150,
            "min_words": 100, "max_words": 350}
    rec = await grade_webhook_applicant(
        _applicant(required_essays=[short, good]), client, APP
    )
    assert rec.outcome == "REJECTED"
    assert rec.decided_at_stage == "stage1"
    assert "tampering or contract drift" in rec.primary_reason
    assert client.calls == []  # zero tokens past a Stage-1 reject


async def test_essay_without_bounds_gets_no_length_check() -> None:
    no_bounds = {"question": "Why apply?", "answer": "short but unbounded"}
    good = {"question": "Research future?", "answer": _WORDS_150}
    rec = await grade_webhook_applicant(
        _applicant(required_essays=[no_bounds, good]), _client(), APP
    )
    assert rec.outcome == "RANKED"  # no bounds delivered -> no length gate


async def test_exact_boundary_words_pass() -> None:
    # Varied tokens so the gibberish unique-word-ratio heuristic (correctly) stays quiet.
    exactly_100 = " ".join(f"word{i}" for i in range(100))
    exactly_350 = " ".join(f"term{i}" for i in range(350))
    rec = await grade_webhook_applicant(
        _applicant(required_essays=[
            {"question": "Q1", "answer": exactly_100, "min_words": 100, "max_words": 350},
            {"question": "Q2", "answer": exactly_350, "min_words": 100, "max_words": 350},
        ]),
        _client(),
        APP,
    )
    assert rec.outcome == "RANKED"  # strict means exact: min and max are inclusive


# ------------------------------------------------------------------------------------------------
# Optional-essay gate semantics (owner decisions, 2026-07-04)
# ------------------------------------------------------------------------------------------------


async def test_profanity_in_optional_essay_rejects_whole_application() -> None:
    client = _client()
    optional = [{"question": "Tech?", "answer": "this fucking compiler " + _TECH_ESSAY,
                 "max_words": 500}]
    rec = await grade_webhook_applicant(_applicant(optional_essays=optional), client, APP)
    assert rec.outcome == "REJECTED"
    assert rec.decided_at_stage == "stage1"
    assert "profanity" in rec.primary_reason.lower()
    assert any(t.startswith("e3:") for t in rec.gates.profanity.terms)
    assert client.calls == []


async def test_gibberish_optional_essay_zeroes_bonus_never_rejects() -> None:
    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_f":
            return _task_f(gibberish=True)
        return _handler(task, user, schema)

    optional = [{"question": "Tech?", "answer": _TECH_ESSAY, "max_words": 500}]
    rec = await grade_webhook_applicant(
        _applicant(optional_essays=optional), _client(handler), APP
    )
    assert rec.outcome == "RANKED"  # never a rejection from the bonus signal
    assert rec.scores.technical_essay_bonus == 0.0


async def test_optional_essay_over_max_voids_bonus_only() -> None:
    client = _client()
    optional = [{"question": "Tech?", "answer": " ".join(["word"] * 501), "max_words": 500}]
    rec = await grade_webhook_applicant(_applicant(optional_essays=optional), client, APP)
    assert rec.outcome == "RANKED"
    assert rec.scores.technical_essay_bonus == 0.0
    assert rec.technical_essay.over_max is True
    assert all(call[0] != "task_f" for call in client.calls)  # voided without a token


# ------------------------------------------------------------------------------------------------
# GPA routing (structured input)
# ------------------------------------------------------------------------------------------------


async def test_weighted_only_gpa_routes_to_task_a_not_fraction_math() -> None:
    from srip_filter.models import TaskAOutput

    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_a":
            return TaskAOutput(
                normalized_gpa=3.6,
                original_scale="weighted_5",
                conversion_method="llm_estimate",
                confidence="med",
                requires_manual_review=False,
                rationale="",
            )
        return _handler(task, user, schema)

    client = _client(handler)
    rec = await grade_webhook_applicant(
        _applicant(gpa={"unweighted": None, "weighted": "4.4 / 5.0"}), client, APP
    )
    assert rec.outcome == "RANKED"
    assert "task_a" in rec.llm_calls  # NOT the deterministic /5 path (would be 3.52)
    assert rec.gpa.normalized_gpa == pytest.approx(3.6)


# ------------------------------------------------------------------------------------------------
# make_grade_fn (worker seam)
# ------------------------------------------------------------------------------------------------


async def test_grade_fn_maps_db_row_to_result() -> None:
    payload = _payload_dict()
    grade_fn = make_grade_fn(_client(), APP)
    result = await grade_fn(
        {"submission_id": payload["submission_id"], "essays_payload": payload,
         "resume_payload": None}
    )
    assert result.outcome == "RANKED"
    assert result.final_score is not None and result.final_score > 0
    assert result.audit_record["submission_id"] == payload["submission_id"]
    assert result.audit_record["cohort_name"] == "su26-cs"


async def test_grade_fn_resume_only_row_is_needs_review_not_rejected() -> None:
    grade_fn = make_grade_fn(_client(), APP)
    result = await grade_fn(
        {"submission_id": str(uuid.uuid4()), "essays_payload": None,
         "resume_payload": {"ats_mode": "resume", "submission_id": str(uuid.uuid4())},
         "student_name": "Syn", "user_email": "s@e.com", "cohort_name": "su26-cs"}
    )
    assert result.outcome == "NEEDS_REVIEW"  # never silently rejected
    assert result.final_score is None
    assert "essays not yet received" in result.audit_record["primary_reason"]


async def test_missing_required_essays_needs_review_before_any_gate() -> None:
    client = _client()
    rec = await grade_webhook_applicant(
        _applicant(required_essays=[{"question": "only one", "answer": _WORDS_150}]),
        client,
        APP,
    )
    assert rec.outcome == "NEEDS_REVIEW"
    assert "contract drift" in rec.primary_reason
    assert client.calls == []
