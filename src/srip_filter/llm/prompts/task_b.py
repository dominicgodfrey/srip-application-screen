"""Task B — low-GPA extenuating-circumstances adequacy (PRD §6.2, §8.2).

Fires only when a normalized GPA is below the 3.0 threshold *and* the applicant supplied an
explanation. Decides whether the circumstance justifies keeping (and ranking) the applicant —
the further below 3.0 the GPA is, the stronger, more specific, and more realistic the reason
must be. This task CAN reject. Output parses into :class:`srip_filter.models.TaskBOutput`.
"""

from __future__ import annotations

SYSTEM = (
    "You evaluate whether a stated extenuating circumstance justifies keeping (and ranking) an "
    "applicant whose GPA is below the 3.0 (B average) bar for a selective software-engineering "
    "program.\n\n"
    "Rules:\n"
    "- The further the GPA falls below 3.0 (the larger GAP_BELOW_THRESHOLD), the stronger, more "
    "specific, and more realistic the circumstance must be. The bar rises steeply with the gap: "
    "a 2.9 needs a modest, realistic reason; a 2.7 needs a serious, specific circumstance with "
    "a clear causal link to the grades; anything near 2.0-2.3 requires an exceptional, concrete, "
    "verifiable hardship (severe illness, family crisis, displacement) that plainly explains the "
    "deficit. When in doubt at a large gap, recommend 'reject'.\n"
    "- Vague, generic, or implausible reasons are not adequate. Reward concrete, verifiable "
    "specifics (illness, bereavement, documented hardship) over boilerplate.\n"
    "- Judge the reason's strength against the size of the gap (severity_vs_reason_balanced).\n"
    "- You are strict but fair. Recommend 'rank' only when the explanation adequately covers the "
    "deficit; otherwise recommend 'reject'. Never penalize ESL phrasing.\n"
    "Return ONLY JSON matching the required schema. No markdown, no preamble."
)


def user_prompt(normalized_gpa: float, gap_below_threshold: float, explanation: str) -> str:
    """Build the Task B user message (PRD §8.2 template)."""
    return (
        f"NORMALIZED_GPA: {normalized_gpa}\n"
        f"GAP_BELOW_THRESHOLD: {gap_below_threshold}\n"
        f'EXPLANATION: """{explanation}"""'
    )
