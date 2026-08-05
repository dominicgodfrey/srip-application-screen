"""PRD v3 §10 invariants, tested against the pipeline that actually runs.

These lived in ``tests/test_pipeline.py`` and exercised ``grade_one``/``grade_batch`` — the
v2 CSV batch path, which no route could reach. Deleting that code would have deleted the
invariant coverage with it, so the assertions were ported here onto
``grade_webhook_applicant``. That is where they belonged anyway: an invariant about what
the service decides is only worth anything if it is asserted about the live decision path.

Invariants owned here:

  #1 no optional-signal absence (essay 3, coursework, school, resume) reduces final_score
  #2 no bonus changes a REJECTED outcome
  #3 every REJECTED record names its gate in primary_reason
  #4 GPA below 3.3 yields points only via an approved Task B, and never above the bottom
  #5 ranking is deterministic and stable across reruns
  #6 nothing unscoreable is ever REJECTED

(#7 unauthenticated writes, #8 idempotent re-delivery and #9 per-row isolation are
transport/queue concerns and stay in tests/api/test_webhook.py and tests/test_worker.py.)

Zero API spend: every model call goes through a scripted FakeLLMClient.
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.ingest_webhook import map_application_payload
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.models import (
    ApplicationPayload,
    AuditRecord,
    CourseItem,
    TaskAOutput,
    TaskBOutput,
    TaskCOutput,
    TaskDOutput,
    TaskEOutput,
    TaskFOutput,
)
from srip_filter.pipeline import grade_webhook_applicant
from srip_filter.scoring.aggregate import assign_read_time_ranks
from tests.live_payload import make_payload

APP = AppConfig()

_WORDS_150 = " ".join(["insight"] * 150)
_TECH_ESSAY = " ".join(["project"] * 200)
# >= 2 deterministic signals (repeat run + low entropy), which is what the gate requires.
_GIBBERISH = "aaaaaaaaaa " * 30


def _payload_dict(**overrides) -> dict:
    answers = {
        "institution": "High School",
        "relevant_coursework": None,
        "gpa_explanation": None,
        "essay_motivation": _WORDS_150,
        "essay_trajectory": _WORDS_150 + " indeed",
        "essay_research": None,
    }
    answers.update(overrides.pop("answers", {}))
    base = make_payload(
        answers=answers,
        gpa_unweighted=overrides.pop("gpa_unweighted", "3.8 / 4.0"),
        gpa_weighted=overrides.pop("gpa_weighted", None),
    )
    base.update(overrides)
    return base


def _applicant(**overrides):
    return map_application_payload(
        ApplicationPayload.model_validate(_payload_dict(**overrides))
    )


def _task_d(*, on_topic: bool = True, gibberish: bool = False, quality: int = 13) -> TaskDOutput:
    return TaskDOutput(
        is_gibberish=gibberish,
        on_topic=on_topic,
        relevance_confidence=0.9,
        quality_score=quality,
        grammar_spelling_penalty=0,
        saliency_notes="",
        rationale="",
    )


def _task_f() -> TaskFOutput:
    return TaskFOutput(
        on_topic=True,
        gibberish=False,
        technical_depth_0_10=8,
        exploration_level_0_10=6,
        impact_0_10=4,
        rationale="",
    )


def _handler(task, user, schema):  # type: ignore[no-untyped-def]
    """Everything on-topic and gradeable — the survivor baseline."""
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
# #1 — no optional-signal absence ever reduces final_score
# ------------------------------------------------------------------------------------------------


async def test_inv1_every_optional_signal_absent_scores_no_worse_than_present() -> None:
    """Absence is neutral, never a deduction — for each optional signal and for all at once."""
    rich = await grade_webhook_applicant(
        _applicant(
            optional_essays=[{"question": "Tech topic?", "answer": _TECH_ESSAY}],
            answers={"institution": "Stanford University", "relevant_coursework": "CS101"},
        ),
        _client(
            lambda task, user, schema: (
                TaskCOutput(
                    courses=[
                        CourseItem(
                            name="CS101",
                            raw_grade="A",
                            grade_pct=95.0,
                            category="cs",
                            weight=1.0,
                            counts=True,
                        )
                    ],
                    rationale="",
                )
                if task == "task_c"
                else _handler(task, user, schema)
            )
        ),
        APP,
    )
    bare = await grade_webhook_applicant(_applicant(), _client(), APP)

    assert rich.outcome == bare.outcome == "RANKED"
    assert bare.final_score is not None and rich.final_score is not None
    # Every bonus absent must land at exactly the required core, never below it.
    assert bare.final_score == pytest.approx(
        bare.scores.gpa_points + bare.scores.essay.total, abs=1e-6
    )
    assert bare.final_score <= rich.final_score
    assert (
        bare.scores.technical_essay_bonus
        == bare.scores.coursework_bonus
        == bare.scores.school_bonus
        == bare.scores.resume_bonus
        == 0.0
    )


async def test_inv1_no_bonus_is_ever_negative() -> None:
    """The floor, stated directly: a bonus subtracting would be invariant #1 violated."""
    for optional in ([], [{"question": "Tech topic?", "answer": _GIBBERISH}]):
        rec = await grade_webhook_applicant(
            _applicant(optional_essays=optional), _client(), APP
        )
        scores = rec.scores
        assert min(
            scores.technical_essay_bonus,
            scores.coursework_bonus,
            scores.school_bonus,
            scores.resume_bonus,
        ) >= 0.0


