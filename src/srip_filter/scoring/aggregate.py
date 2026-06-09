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
    """Sum the five §10.1 score components into the additive ``final_score``. Pure.

    ``gpa_points + essay.total + coursework_bonus + school_bonus + resume_bonus`` — required
    signals (GPA + essays) plus the three additive-only bonuses. Each term is non-negative and
    none is subtracted, so a missing optional signal (coursework / school / resume left at 0)
    never lowers the total (§12 #1). ``cfg`` is accepted for signature parity with the other
    scoring entry points and future composition tuning; the current sum needs no knobs.
    """
    return round(
        scores.gpa_points
        + scores.essay.total
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
