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

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .config import AppConfig
from .gates.essays import run_essay_gates
from .gates.gpa import assess_gpa
from .ingest import (
    AFFIRMATION,
    ESSAY1,
    ESSAY2,
    ApplicantRow,
    DedupedRow,
    HeaderResolution,
    IngestReport,
    ingest_csv,
)
from .llm.client import BaseLLMClient
from .models import AuditRecord, HitGate, ProgramChoices
from .outputs import (
    build_summary,
    decisions_jsonl,
    needs_review_csv,
    ranked_csv,
    rejected_csv,
)
from .scoring.aggregate import rank_records
from .scoring.coursework import score_coursework
from .scoring.essays import grade_essays
from .scoring.resume import resume_bonus
from .scoring.school import score_school

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


# ================================================================================================
# 8.2 — Per-applicant fail-fast runner (LLM)
# ================================================================================================
# grade_one sequences the stages in fail-fast order on one row, filling every audit block as it
# goes and setting the terminal outcome + decided_at_stage + primary_reason the moment a gate
# fires — so zero LLM tokens are spent past a Stage-1 reject or an unchecked affirmation. Survivors
# leave as RANKED with final_score=None (Stage 8 fills score/rank). The whole body is wrapped in
# try/except so any unexpected error becomes a NEEDS_REVIEW row, never an aborted batch ("when
# grading begins, it finishes").
#
# llm_calls is inferred from the stage results rather than instrumenting the client: a GPA
# assessment with source="llm" means Task A ran, a populated explanation_eval means Task B ran;
# reaching Stage 4 means both Task D calls were attempted; a non-empty coursework cell means Task C.

# decided_at_stage labels (PRD §3 pipeline). "affirmation" sits between Stage 1 and Stage 2;
# "error" marks the per-row isolation fallback.
_STAGE_1 = "stage1"
_STAGE_AFFIRMATION = "affirmation"
_STAGE_3 = "stage3"
_STAGE_4 = "stage4"
_STAGE_8 = "stage8"
_STAGE_ERROR = "error"


def _terminal(
    record: AuditRecord, outcome: str, stage: str, reason: str
) -> AuditRecord:
    """Stamp a terminal outcome on a record and return it (REJECTED/NEEDS_REVIEW are unscored)."""
    record.outcome = outcome  # type: ignore[assignment]
    record.decided_at_stage = stage
    record.primary_reason = reason
    record.final_score = None
    record.rank = None
    return record


async def grade_one(
    deduped: DedupedRow,
    resolution: HeaderResolution,
    client: BaseLLMClient,
    cfg: AppConfig,
) -> AuditRecord:
    """Grade one applicant through the ordered fail-fast pipeline (Stages 1→7).

    Hard gates run before soft routing (REJECTED precedes NEEDS_REVIEW, §0.7) and before any LLM
    spend. Each stage fills its audit blocks; the first gate to fire stamps the terminal outcome
    and returns immediately. A gate-survivor returns ``outcome="RANKED"`` with ``final_score=None``
    for Stage 8 (:func:`~srip_filter.scoring.aggregate.rank_records`) to score and rank. A Task B/D
    parse failure routes to ``NEEDS_REVIEW`` (handled inside those stages); a coursework parse
    failure stays bonus-neutral. Any unexpected error → ``NEEDS_REVIEW`` with an ``errors[]`` note.
    """
    record = build_base_record(deduped, resolution)
    row = deduped.row
    try:
        # Stage 1 — essay deterministic gates (token-free; all checks computed for the audit).
        stage1 = run_essay_gates(row, cfg)
        record.gates.essay_length = stage1.length_gate
        record.gates.profanity = stage1.profanity
        record.gates.gibberish = stage1.gibberish
        if stage1.rejected:
            return _terminal(record, "REJECTED", _STAGE_1, stage1.primary_reason)

        # Affirmation validity — unchecked truthfulness affirmation → NEEDS_REVIEW (token-free).
        if not affirmation_ok(row, resolution):
            return _terminal(
                record,
                "NEEDS_REVIEW",
                _STAGE_AFFIRMATION,
                "Truthfulness affirmation not checked",
            )

        # Stage 2-3 — GPA normalization + gate (LLM Task A/B only when needed).
        gpa = await assess_gpa(row, client, cfg)
        record.gpa = gpa.assessment
        record.gates.gpa_gate = gpa.gate
        if gpa.assessment.source == "llm":
            record.llm_calls.append("task_a")
        if gpa.assessment.explanation_eval is not None:
            record.llm_calls.append("task_b")
        if gpa.verdict == "reject":
            return _terminal(record, "REJECTED", _STAGE_3, gpa.reason)
        if gpa.verdict == "needs_review":
            return _terminal(record, "NEEDS_REVIEW", _STAGE_3, gpa.reason)
        record.scores.gpa_points = gpa.gpa_points
        record.reasons.append(f"PASS gpa_gate: {gpa.gate.reason}")

        # Stage 4 — essay LLM grading (Task D ×2). The two resolved essay-question headers are the
        # prompts the applicant answered (required roles, always present after a clean ingest).
        stage4 = await grade_essays(
            row,
            stage1.length_penalty_e1,
            stage1.length_penalty_e2,
            resolution.role_to_header[ESSAY1],
            resolution.role_to_header[ESSAY2],
            client,
            cfg,
        )
        record.llm_calls.extend(("task_d_e1", "task_d_e2"))
        record.gates.essay_relevance = stage4.essay_relevance
        # Reconcile the two gibberish findings: Stage 1's cheap heuristic + Task D's backstop.
        record.gates.gibberish = HitGate(hit=stage1.gibberish.hit or stage4.gibberish.hit)
        if stage4.verdict == "reject":
            return _terminal(record, "REJECTED", _STAGE_4, stage4.primary_reason)
        if stage4.verdict == "needs_review":
            return _terminal(record, "NEEDS_REVIEW", _STAGE_4, stage4.primary_reason)
        record.scores.essay = stage4.subscores
        record.reasons.append(f"essays on-topic; quality total {stage4.subscores.total}")

        # Stages 5/6/7 — bonuses (additive only; never change the outcome).
        coursework = await score_coursework(row, client, cfg)
        record.scores.coursework_bonus = coursework.bonus
        record.coursework_breakdown = coursework.courses
        if row.coursework.strip():
            record.llm_calls.append("task_c")
        if coursework.error:
            record.errors.append(coursework.error)
        counting = sum(1 for c in coursework.courses if c.counts)
        if counting:
            record.reasons.append(f"coursework: {counting} counting course(s)")

        record.scores.resume_bonus = resume_bonus(row, cfg)

        school = score_school(row, cfg)
        record.scores.school_bonus = school.bonus
        record.school_match = school.match
        if school.match.matched_name:
            record.reasons.append(
                f"school match: {school.match.matched_name} ({school.match.list})"
            )

        # Survivor — leave RANKED with final_score=None; Stage 8 composes score + assigns rank.
        record.outcome = "RANKED"
        record.decided_at_stage = _STAGE_8
        record.primary_reason = "Survived all gates"
        return record
    except Exception as exc:  # per-row isolation: a bad row → NEEDS_REVIEW, never an aborted batch
        record.errors.append(f"{type(exc).__name__}: {exc}")
        return _terminal(
            record, "NEEDS_REVIEW", _STAGE_ERROR, "Unexpected error during grading"
        )


