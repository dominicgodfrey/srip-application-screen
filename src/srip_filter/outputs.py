"""Stage 9 — output emission (Phase 7.3, PRD §10/§12).

Pure serializers that turn the finalized :class:`AuditRecord` list into the five deliverables.
Each returns an **in-memory** artifact (a ``str`` or ``dict``) so the stateless API (Phase 9) can
hand results straight back to the user as downloadables without ever touching disk; a thin
:func:`write_outputs` convenience writes the five files for local/CLI use.

Artifacts (PRD §12):
  1. ``decisions.jsonl`` — one audit record per applicant (§9)            — :func:`decisions_jsonl`
  2. ``ranked.csv``      — ``RANKED`` only, sorted by rank                — :func:`ranked_csv`
  3. ``rejected.csv``    — ``REJECTED``, naming the failing gate          — :func:`rejected_csv`
  4. ``needs_review.csv``— ``NEEDS_REVIEW``, naming the blocker           — :func:`needs_review_csv`
  5. ``summary.json``    — counts, ``RANKED`` score histogram, review list — :func:`build_summary`

All emitters are deterministic: ``ranked`` sorts by ``rank``; ``rejected``/``needs_review`` sort
by ``submission_id`` so reruns produce byte-identical files (§12 #5).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .models import AuditRecord

# Histogram bucket width for the RANKED final_score distribution in summary.json.
_HISTOGRAM_BUCKET = 10

# Leading characters a spreadsheet (Excel / Google Sheets / LibreOffice) treats as the start of
# a formula. Applicant-controlled free text (name, choices, reasons) lands in these CSVs and is
# opened by staff in a spreadsheet, so a cell beginning with one of these is a CSV-injection
# vector. We neutralize it by prefixing a single quote (the spreadsheet then renders it as
# literal text). Tab and CR are included per the OWASP guidance.
_FORMULA_TRIGGERS = frozenset("=+-@\t\r")

# Output filenames (PRD §12).
DECISIONS_FILE = "decisions.jsonl"
RANKED_FILE = "ranked.csv"
REJECTED_FILE = "rejected.csv"
NEEDS_REVIEW_FILE = "needs_review.csv"
SUMMARY_FILE = "summary.json"


def _by_outcome(records: list[AuditRecord], outcome: str) -> list[AuditRecord]:
    return [r for r in records if r.outcome == outcome]


def _sanitize_cell(value: object) -> object:
    """Neutralize spreadsheet formula injection in a string cell (CSV-injection guard).

    A string whose first character is a formula trigger (``= + - @``, tab, CR) is prefixed with a
    single quote so Excel/Sheets render it as literal text rather than evaluating it. Non-string
    cells (ints, floats, ``None``) pass through unchanged — a numeric ``-3.0`` is a number, not a
    formula. Pure function.
    """
    if isinstance(value, str) and value and value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def _write_csv(header: list[str], rows: list[list[object]]) -> str:
    """Render a CSV string with a trailing newline per row (``\\r\\n`` disabled for portability).

    Every data cell is run through :func:`_sanitize_cell` so applicant-controlled text can't carry
    a spreadsheet formula into a staff reviewer's Excel/Sheets. Headers are static and trusted.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows([_sanitize_cell(cell) for cell in row] for row in rows)
    return buffer.getvalue()


def decisions_jsonl(records: list[AuditRecord]) -> str:
    """All records as JSON Lines (PRD §9) — one full audit record per line, input order preserved.

    This is the source of truth for audits and the downstream cohort tool; it includes every
    applicant regardless of outcome.
    """
    return "".join(record.model_dump_json() + "\n" for record in records)


