"""Webhook pipeline end-to-end tests (PRD v3 §4), driven with synthetic payloads and a
scripted ``FakeLLMClient``. Zero spend.

Owns the essay-model rules: profanity anywhere stops the application, gibberish or off-topic
in the optional essay only zeroes its bonus, and no length rule rejects anyone.
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.ingest_webhook import map_application_payload
from srip_filter.llm.client import FakeLLMClient
from srip_filter.models import ApplicationPayload, TaskCOutput, TaskDOutput, TaskFOutput
from srip_filter.pipeline import grade_webhook_applicant, make_grade_fn
from tests.live_payload import make_payload

APP = AppConfig()

_WORDS_150 = " ".join(["insight"] * 150)
_TECH_ESSAY = " ".join(["project"] * 200)


def _payload_dict(**overrides) -> dict:
    """Live-shaped payload with two gradeable required essays and no optional essay."""
    base = make_payload(
        answers={
            "institution": "High School",
            "relevant_coursework": None,
            "gpa_explanation": None,
            "essay_motivation": _WORDS_150,
            "essay_trajectory": _WORDS_150 + " indeed",
            "essay_research": None,
        },
        gpa_unweighted="3.8 / 4.0",
        gpa_weighted=None,
    )
    base.update(overrides)
    return base


def _applicant(**overrides):
    return map_application_payload(
        ApplicationPayload.model_validate(_payload_dict(**overrides))
    )


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


# --- Survivor path + composition ---


async def test_survivor_ranked_with_composed_score_and_metadata() -> None:
    optional = [{"question": "Tech topic?", "answer": _TECH_ESSAY}]
    rec = await grade_webhook_applicant(
        _applicant(optional_essays=optional), _client(), APP
    )
    assert rec.outcome == "RANKED"
    assert rec.cohort_name == "SP27-CSE"
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


# --- Word bounds retired (owner, 2026-07-28) — the site server-validates them at submit ---


async def test_length_never_gates_a_required_essay() -> None:
    """A too-short and a too-long required essay both still grade — no length rejection.

    The site returns 400 at submit on any bounds violation, so a violation can only reach
    us from our own stale config; rejecting a real applicant for that is exactly what
    "never silently reject" forbids.
    """
    for answer in ("three words only", " ".join(f"word{i}" for i in range(900))):
        client = _client()
        rec = await grade_webhook_applicant(
            _applicant(required_essays=[
                {"question": "Why apply?", "answer": answer},
                {"question": "Research future?", "answer": _WORDS_150},
            ]),
            client,
            APP,
        )
        assert rec.outcome == "RANKED"
        assert rec.gates.essay_length.hard_fail is False
        assert client.calls  # graded for real, not short-circuited


# --- Optional-essay gate semantics (owner decisions, 2026-07-04) ---


async def test_profanity_in_optional_essay_flags_whole_application_for_review() -> None:
    """Profanity anywhere still stops the application — but as NEEDS_REVIEW, not REJECTED
    (owner, 2026-07-29): the gate is a word list, so a human confirms every flag. Still
    token-free, and still scoped to the whole application even from the optional essay."""
    client = _client()
    optional = [{"question": "Tech?", "answer": "this fucking compiler " + _TECH_ESSAY}]
    rec = await grade_webhook_applicant(_applicant(optional_essays=optional), client, APP)
    assert rec.outcome == "NEEDS_REVIEW"
    assert rec.decided_at_stage == "stage1"
    assert "profanity" in rec.primary_reason.lower()
    assert any(t.startswith("e3:") for t in rec.gates.profanity.terms)
    assert rec.final_score is None
    assert client.calls == []


async def test_profanity_in_a_required_essay_also_only_needs_review() -> None:
    client = _client()
    rec = await grade_webhook_applicant(
        _applicant(
            required_essays=[
                {"question": "Why?", "answer": "this fucking compiler " + _WORDS_150},
                {"question": "How?", "answer": _WORDS_150},
            ]
        ),
        client,
        APP,
    )
    assert rec.outcome == "NEEDS_REVIEW"
    assert rec.gates.profanity.hit
    assert client.calls == []  # still fail-fast: no tokens spent on a flagged row


async def test_gibberish_still_rejects_outright() -> None:
    """Only profanity was softened. Gibberish stays a deterministic rejection — it is a
    positive finding about the text itself, not a word-list guess about intent."""
    mash = "asdfasdf " * 40
    rec = await grade_webhook_applicant(
        _applicant(
            required_essays=[
                {"question": "Why?", "answer": mash},
                {"question": "How?", "answer": mash + "qq"},
            ]
        ),
        _client(),
        APP,
    )
    assert rec.outcome == "REJECTED"
    assert "gibberish" in rec.primary_reason.lower()


async def test_when_profanity_and_gibberish_both_fire_the_rejection_wins() -> None:
    """PRD §0.7: a definite verdict outranks a review, and primary_reason must name the
    gate that actually decided — otherwise a REJECTED record cites a non-rejecting gate."""
    mash = "asdfasdf " * 40 + " fucking "
    rec = await grade_webhook_applicant(
        _applicant(
            required_essays=[
                {"question": "Why?", "answer": mash},
                {"question": "How?", "answer": mash + "qq"},
            ]
        ),
        _client(),
        APP,
    )
    assert rec.outcome == "REJECTED"
    assert "gibberish" in rec.primary_reason.lower()
    assert rec.gates.profanity.hit and rec.gates.gibberish.hit


async def test_promoting_a_profanity_flag_scores_the_application() -> None:
    """The resolution path for a false positive: a reviewer promotes and the row scores,
    with the bypassed flag preserved in the audit trail."""
    optional = [{"question": "Tech?", "answer": "this fucking compiler " + _TECH_ESSAY}]
    rec = await grade_webhook_applicant(
        _applicant(optional_essays=optional), _client(), APP, bypass_gates=True
    )
    assert rec.outcome == "RANKED"
    assert rec.manual_override is True
    assert any("profanity flag bypassed" in r for r in rec.reasons)
    assert rec.gates.profanity.hit  # verdict still recorded, just not terminal


async def test_gibberish_optional_essay_zeroes_bonus_never_rejects() -> None:
    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_f":
            return _task_f(gibberish=True)
        return _handler(task, user, schema)

    optional = [{"question": "Tech?", "answer": _TECH_ESSAY}]
    rec = await grade_webhook_applicant(
        _applicant(optional_essays=optional), _client(handler), APP
    )
    assert rec.outcome == "RANKED"  # never a rejection from the bonus signal
    assert rec.scores.technical_essay_bonus == 0.0


# --- GPA routing (structured input) ---


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
        _applicant(gpa_unweighted=None, gpa_weighted="4.4 / 5.0"), client, APP
    )
    assert rec.outcome == "RANKED"
    assert "task_a" in rec.llm_calls  # NOT the deterministic /5 path (would be 3.52)
    assert rec.gpa.normalized_gpa == pytest.approx(3.6)


# --- make_grade_fn (worker seam) ---


async def test_grade_fn_maps_db_row_to_result() -> None:
    payload = _payload_dict()
    grade_fn = make_grade_fn(_client(), APP)
    result = await grade_fn({"submission_id": payload["submission_id"], "payload": payload})
    assert result.outcome == "RANKED"
    assert result.final_score is not None and result.final_score > 0
    assert result.audit_record["submission_id"] == payload["submission_id"]
    assert result.audit_record["cohort_name"] == "SP27-CSE"


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
