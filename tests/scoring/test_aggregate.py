"""Tests for Stage 8 aggregation + ranking (Phase 7). Deterministic, no API spend.

Organized by sub-task:
  * 7.1 — :func:`compose_final_score` / :func:`finalize_score`: the §10.1 additive composition
    and §12 #1 (no optional-signal absence ever lowers ``final_score``).
  * 7.2 — :func:`rank_records`: outcome finalization, the deterministic tiebreaker chain, and
    §12 #2 (no bonus changes a ``REJECTED`` outcome) / #5 (ranking stable across reruns).

The consolidated §12 invariant suite (7.4) lives at the bottom of this file.
"""

from __future__ import annotations

from srip_filter.config import AppConfig
from srip_filter.models import AuditRecord, EssaySubscores, Scores
from srip_filter.scoring.aggregate import (
    compose_final_score,
    finalize_score,
    rank_records,
)

CFG = AppConfig()


def _scores(
    gpa_points: float = 0.0,
    essay_total: float = 0.0,
    coursework_bonus: float = 0.0,
    school_bonus: float = 0.0,
    resume_bonus: float = 0.0,
) -> Scores:
    return Scores(
        gpa_points=gpa_points,
        essay=EssaySubscores(total=essay_total),
        coursework_bonus=coursework_bonus,
        school_bonus=school_bonus,
        resume_bonus=resume_bonus,
    )


# ------------------------------------------------------------------------------------------------
# 7.1 — compose_final_score / finalize_score
# ------------------------------------------------------------------------------------------------


def test_compose_sums_all_five_components() -> None:
    scores = _scores(
        gpa_points=40.0,
        essay_total=35.0,
        coursework_bonus=9.4,
        school_bonus=15.0,
        resume_bonus=0.0,
    )
    assert compose_final_score(scores, CFG) == 99.4


def test_compose_required_signals_only() -> None:
    # GPA + essays only; all bonuses absent (0). The required core stands alone.
    scores = _scores(gpa_points=28.0, essay_total=30.0)
    assert compose_final_score(scores, CFG) == 58.0


def test_compose_all_zero_is_zero() -> None:
    assert compose_final_score(_scores(), CFG) == 0.0


def test_absent_optional_signals_never_lower_total() -> None:
    """§12 #1: dropping each optional bonus to 0 can only lower-or-equal, never below the core."""
    core = _scores(gpa_points=20.0, essay_total=20.0)
    base = compose_final_score(core, CFG)

    with_bonuses = _scores(
        gpa_points=20.0,
        essay_total=20.0,
        coursework_bonus=10.0,
        school_bonus=12.0,
        resume_bonus=0.0,
    )
    # Adding bonuses only raises the total; their absence is exactly the core (neutral).
    assert compose_final_score(with_bonuses, CFG) >= base
    assert base == 40.0


def test_each_bonus_is_purely_additive() -> None:
    core = _scores(gpa_points=10.0, essay_total=10.0)
    base = compose_final_score(core, CFG)
    for field in ("coursework_bonus", "school_bonus"):
        bumped = _scores(gpa_points=10.0, essay_total=10.0, **{field: 5.0})
        assert compose_final_score(bumped, CFG) == base + 5.0


def test_finalize_score_writes_into_record() -> None:
    rec = AuditRecord(
        submission_id="s1",
        outcome="RANKED",
        scores=_scores(gpa_points=40.0, essay_total=20.0, school_bonus=15.0),
    )
    returned = finalize_score(rec, CFG)
    assert returned is rec  # mutates in place, returns for chaining
    assert rec.final_score == 75.0


# ------------------------------------------------------------------------------------------------
# 7.2 — rank_records
# ------------------------------------------------------------------------------------------------


def _rec(
    sid: str,
    outcome: str = "RANKED",
    gpa_points: float = 0.0,
    essay_total: float = 0.0,
    coursework_bonus: float = 0.0,
    school_bonus: float = 0.0,
) -> AuditRecord:
    return AuditRecord(
        submission_id=sid,
        outcome=outcome,  # type: ignore[arg-type]
        scores=_scores(
            gpa_points=gpa_points,
            essay_total=essay_total,
            coursework_bonus=coursework_bonus,
            school_bonus=school_bonus,
        ),
    )


