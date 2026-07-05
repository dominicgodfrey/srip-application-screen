"""Stage 4b — optional technical-essay bonus via Task F (v3, PRD v3 §4).

Bonus-only by construction (SCORING.md / §0.3 law): absent essay ⇒ 0 with **no LLM
call**; gibberish/off-topic ⇒ 0; over ``max_words`` ⇒ 0 (the site does not
server-validate optional essays, so the bound is enforced here — voided, never a
rejection); a Task F parse failure ⇒ 0 + an audit error note (the Task C precedent).
Nothing in this module can reject or subtract.

Split in the house style: :func:`technical_essay_bonus` is the pure config-priced math
(zero spend, fully testable); :func:`score_technical_essay` is the LLM-touching
aggregator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import AppConfig, TechnicalEssayConfig
from ..gates.essays import word_count
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_f as task_f_prompt
from ..models import TaskFOutput, TechnicalEssayAssessment


def technical_essay_bonus(out: TaskFOutput, cfg: TechnicalEssayConfig) -> float:
    """Price Task F signals into a 0–``bonus_max`` bonus (pure, config-owned).

    ``bonus = bonus_max · Σ(wᵢ·signalᵢ) / (10·Σwᵢ)`` — a weighted mean of the three 0–10
    signals scaled onto the bonus range. Gated to 0 by ``on_topic``/``gibberish``.
    Clamped to ``[0, bonus_max]``; never negative (bonuses only add).
    """
    if not out.on_topic or out.gibberish:
        return 0.0
    weight_sum = cfg.weight_depth + cfg.weight_exploration + cfg.weight_impact
    if weight_sum <= 0 or cfg.bonus_max <= 0:
        return 0.0
    weighted = (
        cfg.weight_depth * out.technical_depth_0_10
        + cfg.weight_exploration * out.exploration_level_0_10
        + cfg.weight_impact * out.impact_0_10
    )
    bonus = cfg.bonus_max * weighted / (10.0 * weight_sum)
    return round(max(0.0, min(cfg.bonus_max, bonus)), 4)


@dataclass(frozen=True)
class Stage4bResult:
    """Outcome of the technical-essay stage: a bonus and its audit block. Never a verdict."""

    bonus: float
    assessment: TechnicalEssayAssessment
    errors: list[str] = field(default_factory=list)
    llm_called: bool = False


async def score_technical_essay(
    essay_text: str,
    question: str,
    max_words: int | None,
    client: BaseLLMClient,
    cfg: AppConfig,
) -> Stage4bResult:
    """Run Stage 4b for one applicant.

    Fail-safe ladder (each rung ⇒ 0 bonus, never a rejection, and only the last rung
    spends a token): absent → over-max → Task F (parse failure → 0 + error note).
    Profanity in this essay was already a Stage-1 reject and never reaches here.
    """
    text = essay_text.strip()
    if not text:
        return Stage4bResult(
            bonus=0.0,
            assessment=TechnicalEssayAssessment(present=False, skipped_reason="absent"),
        )

    wc = word_count(text)
    if max_words is not None and wc > max_words:
        return Stage4bResult(
            bonus=0.0,
            assessment=TechnicalEssayAssessment(
                present=True,
                word_count=wc,
                over_max=True,
                skipped_reason=f"over_max ({wc} > {max_words} words) — bonus voided",
            ),
        )

    try:
        out = await client.complete(
            "task_f",
            system=task_f_prompt.SYSTEM,
            user=task_f_prompt.user_prompt(question, wc, text),
            schema=TaskFOutput,
            cache_text=text,
        )
    except LLMParseFailure:
        return Stage4bResult(
            bonus=0.0,
            assessment=TechnicalEssayAssessment(
                present=True, word_count=wc, skipped_reason="llm_parse_failure"
            ),
            errors=["task_f: LLM_PARSE_FAILURE — technical-essay bonus set to 0"],
            llm_called=True,
        )

    bonus = technical_essay_bonus(out, cfg.technical_essay)
    return Stage4bResult(
        bonus=bonus,
        assessment=TechnicalEssayAssessment(
            present=True, word_count=wc, signals=out, bonus=bonus
        ),
        llm_called=True,
    )
