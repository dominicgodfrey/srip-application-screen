"""Cohort assignment — the PRD §11 downstream layer (Phase 11).

Turns the ranked filter output into program placements (honors / intensive / regular) under
configurable per-tier capacities. Entirely deterministic, pure, and LLM-free: the algorithm is
rank-greedy with displacement chains (augmenting paths), so it yields a **maximum-cardinality
matching with rank priority** — as many students seated as the capacities and their listed
choices allow, with stronger applications winning contested seats and, when nothing binds (the
realistic case), everyone landing in their first choice.

Hard invariants, mirroring PRD §11:
  * Only ``RANKED`` records are ever assignable. ``REJECTED`` can never resurface;
    ``NEEDS_REVIEW`` is excluded with a warning (resolve, re-rank, rerun).
  * Changing capacities only moves the assignment/waitlist boundary along the ranking.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

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
# 11.2 — rank-greedy assignment with displacement chains (maximum matching)
# ================================================================================================


@dataclass
class _Seat:
    """Mutable seating state for one assigned student (internal to the algorithm)."""

    record: AuditRecord
    choices: list[str]  # normalized preference order
    tier: str = ""  # tier currently seated in
    displaced_from: str | None = None  # most recent tier they were bumped out of


class _Board:
    """Seating state: who occupies which tier, against the per-tier caps."""

    def __init__(self, tiers: list[str], capacities: CohortCapacities) -> None:
        self.tiers = tiers
        self.cap: dict[str, int | None] = {tier: capacities.for_tier(tier) for tier in tiers}
        self.seats: dict[str, list[_Seat]] = {tier: [] for tier in tiers}

    def has_open(self, tier: str) -> bool:
        cap = self.cap[tier]
        return cap is None or len(self.seats[tier]) < cap

    def place(self, seat: _Seat, tier: str) -> None:
        self.seats[tier].append(seat)
        seat.tier = tier

    def move(self, seat: _Seat, dest: str) -> None:
        self.seats[seat.tier].remove(seat)
        seat.displaced_from = seat.tier
        self.seats[dest].append(seat)
        seat.tier = dest


def _weakness_key(seat: _Seat) -> tuple[bool, int, str]:
    """Sort key under which ``max`` picks the weakest occupant to displace.

    Lower rank number = stronger, so the largest rank is the weakest; a missing rank (possible
    only in a hand-edited decisions.jsonl) is weakest of all. ``submission_id`` breaks any tie
    deterministically.
    """
    rank = seat.record.rank
    return (rank is None, rank if rank is not None else 0, seat.record.submission_id)


def _open_alternative(board: _Board, seat: _Seat, blocked: frozenset[str]) -> str | None:
    """The occupant's highest-listed tier (other than their own) with an open seat."""
    for tier in seat.choices:
        if tier != seat.tier and tier not in blocked and board.has_open(tier):
            return tier
    return None


def _free_seat(
    board: _Board, tier: str, blocked: frozenset[str]
) -> list[tuple[_Seat, str]] | None:
    """Find a displacement chain that frees one seat in ``tier``; ``None`` if impossible.

    This is the augmenting-path search that makes the greedy walk a *maximum* matching: an
    occupant of ``tier`` is moved to another tier they themselves listed — directly into an open
    seat, or into a seat freed by a deeper chain (``blocked`` grows along the path, so chains
    never revisit a tier and are bounded by the tier count). Moves are returned deepest-first so
    applying them in order is always capacity-legal.

    Deterministic: the weakest (lowest-ranked) movable occupant is the one displaced, moving to
    *their* highest-listed open choice; deeper chains explore tiers in the configured order.
    """
    occupants = board.seats[tier]
    movable = [seat for seat in occupants if _open_alternative(board, seat, blocked) is not None]
    if movable:
        victim = max(movable, key=_weakness_key)
        dest = _open_alternative(board, victim, blocked)
        assert dest is not None  # by construction of `movable`
        return [(victim, dest)]
    for nxt in board.tiers:
        if nxt in blocked:
            continue
        movers = [seat for seat in occupants if nxt in seat.choices]
        if not movers:
            continue
        sub = _free_seat(board, nxt, blocked | {nxt})
        if sub is None:
            continue
        victim = max(movers, key=_weakness_key)
        return [*sub, (victim, nxt)]
    return None


