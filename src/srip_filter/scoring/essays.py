"""Stage 4 — essay LLM grading (Phase 4, Task D).

Runs only on Stage 1-3 survivors. For each essay, LLM Task D applies the gibberish backstop and
the relevance gate (either failing → ``REJECTED`` for the whole application, PRD §4/§8.3) and a
0-20 quality score; the carried Stage-1 soft length penalty and the Task-D grammar penalty are
then subtracted. A Task-D parse failure (after the client's retry) → ``NEEDS_REVIEW``, never a
rejection (PRD §8).

The two Task-D calls per applicant are the only spend in this stage. The work is split so the
LLM call is isolated and the §8.3 post-processing math stays fully testable with zero API spend:

  * 4.2 per-essay post-processing math — :func:`score_one_essay`   (pure, no LLM)
  * 4.3 Stage 4 aggregator             — :func:`grade_essays`      (LLM)

Thresholds come from ``AppConfig.essay_scoring``; no magic numbers here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from ..config import AppConfig, EssayScoringConfig
from ..gates.essays import word_count
from ..ingest import ApplicantRow
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_d as task_d_prompt
from ..models import EssayRelevanceGate, EssaySubscores, HitGate, TaskDOutput

# Internal Stage-4 verdict. Distinct from the final Outcome: "pass" means both essays cleared the
# gibberish and relevance gates and the application continues to bonus scoring — it is not yet
# RANKED.
Stage4Verdict = Literal["pass", "reject", "needs_review"]


# ================================================================================================
# 4.2 — Per-essay post-processing math (pure, no LLM, PRD §8.3)
# ================================================================================================
# Turns one Task D output + the carried Stage-1 length penalty into a gate-aware essay subscore.
# The two gate flags (is_gibberish, not on_topic) disqualify the whole application upstream; a
# gated essay contributes 0. A length penalty can never drive a score below 0 (the max(0, …)
# floor), so a missing/too-short optional length is neutral-to-negative on the essay only, never a
# manufactured rejection.


@dataclass(frozen=True)
class EssayScoreResult:
    """Post-processed Task D result for one essay.

    ``is_gibberish`` and ``on_topic`` are the gate flags read by the aggregator; ``score`` is the
    additive essay subscore in ``[0, quality_max_each]`` (0 whenever the essay is gated). The
    ``gated`` convenience says whether either gate tripped.
    """

    is_gibberish: bool
    on_topic: bool
    score: float

    @property
    def gated(self) -> bool:
        return self.is_gibberish or not self.on_topic


def score_one_essay(
    out: TaskDOutput, length_penalty: float, cfg: EssayScoringConfig
) -> EssayScoreResult:
    """Apply the PRD §8.3 per-essay post-processing to one Task D output. Pure function.

    A gibberish or off-topic essay is gated → score 0 (the application is rejected upstream).
    Otherwise ``score = max(0, quality_score - grammar_spelling_penalty - length_penalty)``,
    capped at ``quality_max_each``. The ``max(0, …)`` floor guarantees a length penalty never
    produces a negative subscore.
    """
    if out.is_gibberish or not out.on_topic:
        return EssayScoreResult(is_gibberish=out.is_gibberish, on_topic=out.on_topic, score=0.0)
    raw = out.quality_score - out.grammar_spelling_penalty - length_penalty
    score = max(0.0, min(float(cfg.quality_max_each), raw))
    return EssayScoreResult(is_gibberish=False, on_topic=True, score=round(score, 4))


# ================================================================================================
# 4.3 — Stage 4 aggregator (LLM)
# ================================================================================================
# grade_essays calls Task D for both essays (the client bounds concurrency + caches), applies 4.2,
# and reduces to a verdict. Gibberish OR off-topic on EITHER essay rejects the whole application
# (PRD §4 "one failed essay fails the application"), with primary_reason naming the failing
# essay/gate in deterministic fail-fast order (gibberish → relevance). A Task-D LLMParseFailure
# (after the client's retry) → NEEDS_REVIEW with reason LLM_PARSE_FAILURE — never a rejection.


@dataclass(frozen=True)
class Stage4Result:
    """Reduced outcome of Stage 4 for one application.

    ``verdict``/``primary_reason`` drive the pipeline; ``essay_relevance`` and ``gibberish`` drop
    straight into ``AuditRecord.gates`` (the Task-D gibberish finding, reconciled with Stage 1's
    in Phase 8); ``subscores`` carries the e1/e2/total essay points. ``e1_grade``/``e2_grade`` are
    the raw Task D outputs (for audit reasons/notes), or ``None`` on a parse failure.
    """

    verdict: Stage4Verdict
    primary_reason: str  # "" on pass; names the failing gate on reject/needs_review
    essay_relevance: EssayRelevanceGate
    gibberish: HitGate
    subscores: EssaySubscores
    e1_grade: TaskDOutput | None
    e2_grade: TaskDOutput | None


def _stage4_reason(e1: EssayScoreResult, e2: EssayScoreResult) -> str:
    """Name the failing gate for a rejected application (fail-fast order: gibberish → relevance)."""
    for n, r in ((1, e1), (2, e2)):
        if r.is_gibberish:
            return f"Essay {n} is gibberish"
    for n, r in ((1, e1), (2, e2)):
        if not r.on_topic:
            return f"Essay {n} off-topic"
    return ""


def _needs_review() -> Stage4Result:
    """Stage 4 could not be scored (Task D parse failure) → NEEDS_REVIEW, never a rejection."""
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
    length_penalty_e1: float,
    length_penalty_e2: float,
    prompt_e1: str,
    prompt_e2: str,
    client: BaseLLMClient,
    cfg: AppConfig,
) -> Stage4Result:
    """Stage 4 end to end: grade both essays with Task D and reduce to a verdict (PRD §8.3).

    ``prompt_e1``/``prompt_e2`` are the resolved CSV essay-question headers (the prompts the
    applicant answered), supplied by the orchestrator. ``length_penalty_*`` are the soft penalties
    carried from Stage 1. Both Task D calls run concurrently (the client bounds concurrency and
    caches by the rendered prompt, so identical essays dedup within a run). Gibberish or off-topic
    on either essay → ``reject``; an :class:`LLMParseFailure` after the client's retry →
    ``needs_review``. Otherwise ``pass`` with the composed subscores.
    """
    try:
        out1, out2 = await asyncio.gather(
            client.complete(
                "task_d",
                system=task_d_prompt.SYSTEM,
                user=task_d_prompt.user_prompt(prompt_e1, word_count(row.essay1), row.essay1),
                schema=TaskDOutput,
            ),
            client.complete(
                "task_d",
                system=task_d_prompt.SYSTEM,
                user=task_d_prompt.user_prompt(prompt_e2, word_count(row.essay2), row.essay2),
                schema=TaskDOutput,
            ),
        )
    except LLMParseFailure:
        return _needs_review()

    e1 = score_one_essay(out1, length_penalty_e1, cfg.essay_scoring)
    e2 = score_one_essay(out2, length_penalty_e2, cfg.essay_scoring)

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
