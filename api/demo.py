"""Dev-only demo LLM handler (Phase 10).

A tiny, deterministic stand-in for the OpenAI calls so the **whole UI can be demoed end-to-end
with no API key and zero token spend**. Activated only when the app is launched with the
``SRIP_DEV_FAKE_LLM=1`` environment flag (see ``api.main``); production always uses the real
:class:`~srip_filter.llm.client.OpenAILLMClient`.

It returns *optimistic* outputs (on-topic, non-gibberish, plausible grades) so gate-survivors
become richly-scored ``RANKED`` records — exactly what makes the audit browser and cohort tool
worth looking at. Outcome variety in a demo run comes from the **deterministic** gates instead
(short essays → REJECTED, blank/low GPA → REJECTED/NEEDS_REVIEW), which run before any LLM call.

Two sentinels let a crafted demo CSV exercise the LLM-driven reject/needs-review paths:

* an essay containing ``[[OFFTOPIC]]``  → Task D ``on_topic = False`` (off-topic REJECTED);
* an essay containing ``[[GIBBERISH]]`` → Task D ``is_gibberish = True`` (gibberish REJECTED).

This module imports nothing from FastAPI and is never used by the test suite (which injects its
own scripted ``FakeLLMClient``). It is intentionally simple and clearly dev-only.
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


def _task_c(user: str) -> TaskCOutput:
    """Decompose the coursework cell with a light comma split (demo only)."""
    # The user prompt wraps the raw cell as COURSEWORK_RAW: """...""" — pull the inner text.
    inner = user
    match = re.search(r'"""(.*)"""', user, flags=re.DOTALL)
    if match:
        inner = match.group(1)
    fragments = [f.strip() for f in re.split(r"[,\n;]", inner) if f.strip()]
    courses: list[CourseItem] = []
    for i, fragment in enumerate(fragments[:6]):  # cap for a tidy demo panel
        category, weight = _CATEGORIES[i % len(_CATEGORIES)]
        courses.append(
            CourseItem(
                name=fragment[:80],
                grade_raw="A-",
                grade_pct=90,
                category=category,  # type: ignore[arg-type]
                counts=True,
                category_weight=weight,
            )
        )
    return TaskCOutput(courses=courses, rationale="Demo handler: light comma-split decomposition.")


def _task_d(user: str) -> TaskDOutput:
    """Grade an essay optimistically, honoring the off-topic / gibberish sentinels (demo only)."""
    off_topic = _OFFTOPIC in user
    gibberish = _GIBBERISH in user
    return TaskDOutput(
        is_gibberish=gibberish,
        on_topic=not off_topic,
        relevance_confidence=0.3 if off_topic else 0.9,
        quality_score=0 if (off_topic or gibberish) else 16,
        grammar_spelling_penalty=1,
        saliency_notes="Demo handler: optimistic grading for illustration.",
        rationale="Demo handler output — not a real assessment.",
    )


def _task_e(user: str) -> TaskEOutput:
    """Extract plausible resume signals (demo only; reached only if a demo CSV carries a
    fetchable resume URL — the shipped sample leaves the column blank)."""
    return TaskEOutput(
        is_resume=True,
        relevant_projects=2,
        relevant_experience=1,
        relevant_awards=1,
        skills_relevance=0.7,
        highlights="Demo handler: two projects, one internship, one award.",
        rationale="Demo handler output — not a real assessment.",
    )


def demo_handler(task: str, user: str, schema: type[BaseModel]) -> BaseModel:
    """Route a faked LLM call to the matching optimistic builder.

    Matches the :data:`~srip_filter.llm.client.FakeHandler` signature
    ``(task, user, schema) -> BaseModel``; ``schema`` is unused (we construct the concrete
    contract model directly).
    """
    builders: dict[TaskName, object] = {
        "task_a": _task_a,
        "task_b": _task_b,
        "task_c": _task_c,
        "task_d": _task_d,
        "task_e": _task_e,
    }
    builder = builders.get(task)  # type: ignore[arg-type]
    if builder is None:  # unknown task — should never happen
        raise ValueError(f"demo_handler: unknown task {task!r}")
    return builder(user)  # type: ignore[operator]


__all__ = ["demo_handler"]
