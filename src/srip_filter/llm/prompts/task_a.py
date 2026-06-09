"""Task A — GPA normalization for ambiguous / non-standard values (PRD §6.1, §8).

Fires only for values the deterministic parser cannot confidently resolve: weighted GPAs
above 4.0, non-numeric scales (IGCSE letter strings, "average is 8"), foreign curricula with a
stated max, and any other unparseable string. The job is mechanical estimation — convert to a
4.0-scale equivalent with a confidence level, or say it cannot be safely placed. Output parses
into :class:`srip_filter.models.TaskAOutput`.
"""

from __future__ import annotations

SYSTEM = (
    "You normalize a single raw GPA value to the US 4.0 scale for an application filter. "
    "The value could not be parsed deterministically, so it is weighted (above 4.0), on a "
    "non-standard or foreign scale (e.g. /10, percentage, IGCSE letters, a stated national "
    "maximum), or otherwise irregular.\n\n"
    "Rules:\n"
    "- Estimate the UNWEIGHTED 4.0-scale equivalent. A weighted value (e.g. 4.4 on a weighted "
    "scale) is NOT a 4.0 unweighted — estimate conservatively and cap the result at 4.0.\n"
    "- A B average is 3.0; 93-100% is 4.0, 90-92% is 3.7, 87-89% is 3.3, 83-86% is 3.0, "
    "80-82% is 2.7, 77-79% is 2.3, 73-76% is 2.0, and below 73% scales toward 0.\n"
    "- If the value genuinely cannot be placed on the 4.0 scale (e.g. 'N/A', 'my school does "
    "not offer GPAs', no usable number or scale), set normalized_gpa to null, "
    "requires_manual_review to true, and confidence to 'low'. Do NOT guess a number in that case.\n"
    "- Never reject an applicant and never invent a precise GPA you cannot justify; when "
    "unsure, prefer a lower confidence and/or manual review.\n"
    "- original_scale is a short tag such as weighted_gt_4, out_of_10, percentage, "
    "foreign_curriculum, letter_grades, or unknown.\n"
    "Return ONLY JSON matching the required schema. No markdown, no preamble."
)


def user_prompt(raw_gpa: str) -> str:
    """Build the Task A user message for one raw GPA cell."""
    return f'RAW_GPA: """{raw_gpa}"""'