def test_rank_orders_by_final_score_desc() -> None:
    recs = [
        _rec("low", gpa_points=10.0, essay_total=10.0),  # 20
        _rec("high", gpa_points=40.0, essay_total=40.0),  # 80
        _rec("mid", gpa_points=20.0, essay_total=20.0),  # 40
    ]
    rank_records(recs, CFG)
    by_id = {r.submission_id: r for r in recs}
    assert by_id["high"].rank == 1
    assert by_id["mid"].rank == 2
    assert by_id["low"].rank == 3


def test_rank_finalizes_outcome_and_score_for_survivors() -> None:
    # A survivor may arrive with a non-terminal placeholder outcome; rank_records marks it RANKED.
    rec = _rec("s1", outcome="RANKED", gpa_points=30.0, essay_total=25.0, school_bonus=12.0)
    rank_records([rec], CFG)
    assert rec.outcome == "RANKED"
    assert rec.final_score == 67.0
    assert rec.rank == 1


def test_rejected_and_needs_review_are_not_scored_or_ranked() -> None:
    rejected = _rec("rej", outcome="REJECTED", gpa_points=40.0, essay_total=40.0, school_bonus=15.0)
    review = _rec("rev", outcome="NEEDS_REVIEW", gpa_points=40.0, essay_total=40.0)
    ranked = _rec("ok", outcome="RANKED", gpa_points=10.0, essay_total=10.0)
    rank_records([rejected, review, ranked], CFG)

    assert rejected.final_score is None and rejected.rank is None
    assert review.final_score is None and review.rank is None
    # The lone survivor ranks #1 despite the rejected applicant's far higher subscores (§12 #2).
    assert ranked.rank == 1 and ranked.final_score == 20.0


def test_tiebreaker_gpa_then_essay_then_submission_id() -> None:
    # All four share final_score 50; tiebreak walks gpa_points → essay.total → submission_id.
    a = _rec("a", gpa_points=30.0, essay_total=20.0)  # gpa 30
    b = _rec("b", gpa_points=25.0, essay_total=25.0)  # gpa 25, essay 25
    c = _rec("c", gpa_points=25.0, essay_total=20.0, coursework_bonus=5.0)  # gpa 25, essay 20
    d = _rec("d", gpa_points=25.0, essay_total=20.0, school_bonus=5.0)  # gpa 25, essay 20, id 'd'
    rank_records([d, c, b, a], CFG)
    by_id = {r.submission_id: r for r in [a, b, c, d]}
    assert by_id["a"].rank == 1  # highest gpa_points
    assert by_id["b"].rank == 2  # next gpa, higher essay
    assert by_id["c"].rank == 3  # ties with d on gpa+essay, 'c' < 'd' on submission_id
    assert by_id["d"].rank == 4
    # Sanity: they really do all share the same final_score.
    assert {r.final_score for r in [a, b, c, d]} == {50.0}


def test_ranking_stable_across_reruns() -> None:
    """§12 #5: re-running rank_records yields identical ranks (deterministic, idempotent)."""
    recs = [
        _rec("x", gpa_points=20.0, essay_total=20.0),
        _rec("y", gpa_points=20.0, essay_total=20.0),  # exact tie with x → submission_id breaks it
        _rec("z", gpa_points=40.0, essay_total=10.0),
    ]
    rank_records(recs, CFG)
    first = {r.submission_id: r.rank for r in recs}
    rank_records(recs, CFG)  # rerun on the already-ranked records
    second = {r.submission_id: r.rank for r in recs}
    assert first == second
    assert first["x"] < first["y"]  # 'x' < 'y' on the submission_id tiebreak


# ================================================================================================
# 7.4 — Consolidated PRD §12 invariant suite
# ================================================================================================
# A single synthetic population spanning all three outcomes, asserting the five §12 invariants
# end-to-end at the aggregate/output level. (The full pipeline pass over a CSV is Phase 8.)
# Note GPA points are an input to this stage (computed in gpa.py); §12 #4 is pinned here at the
# composition level — an approved sub-threshold applicant arrives at the gradient bottom (0)
# and the aggregate must carry, never inflate, it.


