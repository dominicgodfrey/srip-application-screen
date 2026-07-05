"""Tests for the pydantic schemas (Phase 0.3)."""

import pytest
from pydantic import ValidationError

from srip_filter.models import (
    AuditRecord,
    CourseItem,
    GpaAssessment,
    ResumeAssessment,
    TaskAOutput,
    TaskBOutput,
    TaskCOutput,
    TaskDOutput,
    TaskEOutput,
)

LLM_CONTRACTS = [TaskAOutput, TaskBOutput, TaskCOutput, TaskDOutput, TaskEOutput, CourseItem]


def test_task_a_valid() -> None:
    out = TaskAOutput(
        normalized_gpa=3.7,
        original_scale="weighted_gt_4",
        conversion_method="llm_estimate",
        confidence="med",
        requires_manual_review=False,
        rationale="Weighted 4.4 maps to ~3.7 unweighted.",
    )
    assert out.confidence == "med"
    assert out.normalized_gpa is not None and 0.0 <= out.normalized_gpa <= 4.0


def test_task_a_accepts_null_gpa() -> None:
    out = TaskAOutput(
        normalized_gpa=None,
        original_scale="unknown",
        conversion_method="none",
        confidence="low",
        requires_manual_review=True,
        rationale="No scale stated.",
    )
    assert out.normalized_gpa is None
    assert out.requires_manual_review is True


def test_task_b_range_validation() -> None:
    with pytest.raises(ValidationError):
        TaskBOutput(
            explanation_adequate=True,
            strength_of_reason=1.5,  # outside [0, 1]
            realistic=True,
            severity_vs_reason_balanced=True,
            recommended_outcome="rank",
            rationale="x",
        )


def test_task_d_score_bounds() -> None:
    with pytest.raises(ValidationError):
        TaskDOutput(
            is_gibberish=False,
            on_topic=True,
            relevance_confidence=0.9,
            quality_score=16,  # > 15 (v3 cap)
            grammar_spelling_penalty=0,
            saliency_notes="ok",
            rationale="ok",
        )


def test_course_category_literal() -> None:
    with pytest.raises(ValidationError):
        CourseItem(
            name="Underwater Basket Weaving",
            grade_raw="A",
            grade_pct=95,
            category="art",  # not in cs/math/data/other
            counts=False,
            category_weight=0.0,
        )


def test_task_c_round_trip() -> None:
    out = TaskCOutput(
        courses=[
            CourseItem(
                name="AP Computer Science A",
                grade_raw="A",
                grade_pct=95,
                category="cs",
                counts=True,
                category_weight=1.0,
            )
        ],
        rationale="One CS course.",
    )
    assert out.courses[0].category == "cs"


def test_task_e_valid_and_bounds() -> None:
    """Phase 12.1: Task E counts/classifies; negative counts and out-of-range relevance fail."""
    out = TaskEOutput(
        is_resume=True,
        relevant_projects=3,
        relevant_experience=1,
        relevant_awards=2,
        skills_relevance=0.8,
        highlights="Two shipped web apps; USACO silver.",
        rationale="Clear CS resume with concrete projects.",
    )
    assert out.is_resume is True
    with pytest.raises(ValidationError):
        TaskEOutput(
            is_resume=True,
            relevant_projects=-1,  # counts are never negative
            relevant_experience=0,
            relevant_awards=0,
            skills_relevance=0.5,
            highlights="x",
            rationale="x",
        )
    with pytest.raises(ValidationError):
        TaskEOutput(
            is_resume=True,
            relevant_projects=0,
            relevant_experience=0,
            relevant_awards=0,
            skills_relevance=1.5,  # outside [0, 1]
            highlights="x",
            rationale="x",
        )


def test_resume_assessment_defaults_and_no_content_fields() -> None:
    """Phase 12.1: the audit block defaults to 'not attempted' and never carries resume text."""
    block = ResumeAssessment()
    assert block.url_present is False
    assert block.attempted is False
    assert block.fetched is False
    assert block.extracted_chars == 0
    assert block.signals is None
    assert block.failure == ""
    # The fetch->extract->discard memory rule: no field can hold resume bytes or text.
    assert not any(
        name in ResumeAssessment.model_fields for name in ("text", "content", "body", "bytes")
    )


def test_audit_record_nests_resume_assessment() -> None:
    rec = AuditRecord(
        submission_id="x",
        outcome="RANKED",
        resume=ResumeAssessment(
            url_present=True,
            attempted=True,
            fetched=True,
            extracted_chars=4200,
            signals=TaskEOutput(
                is_resume=True,
                relevant_projects=2,
                relevant_experience=1,
                relevant_awards=0,
                skills_relevance=0.7,
                highlights="Python + JS projects.",
                rationale="Standard student resume.",
            ),
        ),
    )
    restored = AuditRecord.model_validate_json(rec.model_dump_json())
    assert restored.resume == rec.resume
    assert restored.resume.signals is not None
    assert restored.resume.signals.relevant_projects == 2


def test_extra_key_forbidden() -> None:
    valid = {
        "is_gibberish": False,
        "on_topic": True,
        "relevance_confidence": 0.9,
        "quality_score": 13,
        "grammar_spelling_penalty": 1,
        "saliency_notes": "n",
        "rationale": "r",
    }
    TaskDOutput.model_validate(valid)  # baseline: valid payload parses
    with pytest.raises(ValidationError):
        TaskDOutput.model_validate({**valid, "surprise": 1})


@pytest.mark.parametrize("model", LLM_CONTRACTS)
def test_llm_contracts_are_strict_schemas(model: type[TaskAOutput]) -> None:
    """Each LLM contract must map to an OpenAI strict json_schema: closed + all-required."""
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(model.model_fields)


def test_audit_record_minimal_and_round_trip() -> None:
    rec = AuditRecord(submission_id="abc-123", outcome="NEEDS_REVIEW")
    assert rec.final_score is None
    assert rec.rank is None
    assert rec.scores.resume_bonus == 0.0
    assert rec.reasons == []
    restored = AuditRecord.model_validate_json(rec.model_dump_json())
    assert restored == rec


def test_audit_record_nests_task_b() -> None:
    rec = AuditRecord(
        submission_id="x",
        outcome="RANKED",
        gpa=GpaAssessment(
            normalized_gpa=2.7,
            below_threshold=True,
            explanation_eval=TaskBOutput(
                explanation_adequate=True,
                strength_of_reason=0.8,
                realistic=True,
                severity_vs_reason_balanced=True,
                recommended_outcome="rank",
                rationale="Documented medical leave.",
            ),
        ),
    )
    assert rec.gpa.explanation_eval is not None
    assert rec.gpa.explanation_eval.recommended_outcome == "rank"
    restored = AuditRecord.model_validate_json(rec.model_dump_json())
    assert restored.gpa.explanation_eval == rec.gpa.explanation_eval
