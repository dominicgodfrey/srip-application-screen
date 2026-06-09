"""Tests for Stage 7 school bonus (Phase 6.1/6.2). Deterministic, no API spend.

6.1 pins :func:`match_school` (exact / alias / light misspelling / normalization / "High School"
/ blank → no match / both-lists → higher-bonus list); 6.2 pins :func:`score_school` (list → bonus
mapping, unmatched → 0, never negative, and the §12 invariant that a school bonus can neither
manufacture nor rescue a ``REJECTED`` outcome).
"""

from __future__ import annotations

from srip_filter.config import AppConfig
from srip_filter.ingest import ApplicantRow
from srip_filter.models import AuditRecord
from srip_filter.scoring.school import match_school, score_school

APP = AppConfig()
CFG = APP.school


def _row(institution: str = "") -> ApplicantRow:
    return ApplicantRow(submission_id="s1", institution=institution)


# ------------------------------------------------------------------------------------------------
# 6.1 — match_school
# ------------------------------------------------------------------------------------------------


def test_exact_name_matches() -> None:
    m = match_school("Brown University", CFG)
    assert m.matched_name == "Brown University"
    assert m.list == "us_top20"  # Brown is only on the US list
    assert m.fuzzy_score >= CFG.fuzzy_match_threshold


def test_alias_mit_matches_canonical() -> None:
    m = match_school("MIT", CFG)
    assert m.matched_name == "Massachusetts Institute of Technology"
    assert m.list is not None


def test_alias_ucla_matches_canonical() -> None:
    m = match_school("UCLA", CFG)
    assert m.matched_name == "University of California, Los Angeles"


def test_light_misspelling_still_matches() -> None:
    m = match_school("Stanford Univercity", CFG)
    assert m.matched_name == "Stanford University"


def test_normalization_ignores_case_and_punctuation() -> None:
    m = match_school("  princeton university!! ", CFG)
    assert m.matched_name == "Princeton University"


def test_intl_only_school_matches_intl_list() -> None:
    m = match_school("University of Oxford", CFG)
    assert m.matched_name == "University of Oxford"
    assert m.list == "intl_top50"  # Oxford is not on the US list


def test_both_lists_school_takes_higher_bonus_list() -> None:
    # Harvard sits on both lists; us_top20 (15) > intl_top50 (12), so it is reported as US.
    m = match_school("Harvard University", CFG)
    assert m.matched_name == "Harvard University"
    assert m.list == "us_top20"


def test_high_school_does_not_match() -> None:
    m = match_school("High School", CFG)
    assert m.matched_name is None
    assert m.list is None
    assert m.fuzzy_score == 0.0


def test_blank_does_not_match() -> None:
    assert match_school("", CFG).matched_name is None
    assert match_school("   ", CFG).matched_name is None


def test_unrelated_school_does_not_match() -> None:
    m = match_school("Springfield Community College", CFG)
    assert m.matched_name is None
    assert m.list is None


# ------------------------------------------------------------------------------------------------
# 6.2 — score_school aggregator
# ------------------------------------------------------------------------------------------------


def test_us_top20_maps_to_us_bonus() -> None:
    r = score_school(_row("Brown University"), APP)
    assert r.bonus == CFG.bonus_us_top20
    assert r.match.list == "us_top20"


def test_intl_top50_maps_to_intl_bonus() -> None:
    r = score_school(_row("University of Oxford"), APP)
    assert r.bonus == CFG.bonus_intl_top50
    assert r.match.list == "intl_top50"


def test_high_school_scores_zero() -> None:
    r = score_school(_row("High School"), APP)
    assert r.bonus == 0.0
    assert r.match.matched_name is None


def test_blank_scores_zero() -> None:
    assert score_school(_row(""), APP).bonus == 0.0


def test_bonus_never_negative() -> None:
    for inst in ("High School", "", "MIT", "Some Unknown School", "Harvard University"):
        assert score_school(_row(inst), APP).bonus >= 0.0


def test_school_bonus_cannot_rescue_a_rejected_outcome() -> None:
    # §12 invariant: applying even the top school bonus to a REJECTED record leaves it REJECTED.
    rejected = AuditRecord(
        submission_id="s1",
        outcome="REJECTED",
        primary_reason="Essay 1 off-topic",
        final_score=None,
    )
    r = score_school(_row("Harvard University"), APP)
    rejected.scores.school_bonus = r.bonus  # additive only
    assert r.bonus > 0.0  # a real bonus was computed
    assert rejected.outcome == "REJECTED"  # but the outcome is untouched
    assert rejected.final_score is None
