"""P4 — webhook payload → ApplicantRow mapping tests (pure, zero spend)."""

from __future__ import annotations

import uuid

from srip_filter.ingest_webhook import (
    EssayMeta,
    is_international,
    map_essays_payload,
)
from srip_filter.models import EssaysModePayload, ResumeModePayload

SID = str(uuid.uuid4())


def _payload(**overrides) -> EssaysModePayload:
    base = {
        "ats_mode": "essays",
        "submission_id": SID,
        "user_email": "syn@example.com",
        "student_name": "Syn Thetic",
        "cohort_name": "su26-cs",
        "gpa": {"unweighted": "3.8 / 4.0", "weighted": "4.4 / 5.0"},
        "gpa_explanation": "  circumstances text  ",
        "relevant_coursework": "AP CS A: 95",
        "programming_languages": "Python, Rust",
        "institution": "MIT",
        "state_of_residence": "California",
        "github_profile": "https://github.com/syn",
        "sub_track": "cs",
        "resume_url": None,
        "first_choice": "Summer 2026 - HONORS",
        "second_choice": "Summer 2026 - INTENSIVE",
        "third_choice": "",
        "required_essays": [
            {"question": "Why?", "answer": "essay one", "field_key": "essay_motivation",
             "min_words": 100, "max_words": 350},
            {"question": "Future?", "answer": "essay two", "field_key": "essay_trajectory",
             "min_words": 100, "max_words": 350},
        ],
        "optional_essays": [
            {"question": "Technical topic?", "answer": "essay three",
             "field_key": "essay_technical", "max_words": 500},
        ],
    }
    base.update(overrides)
    return EssaysModePayload.model_validate(base)


def test_full_mapping_round_trip() -> None:
    a = map_essays_payload(_payload())
    row = a.row
    assert row.submission_id == SID
    assert row.first_name == "Syn Thetic" and row.last_name == ""
    assert row.email == "syn@example.com"
    assert row.gpa == "3.8 / 4.0"  # unweighted primary
    assert a.force_task_a is False
    assert row.gpa_explanation == "circumstances text"
    assert row.essay1 == "essay one" and row.essay2 == "essay two"
    assert row.essay3 == "essay three"
    assert a.e1.min_words == 100 and a.e1.max_words == 350
    assert a.e3.max_words == 500 and a.e3.min_words is None
    assert a.e1.target_range == "100-350" and a.e3.target_range == "0-500"
    assert row.first_choice.endswith("HONORS")
    assert a.cohort_name == "su26-cs"
    assert a.international is False
    assert not a.missing_required_essays
    assert a.mapping_notes == ()


def test_weighted_only_gpa_forces_task_a() -> None:
    a = map_essays_payload(_payload(gpa={"unweighted": None, "weighted": "4.4 / 5.0"}))
    assert a.row.gpa == "4.4 / 5.0"
    assert a.force_task_a is True  # deterministic /5 conversion would misread weighted


def test_legacy_string_gpa_passes_through() -> None:
    a = map_essays_payload(_payload(gpa="3.8 / 4.0"))
    assert a.row.gpa == "3.8 / 4.0"
    assert a.force_task_a is False


def test_blank_gpa_maps_to_empty_string() -> None:
    a = map_essays_payload(_payload(gpa=None))
    assert a.row.gpa == ""


def test_missing_required_essays_flagged_not_defaulted() -> None:
    a = map_essays_payload(_payload(required_essays=[
        {"question": "Why?", "answer": "only one"},
    ]))
    assert a.missing_required_essays is True
    assert any("required essay" in n for n in a.mapping_notes)


def test_surplus_essays_noted_as_contract_drift() -> None:
    extra = {"question": "Q", "answer": "A"}
    a = map_essays_payload(
        _payload(
            required_essays=[extra, extra, extra],
            optional_essays=[extra, extra],
        )
    )
    assert not a.missing_required_essays
    assert len(a.mapping_notes) == 2  # required 3+ note and optional 2+ note


def test_resume_url_falls_back_to_resume_mode_payload() -> None:
    resume = ResumeModePayload.model_validate(
        {"ats_mode": "resume", "submission_id": SID,
         "resume_url": "https://r2.example.com/resume/x.pdf"}
    )
    a = map_essays_payload(_payload(resume_url=None), resume_payload=resume)
    assert a.row.resume_url == "https://r2.example.com/resume/x.pdf"
    # essays-mode value wins when both are present
    b = map_essays_payload(
        _payload(resume_url="https://r2.example.com/resume/main.pdf"), resume_payload=resume
    )
    assert b.row.resume_url.endswith("main.pdf")


def test_international_derivation() -> None:
    assert is_international("Ontario") is True
    assert is_international("International") is True
    assert is_international("california") is False  # case-insensitive US match
    assert is_international("Puerto Rico") is False  # US territory
    assert is_international("") is False  # blank is not a signal
    a = map_essays_payload(_payload(state_of_residence="Ontario"))
    assert a.international is True


def test_essay_meta_without_bounds_has_no_target_range() -> None:
    assert EssayMeta().target_range is None
