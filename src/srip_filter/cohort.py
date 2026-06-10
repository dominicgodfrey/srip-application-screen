"""Cohort assignment — the PRD §11 downstream layer (Phase 11, policy v2 in 11.5).

Turns the ranked filter output into program placements under the **tiered cost model**: the
tiers are ordered by competitiveness *and* cost — HONORS > INTENSIVE > REGULAR (the configured
``cohort.tiers`` order, most expensive first, is load-bearing). Staff caps honors/intensive
(regular optionally); capped tiers fill **strictly by rank** among the students who chose them,
and regular is the de-facto landing tier for applicants who listed it.

Hard policy rules:
  * **Cost ceiling.** A student is never placed in a tier above their *first choice* — even one
    they explicitly ranked #2/#3. Higher tiers cost the student more; the first choice caps what
    they signed up to pay. Pruned tiers are reported in ``excluded_by_cost``.
  * **No silent overflow.** A student whose eligible choices are all full is **waitlisted** with
    a reason naming the chosen program(s) and their remaining regular eligibility — a manual
    staff decision, never an automatic placement in a tier they didn't list.
  * Only ``RANKED`` records are ever assignable (PRD §11: ``REJECTED`` can never resurface;
    ``NEEDS_REVIEW`` is excluded with a warning — resolve, re-rank, rerun).

Entirely deterministic, pure, and LLM-free; with no caps set, everyone lands in their first
choice (the realistic case), and recomputation is instant for staff what-if iteration.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from .config import AppConfig
from .models import (
    AuditRecord,
    CohortAssignment,
    CohortCapacities,
    CohortResult,
    CohortSummary,
    ProgramChoices,
    TierSummary,
)
from .outputs import _write_csv

# Downloaded filename for the cohort CSV artifact (the JSON result has no file form).
COHORT_ASSIGNMENTS_FILE = "cohort_assignments.csv"


def normalize_choices(choices: ProgramChoices, tiers: Sequence[str]) -> list[str]:
    """Parse an applicant's free-text program choices into an ordered tier preference list.

    The Fillout choice strings are inconsistent (``Summer 2026- INTENSIVE`` vs ``Summer 2026 -
    INTENSIVE``), so each slot is matched by case-insensitive *containment* of exactly one
    canonical tier token from ``tiers``. A slot containing zero tokens (blank / garbage) or more
    than one (ambiguous) is dropped, never guessed. Repeated tiers dedupe to their first
    occurrence — an applicant listing the same tier three times means "this tier or nothing",
    not three choices.
    """
    preferences: list[str] = []
    for raw in (choices.first, choices.second, choices.third):
        if not raw:
            continue
        text = raw.lower()
        hits = [tier for tier in tiers if tier.lower() in text]
        if len(hits) != 1:
            continue
        if hits[0] not in preferences:
            preferences.append(hits[0])
    return preferences


# ================================================================================================
# 11.2 / 11.5 — rank-greedy assignment under the tiered cost model
# ================================================================================================


def _waitlist_reason(eligible: list[str], excluded: list[str], lowest_tier: str) -> str:
    """Staff-facing reason for a waitlisted student: what they chose, what the cost ceiling
    pruned, and (when they didn't list it) their remaining eligibility for the lowest tier."""
    parts = [f"Did not qualify by rank for chosen program(s) at capacity: {', '.join(eligible)}"]
    if excluded:
        parts.append(f"excluded by first-choice cost ceiling: {', '.join(excluded)}")
    if lowest_tier not in eligible:
        parts.append(f"still eligible for {lowest_tier} — staff decision required")
    return "; ".join(parts)


def assign_cohorts(
    records: Sequence[AuditRecord],
    capacities: CohortCapacities,
    cfg: AppConfig,
) -> CohortResult:
    """Assign every ``RANKED`` applicant to a program tier (PRD §11). Pure and deterministic.

    Walks the ranking top-down. Each student's listed choices are first pruned by the **cost
    ceiling** (any tier above their first choice is excluded — reported in
    ``excluded_by_cost``); they are then seated in the first eligible tier, in their listed
    order, with an open seat. Capped tiers therefore fill strictly by rank among the students
    who chose them. A student with no open eligible tier is **waitlisted** for manual staff
    handling (the reason records their regular eligibility); a student with no parseable choice
    at all is ``unassignable``. ``REJECTED`` is never seated; ``NEEDS_REVIEW`` is excluded with
    a warning so staff can preview sizing before every case is resolved.
    """
    tiers = list(cfg.cohort.tiers)
    tier_index = {tier: position for position, tier in enumerate(tiers)}
    cap = {tier: capacities.for_tier(tier) for tier in tiers}
    filled: dict[str, int] = {tier: 0 for tier in tiers}

    def has_open(tier: str) -> bool:
        return cap[tier] is None or filled[tier] < cap[tier]

    ranked = sorted(
        (r for r in records if r.outcome == "RANKED"),
        key=lambda r: (r.rank is None, r.rank if r.rank is not None else 0, r.submission_id),
    )
    needs_review_count = sum(1 for r in records if r.outcome == "NEEDS_REVIEW")

    assignments: list[CohortAssignment] = []
    waitlist: list[CohortAssignment] = []
    unassignable: list[CohortAssignment] = []
    first_choice_demand: Counter[str] = Counter()

    for record in ranked:
        prefs = normalize_choices(record.program_choices, tiers)
        if not prefs:
            unassignable.append(
                CohortAssignment(
                    submission_id=record.submission_id,
                    name=record.name,
                    rank=record.rank,
                    final_score=record.final_score,
                    status="unassignable",
                    reason="No valid program choice could be parsed from the application.",
                )
            )
            continue

        first_choice_demand[prefs[0]] += 1
        ceiling = tier_index[prefs[0]]
        eligible = [tier for tier in prefs if tier_index[tier] >= ceiling]
        excluded = [tier for tier in prefs if tier_index[tier] < ceiling]

        assigned_tier = next((tier for tier in eligible if has_open(tier)), None)
        if assigned_tier is not None:
            filled[assigned_tier] += 1
            assignments.append(
                CohortAssignment(
                    submission_id=record.submission_id,
                    name=record.name,
                    rank=record.rank,
                    final_score=record.final_score,
                    status="assigned",
                    assigned_tier=assigned_tier,
                    choice_number=prefs.index(assigned_tier) + 1,
                    excluded_by_cost=excluded,
                    choices=prefs,
                )
            )
        else:
            waitlist.append(
                CohortAssignment(
                    submission_id=record.submission_id,
                    name=record.name,
                    rank=record.rank,
                    final_score=record.final_score,
                    status="waitlisted",
                    excluded_by_cost=excluded,
                    choices=prefs,
                    reason=_waitlist_reason(eligible, excluded, tiers[-1]),
                )
            )

    warnings: list[str] = []
    if needs_review_count:
        warnings.append(
            f"{needs_review_count} NEEDS_REVIEW applicant(s) are excluded from this assignment; "
            "resolve and re-rank before final cohort filling (PRD §11)."
        )
    if any(r.rank is None for r in ranked):
        warnings.append(
            "Some RANKED records carry no rank; they were processed last, in submission-id order."
        )

    summary = CohortSummary(
        total_ranked=len(ranked),
        assigned=len(assignments),
        waitlisted=len(waitlist),
        unassignable=len(unassignable),
        tiers={
            tier: TierSummary(
                capacity=cap[tier],
                filled=filled[tier],
                open_seats=(None if cap[tier] is None else cap[tier] - filled[tier]),
                first_choice_demand=first_choice_demand.get(tier, 0),
            )
            for tier in tiers
        },
        choice_satisfaction=dict(
            sorted(Counter(f"choice_{a.choice_number}" for a in assignments).items())
        ),
        needs_review_count=needs_review_count,
        warnings=warnings,
    )
    return CohortResult(
        assignments=assignments,
        waitlist=waitlist,
        unassignable=unassignable,
        summary=summary,
    )


# ================================================================================================
# 11.3 — output serialization (in-memory, stateless — same pattern as outputs.py)
# ================================================================================================


def _row_key(entry: CohortAssignment) -> tuple[bool, int, str]:
    return (entry.rank is None, entry.rank if entry.rank is not None else 0, entry.submission_id)


def cohort_assignments_csv(result: CohortResult) -> str:
    """All buckets as one rank-ordered CSV — one row per ``RANKED`` applicant.

    Per-tier rosters and the waitlist are filters of this file (``status`` / ``assigned_tier``
    columns), so staff gets a single download instead of four. ``choices`` shows the normalized
    preference order joined with `` > ``; ``excluded_by_cost`` lists tiers pruned by the
    first-choice cost ceiling, joined with `` | ``.
    """
    header = [
        "rank",
        "submission_id",
        "name",
        "final_score",
        "status",
        "assigned_tier",
        "choice_number",
        "excluded_by_cost",
        "choices",
        "reason",
    ]
    entries = sorted(
        [*result.assignments, *result.waitlist, *result.unassignable], key=_row_key
    )
    rows: list[list[object]] = [
        [
            entry.rank,
            entry.submission_id,
            entry.name,
            entry.final_score,
            entry.status,
            entry.assigned_tier,
            entry.choice_number,
            " | ".join(entry.excluded_by_cost),
            " > ".join(entry.choices),
            entry.reason,
        ]
        for entry in entries
    ]
    return _write_csv(header, rows)
