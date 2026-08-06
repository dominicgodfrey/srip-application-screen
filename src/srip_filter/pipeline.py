"""Orchestration — the per-application fail-fast runner (PRD v3 §4).

The transport-agnostic core that wires the stages together; ``api/`` is a thin shell over
:func:`make_grade_fn`. Nothing here knows about HTTP.

Stages run in fail-fast order, hard rejections before soft routing, so an applicant who both
fails a hard gate *and* leaves a blocker blank is ``REJECTED``, never ``NEEDS_REVIEW`` (§0.7).

``llm_calls`` is inferred from the stage results rather than by instrumenting the client —
e.g. a GPA assessment with ``source="llm"`` means Task A ran.
"""

from __future__ import annotations

from .config import AppConfig
from .gates.essays import Stage1Result, run_essay_gates_v3
from .gates.gpa import assess_gpa
from .ingest_webhook import WebhookApplicant, map_application_payload
from .llm.client import BaseLLMClient
from .models import (
    ApplicationPayload,
    AuditRecord,
    EssayTexts,
    HitGate,
    ProgramChoices,
)
from .resume_fetch import ResumeFetcher
from .scoring.aggregate import finalize_score
from .scoring.coursework import score_coursework
from .scoring.essays import Stage4Result, grade_essays
from .scoring.resume import score_resume
from .scoring.school import score_school
from .scoring.technical_essay import score_technical_essay
from .worker import GradeResult

# The Outcome literal has no "pending" value, so a base record starts here and a hard gate
# may overwrite it before the record is ever scored.
_PLACEHOLDER_OUTCOME = "RANKED"

# decided_at_stage labels; "error" marks the per-row isolation fallback.
_STAGE_1 = "stage1"
_STAGE_3 = "stage3"
_STAGE_4 = "stage4"
_STAGE_8 = "stage8"
_STAGE_ERROR = "error"


def _terminal(record: AuditRecord, outcome: str, stage: str, reason: str) -> AuditRecord:
    """Stamp a terminal outcome on a record and return it (REJECTED/NEEDS_REVIEW are unscored)."""
    record.outcome = outcome  # type: ignore[assignment]
    record.decided_at_stage = stage
    record.primary_reason = reason
    record.final_score = None
    record.rank = None
    return record


def _reconcile_gibberish(stage1: Stage1Result, stage4: Stage4Result) -> HitGate:
    """Merge Stage 1's heuristic gibberish finding with Task D's backstop, keeping terms
    essay-attributed (``eN:``) so the audit UI can highlight the right essay."""
    terms = list(stage1.gibberish.terms)
    for n, grade in ((1, stage4.e1_grade), (2, stage4.e2_grade)):
        if grade is not None and grade.is_gibberish:
            terms.append(f"e{n}:task_d")
    return HitGate(hit=stage1.gibberish.hit or stage4.gibberish.hit, terms=terms)


# --- The per-application runner ---
# The score is composed here on the way out; rank is read-time, per cohort, never stored (§7).

_STAGE_4B = "stage4b"


def _build_webhook_base_record(applicant: WebhookApplicant) -> AuditRecord:
    """Assemble the identity/metadata half of the audit record from the mapped payload."""
    row = applicant.row
    name = " ".join(part for part in (row.first_name, row.last_name) if part)
    record = AuditRecord(
        submission_id=row.submission_id,
        name=name,
        email=row.email,
        cohort_name=applicant.cohort_name,
        state_of_residence=applicant.state_of_residence,
        international=applicant.international,
        programming_languages=row.programming_languages,
        github_profile=row.github_profile,
        sub_track=row.sub_track,
        program_choices=ProgramChoices(
            first=row.first_choice or None,
            second=row.second_choice or None,
            third=row.third_choice or None,
        ),
        outcome=_PLACEHOLDER_OUTCOME,
    )
    record.essays = EssayTexts(e1=row.essay1, e2=row.essay2, e3=row.essay3)
    record.errors.extend(applicant.mapping_notes)
    return record


