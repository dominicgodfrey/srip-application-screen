"""Replay tool (P7) — fire signed webhook POSTs at an ATS from a CSV or synthetic fixtures.

The website dispatcher's stand-in for development, integration testing, load testing, and
the v2→v3 calibration run:

    # 3 synthetic applications against a local server:
    uv run python scripts/replay.py --secret dev-secret --fixtures 3

    # replay a Fillout CSV export (the 466-row calibration source — LOCAL ONLY, PII):
    uv run python scripts/replay.py --secret dev-secret --csv path/to/export.csv

    # inspect the payloads without sending:
    uv run python scripts/replay.py --csv path/to/export.csv --dry-run

Signing matches `api/webhook_auth.py` exactly (same module — the single source of the
rule), so a replayed POST is indistinguishable from the website's once WEBSITE_ASKS #1
lands. CSV rows are converted to the PRD v3 §2.2 PROPOSED essays-mode contract;
Fillout's non-UUID submission ids are mapped deterministically via uuid5 so re-replays
hit the same rows (idempotency exercises for free).

Never point this at a production ATS with real data unless that is exactly what you
mean to do. Nothing here is imported by the service.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))  # allow `python scripts/replay.py` without install
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from api.webhook_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign  # noqa: E402

from srip_filter.ingest import (  # noqa: E402
    ApplicantRow,
    read_csv_records,
    validate_headers,
)

# Deterministic namespace for mapping Fillout's non-UUID submission ids onto the UUID
# column: uuid5(NS, raw_id) is stable across replays, so idempotency works end to end.
_SID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 NS_DNS

# The v2 form's fixed word bounds — the CSV path predates per-essay payload metadata.
_V2_MIN_WORDS, _V2_MAX_WORDS = 100, 350


def to_submission_uuid(raw_id: str) -> str:
    """Return raw UUIDs as-is; map anything else deterministically via uuid5."""
    try:
        return str(uuid.UUID(raw_id))
    except ValueError:
        return str(uuid.uuid5(_SID_NAMESPACE, raw_id))


def payload_from_row(row: ApplicantRow, cohort_name: str) -> dict:
    """Convert one canonical CSV row to the PRD v3 §2.2 PROPOSED essays-mode payload."""
    return {
        "ats_mode": "essays",
        "submission_id": to_submission_uuid(row.submission_id),
        "user_email": row.email,
        "student_name": f"{row.first_name} {row.last_name}".strip(),
        "cohort_name": cohort_name,
        "cohort_display_name": cohort_name,
        "is_finaid": False,
        "gpa": {"unweighted": row.gpa or None, "weighted": None},
        "gpa_explanation": row.gpa_explanation,
        "relevant_coursework": row.coursework,
        "programming_languages": "",
        "institution": row.institution,
        "state_of_residence": row.state,
        "github_profile": "",
        "sub_track": "cs",
        "resume_url": row.resume_url or None,
        "first_choice": row.first_choice,
        "second_choice": row.second_choice,
        "third_choice": row.third_choice,
        "required_essays": [
            {
                "question": "What motivates you to apply to Track 2 of the SRIP program?",
                "answer": row.essay1,
                "field_key": "essay_motivation",
                "min_words": _V2_MIN_WORDS,
                "max_words": _V2_MAX_WORDS,
            },
            {
                "question": "How does Track 2 fit your trajectory as a foundation for "
                "future research?",
                "answer": row.essay2,
                "field_key": "essay_trajectory",
                "min_words": _V2_MIN_WORDS,
                "max_words": _V2_MAX_WORDS,
            },
        ],
        "optional_essays": [],
    }


def payloads_from_csv(path: Path, cohort_name: str) -> list[dict]:
    headers, records = read_csv_records(path)
    resolution = validate_headers(headers)
    rows = [ApplicantRow.from_record(record, resolution) for record in records]
    return [payload_from_row(r, cohort_name) for r in rows if r.submission_id]


def synthetic_payloads(count: int, cohort_name: str) -> list[dict]:
    """Deterministic synthetic applications spanning the three outcomes (no PII)."""
    essay = " ".join(f"reason{i} detail{i}" for i in range(75))  # ~150 varied words
    out: list[dict] = []
    for n in range(count):
        sid = str(uuid.uuid5(_SID_NAMESPACE, f"synthetic-{n}"))
        gpa = ["3.9 / 4.0", "3.1 / 4.0", "2.4 / 4.0"][n % 3]
        payload = payload_from_row(
            ApplicantRow(
                submission_id=sid,
                first_name=f"Syn{n}",
                last_name="Thetic",
                email=f"syn{n}@example.com",
                gpa=gpa,
                gpa_explanation="A documented family emergency." if n % 3 == 1 else "",
                coursework="AP Computer Science A: 95, Calculus BC: 92" if n % 2 else "",
                institution="High School",
                state="California" if n % 2 else "Ontario",
                first_choice="Summer 2026 - HONORS",
                second_choice="Summer 2026 - INTENSIVE",
                essay1=essay,
                essay2=essay + " extra",
            ),
            cohort_name,
        )
        if n % 4 == 3:  # add an optional technical essay to exercise Task F
            payload["optional_essays"] = [
                {
                    "question": "Describe a technical problem you are curious about.",
                    "answer": " ".join(f"project{i} build{i}" for i in range(100)),
                    "field_key": "essay_technical",
                    "max_words": 500,
                }
            ]
        out.append(payload)
    return out


def send(url: str, secret: str, payloads: list[dict], *, test_ping: bool) -> int:
    failures = 0
    with httpx.Client(timeout=15.0) as client:  # mirror the website's 15 s abort
        items = ([{"_test": True}] if test_ping else []) + payloads
        for payload in items:
            body = json.dumps(payload).encode("utf-8")
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                TIMESTAMP_HEADER: ts,
                SIGNATURE_HEADER: sign(secret, ts, body),
            }
            resp = client.post(url, content=body, headers=headers)
            label = "_test" if payload.get("_test") else payload["submission_id"]
            print(f"  {label}: {resp.status_code} {resp.text[:120]}")
            if resp.status_code >= 400:
                failures += 1
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="Fillout CSV export to replay (local only)")
    source.add_argument("--fixtures", type=int, metavar="N", help="send N synthetic rows")
    parser.add_argument("--url", default="http://localhost:8321/webhooks/applications")
    parser.add_argument("--secret", default="", help="ATS_WEBHOOK_SECRET of the target")
    parser.add_argument("--cohort", default="replay-cs", help="cohort_name to stamp")
    parser.add_argument("--limit", type=int, default=None, help="send at most N rows")
    parser.add_argument("--dry-run", action="store_true", help="print payload summary, no POSTs")
    parser.add_argument("--no-test-ping", action="store_true", help="skip the _test ping")
    args = parser.parse_args()

    payloads = (
        payloads_from_csv(args.csv, args.cohort)
        if args.csv
        else synthetic_payloads(args.fixtures, args.cohort)
    )
    if args.limit is not None:
        payloads = payloads[: args.limit]

    if args.dry_run:
        for p in payloads:
            wc1 = len(p["required_essays"][0]["answer"].split())
            print(f"  {p['submission_id']}  gpa={p['gpa']['unweighted']!r}  e1_words={wc1}")
        print(f"{len(payloads)} payload(s); dry run — nothing sent.")
        return 0

    if not args.secret:
        parser.error("--secret is required unless --dry-run")
    print(f"POSTing {len(payloads)} payload(s) to {args.url}")
    failures = send(args.url, args.secret, payloads, test_ping=not args.no_test_ping)
    print(f"done — {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
