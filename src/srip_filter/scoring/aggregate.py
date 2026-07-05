"""Stage 8 — aggregation and ranking (Phase 7).

Entirely **deterministic** — no LLM. Composes the additive ``final_score`` for gate-survivors
*only*, finalizes the three outcomes, and ranks the ``RANKED`` applicants with a deterministic
tiebreaker (PRD §10). The work is split by concern:

  * 7.1 pure score composition       — :func:`compose_final_score`, :func:`finalize_score`
  * 7.2 outcome finalize + ranking   — :func:`rank_records`

Hard invariants (PRD §0.3/§10.1/§12): ``final_score`` is the plain additive sum of the five §10.1
subscores, each ≥ 0 and **none subtracted** — so a missing optional signal (coursework / school /
resume = 0) is neutral and can never lower the total. Bonuses are computed before this stage and
can neither manufacture nor rescue a ``REJECTED`` outcome: only ``RANKED`` applicants are scored
and ranked at all. There is **no acceptance cutoff** here — the full ranked list is the deliverable
(§11).

No new config: the per-component caps already live in their own config sections, and the
composition is a pure sum of the existing :class:`Scores` subscores.
"""

from __future__ import annotations

from ..config import AppConfig
from ..models import AuditRecord, Scores

# ================================================================================================
# 7.1 — Pure score composition (PRD §10.1)
# ================================================================================================
# final_score = gpa_points + essay.total + coursework_bonus + school_bonus + resume_bonus.
# Every term is ≥ 0 and additive; nothing is ever subtracted, so an absent optional signal (0)
# is neutral (§0.3 / §12 #1). resume_bonus is inert (0) in current scope (§7.2).


def compose_final_score(scores: Scores, cfg: AppConfig) -> float:
    """Sum the score components into the additive ``final_score``. Pure.

    v3 (SCORING.md): ``gpa_points + essay.total + technical_essay_bonus + coursework_bonus
    + school_bonus + resume_bonus`` — required signals (GPA 40 + essays 30) plus the four
    additive-only bonuses (20 + 15 + 20 + 25 = 150 max). Each term is non-negative and
    none is subtracted, so a missing optional signal left at 0 never lowers the total
    (invariant #1). ``cfg`` is accepted for signature parity with the other scoring entry
    points and future composition tuning; the current sum needs no knobs.
    """
    return round(
        scores.gpa_points
        + scores.essay.total
        + scores.technical_essay_bonus
        + scores.coursework_bonus
        + scores.school_bonus
        + scores.resume_bonus,
        4,
    )


def finalize_score(record: AuditRecord, cfg: AppConfig) -> AuditRecord:
    """Write the composed ``final_score`` onto a record (mutates in place, returns it).

    Thin wrapper over :func:`compose_final_score` applied to ``record.scores``. Intended for
    ``RANKED`` applicants only — :func:`rank_records` calls it on gate-survivors and leaves
    ``REJECTED``/``NEEDS_REVIEW`` records at ``final_score=None``.
    """
    record.final_score = compose_final_score(record.scores, cfg)
    return record


# ================================================================================================
# 7.2 — Outcome finalization + deterministic ranking (Stage 8 aggregator, PRD §10.2)
# ================================================================================================
# Every gate-survivor (outcome not already REJECTED/NEEDS_REVIEW) is scored and marked RANKED;
# RANKED applicants are sorted with a deterministic tiebreaker and assigned rank 1..N. There is
# NO acceptance cutoff — the full ranked list is the deliverable (§11). REJECTED/NEEDS_REVIEW
# records are left unscored/unranked, so a bonus can never change a rejection (§12 #2).

# Outcomes that were decided by an earlier hard gate / blocker and must not be scored or ranked.
_TERMINAL_OUTCOMES = frozenset({"REJECTED", "NEEDS_REVIEW"})


def _rank_sort_key(record: AuditRecord) -> tuple[float, float, float, str]:
    """Deterministic tiebreaker chain (PRD §10.2): ``final_score`` desc → ``gpa_points`` desc →
    ``essay.total`` desc → ``submission_id`` asc.

    The §2 data contract carries no submission timestamp, so the stable UUID ``submission_id`` is
    the final tiebreak — keeping reruns identical (§12 #5) without depending on a field we lack.
    Numeric keys are negated so a single ascending sort yields descending score order.
    """
    return (
        -(record.final_score or 0.0),
        -record.scores.gpa_points,
        -record.scores.essay.total,
        record.submission_id,
    )


def rank_records(records: list[AuditRecord], cfg: AppConfig) -> list[AuditRecord]:
    """Finalize outcomes and assign ranks (Stage 8). Mutates the records, returns the same list.

    Each record whose ``outcome`` is **not** already ``REJECTED``/``NEEDS_REVIEW`` is a
    gate-survivor: it receives its composed ``final_score`` (7.1) and ``outcome="RANKED"``.
    ``RANKED`` applicants are then sorted by the deterministic tiebreaker (:func:`_rank_sort_key`)
    and assigned ``rank`` 1..N. ``REJECTED``/``NEEDS_REVIEW`` records are forced to
    ``final_score=None``/``rank=None`` (a bonus can never score or rank a rejection — §12 #2).

    The input order is preserved in the returned list; ``rank`` carries the ordering. Re-running on
    already-ranked records is idempotent — the same scores re-compose and re-sort identically
    (§12 #5), so ranking is stable across reruns.
    """
    ranked: list[AuditRecord] = []
    for record in records:
        if record.outcome in _TERMINAL_OUTCOMES:
            record.final_score = None
            record.rank = None
            continue
        record.outcome = "RANKED"
        finalize_score(record, cfg)
        ranked.append(record)

    for position, record in enumerate(sorted(ranked, key=_rank_sort_key), start=1):
        record.rank = position

    return records
