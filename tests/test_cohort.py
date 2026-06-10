"""Tests for the cohort assignment layer (Phase 11, tiered cost model since 11.5).
Deterministic, no API spend.

Organized by sub-task:
  * 11.1 — :func:`normalize_choices`: tier-token parsing of the messy free-text choice strings.
  * 11.2/11.5 — :func:`assign_cohorts`: rank-greedy assignment under the tiered cost model and
    its invariants (only RANKED assignable, strict first-choice cost ceiling, capped tiers fill
    strictly by rank, no silent overflow — waitlist is a manual-review bucket, capacity sweeps,
    monotonicity, determinism, NEEDS_REVIEW warning).
  * 11.3 — :func:`cohort_assignments_csv`: the single rank-ordered CSV artifact.
"""

from __future__ import annotations

import itertools

from srip_filter.cohort import assign_cohorts, cohort_assignments_csv, normalize_choices
from srip_filter.config import AppConfig
from srip_filter.models import (
    AuditRecord,
    CohortCapacities,
    CohortResult,
    ProgramChoices,
)

CFG = AppConfig()
TIERS = CFG.cohort.tiers


def _choices(
    first: str | None = None, second: str | None = None, third: str | None = None
) -> ProgramChoices:
    return ProgramChoices(first=first, second=second, third=third)


def _rec(
    sid: str,
    rank: int | None,
    *tiers: str,
    outcome: str = "RANKED",
    score: float | None = None,
) -> AuditRecord:
    """A synthetic record listing ``tiers`` as its choices, in form-style free text."""
    slots = [f"Summer 2026- {tier.upper()}" for tier in tiers] + [None, None, None]
    if score is None and rank is not None:
        score = 200.0 - rank
    return AuditRecord(
        submission_id=sid,
        name=f"Student {sid}",
        outcome=outcome,  # type: ignore[arg-type]
        rank=rank,
        final_score=score,
        program_choices=_choices(*slots[:3]),
    )


def _tier_of(result: CohortResult, sid: str) -> str | None:
    for entry in result.assignments:
        if entry.submission_id == sid:
            return entry.assigned_tier
    return None


def _entry(result: CohortResult, sid: str):
    for entry in [*result.assignments, *result.waitlist, *result.unassignable]:
        if entry.submission_id == sid:
            return entry
    raise AssertionError(f"{sid} missing from result")


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


# ------------------------------------------------------------------------------------------------
# 11.2 — assign_cohorts: the realistic (unbounded) case
# ------------------------------------------------------------------------------------------------


def test_no_caps_everyone_gets_first_choice() -> None:
    records = [
        _rec("s1", 1, "honors", "intensive", "regular"),
        _rec("s2", 2, "intensive", "regular"),
        _rec("s3", 3, "regular"),
        _rec("s4", 4, "regular", "intensive", "honors"),
    ]
    result = assign_cohorts(records, CohortCapacities(), CFG)

    assert [a.submission_id for a in result.assignments] == ["s1", "s2", "s3", "s4"]
    assert all(a.choice_number == 1 for a in result.assignments)
    assert _tier_of(result, "s1") == "honors"
    assert _tier_of(result, "s2") == "intensive"
    assert _tier_of(result, "s3") == "regular"
    assert _tier_of(result, "s4") == "regular"
    assert result.waitlist == [] and result.unassignable == []
    assert result.summary.choice_satisfaction == {"choice_1": 4}
    assert result.summary.warnings == []


def test_repeated_tier_assigned_as_choice_one() -> None:
    # An applicant listing the same tier three times has one distinct choice.
    records = [_rec("s1", 1, "regular", "regular", "regular")]
    result = assign_cohorts(records, CohortCapacities(), CFG)
    entry = _entry(result, "s1")
    assert entry.assigned_tier == "regular"
    assert entry.choice_number == 1
    assert entry.choices == ["regular"]


# ------------------------------------------------------------------------------------------------
# 11.2 — only RANKED is assignable (PRD §11: REJECTED can never resurface)
# ------------------------------------------------------------------------------------------------