def ranked_csv(records: list[AuditRecord]) -> str:
    """``RANKED`` applicants, sorted by ``rank`` (PRD §12): rank, id, name, final_score, the two
    required subscores, the two live bonuses, and ``primary_reason``."""
    header = [
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
    ranked = sorted(_by_outcome(records, "RANKED"), key=lambda r: (r.rank is None, r.rank))
    rows: list[list[object]] = [
        [
            r.rank,
            r.submission_id,
            r.name,
            r.final_score,
            r.scores.gpa_points,
            r.scores.essay.total,
            r.scores.coursework_bonus,
            r.scores.school_bonus,
            r.primary_reason,
        ]
        for r in ranked
    ]
    return _write_csv(header, rows)


# Internal stage ids -> plain-language labels. The downloaded files are read by program staff
# who never saw the development plan, so internal "stageN" ids must not leak into them.
_STAGE_LABELS = {
    "stage1": "essay quality checks",
    "stage2": "GPA normalization",
    "stage3": "GPA gate",
    "stage4": "essay grading",
    "stage8": "final scoring & ranking",
    "manual_override": "manual override",
}


def rejected_csv(records: list[AuditRecord]) -> str:
    """``REJECTED`` applicants (PRD §12): id, name, the failing stage/gate, and ``primary_reason``
    (which itself names the failing gate — §12 #3). Sorted by ``submission_id`` for stable reruns.
    """
    header = ["submission_id", "name", "failing_stage", "primary_reason"]
    rejected = sorted(_by_outcome(records, "REJECTED"), key=lambda r: r.submission_id)
    rows: list[list[object]] = [
        [
            r.submission_id,
            r.name,
            _STAGE_LABELS.get(r.decided_at_stage, r.decided_at_stage),
            r.primary_reason,
        ]
        for r in rejected
    ]
    return _write_csv(header, rows)


def needs_review_csv(records: list[AuditRecord]) -> str:
    """``NEEDS_REVIEW`` applicants (PRD §12): id, name, and the blocker reason. Sorted by
    ``submission_id`` for stable reruns."""
    header = ["submission_id", "name", "blocker_reason"]
    review = sorted(_by_outcome(records, "NEEDS_REVIEW"), key=lambda r: r.submission_id)
    rows: list[list[object]] = [[r.submission_id, r.name, r.primary_reason] for r in review]
    return _write_csv(header, rows)


def _score_histogram(ranked: list[AuditRecord]) -> dict[str, int]:
    """Bucket ``RANKED`` final_scores into fixed-width bins, labeled ``"<lo>-<hi>"``.

    Empty bins between the lowest and highest occupied bucket are included (count 0) so the
    distribution reads continuously; an empty RANKED set yields ``{}``.
    """
    scores = [r.final_score for r in ranked if r.final_score is not None]
    if not scores:
        return {}
    lo_bucket = int(min(scores)) // _HISTOGRAM_BUCKET
    hi_bucket = int(max(scores)) // _HISTOGRAM_BUCKET
    histogram: dict[str, int] = {}
    for bucket in range(lo_bucket, hi_bucket + 1):
        low = bucket * _HISTOGRAM_BUCKET
        label = f"{low}-{low + _HISTOGRAM_BUCKET - 1}"
        histogram[label] = 0
    for score in scores:
        bucket = int(score) // _HISTOGRAM_BUCKET
        low = bucket * _HISTOGRAM_BUCKET
        histogram[f"{low}-{low + _HISTOGRAM_BUCKET - 1}"] += 1
    return histogram


def build_summary(records: list[AuditRecord]) -> dict:
    """Run summary (PRD §12.5): per-outcome counts, the ``RANKED`` score histogram, and the list
    of ``NEEDS_REVIEW`` cases with their blocker reasons.

    ``counts`` reconciles to ``total`` (every record lands in exactly one outcome bucket).
    """
    ranked = _by_outcome(records, "RANKED")
    rejected = _by_outcome(records, "REJECTED")
    review = sorted(_by_outcome(records, "NEEDS_REVIEW"), key=lambda r: r.submission_id)
    return {
        "counts": {
            "total": len(records),
            "RANKED": len(ranked),
            "REJECTED": len(rejected),
            "NEEDS_REVIEW": len(review),
        },
        "ranked_score_histogram": _score_histogram(ranked),
        "needs_review": [
            {"submission_id": r.submission_id, "name": r.name, "reason": r.primary_reason}
            for r in review
        ],
    }


def write_outputs(records: list[AuditRecord], out_dir: str | Path) -> dict[str, Path]:
    """Write all five artifacts into ``out_dir`` (created if missing). Returns the file paths.

    Convenience for local/CLI use only — the stateless API streams the in-memory serializers
    above instead and never persists to disk (PRD Privacy / §0).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {
        DECISIONS_FILE: decisions_jsonl(records),
        RANKED_FILE: ranked_csv(records),
        REJECTED_FILE: rejected_csv(records),
        NEEDS_REVIEW_FILE: needs_review_csv(records),
        SUMMARY_FILE: json.dumps(build_summary(records), indent=2) + "\n",
    }
    paths: dict[str, Path] = {}
    for filename, content in artifacts.items():
        path = out / filename
        path.write_text(content, encoding="utf-8")
        paths[filename] = path
    return paths
