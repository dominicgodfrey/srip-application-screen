"""Tests for the Stage 0 data contract (Phase 1.1): header resolution + ApplicantRow.

Synthetic headers only — no real applicant content. The long question columns use realistic
Fillout-style full-text titles to exercise the substring matchers.
"""

from __future__ import annotations

import pytest

from srip_filter.ingest import (
    AFFIRMATION,
    EMAIL,
    ESSAY1,
    ESSAY2,
    FIRST_NAME,
    GPA_EXPLANATION,
    INSTITUTION,
    LAST_NAME,
    REQUIRED_ROLES,
    ApplicantRow,
    HeaderValidationError,
    deduplicate,
    ingest_csv,
    normalize_cell,
    read_csv_records,
    resolve_headers,
    validate_headers,
    validate_identity,
)


def _row(**overrides: str) -> ApplicantRow:
    """An identifiable ApplicantRow by default; override fields to make it deficient."""
    base = dict(submission_id="id", first_name="Ann", last_name="Lee", email="a@b.com")
    base.update(overrides)
    return ApplicantRow(**base)

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


# --- Phase 1.3: identity validation --------------------------------------------------------------


def test_identity_keeps_fully_identified_rows() -> None:
    res = validate_identity([_row(), _row(submission_id="id2", email="c@d.com")])
    assert res.dropped_count == 0
    assert len(res.kept) == 2


@pytest.mark.parametrize("missing", [FIRST_NAME, LAST_NAME, EMAIL])
def test_identity_drops_row_missing_any_identity_field(missing: str) -> None:
    res = validate_identity([_row(**{missing: ""})])
    assert res.kept == []
    assert res.dropped_count == 1
    assert res.dropped[0].missing_fields == (missing,)


def test_identity_records_index_and_submission_id_of_dropped() -> None:
    rows = [_row(), _row(submission_id="bad", first_name=""), _row(submission_id="id3")]
    res = validate_identity(rows)
    assert [r.submission_id for r in res.kept] == ["id", "id3"]
    assert res.dropped[0].row_index == 1
    assert res.dropped[0].submission_id == "bad"


def test_identity_reports_all_missing_fields() -> None:
    res = validate_identity([_row(first_name="", email="")])
    assert set(res.dropped[0].missing_fields) == {FIRST_NAME, EMAIL}


def test_identity_does_not_drop_blank_gpa_or_essays() -> None:
    # Blank GPA / empty essays are NOT identity problems — they must flow to the pipeline.
    res = validate_identity([_row(gpa="", essay1="", essay2="")])
    assert res.dropped_count == 0
    assert len(res.kept) == 1


def test_identity_blank_submission_id_still_dropped_if_unidentifiable() -> None:
    res = validate_identity([_row(submission_id="", first_name="")])
    assert res.dropped_count == 1
    assert res.dropped[0].submission_id == ""


# --- Phase 1.4: deduplication --------------------------------------------------------------------


def test_dedup_keeps_distinct_applicants() -> None:
    res = deduplicate(
        [
            _row(submission_id="1"),
            _row(submission_id="2", first_name="Bob", last_name="Ng", email="c@d.com"),
        ]
    )
    assert len(res.kept) == 2
    assert res.dropped == []
    assert all(not k.dedup.is_duplicate_email for k in res.kept)
    assert all(not k.dedup.is_duplicate_name for k in res.kept)


def test_dedup_email_keeps_first_drops_surplus() -> None:
    rows = [
        _row(submission_id="1", email="dup@x.com"),
        _row(submission_id="2", email="dup@x.com", last_name="Other"),
        _row(submission_id="3", email="dup@x.com", last_name="Third"),
    ]
    res = deduplicate(rows)
    assert [k.row.submission_id for k in res.kept] == ["1"]
    assert [d.row.submission_id for d in res.dropped] == ["2", "3"]
    assert res.kept[0].dedup.is_duplicate_email is True
    assert res.kept[0].dedup.kept is True
    assert all(d.dedup.is_duplicate_email and not d.dedup.kept for d in res.dropped)


def test_dedup_email_is_case_and_whitespace_insensitive() -> None:
    rows = [_row(submission_id="1", email="A@B.com"), _row(submission_id="2", email="  a@b.COM ")]
    res = deduplicate(rows)
    assert len(res.kept) == 1
    assert len(res.dropped) == 1


def test_dedup_name_pair_different_email_is_flagged_not_dropped() -> None:
    rows = [
        _row(submission_id="1", first_name="Sam", last_name="Roy", email="sam1@x.com"),
        _row(submission_id="2", first_name="Sam", last_name="Roy", email="sam2@x.com"),
    ]
    res = deduplicate(rows)
    assert len(res.kept) == 2  # kept, not merged
    assert res.dropped == []
    assert all(k.dedup.is_duplicate_name for k in res.kept)
    assert all(not k.dedup.is_duplicate_email for k in res.kept)


def test_dedup_name_match_is_case_insensitive() -> None:
    rows = [
        _row(submission_id="1", first_name="Sam", last_name="Roy", email="a@x.com"),
        _row(submission_id="2", first_name="sam", last_name="ROY", email="b@x.com"),
    ]
    res = deduplicate(rows)
    assert all(k.dedup.is_duplicate_name for k in res.kept)