def test_rejected_and_needs_review_are_never_assigned() -> None:
    records = [
        _rec("s1", 1, "honors"),
        _rec("rej", None, "honors", outcome="REJECTED"),
        _rec("rev", None, "honors", outcome="NEEDS_REVIEW"),
    ]
    result = assign_cohorts(records, CohortCapacities(), CFG)

    all_ids = {
        e.submission_id
        for e in [*result.assignments, *result.waitlist, *result.unassignable]
    }
    assert all_ids == {"s1"}
    assert result.summary.total_ranked == 1
    assert result.summary.needs_review_count == 1
    assert any("NEEDS_REVIEW" in w for w in result.summary.warnings)


# ------------------------------------------------------------------------------------------------
# 11.5 — strict first-choice cost ceiling
# ------------------------------------------------------------------------------------------------


def test_cost_ceiling_blocks_listed_higher_tiers() -> None:
    # The real-data R-I-H pattern (67 applicants): regular first means regular ONLY — never
    # intensive or honors, even with seats wide open there.
    records = [_rec("s1", 1, "regular", "intensive", "honors")]
    result = assign_cohorts(records, CohortCapacities(regular=0), CFG)

    entry = _entry(result, "s1")
    assert entry.status == "waitlisted"  # regular closed; higher tiers are not an option
    assert entry.excluded_by_cost == ["intensive", "honors"]
    assert "regular" in entry.reason
    assert result.summary.tiers["intensive"].filled == 0
    assert result.summary.tiers["honors"].filled == 0


def test_cost_ceiling_recorded_on_assigned_students() -> None:
    records = [_rec("s1", 1, "regular", "intensive", "honors")]
    result = assign_cohorts(records, CohortCapacities(), CFG)
    entry = _entry(result, "s1")
    assert entry.assigned_tier == "regular"
    assert entry.choice_number == 1
    assert entry.excluded_by_cost == ["intensive", "honors"]


def test_intensive_first_excludes_honors_but_not_regular() -> None:
    # I-R-H: honors (above first choice) is excluded; regular (below) is a real fallback.
    records = [_rec("s1", 1, "intensive", "regular", "honors")]
    result = assign_cohorts(records, CohortCapacities(intensive=0), CFG)
    entry = _entry(result, "s1")
    assert entry.assigned_tier == "regular"
    assert entry.choice_number == 2
    assert entry.excluded_by_cost == ["honors"]


def test_honors_first_can_fall_through_all_listed_tiers() -> None:
    records = [_rec("s1", 1, "honors", "intensive", "regular")]
    result = assign_cohorts(records, CohortCapacities(honors=0, intensive=0), CFG)
    entry = _entry(result, "s1")
    assert entry.assigned_tier == "regular"
    assert entry.choice_number == 3
    assert entry.excluded_by_cost == []


# ------------------------------------------------------------------------------------------------
# 11.5 — capped tiers fill strictly by rank; waitlist is a manual-review bucket
# ------------------------------------------------------------------------------------------------


def test_capacity_binds_in_rank_order() -> None:
    # Two honors-first students, one honors seat: the stronger gets it, the other falls to
    # their listed (cheaper) second choice.
    records = [
        _rec("s1", 1, "honors", "intensive"),
        _rec("s2", 2, "honors", "intensive"),
    ]
    result = assign_cohorts(records, CohortCapacities(honors=1), CFG)
    assert _tier_of(result, "s1") == "honors"
    assert _tier_of(result, "s2") == "intensive"
    assert _entry(result, "s2").choice_number == 2


def test_no_displacement_capped_tier_is_purely_rank_ordered() -> None:
    # Under the old max-matching policy, honors-only s2 would have displaced flexible s1.
    # Now honors seats go strictly by rank: s1 keeps honors, s2 goes to manual review.
    records = [
        _rec("s1", 1, "honors", "intensive"),
        _rec("s2", 2, "honors"),
    ]
    result = assign_cohorts(records, CohortCapacities(honors=1), CFG)

    assert _tier_of(result, "s1") == "honors"  # never bumped
    entry = _entry(result, "s2")
    assert entry.status == "waitlisted"
    assert result.summary.assigned == 1
    assert result.summary.waitlisted == 1