def _try_seat(board: _Board, seat: _Seat) -> bool:
    """Seat one student: highest-listed open tier, else crack a listed tier open via a chain.

    Displacement fires only when *nothing* the student listed is open — it exists to seat a
    student who would otherwise go unmatched, never to upgrade anyone's choice. Processing in
    rank order with this augmentation yields a maximum-cardinality matching with rank priority.
    """
    for tier in seat.choices:
        if board.has_open(tier):
            board.place(seat, tier)
            return True
    for tier in seat.choices:
        chain = _free_seat(board, tier, frozenset({tier}))
        if chain is not None:
            for victim, dest in chain:
                board.move(victim, dest)
            board.place(seat, tier)
            return True
    return False


def assign_cohorts(
    records: Sequence[AuditRecord],
    capacities: CohortCapacities,
    cfg: AppConfig,
) -> CohortResult:
    """Assign every ``RANKED`` applicant to a program tier (PRD §11). Pure and deterministic.

    Walks the ranking top-down, seating each student in their highest-listed tier with space;
    when all their listed tiers are full, a displacement chain (see :func:`_free_seat`) reshuffles
    already-seated students *within their own listed choices* to make room. Students whose listed
    tiers stay full go to the rank-ordered waitlist; students with no parseable choice are
    ``unassignable`` (staff resolves). ``REJECTED`` is never seated; ``NEEDS_REVIEW`` is excluded
    with a warning so staff can preview sizing before every case is resolved.
    """
    tiers = list(cfg.cohort.tiers)
    board = _Board(tiers, capacities)

    ranked = sorted(
        (r for r in records if r.outcome == "RANKED"),
        key=lambda r: (r.rank is None, r.rank if r.rank is not None else 0, r.submission_id),
    )
    needs_review_count = sum(1 for r in records if r.outcome == "NEEDS_REVIEW")

    seated: list[_Seat] = []
    waitlisted: list[tuple[AuditRecord, list[str]]] = []
    no_choices: list[AuditRecord] = []
    first_choice_demand: Counter[str] = Counter()

    for record in ranked:
        prefs = normalize_choices(record.program_choices, tiers)
        if not prefs:
            no_choices.append(record)
            continue
        first_choice_demand[prefs[0]] += 1
        seat = _Seat(record=record, choices=prefs)
        if _try_seat(board, seat):
            seated.append(seat)
        else:
            waitlisted.append((record, prefs))

    # Entries are materialized only now: a later displacement chain can re-tier an earlier seat.
    assignments = [
        CohortAssignment(
            submission_id=s.record.submission_id,
            name=s.record.name,
            rank=s.record.rank,
            final_score=s.record.final_score,
            status="assigned",
            assigned_tier=s.tier,
            choice_number=s.choices.index(s.tier) + 1,
            displaced_from=s.displaced_from,
            choices=s.choices,
        )
        for s in seated
    ]
    waitlist = [
        CohortAssignment(
            submission_id=r.submission_id,
            name=r.name,
            rank=r.rank,
            final_score=r.final_score,
            status="waitlisted",
            choices=prefs,
            reason=f"All listed programs are at capacity: {', '.join(prefs)}",
        )
        for r, prefs in waitlisted
    ]
    unassignable = [
        CohortAssignment(
            submission_id=r.submission_id,
            name=r.name,
            rank=r.rank,
            final_score=r.final_score,
            status="unassignable",
            reason="No valid program choice could be parsed from the application.",
        )
        for r in no_choices
    ]

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
        displaced=sum(1 for a in assignments if a.displaced_from is not None),
        tiers={
            tier: TierSummary(
                capacity=board.cap[tier],
                filled=len(board.seats[tier]),
                open_seats=(
                    None if board.cap[tier] is None else board.cap[tier] - len(board.seats[tier])
                ),
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
