"""Stage 8 — aggregation and ranking (PRD §10). Deterministic, no LLM.

``final_score`` is a plain additive sum of non-negative subscores with nothing ever subtracted,
so a missing optional signal (0) is neutral. Only gate-survivors are scored and ranked at all,
which is what keeps a bonus from touching a rejection. There is no acceptance cutoff — the full
ranked list is the deliverable (§11).
"""

from __future__ import annotations

from ..config import AppConfig
from ..models import AuditRecord, Scores

# --- Pure score composition (PRD §10.1) ---


def compose_final_score(scores: Scores, cfg: AppConfig) -> float:
    """Sum the subscores: GPA 40 + essays 30 required, plus four additive-only bonuses
    (20 + 15 + 20 + 25) = 150 max. ``cfg`` is signature parity — the sum needs no knobs."""
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

    For gate-survivors only; ``REJECTED``/``NEEDS_REVIEW`` records stay at ``final_score=None``.
    """
    record.final_score = compose_final_score(record.scores, cfg)
    return record


# --- Outcome finalization + deterministic ranking (PRD §10.2) ---

# Decided by an earlier gate; must not be scored or ranked (§12 #2).
_TERMINAL_OUTCOMES = frozenset({"REJECTED", "NEEDS_REVIEW"})


def _rank_sort_key(record: AuditRecord) -> tuple[float, float, float, str]:
    """Tiebreaker chain (PRD §10.2): ``final_score`` → ``gpa_points`` → ``essay.total`` desc,
    then ``submission_id`` asc — the contract carries no submission timestamp, so the stable
    UUID is the final tiebreak. Numeric keys are negated to sort descending."""
    return (
        -(record.final_score or 0.0),
        -record.scores.gpa_points,
        -record.scores.essay.total,
        record.submission_id,
    )


def rank_records(records: list[AuditRecord], cfg: AppConfig) -> list[AuditRecord]:
    """Finalize outcomes and assign ranks (Stage 8). Mutates the records, returns the same list.

    Gate-survivors are scored and marked ``RANKED``, then sorted and given ``rank`` 1..N;
    terminal outcomes are forced to ``final_score=None``/``rank=None``. Input order is preserved
    — ``rank`` carries the ordering — and re-running is idempotent (§12 #5).
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


# --- Read-time, per-cohort ranking (PRD v3 §7) ---
# Rank is never stored: it is assigned on every read so the ranking is always live. Stored
# final_scores are reused rather than recomposed — a config change between grades must not
# silently reshuffle scores that were composed under the old one.


def assign_read_time_ranks(records: list[AuditRecord]) -> list[AuditRecord]:
    """Assign ``rank`` 1..N per ``cohort_name`` to RANKED records (others get ``None``).
    Deterministic regardless of input order (§10 invariant #5). Mutates + returns the list."""
    by_cohort: dict[str, list[AuditRecord]] = {}
    for record in records:
        if record.outcome == "RANKED" and record.final_score is not None:
            by_cohort.setdefault(record.cohort_name, []).append(record)
        else:
            record.rank = None
    for cohort_records in by_cohort.values():
        for position, record in enumerate(sorted(cohort_records, key=_rank_sort_key), start=1):
            record.rank = position
    return records
