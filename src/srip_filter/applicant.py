"""``ApplicantRow`` — the canonical shape every scoring stage grades.

Deliberately its own module and dependency-free beyond pydantic. It used to live in
:mod:`srip_filter.ingest`, whose pandas import then reached the whole app through the stages —
~0.6 s of every serverless cold start for a code path the deployed service never runs.
``ingest`` imports from here, never the reverse.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApplicantRow(BaseModel):
    """One applicant, canonicalized — the input to every gate and scoring stage.

    Every field is a whitespace-normalized string, and unknown keys are forbidden so a mapping
    bug surfaces immediately. The names are CSV-era because that is what the scoring layer
    reads; :mod:`srip_filter.ingest_webhook` maps the live payload onto them.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    institution: str = ""
    state: str = ""
    phone: str = ""
    first_choice: str = ""
    second_choice: str = ""
    third_choice: str = ""
    gpa: str = ""
    gpa_explanation: str = ""
    coursework: str = ""
    resume_url: str = ""
    linkedin: str = ""
    essay1: str = ""
    essay2: str = ""
    # Retired with the CSV form; the live site enforces it at submit. Kept only because the
    # CSV reader still maps the column for the replay tool — nothing scores it.
    affirmation: str = ""
    # Webhook-only, blank on the CSV path: the optional technical essay plus metadata that is
    # carried but not scored (PRD v3 §2.2).
    essay3: str = ""
    programming_languages: str = ""
    github_profile: str = ""
    sub_track: str = ""
