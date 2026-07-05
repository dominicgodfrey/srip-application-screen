"""P7 — replay-tool conversion tests (no network; the send path is exercised in E2E).

Proves the CSV→payload conversion emits the PROPOSED §2.2 contract exactly (it must
round-trip through the real edge models), that submission-id mapping is deterministic,
and that the synthetic fixtures are contract-valid and span the outcome space.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from replay import (  # noqa: E402
    payload_from_row,
    synthetic_payloads,
    to_submission_uuid,
)

from srip_filter.ingest import ApplicantRow
from srip_filter.ingest_webhook import map_essays_payload
from srip_filter.models import EssaysModePayload


def _row(**overrides) -> ApplicantRow:
    base = dict(
        submission_id="fillout-abc-123",
        first_name="Syn",
        last_name="Thetic",
        email="syn@example.com",
        gpa="3.8",
        gpa_explanation="",
        coursework="AP CS A: 95",
        institution="High School",
        state="California",
        first_choice="Summer 2026 - HONORS",
        essay1=" ".join(f"w{i}" for i in range(150)),
        essay2=" ".join(f"v{i}" for i in range(150)),
    )
    base.update(overrides)
    return ApplicantRow(**base)


def test_payload_validates_against_the_edge_contract() -> None:
    payload = payload_from_row(_row(), "replay-cs")
    parsed = EssaysModePayload.model_validate(payload)  # would raise on contract drift
    assert parsed.cohort_name == "replay-cs"
    assert parsed.required_essays[0].min_words == 100
    assert parsed.required_essays[1].max_words == 350
    # And the full round trip into the pipeline mapping works.
    applicant = map_essays_payload(parsed)
    assert applicant.row.essay1.startswith("w0 ")
    assert applicant.row.gpa == "3.8"
    assert not applicant.missing_required_essays


def test_submission_id_mapping_is_deterministic_and_uuid_preserving() -> None:
    real = str(uuid.uuid4())
    assert to_submission_uuid(real) == real  # already a UUID: untouched
    a = to_submission_uuid("fillout-abc-123")
    b = to_submission_uuid("fillout-abc-123")
    c = to_submission_uuid("fillout-abc-124")
    assert a == b  # re-replay hits the same row (idempotency end to end)
    assert a != c
    uuid.UUID(a)  # valid UUID for the DB column


def test_synthetic_fixtures_are_contract_valid_and_varied() -> None:
    payloads = synthetic_payloads(8, "replay-cs")
    assert len(payloads) == 8
    gpas = set()
    with_optional = 0
    for p in payloads:
        parsed = EssaysModePayload.model_validate(p)
        gpas.add(parsed.gpa.unweighted)
        with_optional += bool(parsed.optional_essays)
        assert "@example.com" in parsed.user_email  # synthetic-only guarantee
    assert len(gpas) == 3  # high / low-with-explanation / below-floor
    assert with_optional == 2  # every 4th row exercises Task F
    # Deterministic: a second call produces identical ids (stable replays).
    again = synthetic_payloads(8, "replay-cs")
    assert [p["submission_id"] for p in again] == [p["submission_id"] for p in payloads]