# ------------------------------------------------------------------------------------------------
# #2 / #3 — bonuses never touch a rejection, and a rejection always names its gate
# ------------------------------------------------------------------------------------------------


async def _rejected_by_gibberish() -> AuditRecord:
    return await grade_webhook_applicant(
        _applicant(
            answers={"essay_motivation": _GIBBERISH},
            optional_essays=[{"question": "Tech topic?", "answer": _TECH_ESSAY}],
        ),
        _client(),
        APP,
    )


async def test_inv2_a_strong_bonus_profile_never_rescues_a_rejection() -> None:
    rec = await _rejected_by_gibberish()
    assert rec.outcome == "REJECTED"
    assert rec.final_score is None and rec.rank is None
    # Stage 1 stops the row, so no bonus stage even ran — the cheapest possible proof.
    assert rec.scores.technical_essay_bonus == 0.0
    assert rec.llm_calls == []


async def test_inv2_an_off_topic_rejection_after_bonuses_would_still_score_none() -> None:
    """A rejection decided at Stage 4 — past where bonuses could have accumulated."""
    rec = await grade_webhook_applicant(
        _applicant(optional_essays=[{"question": "Tech topic?", "answer": _TECH_ESSAY}]),
        _client(
            lambda task, user, schema: (
                _task_d(on_topic=False) if task == "task_d" else _handler(task, user, schema)
            )
        ),
        APP,
    )
    assert rec.outcome == "REJECTED"
    assert rec.final_score is None and rec.rank is None


@pytest.mark.parametrize(
    "case",
    ["gibberish", "off_topic", "gpa_below_floor", "gpa_blank_and_unexplained", "task_b_rejects"],
)
async def test_inv3_every_rejection_names_its_gate(case: str) -> None:
    """A REJECTED record that cannot say which gate decided it is not auditable."""
    if case == "gibberish":
        rec = await _rejected_by_gibberish()
    elif case == "off_topic":
        rec = await grade_webhook_applicant(
            _applicant(),
            _client(
                lambda t, u, s: _task_d(on_topic=False) if t == "task_d" else _handler(t, u, s)
            ),
            APP,
        )
    elif case == "gpa_below_floor":
        rec = await grade_webhook_applicant(
            _applicant(gpa_unweighted="1.4 / 4.0"), _client(), APP
        )
    elif case == "gpa_blank_and_unexplained":
        rec = await grade_webhook_applicant(_applicant(gpa_unweighted=""), _client(), APP)
    else:
        rec = await grade_webhook_applicant(
            _applicant(
                gpa_unweighted="3.0 / 4.0",
                answers={"gpa_explanation": "I was unwell for a term."},
            ),
            _client(
                lambda t, u, s: TaskBOutput(
                    explanation_adequate=False,
                    strength_of_reason=0.1,
                    realistic=True,
                    severity_vs_reason_balanced=False,
                    recommended_outcome="reject",
                    rationale="",
                )
                if t == "task_b"
                else _handler(t, u, s)
            ),
            APP,
        )

    assert rec.outcome == "REJECTED", case
    assert rec.primary_reason.strip(), case
    assert rec.decided_at_stage, case


# ------------------------------------------------------------------------------------------------
# #4 — sub-threshold GPA
# ------------------------------------------------------------------------------------------------


async def test_inv4_below_threshold_needs_task_b_approval_and_lands_at_the_bottom() -> None:
    approved = await grade_webhook_applicant(
        _applicant(
            gpa_unweighted="3.0 / 4.0",
            answers={"gpa_explanation": "Family illness during junior year."},
        ),
        _client(
            lambda t, u, s: TaskBOutput(
                explanation_adequate=True,
                strength_of_reason=0.9,
                realistic=True,
                severity_vs_reason_balanced=True,
                recommended_outcome="rank",
                rationale="",
            )
            if t == "task_b"
            else _handler(t, u, s)
        ),
        APP,
    )
    assert approved.outcome == "RANKED"
    # The deficit is reflected, never erased: bottom of the gradient, and never negative.
    assert approved.scores.gpa_points == 0.0
    assert "task_b" in approved.llm_calls