# ================================================================================================
# 8.3 — Batch runner (LLM)
# ================================================================================================
# grade_batch runs Stage 0 ingest, fires grade_one for every kept row concurrently (the client's
# Semaphore bounds real concurrency, so no extra pool here), then Stage 8 ranking and Stage 9
# output emission. It returns everything in memory — the records, the five artifacts, and the
# Stage-0 report — so a stateless API can stream them back and never persist to disk.


@dataclass(frozen=True)
class BatchResult:
    """Everything a run produces, in memory (PRD §10/§12 + the Stage-0 report).

    ``records`` are the finalized, ranked :class:`AuditRecord`s (the source of truth). The four
    string artifacts and ``summary`` dict are the §12 deliverables; ``ingest_report`` accounts for
    every input row dropped or flagged before grading so a shrinking row count is explained.
    Nothing is written to disk — :func:`~srip_filter.outputs.write_outputs` is the opt-in path.
    """

    records: list[AuditRecord]
    decisions_jsonl: str
    ranked_csv: str
    rejected_csv: str
    needs_review_csv: str
    summary: dict
    ingest_report: IngestReport


async def grade_batch(
    source: str | Path | bytes | IO[bytes],
    client: BaseLLMClient,
    cfg: AppConfig,
    progress: Callable[[int, int], None] | None = None,
) -> BatchResult:
    """Run the whole pipeline over an uploaded CSV: ingest → grade → rank → emit (Stages 0-9).

    Stage 0 :func:`~srip_filter.ingest.ingest_csv` reads, validates, de-identifies, and dedups the
    upload (raising :class:`~srip_filter.ingest.HeaderValidationError` if the columns can't satisfy
    the contract — the API turns that into a 4xx). Every kept row is graded by :func:`grade_one`
    concurrently (bounded by the client's semaphore); per-row ``try/except`` means one bad row
    becomes a ``NEEDS_REVIEW`` record, never an aborted batch. Stage 8
    :func:`~srip_filter.scoring.aggregate.rank_records` composes scores and assigns ranks, then the
    Stage 9 serializers build the five in-memory artifacts. Stateless — nothing is persisted.

    ``progress`` is an optional ``(rows_done, rows_total)`` callback for a caller that wants live
    progress (the API poll, Phase 9.3). It is invoked once with ``(0, total)`` after ingest and
    again after each row finishes; the final call is ``(total, total)``. The core never imports the
    caller — this is the only HTTP-aware seam, kept signature-compatible (default ``None``). Safe
    under the concurrent gather: increments happen at ``await`` boundaries on the single event loop,
    so the counter is never raced.
    """
    ingest = ingest_csv(source)
    total = len(ingest.rows)
    if progress is not None:
        progress(0, total)

    done = 0

    async def _graded(deduped: DedupedRow) -> AuditRecord:
        nonlocal done
        record = await grade_one(deduped, ingest.resolution, client, cfg)
        done += 1
        if progress is not None:
            progress(done, total)
        return record

    records = list(await asyncio.gather(*(_graded(deduped) for deduped in ingest.rows)))
    rank_records(records, cfg)
    return BatchResult(
        records=records,
        decisions_jsonl=decisions_jsonl(records),
        ranked_csv=ranked_csv(records),
        rejected_csv=rejected_csv(records),
        needs_review_csv=needs_review_csv(records),
        summary=build_summary(records),
        ingest_report=ingest.report,
    )
