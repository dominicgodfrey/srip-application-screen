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
    "For each distinct course:\n"
    "1. NAME: the course title exactly as the applicant wrote it.\n"
    "2. GRADE: copy the grade exactly as written (grade_raw), then normalize it to a 0-100 "
    "percentage (grade_pct). Convert any stated scale: letters A=95, A-=92, B+=88, B=85, B-=82, "
    "C+=78, C=75; a 4.0-scale value x => x/4*100; a fraction n/m => n/m*100; a bare percentage "
    "as-is. If no grade is given, use 0.\n"
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
