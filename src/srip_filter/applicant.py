"""``ApplicantRow`` — the canonical shape every scoring stage grades.

Deliberately its own module, and deliberately dependency-free beyond pydantic.

This type used to live in :mod:`srip_filter.ingest`, which imports pandas for the CSV
reader. Every scoring stage needs ``ApplicantRow``, ``pipeline`` imports the stages, and
``api.main`` imports ``pipeline`` — so importing the app dragged pandas in, costing ~0.6 s
of every serverless cold start for a code path the deployed service never reaches. The
webhook is the only live source of applications; the CSV reader survives solely for
``scripts/replay.py``, which is a development tool.

Splitting the type out breaks that chain: ``ingest`` imports from here, never the reverse.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApplicantRow(BaseModel):
    """One applicant, canonicalized — the input to every gate and scoring stage.

    Every field is a whitespace-normalized string (defaulting to ""); GPA normalization,
    essay gating, and the rest of the pipeline run on these. Unknown keys are forbidden so
    a mapping bug surfaces immediately.

    The field set still carries its CSV-era names because that is what the whole scoring
    layer reads; :mod:`srip_filter.ingest_webhook` maps the live payload onto them.
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
    # Retired with the v2 CSV form, which carried a truthfulness checkbox the live site
    # enforces at submit instead. Kept as a field only because the CSV reader still maps
    # the column for the replay tool; nothing scores it.
    affirmation: str = ""
    # v3 webhook fields (blank on the CSV path): the optional technical essay and the
    # carried-not-scored metadata (PRD v3 §2.2). Populated by ingest_webhook.
    essay3: str = ""
    programming_languages: str = ""
    github_profile: str = ""
    sub_track: str = ""
