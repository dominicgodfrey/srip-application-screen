"""Dev-only demo LLM handler, active only under ``SRIP_DEV_FAKE_LLM=1``, so the whole UI can be
demoed end to end with no API key and zero token spend.

Outputs are deliberately *optimistic* (on-topic, plausible grades) so gate-survivors become
richly-scored ``RANKED`` records — what makes the audit browser worth looking at. Outcome
variety comes from the deterministic gates, which run first. Two sentinels, ``[[OFFTOPIC]]``
and ``[[GIBBERISH]]``, let a crafted demo CSV exercise the LLM-driven reject paths.

Never used by the test suite, which injects its own scripted ``FakeLLMClient``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from srip_filter.llm.client import TaskName
from srip_filter.models import (
    CourseItem,
    TaskAOutput,
    TaskBOutput,
    TaskCOutput,
    TaskDOutput,
    TaskEOutput,
    TaskFOutput,
)

_OFFTOPIC = "[[OFFTOPIC]]"
_GIBBERISH = "[[GIBBERISH]]"

# Light, deterministic category rotation so a demo coursework cell shows a believable mix.
_CATEGORIES: tuple[tuple[str, float], ...] = (("cs", 1.0), ("math", 0.8), ("data", 0.6))


def _task_a(user: str) -> TaskAOutput:
    """Place an ambiguous GPA at a plausible mid value (demo only)."""
    return TaskAOutput(
        normalized_gpa=3.5,
        original_scale="demo_estimate",
        conversion_method="demo handler — fixed plausible placement",
        confidence="med",
        requires_manual_review=False,
        rationale="Demo handler: ambiguous GPA placed at 3.5 for illustration.",
    )


def _task_b(user: str) -> TaskBOutput:
    """Treat a present low-GPA explanation as adequate (demo only)."""
    return TaskBOutput(
        explanation_adequate=True,
        strength_of_reason=0.7,
        realistic=True,
        severity_vs_reason_balanced=True,
        recommended_outcome="rank",
        rationale="Demo handler: explanation accepted so the applicant is ranked.",
    )


# Grade tokens interleaved in a run of courses: a letter after a dash ("Biology - A"), or a
# standalone fraction/percentage ("AP Calc 92%") with no dash.
_GRADE_RE = re.compile(
    r"(?:-\s*[A-DF][+\-]?\*?|\d{1,3}(?:\.\d+)?\s*/\s*\d{1,3}|\d{1,3}(?:\.\d+)?\s*%)(?=\s|$)"
)
# Letter grade -> percentage, mirroring the Task C prompt's conversion table.
_GRADE_PCT = {
    "A": 95,
    "A-": 92,
    "A+": 97,
    "B+": 88,
    "B": 85,
    "B-": 82,
    "C+": 78,
    "C": 75,
    "C-": 72,
    "D": 65,
    "F": 50,
}


def _grade_to_pct(grade_raw: str) -> int | None:
    """Normalize a captured grade token to a 0-100 percentage; None when unparseable."""
    g = grade_raw.strip()
    if not g:
        return None
    pct = _GRADE_PCT.get(g.rstrip("*"))
    if pct is not None:
        return pct
    try:
        if g.endswith("%"):
            return round(float(g[:-1].strip()))
        if "/" in g:
            num, _, den = g.partition("/")
            return round(float(num) / float(den) * 100)
    except (ValueError, ZeroDivisionError):
        return None
    return None


def _split_pairs(fragment: str) -> list[tuple[str, str]]:
    """Split one fragment into (name, grade_raw) pairs on interleaved grade tokens."""
    matches = list(_GRADE_RE.finditer(fragment))
    if not matches:
        return [(fragment, "")]
    pairs: list[tuple[str, str]] = []
    prev_end = 0
    for m in matches:
        name = fragment[prev_end : m.start()].strip(" \t-")
        if name:
            pairs.append((name, m.group().lstrip("- ").strip()))
        prev_end = m.end()
    tail = fragment[prev_end:].strip(" \t-")
    if tail:
        pairs.append((tail, ""))
    return pairs


def _task_c(user: str) -> TaskCOutput:
    """Decompose the coursework cell with light splitting: separators first, then
    grade-interleaved runs. A course with no explicit grade gets ``grade_pct=None`` rather than
    an invented one, matching the real Task C contract."""
    # The prompt wraps the raw cell in triple quotes — pull the inner text back out.
    inner = user
    match = re.search(r'"""(.*)"""', user, flags=re.DOTALL)
    if match:
        inner = match.group(1)
    pairs: list[tuple[str, str]] = []  # (name, grade_raw); grade_raw "" when unstated
    for fragment in (f.strip() for f in re.split(r"[,\n;]", inner)):
        if fragment:
            pairs.extend(_split_pairs(fragment))
    courses: list[CourseItem] = []
    for i, (name, grade_raw) in enumerate(pairs[:8]):  # cap for a tidy demo panel
        category, weight = _CATEGORIES[i % len(_CATEGORIES)]
        courses.append(
            CourseItem(
                name=name[:80],
                grade_raw=grade_raw,
                grade_pct=_grade_to_pct(grade_raw),
                category=category,  # type: ignore[arg-type]
                counts=True,
                category_weight=weight,
            )
        )
    return TaskCOutput(courses=courses, rationale="Demo handler: light split decomposition.")


def _task_d(user: str) -> TaskDOutput:
    """Grade an essay optimistically, honoring the off-topic / gibberish sentinels (demo only)."""
    off_topic = _OFFTOPIC in user
    gibberish = _GIBBERISH in user
    return TaskDOutput(
        is_gibberish=gibberish,
        on_topic=not off_topic,
        relevance_confidence=0.3 if off_topic else 0.9,
        quality_score=0 if (off_topic or gibberish) else 12,
        grammar_spelling_penalty=1,
        saliency_notes="Demo handler: optimistic grading for illustration.",
        rationale="Demo handler output — not a real assessment.",
    )


def _task_e(user: str) -> TaskEOutput:
    """Extract plausible resume signals; reached only if a demo CSV carries a fetchable URL."""
    return TaskEOutput(
        is_resume=True,
        relevant_projects=2,
        relevant_experience=1,
        relevant_awards=1,
        skills_relevance=0.7,
        highlights="Demo handler: two projects, one internship, one award.",
        rationale="Demo handler output — not a real assessment.",
    )


def _task_f(user: str) -> TaskFOutput:
    """Score the optional technical essay mid-range — deliberately not full marks, since a demo
    where every bonus maxes out hides the arithmetic. The sentinels only zero the bonus here."""
    off_topic = _OFFTOPIC in user
    gibberish = _GIBBERISH in user
    return TaskFOutput(
        on_topic=not off_topic,
        gibberish=gibberish,
        technical_depth_0_10=0 if (off_topic or gibberish) else 6,
        exploration_level_0_10=0 if (off_topic or gibberish) else 7,
        impact_0_10=0 if (off_topic or gibberish) else 5,
        rationale="Demo handler output — not a real assessment.",
    )


def demo_handler(task: str, user: str, schema: type[BaseModel]) -> BaseModel:
    """Route a faked LLM call to the matching builder. ``schema`` is unused — each builder
    constructs its concrete contract model directly."""
    builders: dict[TaskName, object] = {
        "task_a": _task_a,
        "task_b": _task_b,
        "task_c": _task_c,
        "task_d": _task_d,
        "task_e": _task_e,
        "task_f": _task_f,
    }
    builder = builders.get(task)  # type: ignore[arg-type]
    if builder is None:  # unknown task — should never happen
        raise ValueError(f"demo_handler: unknown task {task!r}")
    return builder(user)  # type: ignore[operator]


__all__ = ["demo_handler"]