async def test_inv4_below_threshold_without_an_explanation_is_rejected_unscored() -> None:
    rec = await grade_webhook_applicant(_applicant(gpa_unweighted="3.0 / 4.0"), _client(), APP)
    assert rec.outcome == "REJECTED"
    assert rec.scores.gpa_points == 0.0 and rec.final_score is None


async def test_inv4_the_hard_floor_takes_precedence_over_any_explanation() -> None:
    """Below 2.0 no explanation can rescue — and Task B must not even be called."""
    called: list[str] = []

    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        called.append(task)
        return _handler(task, user, schema)

    rec = await grade_webhook_applicant(
        _applicant(
            gpa_unweighted="1.5 / 4.0",
            answers={"gpa_explanation": "A very compelling and detailed explanation."},
        ),
        _client(handler),
        APP,
    )
    assert rec.outcome == "REJECTED"
    assert "task_b" not in called  # zero token spend past the floor


# ------------------------------------------------------------------------------------------------
# #5 — deterministic, stable ranking
# ------------------------------------------------------------------------------------------------


async def test_inv5_ranking_is_stable_across_reruns_and_input_order() -> None:
    """Same population ⇒ same ranks, regardless of the order they are handed over."""

    async def _score(sid: str, quality: int) -> AuditRecord:
        def handler(t, u, s):  # type: ignore[no-untyped-def]
            return _task_d(quality=quality) if t == "task_d" else _handler(t, u, s)

        return await grade_webhook_applicant(
            _applicant(submission_id=sid), _client(handler), APP
        )

    a = await _score("11111111-1111-4111-8111-111111111111", 15)
    b = await _score("22222222-2222-4222-8222-222222222222", 11)
    c = await _score("33333333-3333-4333-8333-333333333333", 13)

    def ranks(records):
        assign_read_time_ranks(records)
        return {r.submission_id: r.rank for r in records}

    first = ranks([a, b, c])
    again = ranks([a, b, c])          # rerun: idempotent
    reordered = ranks([c, a, b])      # different input order: same answer

    assert first == again == reordered
    assert first[a.submission_id] == 1 and first[b.submission_id] == 3


