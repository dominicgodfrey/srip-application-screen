"""P9 — live webhook payload → ApplicantRow mapping tests (pure, zero spend)."""

from __future__ import annotations

from srip_filter.ingest_webhook import is_international, map_application_payload
from srip_filter.models import ApplicationPayload

from .live_payload import make_payload


def _mapped(**kwargs):
    return map_application_payload(ApplicationPayload.model_validate(make_payload(**kwargs)))


def test_full_mapping_round_trip() -> None:
    a = _mapped()
    row = a.row
    assert row.first_name == "Syn Thetic" and row.last_name == ""
    assert row.email == "syn@example.com"
    assert row.gpa == "3.95/4.0"  # unweighted primary
    assert a.force_task_a is False
    assert row.essay1 == "essay one" and row.essay2 == "essay two"
    assert row.essay3 == "essay three"
    assert row.first_choice.endswith("HONORS")
    assert a.cohort_name == "SP27-CSE"
    assert a.international is False
    assert not a.missing_required_essays
    assert a.mapping_notes == ()


def test_named_fields_come_from_all_answers() -> None:
    """institution / coursework / state / gpa_explanation exist only in all_answers."""
    a = _mapped(answers={"institution": "MIT", "gpa_explanation": "  circumstances  "})
    assert a.row.institution == "MIT"
    assert a.row.gpa_explanation == "circumstances"  # stripped
    assert a.row.coursework.startswith("AP CS A")
    assert a.row.state == "California"


def test_unanswered_and_absent_gpa_explanation_both_read_blank() -> None:
    """Owner decision D3: a null answer and a missing key are the same to the gates."""
    unanswered = _mapped(answers={"gpa_explanation": None})
    assert unanswered.row.gpa_explanation == ""
    assert unanswered.mapping_notes == ()  # present-but-null is normal, not drift

    absent = make_payload()
    absent["all_answers"] = [
        e for e in absent["all_answers"] if e["field_key"] != "gpa_explanation"
    ]
    a = map_application_payload(ApplicationPayload.model_validate(absent))
    assert a.row.gpa_explanation == ""
    # ...but a vanished key IS drift, and must be visible in the audit trail.
    assert any("gpa_explanation" in n for n in a.mapping_notes)


def test_absent_languages_and_github_are_not_drift() -> None:
    """Neither field is on the live CS form — blank is normal, never a note."""
    a = _mapped()
    assert a.row.programming_languages == "" and a.row.github_profile == ""
    assert a.mapping_notes == ()


def test_weighted_only_gpa_forces_task_a() -> None:
    a = _mapped(gpa_unweighted=None, gpa_weighted="4.4/5.0")
    assert a.row.gpa == "4.4/5.0"
    assert a.force_task_a is True  # deterministic /5 conversion would misread weighted


def test_blank_gpa_maps_to_empty_string() -> None:
    a = _mapped(gpa_unweighted=None, gpa_weighted=None)
    assert a.row.gpa == "" and a.force_task_a is False


def test_missing_required_essays_flagged_not_defaulted() -> None:
    a = _mapped(required_essays=[{"question": "Why?", "answer": "only one"}])
    assert a.missing_required_essays is True
    assert any("required essay" in n for n in a.mapping_notes)


def test_surplus_essays_noted_as_contract_drift() -> None:
    extra = {"question": "Q", "answer": "A"}
    a = _mapped(required_essays=[extra] * 3, optional_essays=[extra] * 2)
    assert not a.missing_required_essays
    assert len(a.mapping_notes) == 2  # required 3+ note and optional 2+ note


def test_absent_optional_essay_is_neutral() -> None:
    """Essay 3 stays bonus-only even though the live form requires it to submit."""
    a = _mapped(answers={"essay_research": None})
    assert a.row.essay3 == ""
    assert not a.missing_required_essays
    assert a.mapping_notes == ()


def test_resume_url_maps_when_present() -> None:
    a = _mapped(resume_url="https://acct.r2.cloudflarestorage.com/resume/x.pdf?X-Amz-Expires=600")
    assert a.row.resume_url.endswith("X-Amz-Expires=600")
    assert _mapped().row.resume_url == ""


def test_international_derivation() -> None:
    assert is_international("Ontario") is True
    assert is_international("Non-U.S. Territory") is True  # the live form's only non-US value
    assert is_international("california") is False  # case-insensitive US match
    assert is_international("Puerto Rico") is False  # US territory
    assert is_international("") is False  # blank is not a signal
    assert _mapped(answers={"state_of_residence": "Non-U.S. Territory"}).international is True


def test_ats_run_selects_grading() -> None:
    assert ApplicationPayload.model_validate(make_payload()).grades_essays() is True
    resume_only = ApplicationPayload.model_validate(make_payload(ats_run=["resume"]))
    assert resume_only.grades_essays() is False
