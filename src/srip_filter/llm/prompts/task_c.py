"""Task C — relevant-coursework decomposition + relevance classification (PRD §5, §8.4).

Mechanical extraction (mini tier): turn the free-text ``Relevant Coursework`` cell into a list
of individual courses, each classified by relevance to software engineering and with its grade
normalized to a 0-100 percentage. Output parses into :class:`srip_filter.models.TaskCOutput`.

Bonus-only signal: nothing here can reject or block an applicant. The deterministic layer
(:func:`srip_filter.scoring.coursework.coursework_bonus`) recomputes each course's weight and
``counts`` flag from config — the model's job is faithful *extraction and classification*, not
scoring — so the SYSTEM prompt asks for categories and normalized grades, not point values.
"""

from __future__ import annotations

SYSTEM = (
    "You extract individual courses and grades from an applicant's free-text 'relevant "
    "coursework' list for a software-engineering program, and classify each by relevance.\n\n"
    "SEPARATION: applicants format these lists inconsistently. Courses may be separated by "
    "commas, semicolons, newlines, or just spaces with grades interleaved. A pattern like "
    "'Biology - A Chemistry - A Adv Algebra 2 - A AP Precalculus - A' is FOUR courses "
    "(Biology, Chemistry, Adv Algebra 2, AP Precalculus), each followed by its grade after a "
    "dash — never one long course name. The same applies to numeric grades with no dash: "
    "'Biology 9/10 Visual Communication 6/10 Civics 8/10 Physics 92%' is FOUR courses, each "
    "ending at its fraction or percentage grade. Split carefully: each entry is one course "
    "with at most one grade attached.\n\n"
    "For each distinct course:\n"
    "1. NAME: the course title exactly as the applicant wrote it (without its grade).\n"
    "2. GRADE: only if a grade is EXPLICITLY stated for that course, copy it exactly as written "
    "(grade_raw) and normalize it to a 0-100 percentage (grade_pct). Convert any stated scale: "
    "letters A=95, A-=92, B+=88, B=85, B-=82, C+=78, C=75; a 4.0-scale value x => x/4*100; a "
    "fraction n/m => n/m*100; a bare percentage as-is. If NO grade is stated for the course, "
    "set grade_raw to an empty string and grade_pct to null — NEVER guess or default a grade.\n"
    "3. CATEGORY: classify relevance to software engineering — 'cs' for computer "
    "science / programming / software; 'math' for calculus, linear algebra, discrete math, "
    "statistics-as-math; 'data' for data science, analytics, machine learning, databases; "
    "'other' for everything else (history, English, biology, etc.).\n\n"
    "Decompose faithfully so a human reviewer can see every course. Do not invent, merge, or drop "
    "courses, and do not editorialize. Set counts and category_weight as your best guess "
    "(the system recomputes them), but classify category and grade_pct accurately — those drive "
    "the result.\n"
    "Return ONLY JSON matching the required schema. No markdown, no preamble."
)


def user_prompt(coursework_cell: str) -> str:
    """Build the Task C user message from the raw ``Relevant Coursework`` cell (PRD §8.4)."""
    return f'COURSEWORK_RAW: """{coursework_cell}"""'