def test_dedup_same_email_does_not_trigger_name_flag() -> None:
    # Same name AND same email -> collapsed by email dedup, so only one kept; no name flag.
    rows = [
        _row(submission_id="1", first_name="Sam", last_name="Roy", email="same@x.com"),
        _row(submission_id="2", first_name="Sam", last_name="Roy", email="same@x.com"),
    ]
    res = deduplicate(rows)
    assert len(res.kept) == 1
    assert res.kept[0].dedup.is_duplicate_name is False
    assert res.kept[0].dedup.is_duplicate_email is True


def test_dedup_preserves_input_order() -> None:
    rows = [_row(submission_id=str(i), email=f"u{i}@x.com") for i in range(5)]
    res = deduplicate(rows)
    assert [k.row.submission_id for k in res.kept] == ["0", "1", "2", "3", "4"]


# --- Phase 1.5: ingest_csv() orchestration -------------------------------------------------------

# Header index for the synthetic CSV builder, aligned to GOOD_HEADERS positions.
_H = {"sid": 0, "first": 1, "last": 2, "email": 3, "gpa": 9, "essay1": 14, "essay2": 15}


def _data_row(**vals: str) -> list[str]:
    """One CSV data row aligned to GOOD_HEADERS; unspecified columns are blank."""
    cells = [""] * len(GOOD_HEADERS)
    for alias, value in vals.items():
        cells[_H[alias]] = value
    return cells


def _csv_with(rows: list[list[str]]) -> bytes:
    return _csv_bytes([GOOD_HEADERS, *rows])


def test_ingest_csv_happy_path() -> None:
    data = _csv_with(
        [
            _data_row(sid="1", first="Ann", last="Lee", email="ann@x.com", gpa="3.9"),
            _data_row(sid="2", first="Bob", last="Ng", email="bob@x.com", gpa="3.1"),
        ]
    )
    result = ingest_csv(data)
    assert result.report.total_rows_read == 2
    assert result.report.kept_count == 2
    assert [r.row.submission_id for r in result.rows] == ["1", "2"]
    assert result.report.identity_dropped == []
    assert result.report.duplicate_email_dropped == []


def test_ingest_csv_drops_unidentifiable_and_reports() -> None:
    data = _csv_with(
        [
            _data_row(sid="1", first="Ann", last="Lee", email="ann@x.com"),
            _data_row(sid="2", first="", last="Ng", email="bob@x.com"),  # missing first name
        ]
    )
    result = ingest_csv(data)
    assert result.report.kept_count == 1
    assert len(result.report.identity_dropped) == 1
    assert result.report.identity_dropped[0].submission_id == "2"


def test_ingest_csv_collapses_email_duplicates() -> None:
    data = _csv_with(
        [
            _data_row(sid="1", first="Ann", last="Lee", email="dup@x.com"),
            _data_row(sid="2", first="Ann", last="Lee", email="dup@x.com"),
        ]
    )
    result = ingest_csv(data)
    assert result.report.kept_count == 1
    assert len(result.report.duplicate_email_dropped) == 1
    assert result.report.duplicate_email_dropped[0].row.submission_id == "2"


def test_ingest_csv_flags_name_dupes_without_dropping() -> None:
    data = _csv_with(
        [
            _data_row(sid="1", first="Sam", last="Roy", email="sam1@x.com"),
            _data_row(sid="2", first="Sam", last="Roy", email="sam2@x.com"),
        ]
    )
    result = ingest_csv(data)
    assert result.report.kept_count == 2
    assert result.report.duplicate_name_flagged == 2


def test_ingest_csv_keeps_blank_gpa_and_essays() -> None:
    # Blank GPA / essays are not identity failures — the row survives ingest.
    data = _csv_with([_data_row(sid="1", first="Ann", last="Lee", email="ann@x.com")])
    result = ingest_csv(data)
    assert result.report.kept_count == 1
    assert result.rows[0].row.gpa == ""
    assert result.rows[0].row.essay1 == ""


def test_ingest_csv_raises_on_missing_required_column() -> None:
    headers_no_gpa = [h for h in GOOD_HEADERS if h != "GPA"]
    data = _csv_bytes([headers_no_gpa, ["x"] * len(headers_no_gpa)])
    with pytest.raises(HeaderValidationError, match="gpa"):
        ingest_csv(data)


def test_ingest_csv_reports_unrecognized_and_missing_optional() -> None:
    headers = [*GOOD_HEADERS, "Mystery Column"]
    headers = [h for h in headers if h != "LinkedIn (optional)"]
    data = _csv_bytes([headers, _data_row_padded(headers)])
    result = ingest_csv(data)
    assert "Mystery Column" in result.report.unrecognized_headers
    assert "linkedin" in result.report.missing_optional_roles


def _data_row_padded(headers: list[str]) -> list[str]:
    """A row matching an arbitrary header list, with identity fields filled."""
    cells = [""] * len(headers)
    for i, h in enumerate(headers):
        if h == "Submission ID":
            cells[i] = "1"
        elif h == "Student First Name":
            cells[i] = "Ann"
        elif h == "Student Last Name":
            cells[i] = "Lee"
        elif h == "What is your email address?":
            cells[i] = "ann@x.com"
    return cells