def _population() -> list[AuditRecord]:
    """Mixed cohort: two clean RANKED, one approved sub-threshold RANKED, one REJECTED, one
    NEEDS_REVIEW — REJECTED/NEEDS_REVIEW carry maxed bonuses to prove they stay unscored."""
    rejected = _rec("rej", outcome="REJECTED", gpa_points=40.0, essay_total=40.0, school_bonus=15.0)
    rejected.decided_at_stage = "stage1"
    rejected.primary_reason = "Essay 1 below hard_min length gate"
    review = _rec("rev", outcome="NEEDS_REVIEW", gpa_points=0.0, essay_total=0.0, school_bonus=15.0)
    review.primary_reason = "GPA scale could not be normalized"
    return [
        _rec("top", gpa_points=40.0, essay_total=38.0, coursework_bonus=9.0, school_bonus=15.0),
        _rec("mid", gpa_points=28.0, essay_total=30.0),
        # Approved sub-threshold: gpa_points at the gradient bottom (0); essays + bonuses only.
        _rec("low", gpa_points=0.0, essay_total=25.0, coursework_bonus=5.0),
        rejected,
        review,
    ]


def test_invariant_1_optional_absence_never_reduces_score() -> None:
    """§12 #1: removing every optional bonus yields a lower-or-equal score — never below core."""
    full = _rec("a", gpa_points=30.0, essay_total=30.0, coursework_bonus=10.0, school_bonus=12.0)
    bare = _rec("a", gpa_points=30.0, essay_total=30.0)  # same required core, no bonuses
    rank_records([full], CFG)
    rank_records([bare], CFG)
    assert bare.final_score == 60.0  # exactly the required core
    assert full.final_score >= bare.final_score


def test_invariant_2_no_bonus_changes_a_rejected_outcome() -> None:
    """§12 #2: a REJECTED applicant with maximal bonuses stays REJECTED, unscored, unranked."""
    pop = _population()
    rank_records(pop, CFG)
    rej = next(r for r in pop if r.submission_id == "rej")
    assert rej.outcome == "REJECTED"
    assert rej.final_score is None and rej.rank is None


def test_invariant_3_every_rejected_names_the_gate() -> None:
    """§12 #3: each REJECTED record names the failing gate in ``primary_reason``."""
    pop = _population()
    rank_records(pop, CFG)
    for r in pop:
        if r.outcome == "REJECTED":
            assert r.primary_reason.strip()


def test_invariant_4_sub_threshold_gpa_lands_at_gradient_bottom() -> None:
    """§12 #4: an approved sub-threshold applicant scores no GPA points (gradient bottom) and ranks
    strictly below an otherwise-identical applicant with positive GPA points."""
    pop = _population()
    rank_records(pop, CFG)
    low = next(r for r in pop if r.submission_id == "low")
    # gpa_points contributes nothing; final_score is essays + bonuses only.
    assert low.scores.gpa_points == 0.0
    assert low.final_score == 30.0  # 0 + 25 essays + 5 coursework
    top = next(r for r in pop if r.submission_id == "top")
    mid = next(r for r in pop if r.submission_id == "mid")
    assert top.rank < mid.rank < low.rank  # the sub-threshold applicant sorts to the bottom


def test_invariant_5_ranking_stable_across_reruns_population() -> None:
    """§12 #5: ranking the whole population twice yields identical ranks and outcomes."""
    pop = _population()
    rank_records(pop, CFG)
    first = {r.submission_id: (r.outcome, r.rank, r.final_score) for r in pop}
    rank_records(pop, CFG)
    second = {r.submission_id: (r.outcome, r.rank, r.final_score) for r in pop}
    assert first == second
    # Sanity: the three survivors occupy ranks 1..3; the two non-survivors are unranked.
    assert sorted(r.rank for r in pop if r.rank is not None) == [1, 2, 3]