def test_waitlist_reason_names_programs_and_regular_eligibility() -> None:
    records = [_rec("s1", 1, "honors"), _rec("s2", 2, "honors")]
    result = assign_cohorts(records, CohortCapacities(honors=1), CFG)
    entry = _entry(result, "s2")
    assert entry.status == "waitlisted"
    assert "honors" in entry.reason  # the sole program they chose
    assert "still eligible for regular" in entry.reason  # staff can place them manually
    assert "staff decision" in entry.reason


def test_listed_regular_is_a_normal_fallback() -> None:
    # A student who listed regular lands there via the ordinary walk — no manual review needed.
    records = [_rec("s1", 1, "honors"), _rec("s2", 2, "honors", "regular")]
    result = assign_cohorts(records, CohortCapacities(honors=1), CFG)
    entry = _entry(result, "s2")
    assert entry.status == "assigned"
    assert entry.assigned_tier == "regular"
    assert entry.choice_number == 2


def test_optional_regular_cap_binds_by_rank() -> None:
    records = [_rec("s1", 1, "regular"), _rec("s2", 2, "regular"), _rec("s3", 3, "regular")]
    result = assign_cohorts(records, CohortCapacities(regular=2), CFG)
    assert _tier_of(result, "s1") == "regular"
    assert _tier_of(result, "s2") == "regular"
    entry = _entry(result, "s3")
    assert entry.status == "waitlisted"
    assert "regular" in entry.reason
    assert "still eligible" not in entry.reason  # they chose regular; its cap is simply full
    assert result.summary.tiers["regular"].open_seats == 0


def test_zero_capacity_closes_a_tier() -> None:
    records = [_rec("s1", 1, "honors"), _rec("s2", 2, "honors", "regular")]
    result = assign_cohorts(records, CohortCapacities(honors=0), CFG)
    assert _entry(result, "s1").status == "waitlisted"
    assert _tier_of(result, "s2") == "regular"
    assert result.summary.tiers["honors"].filled == 0
    assert result.summary.tiers["honors"].open_seats == 0


# ------------------------------------------------------------------------------------------------
# 11.2 — unassignable, summary facts, and warnings
# ------------------------------------------------------------------------------------------------


def test_unparseable_choices_are_unassignable_never_seated() -> None:
    record = AuditRecord(
        submission_id="s1",
        name="Student s1",
        outcome="RANKED",
        rank=1,
        final_score=90.0,
        program_choices=_choices("no idea", "???"),
    )
    result = assign_cohorts([record], CohortCapacities(), CFG)
    entry = _entry(result, "s1")
    assert entry.status == "unassignable"
    assert entry.reason
    assert result.summary.unassignable == 1
    assert result.summary.assigned == 0


def test_summary_capacity_fill_and_demand() -> None:
    records = [
        _rec("s1", 1, "intensive"),
        _rec("s2", 2, "intensive", "regular"),
        _rec("s3", 3, "regular"),
    ]
    result = assign_cohorts(records, CohortCapacities(intensive=1), CFG)

    intensive = result.summary.tiers["intensive"]
    assert intensive.capacity == 1
    assert intensive.filled == 1
    assert intensive.open_seats == 0
    assert intensive.first_choice_demand == 2

    regular = result.summary.tiers["regular"]
    assert regular.capacity is None
    assert regular.filled == 2
    assert regular.open_seats is None
    assert regular.first_choice_demand == 1


def test_missing_rank_is_processed_last_with_warning() -> None:
    records = [_rec("late", None, "honors"), _rec("s1", 1, "honors")]
    result = assign_cohorts(records, CohortCapacities(honors=1), CFG)
    assert _tier_of(result, "s1") == "honors"
    assert _entry(result, "late").status == "waitlisted"
    assert any("no rank" in w for w in result.summary.warnings)


# ------------------------------------------------------------------------------------------------
# Global invariants: capacity, monotonicity, determinism (brute-force over cap combos)
# ------------------------------------------------------------------------------------------------

_POPULATION = [
    _rec("s1", 1, "honors", "intensive"),
    _rec("s2", 2, "intensive", "regular"),
    _rec("s3", 3, "honors"),
    _rec("s4", 4, "regular", "honors", "intensive"),
    _rec("s5", 5, "regular"),
    _rec("s6", 6, "intensive"),
]
_CAP_VALUES: tuple[int | None, ...] = (0, 1, 2, None)


