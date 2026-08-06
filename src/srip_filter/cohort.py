"""Cohort assignment — the PRD §11 downstream layer. Deterministic, pure, and LLM-free.

Ranked output becomes program placements under the tiered cost model, where ``cohort.tiers``
order (most expensive first) is load-bearing. Capped tiers fill strictly by rank among the
students who chose them. Three hard rules:

  * **Cost ceiling.** Never place a student in a tier above their *first choice*, even one they
    ranked #2 — higher tiers cost more, and the first choice caps what they signed up to pay.
  * **No silent overflow.** A student whose eligible choices are all full is waitlisted for a
    staff decision, never auto-placed in a tier they did not list.
  * Only ``RANKED`` records are assignable; ``NEEDS_REVIEW`` is excluded with a warning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence

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
    """Parse free-text program choices into an ordered tier preference list.

    The choice strings are inconsistently punctuated, so each slot matches by containment of
    exactly one canonical tier token; zero tokens or more than one is dropped, never guessed.
    Repeats dedupe — listing one tier three times means "this tier or nothing", not three choices.
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


# --- Rank-greedy assignment under the tiered cost model ---


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

    Walks the ranking top-down: each student's choices are pruned by the cost ceiling, then they
    take the first one with an open seat. No open eligible tier means waitlisted; no parseable
    choice at all means ``unassignable``.
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
                    email=record.email,
                    phone=record.phone,
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
                    email=record.email,
                    phone=record.phone,
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
                    email=record.email,
                    phone=record.phone,
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
            "resolve and re-rank them before final cohort filling."
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


# --- Output serialization (in-memory, stateless — the outputs.py pattern) ---


def _rank_key(entry: CohortAssignment) -> tuple[bool, int, str]:
    return (entry.rank is None, entry.rank if entry.rank is not None else 0, entry.submission_id)


def _cohort_sort_key(result: CohortResult) -> Callable[[CohortAssignment], tuple]:
    """Sort key grouping rows by assigned cohort in tier order, then rank — so each cohort's
    roster reads as one contiguous block, with waitlisted and unassignable rows after."""
    tier_order = {tier: position for position, tier in enumerate(result.summary.tiers)}
    unplaced = len(tier_order)  # sorts after every real tier

    def key(entry: CohortAssignment) -> tuple:
        group = tier_order.get(entry.assigned_tier or "", unplaced)
        status_order = (
            0 if entry.status == "assigned" else 1 if entry.status == "waitlisted" else 2
        )
        return (group, status_order, *_rank_key(entry))

    return key


def cohort_assignments_csv(result: CohortResult) -> str:
    """All buckets as one CSV, grouped by assigned cohort then rank, so staff can read or split
    the file by cohort directly. One row per ``RANKED`` applicant."""
    header = [
        "assigned_tier",
        "rank",
        "submission_id",
        "name",
        "email",
        "phone",
        "final_score",
        "status",
        "choice_number",
        "excluded_by_cost",
        "choices",
        "reason",
    ]
    entries = sorted(
        [*result.assignments, *result.waitlist, *result.unassignable],
        key=_cohort_sort_key(result),
    )
    rows: list[list[object]] = [
        [
            entry.assigned_tier,
            entry.rank,
            entry.submission_id,
            entry.name,
            entry.email,
            entry.phone,
            entry.final_score,
            entry.status,
            entry.choice_number,
            " | ".join(entry.excluded_by_cost),
            " > ".join(entry.choices),
            entry.reason,
        ]
        for entry in entries
    ]
    return _write_csv(header, rows)


def cohort_roster_filename(tier: str) -> str:
    """Download filename for one cohort's roster CSV."""
    return f"cohort_{tier}.csv"


def cohort_roster_csv(result: CohortResult, tier: str) -> str:
    """One cohort's roster by rank with contact details — the staff export for outreach once an
    allocation is settled. Only ``assigned`` rows for the requested tier."""
    header = ["rank", "submission_id", "name", "email", "phone", "final_score"]
    members = sorted((a for a in result.assignments if a.assigned_tier == tier), key=_rank_key)
    rows: list[list[object]] = [
        [a.rank, a.submission_id, a.name, a.email, a.phone, a.final_score] for a in members
    ]
    return _write_csv(header, rows)
