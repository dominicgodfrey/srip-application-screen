"""Tests for Stage 9 output emission (Phase 7.3). Deterministic, no API spend.

Pins the five artifacts (PRD §12): decisions.jsonl round-trips every record; ranked.csv carries
the right columns sorted by rank; rejected.csv names the failing gate; needs_review.csv names the
blocker; summary.json counts reconcile to the total. Also checks the on-disk ``write_outputs``.
"""

from __future__ import annotations

import csv
import io
import json

from srip_filter.models import AuditRecord, EssaySubscores, Scores
from srip_filter.outputs import (
    DECISIONS_FILE,
    NEEDS_REVIEW_FILE,
    RANKED_FILE,
    REJECTED_FILE,
    SUMMARY_FILE,
    build_summary,
    decisions_jsonl,
    needs_review_csv,
    ranked_csv,
    rejected_csv,
    write_outputs,
)


def _ranked(sid: str, name: str, rank: int, final_score: float) -> AuditRecord:
    return AuditRecord(
        submission_id=sid,
        name=name,
        outcome="RANKED",
        final_score=final_score,
        rank=rank,
        decided_at_stage="stage8",
        primary_reason="Survived all gates",
        scores=Scores(
            gpa_points=40.0,
            essay=EssaySubscores(e1=18.0, e2=17.0, total=35.0),
            coursework_bonus=9.4,
            school_bonus=15.0,
        ),
    )


def _rejected(sid: str, name: str, stage: str, reason: str) -> AuditRecord:
    return AuditRecord(
        submission_id=sid,
        name=name,
        outcome="REJECTED",
        decided_at_stage=stage,
        primary_reason=reason,
    )


def _review(sid: str, name: str, reason: str) -> AuditRecord:
    return AuditRecord(
        submission_id=sid,
        name=name,
        outcome="NEEDS_REVIEW",
        decided_at_stage="stage3",
        primary_reason=reason,
    )


def _sample() -> list[AuditRecord]:
    return [
        _ranked("r2", "Bob", rank=2, final_score=80.0),
        _rejected("x1", "Carol", "stage1", "Essay 1 below hard_min length gate"),
        _ranked("r1", "Alice", rank=1, final_score=99.4),
        _review("v1", "Dan", "GPA scale could not be normalized"),
    ]


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ------------------------------------------------------------------------------------------------
# decisions.jsonl
# ------------------------------------------------------------------------------------------------


def test_decisions_jsonl_one_record_per_line_roundtrips() -> None:
    records = _sample()
    text = decisions_jsonl(records)
    lines = text.strip().splitlines()
    assert len(lines) == len(records)
    # Every line is valid JSON re-parseable into an AuditRecord, input order preserved.
    parsed = [AuditRecord.model_validate_json(line) for line in lines]
    assert [r.submission_id for r in parsed] == [r.submission_id for r in records]


# ------------------------------------------------------------------------------------------------
# ranked.csv
# ------------------------------------------------------------------------------------------------


def test_ranked_csv_columns_and_sorted_by_rank() -> None:
    rows = _parse_csv(ranked_csv(_sample()))
    assert rows[0] == [
        "rank",
        "submission_id",
        "name",
        "final_score",
        "gpa_points",
        "essay_total",
        "coursework_bonus",
        "school_bonus",
        "primary_reason",
    ]
    # Only RANKED rows, sorted by rank ascending.
    assert [row[0] for row in rows[1:]] == ["1", "2"]
    assert rows[1][1] == "r1" and rows[1][2] == "Alice"
    assert rows[2][1] == "r2"
    assert rows[1][5] == "35.0"  # essay_total column


# ------------------------------------------------------------------------------------------------
# rejected.csv
# ------------------------------------------------------------------------------------------------


def test_rejected_csv_names_the_failing_gate() -> None:
    rows = _parse_csv(rejected_csv(_sample()))
    assert rows[0] == ["submission_id", "name", "failing_stage", "primary_reason"]
    assert len(rows) == 2  # header + 1 rejected
    assert rows[1][0] == "x1"
    assert rows[1][2] == "stage1"
    assert "length gate" in rows[1][3]


# ------------------------------------------------------------------------------------------------
# needs_review.csv
# ------------------------------------------------------------------------------------------------


def test_needs_review_csv_names_the_blocker() -> None:
    rows = _parse_csv(needs_review_csv(_sample()))
    assert rows[0] == ["submission_id", "name", "blocker_reason"]
    assert rows[1][0] == "v1"
    assert "GPA scale" in rows[1][2]


# ------------------------------------------------------------------------------------------------
# summary.json
# ------------------------------------------------------------------------------------------------


def test_summary_counts_reconcile() -> None:
    summary = build_summary(_sample())
    counts = summary["counts"]
    assert counts == {"total": 4, "RANKED": 2, "REJECTED": 1, "NEEDS_REVIEW": 1}
    assert counts["RANKED"] + counts["REJECTED"] + counts["NEEDS_REVIEW"] == counts["total"]


def test_summary_histogram_and_review_list() -> None:
    summary = build_summary(_sample())
    # Two RANKED scores: 80.0 → "80-89", 99.4 → "90-99"; the 90-99 bucket exists with count 1.
    histogram = summary["ranked_score_histogram"]
    assert histogram["80-89"] == 1
    assert histogram["90-99"] == 1
    assert sum(histogram.values()) == 2
    review = summary["needs_review"]
    assert review == [
        {"submission_id": "v1", "name": "Dan", "reason": "GPA scale could not be normalized"}
    ]


def test_summary_empty_ranked_histogram_is_empty() -> None:
    summary = build_summary([_rejected("x1", "C", "stage1", "gate")])
    assert summary["ranked_score_histogram"] == {}
    assert summary["counts"] == {"total": 1, "RANKED": 0, "REJECTED": 1, "NEEDS_REVIEW": 0}


# ------------------------------------------------------------------------------------------------
# write_outputs (on-disk convenience)
# ------------------------------------------------------------------------------------------------


def test_write_outputs_writes_five_files(tmp_path) -> None:
    paths = write_outputs(_sample(), tmp_path)
    for name in (DECISIONS_FILE, RANKED_FILE, REJECTED_FILE, NEEDS_REVIEW_FILE, SUMMARY_FILE):
        assert paths[name].exists()
    # summary.json on disk parses and reconciles.
    summary = json.loads((tmp_path / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["counts"]["total"] == 4


def test_outputs_are_deterministic_across_calls() -> None:
    records = _sample()
    assert ranked_csv(records) == ranked_csv(records)
    assert rejected_csv(records) == rejected_csv(records)
    assert build_summary(records) == build_summary(records)
