"""Tests for the cohort assignment layer (Phase 11, PRD §11). Deterministic, no API spend.

Organized by sub-task:
  * 11.1 — :func:`normalize_choices`: tier-token parsing of the messy free-text choice strings.
"""

from __future__ import annotations

from srip_filter.cohort import normalize_choices
from srip_filter.config import AppConfig
from srip_filter.models import ProgramChoices

CFG = AppConfig()
TIERS = CFG.cohort.tiers


def _choices(
    first: str | None = None, second: str | None = None, third: str | None = None
) -> ProgramChoices:
    return ProgramChoices(first=first, second=second, third=third)


# ------------------------------------------------------------------------------------------------
# 11.1 — normalize_choices
# ------------------------------------------------------------------------------------------------


def test_parses_both_dash_formats_seen_in_the_form() -> None:
    # The real export mixes "Summer 2026- INTENSIVE" (First Choice) and
    # "Summer 2026 - INTENSIVE" (Second/Third Choice). Both must parse.
    parsed = normalize_choices(
        _choices("Summer 2026- INTENSIVE", "Summer 2026 - REGULAR", "Summer 2026 - HONORS"),
        TIERS,
    )
    assert parsed == ["intensive", "regular", "honors"]


def test_matching_is_case_insensitive() -> None:
    assert normalize_choices(_choices("summer 2026 - honors"), TIERS) == ["honors"]
    assert normalize_choices(_choices("HONORS"), TIERS) == ["honors"]


def test_order_is_preserved() -> None:
    parsed = normalize_choices(
        _choices("Summer 2026- REGULAR", "Summer 2026 - INTENSIVE", "Summer 2026 - HONORS"),
        TIERS,
    )
    assert parsed == ["regular", "intensive", "honors"]


def test_repeated_tier_dedupes_to_first_occurrence() -> None:
    # 28 real applicants list the same tier three times: that's one choice, not three.
    parsed = normalize_choices(
        _choices("Summer 2026- REGULAR", "Summer 2026 - REGULAR", "Summer 2026 - REGULAR"),
        TIERS,
    )
    assert parsed == ["regular"]


def test_blank_slots_are_skipped() -> None:
    assert normalize_choices(_choices("Summer 2026- HONORS"), TIERS) == ["honors"]
    assert normalize_choices(_choices(None, "Summer 2026 - REGULAR"), TIERS) == ["regular"]


def test_garbage_slot_is_dropped_but_valid_slots_kept() -> None:
    parsed = normalize_choices(
        _choices("Summer 2026", "N/A", "Summer 2026 - INTENSIVE"),
        TIERS,
    )
    assert parsed == ["intensive"]


def test_ambiguous_slot_with_two_tier_tokens_is_dropped() -> None:
    parsed = normalize_choices(_choices("HONORS or REGULAR please", "INTENSIVE"), TIERS)
    assert parsed == ["intensive"]


def test_all_empty_yields_no_preferences() -> None:
    assert normalize_choices(_choices(), TIERS) == []
    assert normalize_choices(_choices("", "", ""), TIERS) == []
