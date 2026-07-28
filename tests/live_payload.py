"""The canonical live webhook payload, as one shared test builder.

Mirrors `thinkNeuroWebsite/lib/ats.ts::buildAtsPayload` against the live **SP27-CSE**
question config (read 2026-07-28), so every suite exercises the real shape instead of four
hand-rolled approximations. Synthetic values only — never real applicant content.

Field facts worth keeping straight (all verified, not assumed):

* ``all_answers`` is the only place ``field_key`` appears; the essay arrays carry question
  text and answer alone.
* The third essay is ``essay_research`` (not ``essay_technical``) and is tagged
  ``ats_role: optional_essay`` on the live cohort, so it lands in ``optional_essays``.
* ``programming_languages`` / ``github_profile`` are **not** on the live CS form.
* ``gpa_unweighted`` / ``gpa_weighted`` are separate top-level ``"value/max"`` strings.
* An unanswered question arrives as ``answer: null``, not ``""``.
* ``submitted_at`` is U.S. Pacific with an offset, not UTC ``Z``.
* ``finaid`` is present for everyone; ``ats_run`` drops ``"finaid"`` instead of omitting it.
"""

from __future__ import annotations

import uuid

# Live SP27-CSE field keys, in sort_order. Answers default to something plausible so a test
# only has to override what it cares about.
LIVE_ANSWERS: dict[str, str | None] = {
    "first_name": "Syn",
    "last_name": "Thetic",
    "institution": "Lincoln High School",
    "state_of_residence": "California",
    "phone": "555-0100",
    "previously_applied": "No",
    "cohort_choice_1": "Spring 2027 - HONORS",
    "cohort_choice_2": "Spring 2027 - INTENSIVE",
    "cohort_choice_3": "Regular",
    "regular_cohort_acknowledgment": None,
    "gpa_unweighted": "3.95/4.0",
    "gpa_weighted": "4.23/4.0",
    "gpa_explanation": None,
    "relevant_coursework": "AP CS A: 95, AP Calculus BC: 92",
    "resume": None,
    "linkedin": None,
    "essay_motivation": "essay one",
    "essay_trajectory": "essay two",
    "essay_research": "essay three",
    "financial_aid_needed": "No",
}

_QUESTIONS = {
    "essay_motivation": "What motivates you to apply to Track 2?",
    "essay_trajectory": "How will you leverage this to advance your trajectory?",
    "essay_research": "Describe a technical problem you are independently curious about.",
}


def make_payload(*, answers: dict[str, str | None] | None = None, **overrides) -> dict:
    """Build one live-shaped payload dict.

    ``answers`` patches individual ``all_answers`` entries by field_key (``None`` = the
    question exists but is unanswered; delete a key to simulate it being absent from the
    live form entirely). ``overrides`` replaces top-level payload keys.
    """
    merged = {**LIVE_ANSWERS, **(answers or {})}
    payload = {
        "submission_id": str(uuid.uuid4()),
        "user_email": "syn@example.com",
        "student_name": "Syn Thetic",
        "cohort_name": "SP27-CSE",
        "cohort_display_name": "Spring 2027 - Computer & Software Engineering (CSE)",
        "submitted_at": "2026-07-06T11:20:00-07:00",
        "referral": "",
        "referral_code": None,
        "time_spent_seconds": 900,
        "ed": False,
        "is_finaid": False,
        "ats_run": ["essays"],
        "tier_first_choice": merged.get("cohort_choice_1"),
        "tier_second_choice": merged.get("cohort_choice_2"),
        "tier_third_choice": merged.get("cohort_choice_3"),
        "detected_sub_track": "intensive",
        "gpa_unweighted": merged.get("gpa_unweighted"),
        "gpa_weighted": merged.get("gpa_weighted"),
        "all_answers": [
            {"field_key": k, "question": _QUESTIONS.get(k, k.replace("_", " ").title()),
             "answer": v}
            for k, v in merged.items()
        ],
        "resume_url": None,
        # Built from the live ats_role tags: motivation + trajectory are required_essay,
        # essay_research is optional_essay. optional_essays carries answered entries only.
        "required_essays": [
            {"question": _QUESTIONS[k], "answer": merged.get(k) or ""}
            for k in ("essay_motivation", "essay_trajectory")
        ],
        "optional_essays": (
            [{"question": _QUESTIONS["essay_research"], "answer": merged["essay_research"]}]
            if merged.get("essay_research")
            else []
        ),
        "finaid": {"sat_score": None, "test_score_scale": {"SAT": 1600, "PSAT": 1600, "ACT": 36},
                   "fin_aid_essays": []},
    }
    payload.update(overrides)
    return payload