async def test_inv5_ties_break_deterministically_not_by_arrival() -> None:
    """Identical scores must still produce one stable order (gpa → essays → submission_id)."""

    async def _score(sid: str) -> AuditRecord:
        return await grade_webhook_applicant(_applicant(submission_id=sid), _client(), APP)

    x = await _score("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    y = await _score("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert x.final_score == y.final_score  # genuinely tied

    assign_read_time_ranks([x, y])
    forward = (x.rank, y.rank)
    assign_read_time_ranks([y, x])
    assert (x.rank, y.rank) == forward


# ------------------------------------------------------------------------------------------------
# #6 — unscoreable is never a rejection
# ------------------------------------------------------------------------------------------------


async def test_inv6_an_unresolvable_gpa_is_reviewed_never_rejected() -> None:
    rec = await grade_webhook_applicant(
        _applicant(
            gpa_unweighted="my school does not issue GPAs",
            answers={"gpa_explanation": "We are graded by narrative report."},
        ),
        _client(
            lambda t, u, s: TaskAOutput(
                normalized_gpa=None,
                original_scale="unknown",
                conversion_method="none",
                confidence="low",
                requires_manual_review=True,
                rationale="",
            )
            if t == "task_a"
            else _handler(t, u, s)
        ),
        APP,
    )
    assert rec.outcome == "NEEDS_REVIEW"


async def test_inv6_a_required_signal_parse_failure_is_reviewed_never_rejected() -> None:
    """A terminal Task D failure is our problem, not evidence against the applicant."""

    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_d":
            raise LLMParseFailure("task_d", "unparseable")
        return _handler(task, user, schema)

    rec = await grade_webhook_applicant(_applicant(), _client(handler), APP)
    assert rec.outcome == "NEEDS_REVIEW"


async def test_inv6_an_unexpected_crash_is_reviewed_never_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-row isolation: an error nobody anticipated lands as NEEDS_REVIEW with a note.

    Raised from the school stage rather than the model handler on purpose — an LLM failure
    is *expected* and each stage catches its own, so it would prove the stage handler works
    rather than the outer net. This is the net: an ordinary bug in ordinary code.
    """
    import srip_filter.pipeline as pipeline_mod

    def boom(row, cfg):  # type: ignore[no-untyped-def]
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(pipeline_mod, "score_school", boom)

    rec = await grade_webhook_applicant(_applicant(), _client(), APP)
    assert rec.outcome == "NEEDS_REVIEW"
    assert rec.errors and "RuntimeError" in rec.errors[-1]


async def test_inv6_a_bonus_signal_parse_failure_only_zeroes_that_bonus() -> None:
    """The other half of the split: an optional signal failing must not reach the outcome."""

    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task in ("task_c", "task_f"):
            raise LLMParseFailure(task, "unparseable")
        return _handler(task, user, schema)

    rec = await grade_webhook_applicant(
        _applicant(
            optional_essays=[{"question": "Tech topic?", "answer": _TECH_ESSAY}],
            answers={"relevant_coursework": "CS101, Calculus"},
        ),
        _client(handler),
        APP,
    )
    assert rec.outcome == "RANKED"
    assert rec.scores.technical_essay_bonus == 0.0
    assert rec.scores.coursework_bonus == 0.0


# ------------------------------------------------------------------------------------------------
# Fail-fast spend discipline (PRD §0.1) — the reason the gate order exists
# ------------------------------------------------------------------------------------------------


async def test_a_stage1_rejection_spends_zero_tokens() -> None:
    client = _client()
    rec = await grade_webhook_applicant(
        _applicant(answers={"essay_motivation": _GIBBERISH}), client, APP
    )
    assert rec.outcome == "REJECTED"
    assert client.calls == []


async def test_the_resume_kill_switch_costs_no_fetch_and_no_token() -> None:
    """resume.bonus_max = 0 (the shipping default) must be a complete no-op."""
    assert APP.resume.bonus_max == 0
    client = _client()
    rec = await grade_webhook_applicant(
        _applicant(resume_url="https://example.invalid/resume.pdf"), client, APP
    )
    assert rec.outcome == "RANKED"
    assert rec.scores.resume_bonus == 0.0
    assert "task_e" not in rec.llm_calls
    assert not any(task == "task_e" for task, _ in client.calls)


async def test_a_resume_failure_keeps_the_applicant_ranked() -> None:
    """Bonus-only means a broken resume is an audit note, never a block (stage enabled)."""
    cfg = APP.model_copy(update={"resume": APP.resume.model_copy(update={"bonus_max": 25.0})})

    class _DeadFetcher:
        async def fetch(self, url):  # type: ignore[no-untyped-def]
            from srip_filter.resume_fetch import FAIL_NETWORK, FetchResult

            return FetchResult(ok=False, content=b"", failure=FAIL_NETWORK)

    rec = await grade_webhook_applicant(
        _applicant(resume_url="https://example.invalid/resume.pdf"),
        FakeLLMClient(cfg, handler=_handler),
        cfg,
        _DeadFetcher(),  # type: ignore[arg-type]
    )
    assert rec.outcome == "RANKED"
    assert rec.scores.resume_bonus == 0.0
    assert any("resume" in note for note in rec.errors)


async def test_records_carry_essay_text_for_the_audit_ui() -> None:
    """Highlight-on-reject needs the text on the record, for rejections above all."""
    rec = await _rejected_by_gibberish()
    assert rec.essays is not None
    assert rec.essays.e1.strip() == _GIBBERISH.strip()


async def test_a_scored_resume_prices_signals_from_config() -> None:
    """The enabled path still works end to end — the stage is dormant, not broken."""
    cfg = APP.model_copy(update={"resume": APP.resume.model_copy(update={"bonus_max": 25.0})})

    class _Fetcher:
        async def fetch(self, url):  # type: ignore[no-untyped-def]
            from srip_filter.resume_fetch import FetchResult

            return FetchResult(ok=True, content=b"%PDF-1.4 synthetic", failure="")

    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_e":
            return TaskEOutput(
                is_resume=True,
                relevant_projects=2,
                relevant_experience=1,
                relevant_awards=0,
                skills_relevance=0.5,
                highlights="",
                rationale="",
            )
        return _handler(task, user, schema)

    import srip_filter.scoring.resume as resume_mod

    class _Extracted:
        ok, text, failure = True, "synthetic resume text", ""

    original = resume_mod.extract_resume_text
    resume_mod.extract_resume_text = lambda content, cfg: _Extracted()  # type: ignore[assignment]
    try:
        rec = await grade_webhook_applicant(
            _applicant(resume_url="https://example.invalid/resume.pdf"),
            FakeLLMClient(cfg, handler=handler),
            cfg,
            _Fetcher(),  # type: ignore[arg-type]
        )
    finally:
        resume_mod.extract_resume_text = original  # type: ignore[assignment]

    expected = 3.75 * 2 + 5.0 * 1 + 2.5 * 0 + 5.0 * 0.5
    assert rec.outcome == "RANKED"
    assert rec.scores.resume_bonus == pytest.approx(expected)
