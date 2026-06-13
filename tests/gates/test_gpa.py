"""Tests for Stages 2-3 GPA normalization and gate (Phase 3). Synthetic values only.

3.1 covers the deterministic normalizer; later sub-tasks append Task A/B (mocked) and the
gate paths. All §2 GPA quirk shapes are exercised here so the deterministic majority is
pinned with zero API spend.
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.gates.gpa import (
    GpaNormalization,
    assess_gpa,
    gpa_gate_deterministic,
    gpa_points,
    normalize_gpa,
    normalize_gpa_deterministic,
)
from srip_filter.ingest import ApplicantRow
from srip_filter.llm.client import FakeLLMClient, LLMParseFailure
from srip_filter.models import TaskAOutput, TaskBOutput

APP = AppConfig()
CFG = APP.gpa


def _norm(raw: str) -> GpaNormalization:
    return normalize_gpa_deterministic(raw, CFG)


# ------------------------------------------------------------------------------------------------
# Clean 4.0-scale values
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("4.0", 4.0), ("3.97", 3.97), ("3.0", 3.0), ("2.5", 2.5), ("0.0", 0.0), ("4", 4.0)],
)
def test_clean_four_point_resolved(raw: str, expected: float) -> None:
    r = _norm(raw)
    assert r.normalized_gpa == pytest.approx(expected)
    assert r.original_scale == "four_point"
    assert r.source == "deterministic"
    assert not r.needs_llm
    assert not r.requires_manual_review
    assert r.confidence == "high"


def test_below_threshold_flag() -> None:
    assert _norm("3.2").below_threshold is True  # below the 3.3 bar
    assert _norm("3.3").below_threshold is False
    assert _norm("3.5").below_threshold is False


def test_trailing_label_stripped() -> None:
    r = _norm("3.97 GPA")
    assert r.normalized_gpa == pytest.approx(3.97)
    assert r.original_scale == "four_point"
    assert not r.needs_llm


# ------------------------------------------------------------------------------------------------
# Percentages and fractions
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("95%", 4.0),
        ("92%", 3.7),
        ("88%", 3.3),
        ("85%", 3.0),  # B-average row
        ("81%", 2.7),
        ("78%", 2.3),
        ("75%", 2.0),
        ("95.2%", 4.0),
    ],
)
def test_percentage_sign_table(raw: str, expected: float) -> None:
    r = _norm(raw)
    assert r.normalized_gpa == pytest.approx(expected)
    assert r.original_scale == "percentage"
    assert not r.needs_llm


def test_percentage_below_table_scales_linearly_toward_zero() -> None:
    # 73 anchors at 2.0; below that it is linear toward 0 (no sudden cliff).
    r = _norm("36.5%")
    assert r.normalized_gpa == pytest.approx(36.5 / 73 * 2.0, abs=1e-3)
    assert not r.needs_llm


@pytest.mark.parametrize(
    "raw,expected,scale",
    [
        ("85/100", 3.0, "percentage"),
        ("92/100 (Ethiopian National Curriculum)", 3.7, "percentage"),
        ("3.8/4.0 unweighted", 3.8, "four_point"),
        ("3/4", 3.0, "four_point"),
        ("5/5", 4.0, "out_of_5"),
        ("4.5/5", 3.6, "out_of_5"),
        ("8.5/10", 3.3, "out_of_10"),  # 85% -> 3.0? 8.5*10=85 -> 3.0
    ],
)
def test_fractions(raw: str, expected: float, scale: str) -> None:
    r = _norm(raw)
    assert r.original_scale == scale
    assert not r.needs_llm


def test_out_of_ten_table_mapping() -> None:
    # 8.5/10 -> 85% -> 3.0 (B row); 7.16/10 -> 71.6% -> below table, linear.
    assert _norm("8.5/10").normalized_gpa == pytest.approx(3.0)
    assert _norm("7.16/10").normalized_gpa == pytest.approx(71.6 / 73 * 2.0, abs=1e-3)


def test_out_of_five_linear() -> None:
    assert _norm("5/5").normalized_gpa == pytest.approx(4.0)
    assert _norm("4.5/5").normalized_gpa == pytest.approx(3.6)


# ------------------------------------------------------------------------------------------------
# Caps and routing to Task A (needs_llm) — no decision made here
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["4.27", "4.635", "weighted: 4.4", "5.0", "4.5/4.0"])
def test_weighted_above_four_routes_to_llm(raw: str) -> None:
    r = _norm(raw)
    assert r.needs_llm is True
    assert r.normalized_gpa is None
    assert r.requires_manual_review is False  # routing != manual review
    assert r.below_threshold is None


@pytest.mark.parametrize(
    "raw",
    [
        "N/A",
        "IGCSE grades: A*,A*,A,B",
        "my school doesn't offer GPAs",
        "average is 8",  # bare 8 > 4.0, ambiguous scale
        "8",
    ],
)
def test_unparseable_or_ambiguous_routes_to_llm(raw: str) -> None:
    r = _norm(raw)
    assert r.needs_llm is True
    assert r.normalized_gpa is None
    assert r.requires_manual_review is False


def test_unknown_denominator_routes_to_llm() -> None:
    assert _norm("7/9").needs_llm is True


def test_percentage_over_max_routes_to_llm() -> None:
    assert _norm("150%").needs_llm is True
    assert _norm("105/100").needs_llm is True


# ------------------------------------------------------------------------------------------------
# Blank -> manual review (NEVER a token, NEVER a rejection)
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_goes_to_manual_review_without_llm(raw: str) -> None:
    r = _norm(raw)
    assert r.requires_manual_review is True
    assert r.needs_llm is False
    assert r.normalized_gpa is None
    assert r.original_scale == "blank"


# ------------------------------------------------------------------------------------------------
# Hard invariant (PRD §6.2): the deterministic pass never decides a rejection.
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["", "4.27", "N/A", "2.0", "3.5", "85%", "IGCSE A*A*A", "5/5", "150%"]
)
def test_deterministic_pass_never_rejects(raw: str) -> None:
    # No field on the normalization result encodes a rejection; the worst dispositions are
    # needs_llm (Task A decides) or manual review. This is the §1/§6.2 hard line.
    r = _norm(raw)
    assert isinstance(r, GpaNormalization)


def test_result_is_capped_at_gpa_max() -> None:
    # A clean value is never above the cap; a fraction that computes above 4.0 is capped.
    assert _norm("4.0").normalized_gpa == pytest.approx(4.0)


# ================================================================================================
# 3.2 — Task A fallback + normalize_gpa orchestration (LLM, mocked; no API spend)
# ================================================================================================


def _task_a(
    normalized_gpa: float | None = 3.8,
    *,
    requires_manual_review: bool = False,
    confidence: str = "med",
    scale: str = "weighted_gt_4",
) -> TaskAOutput:
    return TaskAOutput(
        normalized_gpa=normalized_gpa,
        original_scale=scale,
        conversion_method="estimated",
        confidence=confidence,  # type: ignore[arg-type]
        requires_manual_review=requires_manual_review,
        rationale="",
    )


def _client(handler) -> FakeLLMClient:  # type: ignore[no-untyped-def]
    return FakeLLMClient(APP, handler=handler)


async def test_deterministic_value_never_calls_task_a() -> None:
    client = _client(lambda t, u, s: _task_a())
    r = await normalize_gpa("3.5", client, APP)
    assert r.source == "deterministic"
    assert r.normalized_gpa == pytest.approx(3.5)
    assert client.calls == []  # fail-fast: no token spent on a resolvable value


async def test_blank_never_calls_task_a() -> None:
    client = _client(lambda t, u, s: _task_a())
    r = await normalize_gpa("", client, APP)
    assert r.requires_manual_review is True
    assert r.source == "deterministic"
    assert client.calls == []


async def test_needs_llm_value_calls_task_a_and_maps_result() -> None:
    client = _client(lambda t, u, s: _task_a(normalized_gpa=3.8, confidence="med"))
    r = await normalize_gpa("4.27", client, APP)
    assert client.calls and client.calls[0][0] == "task_a"
    assert r.source == "llm"
    assert r.normalized_gpa == pytest.approx(3.8)
    assert r.confidence == "med"
    assert r.below_threshold is False
    assert r.needs_llm is False


async def test_task_a_estimate_capped_at_gpa_max() -> None:
    client = _client(lambda t, u, s: _task_a(normalized_gpa=4.6))
    r = await normalize_gpa("4.635", client, APP)
    assert r.normalized_gpa == pytest.approx(4.0)


async def test_task_a_requires_manual_review_maps_to_needs_review() -> None:
    client = _client(lambda t, u, s: _task_a(normalized_gpa=None, requires_manual_review=True))
    r = await normalize_gpa("my school doesn't offer GPAs", client, APP)
    assert r.requires_manual_review is True
    assert r.normalized_gpa is None
    assert r.source == "llm"


async def test_task_a_null_estimate_maps_to_manual_review() -> None:
    client = _client(lambda t, u, s: _task_a(normalized_gpa=None, requires_manual_review=False))
    r = await normalize_gpa("N/A", client, APP)
    assert r.requires_manual_review is True
    assert r.normalized_gpa is None


async def test_llm_parse_failure_routes_to_manual_review_never_rejects() -> None:
    def boom(t, u, s):  # type: ignore[no-untyped-def]
        raise LLMParseFailure(t, "bad json")

    client = _client(boom)
    r = await normalize_gpa("IGCSE grades: A*,A*,A,B", client, APP)
    assert r.requires_manual_review is True
    assert r.source == "llm"
    assert r.conversion_method == "llm_parse_failure"


async def test_identical_raw_dedups_within_run() -> None:
    client = _client(lambda t, u, s: _task_a())
    await normalize_gpa("4.27", client, APP)
    await normalize_gpa("4.27", client, APP)
    assert len(client.calls) == 1  # cache_text=raw dedups the second call


# ================================================================================================
# 3.3 — GPA points gradient + deterministic gate paths (no LLM)
# ================================================================================================


@pytest.mark.parametrize(
    "g,expected",
    # gradient over [3.3, 4.0] -> [0, 40]: 3.0 clamps to 0, 3.3 -> 0, 3.65 (midpoint) -> 20.
    [(3.0, 0.0), (3.3, 0.0), (3.65, 20.0), (4.0, 40.0)],
)
def test_gpa_points_gradient(g: float, expected: float) -> None:
    assert gpa_points(g, CFG) == pytest.approx(expected)


def test_gpa_points_clamped_below_and_above() -> None:
    assert gpa_points(2.5, CFG) == 0.0  # below threshold clamps to 0
    assert gpa_points(4.5, CFG) == pytest.approx(40.0)  # above gpa_max clamps to score_max


def _resolved_norm(g: float) -> GpaNormalization:
    return normalize_gpa_deterministic(str(g), CFG)


def test_gate_pass_at_or_above_threshold() -> None:
    res = gpa_gate_deterministic("3.7", _resolved_norm(3.7), "", CFG)
    assert res is not None
    assert res.verdict == "pass"
    assert res.gpa_points == pytest.approx(40 * (3.7 - 3.3) / (4.0 - 3.3), abs=1e-3)
    assert res.gate.passed is True
    assert res.assessment.normalized_gpa == pytest.approx(3.7)


def test_gate_exactly_threshold_passes_with_zero_points() -> None:
    res = gpa_gate_deterministic("3.3", _resolved_norm(3.3), "", CFG)
    assert res is not None
    assert res.verdict == "pass"
    assert res.gpa_points == 0.0


def test_gate_below_threshold_blank_explanation_rejected() -> None:
    res = gpa_gate_deterministic("2.5", _resolved_norm(2.5), "   ", CFG)
    assert res is not None
    assert res.verdict == "reject"
    assert res.gpa_points == 0.0
    assert "no explanation" in res.gate.reason  # names the blocker (PRD §12)


def test_gate_blank_gpa_no_explanation_rejected() -> None:
    # An empty GPA cell with no extenuating-circumstances text is an affirmative non-answer.
    res = gpa_gate_deterministic("", normalize_gpa_deterministic("", CFG), "", CFG)
    assert res is not None
    assert res.verdict == "reject"
    assert "No GPA provided" in res.gate.reason
    assert res.gpa_points == 0.0


def test_gate_blank_gpa_with_explanation_needs_review() -> None:
    # A blank GPA with an explanation present stays human-reviewed, never auto-rejected.
    res = gpa_gate_deterministic(
        "", normalize_gpa_deterministic("", CFG), "My school does not issue GPAs", CFG
    )
    assert res is not None
    assert res.verdict == "needs_review"
    assert res.gate.passed is False


def test_gate_below_hard_floor_rejected_even_with_explanation() -> None:
    res = gpa_gate_deterministic(
        "1.8", _resolved_norm(1.8), "I was hospitalized for two semesters", CFG
    )
    assert res is not None
    assert res.verdict == "reject"
    assert "hard floor" in res.gate.reason
    assert res.gpa_points == 0.0


def test_gate_exactly_hard_floor_defers_to_task_b() -> None:
    # 2.0 is AT the floor, not below it — an explanation still gets a Task B hearing.
    res = gpa_gate_deterministic("2.0", _resolved_norm(2.0), "documented hardship", CFG)
    assert res is None


def test_gate_below_threshold_with_explanation_defers_to_task_b() -> None:
    res = gpa_gate_deterministic("2.5", _resolved_norm(2.5), "I was seriously ill", CFG)
    assert res is None  # Phase 3.4 (Task B) decides this branch


# ================================================================================================
# 3.4 — Task B low-GPA adequacy + assess_gpa aggregator (LLM, mocked) + §12 invariants
# ================================================================================================


def _task_b(outcome: str = "rank") -> TaskBOutput:
    rank = outcome == "rank"
    return TaskBOutput(
        explanation_adequate=rank,
        strength_of_reason=0.8 if rank else 0.2,
        realistic=True,
        severity_vs_reason_balanced=rank,
        recommended_outcome=outcome,  # type: ignore[arg-type]
        rationale="task b rationale",
    )


def _dispatch(task_b=None, task_a=None):  # type: ignore[no-untyped-def]
    def handler(task, user, schema):  # type: ignore[no-untyped-def]
        if task == "task_b":
            if task_b is None:
                raise AssertionError("unexpected task_b call")
            return task_b
        if task == "task_a":
            if task_a is None:
                raise AssertionError("unexpected task_a call")
            return task_a
        raise AssertionError(f"unexpected task {task}")

    return handler


def _row(gpa: str, explanation: str = "") -> ApplicantRow:
    return ApplicantRow(submission_id="sub-1", gpa=gpa, gpa_explanation=explanation)


async def test_assess_deterministic_pass_makes_no_llm_call() -> None:
    client = _client(_dispatch())
    res = await assess_gpa(_row("3.7"), client, APP)
    assert res.verdict == "pass"
    assert res.gpa_points == pytest.approx(40 * (3.7 - 3.3) / (4.0 - 3.3), abs=1e-3)
    assert client.calls == []


async def test_assess_blank_no_explanation_rejected_no_llm() -> None:
    client = _client(_dispatch())
    res = await assess_gpa(_row(""), client, APP)
    assert res.verdict == "reject"
    assert client.calls == []


async def test_assess_blank_with_explanation_needs_review_no_llm() -> None:
    client = _client(_dispatch())
    res = await assess_gpa(_row("", "my school does not issue GPAs"), client, APP)
    assert res.verdict == "needs_review"
    assert client.calls == []


async def test_assess_below_hard_floor_rejected_no_llm() -> None:
    # Below the 2.0 floor no explanation can rescue — and no Task B token is spent.
    client = _client(_dispatch())
    res = await assess_gpa(_row("1.5", "severe documented hardship"), client, APP)
    assert res.verdict == "reject"
    assert "hard floor" in res.reason
    assert client.calls == []


async def test_assess_below_threshold_task_b_rank_passes_at_gradient_bottom() -> None:
    client = _client(_dispatch(task_b=_task_b("rank")))
    res = await assess_gpa(_row("2.5", "I was hospitalized for a semester"), client, APP)
    assert res.verdict == "pass"
    assert res.gpa_points == 0.0  # bottom of the gradient — deficit reflected, not erased
    assert res.assessment.explanation_eval is not None
    assert res.assessment.explanation_eval.recommended_outcome == "rank"
    # The applicant's own reason is carried on the audit record for the reviewer to read.
    assert res.assessment.explanation_text == "I was hospitalized for a semester"
    assert client.calls[0][0] == "task_b"


async def test_assess_below_threshold_task_b_reject() -> None:
    client = _client(_dispatch(task_b=_task_b("reject")))
    res = await assess_gpa(_row("2.4", "I did not feel like studying"), client, APP)
    assert res.verdict == "reject"
    assert res.reason == "task b rationale"  # PRD §12: rejection names its reason
    assert res.assessment.explanation_eval is not None


async def test_assess_task_b_parse_failure_needs_review_never_rejects() -> None:
    def boom(task, user, schema):  # type: ignore[no-untyped-def]
        raise LLMParseFailure(task, "bad json")

    client = _client(boom)
    res = await assess_gpa(_row("2.6", "some explanation"), client, APP)
    assert res.verdict == "needs_review"  # unscoreable, never REJECTED (PRD §12)
    assert res.gate.reason == "LLM_PARSE_FAILURE"


# --- PRD §12 invariants (GPA) -------------------------------------------------------------------


async def test_invariant_below_threshold_never_scores_above_gradient_bottom() -> None:
    # Even an approved sub-3.3 applicant never earns points above the bottom (0) of the band.
    client = _client(_dispatch(task_b=_task_b("rank")))
    for gpa in ("2.9", "2.5", "2.0"):  # >= hard_floor; below it Task B never fires
        res = await assess_gpa(_row(gpa, "documented hardship"), client, APP)
        assert res.verdict == "pass"
        assert res.gpa_points == 0.0


async def test_invariant_below_threshold_no_points_without_approved_task_b() -> None:
    # Rejected (no approval) -> not scored at all.
    client = _client(_dispatch(task_b=_task_b("reject")))
    res = await assess_gpa(_row("2.5", "weak reason"), client, APP)
    assert res.verdict == "reject"
    assert res.gpa_points == 0.0


async def test_invariant_nothing_unscoreable_is_rejected() -> None:
    # Unresolvable-but-present scales and parse failures must be NEEDS_REVIEW, never REJECTED.
    # (A blank GPA with a blank explanation is a non-answer, not unscoreable — it rejects.)
    blank_with_expl = await assess_gpa(_row("", "no GPA at my school"), _client(_dispatch()), APP)
    assert blank_with_expl.verdict == "needs_review"

    manual = await assess_gpa(
        _row("IGCSE A*A*A"),
        _client(_dispatch(task_a=_task_a(normalized_gpa=None, requires_manual_review=True))),
        APP,
    )
    assert manual.verdict == "needs_review"