async def grade_webhook_applicant(
    applicant: WebhookApplicant,
    client: BaseLLMClient,
    cfg: AppConfig,
    fetcher: ResumeFetcher | None = None,
    *,
    bypass_gates: bool = False,
) -> AuditRecord:
    """Grade one webhook application through the fail-fast pipeline (PRD v3 §4).

    Returns a terminal record: ``REJECTED``/``NEEDS_REVIEW`` unscored, or ``RANKED`` with
    ``final_score`` composed. Any unexpected error becomes ``NEEDS_REVIEW`` plus an
    ``errors[]`` note (invariant #9).

    ``bypass_gates`` is the manual-promote path (PRD v3 §6), never used by the worker: every
    gate verdict is still computed and recorded, but none is terminal — unscoreable signals
    contribute 0 and the record leaves ``RANKED`` with ``manual_override=True``.
    """
    record = _build_webhook_base_record(applicant)
    row = applicant.row
    try:
        # Unscoreable before any gate can misread the blanks as an applicant failure (§0.7).
        essays_scoreable = not applicant.missing_required_essays
        if applicant.missing_required_essays and not bypass_gates:
            return _terminal(
                record,
                "NEEDS_REVIEW",
                _STAGE_1,
                "Payload delivered fewer than two required essays (contract drift)",
            )

        # Stage 1 — gibberish rejects, profanity routes to a human. Rejection is checked
        # first: where both fire, the definite verdict wins (PRD §0.7).
        stage1 = run_essay_gates_v3(applicant, cfg)
        record.gates.essay_length = stage1.length_gate
        record.gates.profanity = stage1.profanity
        record.gates.gibberish = stage1.gibberish
        if stage1.rejected:
            if not bypass_gates:
                return _terminal(record, "REJECTED", _STAGE_1, stage1.primary_reason)
            record.reasons.append(f"OVERRIDE: stage1 gate bypassed ({stage1.primary_reason})")
        elif stage1.needs_review:
            if not bypass_gates:
                return _terminal(record, "NEEDS_REVIEW", _STAGE_1, stage1.primary_reason)
            record.reasons.append(f"OVERRIDE: profanity flag bypassed ({stage1.primary_reason})")

        # Stage 2-3 — GPA (structured input; weighted-only forces Task A).
        gpa = await assess_gpa(row, client, cfg, force_task_a=applicant.force_task_a)
        record.gpa = gpa.assessment
        record.gates.gpa_gate = gpa.gate
        if gpa.assessment.source == "llm":
            record.llm_calls.append("task_a")
        if gpa.assessment.explanation_eval is not None:
            record.llm_calls.append("task_b")
        if gpa.verdict == "reject" and not bypass_gates:
            return _terminal(record, "REJECTED", _STAGE_3, gpa.reason)
        if gpa.verdict == "needs_review" and not bypass_gates:
            return _terminal(record, "NEEDS_REVIEW", _STAGE_3, gpa.reason)
        if gpa.verdict == "pass":
            record.scores.gpa_points = gpa.gpa_points
            record.reasons.append(f"PASS gpa_gate: {gpa.gate.reason}")
        else:  # bypassed: an unscoreable GPA contributes 0, never blocks
            record.scores.gpa_points = 0.0
            record.reasons.append(f"OVERRIDE: gpa gate bypassed ({gpa.reason}); 0 points")

        # Stage 4 — required essays (Task D ×2); prompts come from the payload itself, so
        # they can never drift from the live form.
        if essays_scoreable:
            stage4 = await grade_essays(
                row,
                applicant.e1.question or "(essay question not delivered in payload)",
                applicant.e2.question or "(essay question not delivered in payload)",
                client,
                cfg,
                # No per-essay range: the payload carries no bounds, so Task D uses its default.
            )
            record.llm_calls.extend(("task_d_e1", "task_d_e2"))
            record.gates.essay_relevance = stage4.essay_relevance
            record.gates.gibberish = _reconcile_gibberish(stage1, stage4)
            if stage4.verdict == "reject" and not bypass_gates:
                return _terminal(record, "REJECTED", _STAGE_4, stage4.primary_reason)
            if stage4.verdict == "needs_review" and not bypass_gates:
                return _terminal(record, "NEEDS_REVIEW", _STAGE_4, stage4.primary_reason)
            record.scores.essay = stage4.subscores
            if stage4.verdict == "pass":
                record.reasons.append(
                    f"essays on-topic; quality total {stage4.subscores.total}"
                )
            else:
                record.reasons.append(
                    f"OVERRIDE: essay gate bypassed ({stage4.primary_reason}); "
                    "gated essays score 0"
                )
        else:  # bypass with missing essays: nothing to grade
            record.reasons.append("OVERRIDE: required essays missing from payload; essays 0")

        # Stage 4b — technical-essay bonus (Task F; bonus-only, absent → free no-op).
        stage4b = await score_technical_essay(
            row.essay3,
            applicant.e3.question or "(technical essay prompt not delivered in payload)",
            client,
            cfg,
            # No max_words: the site server-validates the cap at submit, so the over-max
            # rung cannot fire from a real applicant.
        )
        record.scores.technical_essay_bonus = stage4b.bonus
        record.technical_essay = stage4b.assessment
        if stage4b.llm_called:
            record.llm_calls.append("task_f")
        record.errors.extend(stage4b.errors)
        if stage4b.bonus > 0:
            record.reasons.append(f"technical essay bonus {stage4b.bonus}")

        # Stage 5 — coursework bonus (Task C; empty cell → free no-op).
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

        # Stage 6 — resume bonus (bonus_max=0 → free no-op).
        stage6 = await score_resume(row, fetcher, client, cfg)
        record.scores.resume_bonus = stage6.bonus
        record.resume = stage6.assessment
        if stage6.task_e_called:
            record.llm_calls.append("task_e")
        if stage6.error:
            record.errors.append(stage6.error)

        # Stage 7 — school bonus.
        school = score_school(row, cfg)
        record.scores.school_bonus = school.bonus
        record.school_match = school.match
        if school.match.matched_name:
            record.reasons.append(
                f"school match: {school.match.matched_name} ({school.match.list})"
            )

        # Survivor — compose the score (Stage 8); rank is read-time, per cohort (§7).
        record.outcome = "RANKED"
        if bypass_gates:
            record.manual_override = True
            record.decided_at_stage = "manual_override"
            record.primary_reason = "Manually promoted into the ranking"
        else:
            record.decided_at_stage = _STAGE_8
            record.primary_reason = "Survived all gates"
        finalize_score(record, cfg)
        return record
    except Exception as exc:  # per-row isolation (invariant #9)
        record.errors.append(f"{type(exc).__name__}: {exc}")
        return _terminal(record, "NEEDS_REVIEW", _STAGE_ERROR, "Unexpected error during grading")


def make_grade_fn(
    client: BaseLLMClient, cfg: AppConfig, fetcher: ResumeFetcher | None = None
):
    """Bind the runner into the worker's ``GradeFn`` shape.

    The stored payload is re-validated here — it passed validation at the edge, but the DB is
    not trusted blindly. A corrupt one raises into the worker's per-row handler (invariant #9).

    The Stage-6 fetcher is built off ``resume.bonus_max``, so raising it is what turns the
    stage on: without a fetcher ``score_resume`` no-ops silently, which is the worst way for a
    stage to be off. At ``bonus_max: 0`` nothing is built and the kill switch stays free.
    """
    if fetcher is None and cfg.resume.bonus_max > 0:
        fetcher = ResumeFetcher(cfg)

    async def grade_fn(db_row: dict) -> GradeResult:
        payload = ApplicationPayload.model_validate(db_row["payload"])
        applicant = map_application_payload(payload)
        record = await grade_webhook_applicant(applicant, client, cfg, fetcher)
        return GradeResult(
            audit_record=record.model_dump(mode="json"),
            outcome=record.outcome,
            final_score=record.final_score,
        )

    return grade_fn
