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

from collections.abc import Sequence

from .models import ProgramChoices


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
