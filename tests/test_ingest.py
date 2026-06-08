"""Tests for the Stage 0 data contract (Phase 1.1): header resolution + ApplicantRow.

Synthetic headers only — no real applicant content. The long question columns use realistic
Fillout-style full-text titles to exercise the substring matchers.
"""

from __future__ import annotations

import pytest

from srip_filter.ingest import (
    AFFIRMATION,
    ESSAY1,
    ESSAY2,
    GPA_EXPLANATION,
    INSTITUTION,
    REQUIRED_ROLES,
    ApplicantRow,
    HeaderValidationError,
    resolve_headers,
    validate_headers,
)

# A full, well-formed header set resembling the reference export's 29 columns.
GOOD_HEADERS = [
    "Submission ID",
    "Student First Name",
    "Student Last Name",
    "What is your email address?",
    "Please list your undergraduate institution of study below.",
    "What is your state of residence?",
    "First Choice",
    "Second Choice (optional)",
    "Third Choice (optional)",
    "GPA",
    "If your cumulative GPA is below 3.3, please briefly describe any extenuating circumstances "
    "that may have affected it.",
    "Relevant Coursework",
    "Resume (optional)",
    "LinkedIn (optional)",
    "What motivates you to apply to Track 2 of the SRIP program? (100-350 words)",
    "Track 2 is designed as a foundation for future research. (100-350 words)",
    "I affirm that the information provided above is truthful and accurate.",
    "Errors",
    "Url",
    "Network ID",
]


def test_resolves_all_roles_from_good_headers() -> None:
    res = resolve_headers(GOOD_HEADERS)
    assert res.ok
    assert not res.missing_required
    assert not res.missing_optional
    assert not res.ambiguous
    # Every contract role is present in the canonical export.
    assert set(res.role_to_header) >= set(REQUIRED_ROLES)


def test_long_columns_matched_by_substring() -> None:
    res = resolve_headers(GOOD_HEADERS)
    assert "extenuating circumstances" in res.role_to_header[GPA_EXPLANATION].lower()
    assert "What motivates you to apply" in res.role_to_header[ESSAY1]
    assert "foundation for future research" in res.role_to_header[ESSAY2]
    assert "affirm" in res.role_to_header[AFFIRMATION].lower()


def test_ignored_headers_not_reported_unrecognized() -> None:
    res = resolve_headers(GOOD_HEADERS)
    assert res.unrecognized_headers == ()


def test_unrecognized_header_reported_but_not_fatal() -> None:
    res = resolve_headers([*GOOD_HEADERS, "Some Surprise Column"])
    assert "Some Surprise Column" in res.unrecognized_headers
    assert res.ok  # extra columns are noise, not a contract failure


def test_missing_required_is_reported_and_validate_raises() -> None:
    headers = [h for h in GOOD_HEADERS if h != "GPA"]
    res = resolve_headers(headers)
    assert "gpa" in res.missing_required
    assert not res.ok
    with pytest.raises(HeaderValidationError, match="gpa"):
        validate_headers(headers)


def test_missing_optional_does_not_fail_validation() -> None:
    headers = [h for h in GOOD_HEADERS if h != "LinkedIn (optional)"]
    res = resolve_headers(headers)
    assert "linkedin" in res.missing_optional
    assert res.ok
    validate_headers(headers)  # must not raise


def test_institution_falls_back_to_substring_when_copy_drifts() -> None:
    # Form copy reworded; exact match fails but the substring matcher still locates it.
    reworded = "List the undergraduate institution you attend"
    drifted = [reworded if "undergraduate institution" in h else h for h in GOOD_HEADERS]
    res = resolve_headers(drifted)
    assert res.ok
    assert res.role_to_header[INSTITUTION] == reworded


def test_ambiguous_header_matching_two_roles_is_unresolved() -> None:
    # A single column that contains both essay markers can't be trusted for either role.
    bad = "What motivates you to apply as a foundation for future research?"
    headers = [
        h
        for h in GOOD_HEADERS
        if "What motivates you to apply" not in h and "foundation for future research" not in h
    ]
    headers.append(bad)
    res = resolve_headers(headers)
    assert ESSAY1 in res.ambiguous and ESSAY2 in res.ambiguous
    assert not res.ok
    with pytest.raises(HeaderValidationError, match="ambiguous"):
        validate_headers(headers)


def test_duplicate_header_for_one_role_is_ambiguous() -> None:
    res = resolve_headers([*GOOD_HEADERS, "GPA"])
    assert "gpa" in res.ambiguous
    assert not res.ok


def test_validate_returns_resolution_when_ok() -> None:
    res = validate_headers(GOOD_HEADERS)
    assert res.ok


def test_applicant_row_from_record_populates_resolved_roles() -> None:
    res = resolve_headers(GOOD_HEADERS)
    record = {h: f"value::{h[:10]}" for h in GOOD_HEADERS}
    row = ApplicantRow.from_record(record, res)
    assert row.submission_id == "value::Submission"
    assert row.essay1.startswith("value::")
    assert row.affirmation.startswith("value::")


def test_applicant_row_missing_cells_become_empty_string() -> None:
    res = resolve_headers(GOOD_HEADERS)
    row = ApplicantRow.from_record({"Submission ID": "abc"}, res)
    assert row.submission_id == "abc"
    assert row.email == ""  # resolved role, absent in record
    assert row.gpa == ""


def test_applicant_row_coerces_none_and_nonstring() -> None:
    res = resolve_headers(GOOD_HEADERS)
    record = {"Submission ID": None, "GPA": 3.97}
    row = ApplicantRow.from_record(record, res)
    assert row.submission_id == ""
    assert row.gpa == "3.97"


def test_applicant_row_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ApplicantRow(bogus="x")  # type: ignore[call-arg]
