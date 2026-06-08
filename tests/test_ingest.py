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
    normalize_cell,
    read_csv_records,
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


def test_applicant_row_normalizes_whitespace() -> None:
    res = resolve_headers(GOOD_HEADERS)
    record = {"Submission ID": "  abc  ", "GPA": "   ", "What is your email address?": "\tx@y.z\n"}
    row = ApplicantRow.from_record(record, res)
    assert row.submission_id == "abc"
    assert row.gpa == ""  # whitespace-only -> empty
    assert row.email == "x@y.z"


def test_applicant_row_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ApplicantRow(bogus="x")  # type: ignore[call-arg]


# --- Phase 1.2: cell normalization + CSV loading -------------------------------------------------


def test_normalize_cell_handles_blanks_and_types() -> None:
    assert normalize_cell(None) == ""
    assert normalize_cell(float("nan")) == ""
    assert normalize_cell("   ") == ""
    assert normalize_cell("  hi  ") == "hi"
    assert normalize_cell(3.97) == "3.97"


def test_normalize_cell_preserves_interior_newlines() -> None:
    assert normalize_cell("  line1\nline2  ") == "line1\nline2"


def _csv_bytes(rows: list[list[str]], encoding: str = "utf-8") -> bytes:
    import csv
    import io

    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode(encoding)


def test_read_csv_records_basic() -> None:
    data = _csv_bytes(
        [
            ["Submission ID", "GPA", "What is your email address?"],
            ["abc", "4.0", "  a@b.com "],
            ["def", "", "c@d.com"],
        ]
    )
    headers, records = read_csv_records(data)
    assert headers == ["Submission ID", "GPA", "What is your email address?"]
    assert len(records) == 2
    # Strings stay strings (no numeric inference), cells are trimmed, blanks are "".
    assert records[0]["GPA"] == "4.0"
    assert records[0]["What is your email address?"] == "a@b.com"
    assert records[1]["GPA"] == ""


def test_read_csv_records_no_na_inference() -> None:
    # "N/A" is meaningful GPA content and must survive as the literal string.
    data = _csv_bytes([["Submission ID", "GPA"], ["abc", "N/A"]])
    _, records = read_csv_records(data)
    assert records[0]["GPA"] == "N/A"


def test_read_csv_records_utf8_bom() -> None:
    data = _csv_bytes([["Submission ID", "GPA"], ["abc", "3.5"]], encoding="utf-8-sig")
    headers, records = read_csv_records(data)
    assert headers[0] == "Submission ID"  # BOM stripped, not glued to the first header
    assert records[0]["GPA"] == "3.5"


def test_read_csv_records_non_utf8_fallback() -> None:
    # cp1252 smart-quote byte (0x92) is invalid UTF-8; loader must fall back, not crash.
    data = _csv_bytes([["Submission ID", "GPA"], ["o’brien", "3.5"]], encoding="cp1252")
    _, records = read_csv_records(data)
    assert records[0]["GPA"] == "3.5"
    assert records[0]["Submission ID"]  # decoded to *something* non-empty


def test_read_then_build_rows_end_to_end() -> None:
    headers, records = read_csv_records(_csv_bytes([GOOD_HEADERS, ["x"] * len(GOOD_HEADERS)]))
    res = validate_headers(headers)
    rows = [ApplicantRow.from_record(r, res) for r in records]
    assert len(rows) == 1
    assert rows[0].submission_id == "x"