def _caps(h: int | None, i: int | None, r: int | None) -> CohortCapacities:
    return CohortCapacities(honors=h, intensive=i, regular=r)


def test_capacity_is_never_exceeded_across_all_combos() -> None:
    for h, i, r in itertools.product(_CAP_VALUES, repeat=3):
        result = assign_cohorts(_POPULATION, _caps(h, i, r), CFG)
        for tier, cap in (("honors", h), ("intensive", i), ("regular", r)):
            if cap is not None:
                assert result.summary.tiers[tier].filled <= cap
        # every RANKED record lands in exactly one bucket
        total = (
            result.summary.assigned + result.summary.waitlisted + result.summary.unassignable
        )
        assert total == len(_POPULATION)


def test_cost_ceiling_holds_across_all_combos() -> None:
    # No student is ever assigned a tier more expensive than their first choice.
    tier_rank = {tier: idx for idx, tier in enumerate(TIERS)}
    for h, i, r in itertools.product(_CAP_VALUES, repeat=3):
        result = assign_cohorts(_POPULATION, _caps(h, i, r), CFG)
        for entry in result.assignments:
            assert tier_rank[entry.assigned_tier] >= tier_rank[entry.choices[0]]


def test_raising_any_capacity_never_reduces_total_assigned() -> None:
    bounded = (0, 1, 2)
    for h, i, r in itertools.product(bounded, repeat=3):
        base = assign_cohorts(_POPULATION, _caps(h, i, r), CFG).summary.assigned
        for bumped in (_caps(h + 1, i, r), _caps(h, i + 1, r), _caps(h, i, r + 1)):
            assert assign_cohorts(_POPULATION, bumped, CFG).summary.assigned >= base


def test_assignment_is_deterministic_across_reruns() -> None:
    caps = _caps(1, 2, 1)
    first = assign_cohorts(_POPULATION, caps, CFG)
    second = assign_cohorts(_POPULATION, caps, CFG)
    assert first.model_dump() == second.model_dump()


# ------------------------------------------------------------------------------------------------
# 11.3 — cohort_assignments_csv
# ------------------------------------------------------------------------------------------------


def test_csv_has_pinned_columns_and_every_record_once() -> None:
    records = [
        _rec("s1", 1, "honors", "intensive"),
        _rec("s2", 2, "honors"),  # waitlisted: honors cap taken by s1
        _rec("s3", 3, "honors"),  # waitlisted
        AuditRecord(
            submission_id="s4",
            name="Student s4",
            outcome="RANKED",
            rank=4,
            final_score=50.0,
            program_choices=_choices("garbage"),
        ),
    ]
    result = assign_cohorts(records, CohortCapacities(honors=1, intensive=1), CFG)
    lines = cohort_assignments_csv(result).strip().split("\n")

    assert lines[0] == (
        "rank,submission_id,name,final_score,status,assigned_tier,"
        "choice_number,excluded_by_cost,choices,reason"
    )
    assert len(lines) == 1 + len(records)  # every RANKED record exactly once
    # rank order across all statuses
    assert [line.split(",")[1] for line in lines[1:]] == ["s1", "s2", "s3", "s4"]


def test_csv_rows_carry_status_tier_and_cost_exclusions() -> None:
    records = [
        _rec("s1", 1, "regular", "intensive", "honors"),
        _rec("s2", 2, "honors", "regular"),
    ]
    result = assign_cohorts(records, CohortCapacities(), CFG)
    lines = cohort_assignments_csv(result).strip().split("\n")

    s1 = lines[1].split(",")
    assert s1[4] == "assigned"
    assert s1[5] == "regular"
    assert s1[6] == "1"
    assert s1[7] == "intensive | honors"  # pruned by the cost ceiling
    assert s1[8] == "regular > intensive > honors"

    s2 = lines[2].split(",")
    assert s2[4] == "assigned"
    assert s2[5] == "honors"
    assert s2[7] == ""  # nothing above their first choice


def test_csv_is_deterministic() -> None:
    caps = _caps(1, 1, 2)
    first = cohort_assignments_csv(assign_cohorts(_POPULATION, caps, CFG))
    second = cohort_assignments_csv(assign_cohorts(_POPULATION, caps, CFG))
    assert first == second
