"""Orchestration — the ordered fail-fast batch runner (Phase 8).

This is the transport-agnostic core that wires Stages 0→9 together; the FastAPI shell (Phase 9)
is a thin layer over :func:`grade_batch`. Nothing here knows about HTTP.

Per-row fail-fast order (PRD §0.1/§3, §10.2) — hard rejections precede soft routing so an
applicant who both fails a hard gate *and* leaves a blocker blank is ``REJECTED``, never
``NEEDS_REVIEW`` (§0.7):

  Stage 1 essay gates          → REJECTED (stop, zero LLM spend)
  affirmation validity         → NEEDS_REVIEW (stop, zero LLM spend)
  Stage 2-3 GPA                → REJECTED / NEEDS_REVIEW
  Stage 4 essay grading        → REJECTED / NEEDS_REVIEW
  Stages 5/6/7 bonuses         → additive only (never change the outcome)
  survivor                     → RANKED (final_score/rank filled by Stage 8)

The module is built up across Phase 8:

  * 8.1 deterministic glue   — :func:`build_base_record`, :func:`affirmation_ok`  (this commit)
  * 8.2 per-applicant runner — :func:`grade_one`                                  (next)
  * 8.3 batch runner         — :func:`grade_batch`
  * 8.4 end-to-end suite     — ``tests/test_pipeline.py``

The 8.1 glue is pure and zero-spend: assembling the identity/dedup half of the audit record and
the deterministic affirmation check, isolated so they are fully testable without the LLM.
"""

from __future__ import annotations

from .ingest import AFFIRMATION, ApplicantRow, DedupedRow, HeaderResolution
from .models import AuditRecord, ProgramChoices

# A gate-survivor leaves grade_one marked RANKED with final_score=None; Stage 8 rank_records
# (which treats any non-terminal outcome as a survivor) fills score + rank. The AuditRecord
# Outcome literal has no "pending" value, so RANKED is the non-terminal placeholder a base
# record starts at — a hard gate may overwrite it with REJECTED/NEEDS_REVIEW.
_PLACEHOLDER_OUTCOME = "RANKED"


def build_base_record(deduped: DedupedRow, resolution: HeaderResolution) -> AuditRecord:
    """Assemble the identity / dedup half of an :class:`AuditRecord` (deterministic, no LLM).

    Fills ``submission_id``/``name``/``email``, ``program_choices`` (first/second/third, empty →
    ``None``), and the ``dedup`` block from :class:`~srip_filter.ingest.DedupInfo`. Scoring,
    gates, and the terminal outcome are left at their defaults for :func:`grade_one` to fill;
    ``outcome`` starts at the non-terminal ``RANKED`` placeholder. ``resolution`` is accepted for
    signature parity with :func:`affirmation_ok` and future header-aware assembly.
    """
    row = deduped.row
    name = " ".join(part for part in (row.first_name, row.last_name) if part)
    return AuditRecord(
        submission_id=row.submission_id,
        name=name,
        email=row.email,
        program_choices=ProgramChoices(
            first=row.first_choice or None,
            second=row.second_choice or None,
            third=row.third_choice or None,
        ),
        dedup=deduped.dedup,
        outcome=_PLACEHOLDER_OUTCOME,
    )


def affirmation_ok(row: ApplicantRow, resolution: HeaderResolution) -> bool:
    """True unless the truthfulness affirmation resolved *and* is unchecked (blank) (PRD §2/§10.2).

    An unchecked affirmation → ``NEEDS_REVIEW``. But the check **only fires when the affirmation
    column actually resolved** (is in ``resolution.role_to_header``): the affirmation role is
    optional in the §2 contract, and an absent column must not be read as "everyone left it
    blank" and blanket-route the whole batch (§0.7 — never silently route). A checked affirmation
    carries the affirmation text / a non-empty value; an unchecked one is "".
    """
    if AFFIRMATION not in resolution.role_to_header:
        return True
    return bool(row.affirmation.strip())
