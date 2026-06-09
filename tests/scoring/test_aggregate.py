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
