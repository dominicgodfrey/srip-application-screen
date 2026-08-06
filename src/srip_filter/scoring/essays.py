"""Stage 4 — required-essay grading with LLM Task D (PRD §4/§8.3).

Runs only on Stage 1-3 survivors. Task D applies the gibberish backstop and the relevance gate
(either failing rejects the whole application) and scores quality, less a grammar penalty. A
parse failure after the client's retry → ``NEEDS_REVIEW``, never a rejection (PRD §8).
Thresholds come from ``AppConfig.essay_scoring``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from ..applicant import ApplicantRow
from ..config import AppConfig, EssayScoringConfig
from ..gates.essays import word_count
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_d as task_d_prompt
from ..models import EssayRelevanceGate, EssaySubscores, HitGate, TaskDOutput

# Internal to this stage: "pass" clears both gates and continues to bonus scoring — not yet RANKED.
Stage4Verdict = Literal["pass", "reject", "needs_review"]


# --- Per-essay post-processing math (pure, no LLM, PRD §8.3) ---


@dataclass(frozen=True)
class EssayScoreResult:
    """Post-processed Task D result for one essay: the two gate flags the aggregator reads, plus
    the subscore in ``[0, quality_max_each]`` (0 whenever the essay is gated)."""

    is_gibberish: bool
    on_topic: bool
    score: float

    @property
    def gated(self) -> bool:
        return self.is_gibberish or not self.on_topic


def score_one_essay(out: TaskDOutput, cfg: EssayScoringConfig) -> EssayScoreResult:
    """Post-process one Task D output: a gated essay scores 0 (the aggregator rejects), otherwise
    ``max(0, quality_score - grammar_spelling_penalty)`` capped at ``quality_max_each``."""
    if out.is_gibberish or not out.on_topic:
        return EssayScoreResult(is_gibberish=out.is_gibberish, on_topic=out.on_topic, score=0.0)
    raw = out.quality_score - out.grammar_spelling_penalty
    score = max(0.0, min(float(cfg.quality_max_each), raw))
    return EssayScoreResult(is_gibberish=False, on_topic=True, score=round(score, 4))


# --- Stage 4 aggregator (LLM) ---
# One failed essay fails the application (PRD §4), so gibberish or off-topic on either rejects.


@dataclass(frozen=True)
class Stage4Result:
    """Reduced Stage-4 outcome. ``essay_relevance``/``gibberish`` drop into ``AuditRecord.gates``;
    ``e1_grade``/``e2_grade`` are the raw Task D outputs, ``None`` on a parse failure."""

    verdict: Stage4Verdict
    primary_reason: str  # "" on pass; names the failing gate on reject/needs_review
    essay_relevance: EssayRelevanceGate
    gibberish: HitGate
    subscores: EssaySubscores
    e1_grade: TaskDOutput | None
    e2_grade: TaskDOutput | None


def _stage4_reason(e1: EssayScoreResult, e2: EssayScoreResult) -> str:
    """Name the failing gate, in fail-fast order: gibberish → relevance."""
    for n, r in ((1, e1), (2, e2)):
        if r.is_gibberish:
            return f"Essay {n} is gibberish"
    for n, r in ((1, e1), (2, e2)):
        if not r.on_topic:
            return f"Essay {n} off-topic"
    return ""


def _needs_review() -> Stage4Result:
    """Unscoreable (Task D parse failure) → NEEDS_REVIEW, never a rejection."""
    return Stage4Result(
        verdict="needs_review",
        primary_reason="LLM_PARSE_FAILURE",
        essay_relevance=EssayRelevanceGate(),
        gibberish=HitGate(),
        subscores=EssaySubscores(),
        e1_grade=None,
        e2_grade=None,
    )


async def grade_essays(
    row: ApplicantRow,
    prompt_e1: str,
    prompt_e2: str,
    client: BaseLLMClient,
    cfg: AppConfig,
    *,
    target_range_e1: str | None = None,
    target_range_e2: str | None = None,
) -> Stage4Result:
    """Stage 4 end to end: grade both essays with Task D and reduce to a verdict.

    ``prompt_e1``/``prompt_e2`` are the questions the applicant actually answered, taken from
    the payload, so they can never drift from the live form.
    """
    # The payload carries no per-essay bounds; None falls back to the prompt's default band.
    range_kw1 = {"target_range": target_range_e1} if target_range_e1 else {}
    range_kw2 = {"target_range": target_range_e2} if target_range_e2 else {}
    try:
        out1, out2 = await asyncio.gather(
            client.complete(
                "task_d",
                system=task_d_prompt.SYSTEM,
                user=task_d_prompt.user_prompt(
                    prompt_e1, word_count(row.essay1), row.essay1, **range_kw1
                ),
                schema=TaskDOutput,
            ),
            client.complete(
                "task_d",
                system=task_d_prompt.SYSTEM,
                user=task_d_prompt.user_prompt(
                    prompt_e2, word_count(row.essay2), row.essay2, **range_kw2
                ),
                schema=TaskDOutput,
            ),
        )
    except LLMParseFailure:
        return _needs_review()

    e1 = score_one_essay(out1, cfg.essay_scoring)
    e2 = score_one_essay(out2, cfg.essay_scoring)

    relevance = EssayRelevanceGate(e1_on_topic=out1.on_topic, e2_on_topic=out2.on_topic)
    gibberish = HitGate(hit=out1.is_gibberish or out2.is_gibberish)
    subscores = EssaySubscores(e1=e1.score, e2=e2.score, total=round(e1.score + e2.score, 4))

    rejected = e1.gated or e2.gated
    verdict: Stage4Verdict = "reject" if rejected else "pass"
    return Stage4Result(
        verdict=verdict,
        primary_reason=_stage4_reason(e1, e2) if rejected else "",
        essay_relevance=relevance,
        gibberish=gibberish,
        subscores=subscores,
        e1_grade=out1,
        e2_grade=out2,
    )
