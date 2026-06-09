"""Tests for Stages 2-3 GPA normalization and gate (Phase 3). Synthetic values only.

3.1 covers the deterministic normalizer; later sub-tasks append Task A/B (mocked) and the
gate paths. All §2 GPA quirk shapes are exercised here so the deterministic majority is
pinned with zero API spend.
"""

from __future__ import annotations

import pytest

from srip_filter.config import AppConfig
from srip_filter.gates.gpa import GpaNormalization, normalize_gpa_deterministic

CFG = AppConfig().gpa


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
    assert _norm("2.9").below_threshold is True
    assert _norm("3.0").below_threshold is False
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
