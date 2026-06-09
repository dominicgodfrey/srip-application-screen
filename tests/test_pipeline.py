"""Tests for the orchestration core (Phase 8). Synthetic data only, no API spend.

8.1 covers the deterministic glue — :func:`build_base_record` (identity/dedup assembly) and
:func:`affirmation_ok` (unchecked-affirmation → NEEDS_REVIEW, but only when the column resolved).
8.2/8.3/8.4 (the LLM-driven runner, the batch runner, and the end-to-end §12 + fail-fast suite)
land in later commits with a scripted :class:`FakeLLMClient`.
"""

from __future__ import annotations

from srip_filter.ingest import (
    AFFIRMATION,
    EMAIL,
    FIRST_NAME,
    LAST_NAME,
    ApplicantRow,
    DedupedRow,
    HeaderResolution,
)
from srip_filter.models import DedupInfo
from srip_filter.pipeline import affirmation_ok, build_base_record


def _resolution(*roles: str) -> HeaderResolution:
    """A resolution where the given roles resolved to a dummy header."""
    return HeaderResolution(role_to_header={role: f"<{role}>" for role in roles})


def _deduped(**overrides: str) -> DedupedRow:
    base = dict(submission_id="s1", first_name="Ann", last_name="Lee", email="a@b.com")
    base.update(overrides)
    return DedupedRow(row=ApplicantRow(**base), dedup=DedupInfo())


# ------------------------------------------------------------------------------------------------
# 8.1 — build_base_record
# ------------------------------------------------------------------------------------------------


def test_base_record_fills_identity() -> None:
    rec = build_base_record(_deduped(), _resolution(FIRST_NAME, LAST_NAME, EMAIL))
    assert rec.submission_id == "s1"
    assert rec.name == "Ann Lee"
    assert rec.email == "a@b.com"


def test_base_record_starts_non_terminal() -> None:
    # A survivor placeholder: rank_records treats RANKED as a gate-survivor to score.
    rec = build_base_record(_deduped(), _resolution())
    assert rec.outcome == "RANKED"
    assert rec.final_score is None
    assert rec.rank is None


def test_base_record_program_choices_empty_to_none() -> None:
    rec = build_base_record(_deduped(first_choice="Summer 2026- HONORS"), _resolution())
    assert rec.program_choices.first == "Summer 2026- HONORS"
    assert rec.program_choices.second is None
    assert rec.program_choices.third is None


def test_base_record_carries_dedup_block() -> None:
    deduped = DedupedRow(
        row=ApplicantRow(submission_id="s2", first_name="Bo", last_name="Ng", email="b@c.com"),
        dedup=DedupInfo(is_duplicate_email=True, kept=True, notes="kept first of 2"),
    )
    rec = build_base_record(deduped, _resolution())
    assert rec.dedup.is_duplicate_email is True
    assert rec.dedup.notes == "kept first of 2"


def test_base_record_name_handles_partial() -> None:
    # Identity validation guarantees both names downstream, but assembly must not emit stray space.
    rec = build_base_record(_deduped(last_name=""), _resolution())
    assert rec.name == "Ann"


# ------------------------------------------------------------------------------------------------
# 8.1 — affirmation_ok
# ------------------------------------------------------------------------------------------------


def test_affirmation_present_and_checked_is_ok() -> None:
    row = ApplicantRow(submission_id="s1", affirmation="I affirm this is truthful.")
    assert affirmation_ok(row, _resolution(AFFIRMATION)) is True


def test_affirmation_present_and_blank_is_not_ok() -> None:
    row = ApplicantRow(submission_id="s1", affirmation="")
    assert affirmation_ok(row, _resolution(AFFIRMATION)) is False


def test_affirmation_present_and_whitespace_is_not_ok() -> None:
    row = ApplicantRow(submission_id="s1", affirmation="   ")
    assert affirmation_ok(row, _resolution(AFFIRMATION)) is False


def test_affirmation_column_absent_never_routes() -> None:
    # The column did not resolve: a blank value must NOT be read as "unchecked" for everyone.
    row = ApplicantRow(submission_id="s1", affirmation="")
    assert affirmation_ok(row, _resolution()) is True
