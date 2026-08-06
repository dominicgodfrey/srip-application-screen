"""Stage 4b — optional technical-essay bonus via Task F (PRD v3 §4).

Bonus-only by construction: nothing here can reject or subtract. Absent essay, gibberish,
off-topic, over ``max_words``, or a parse failure all resolve to 0 bonus.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import AppConfig, TechnicalEssayConfig
from ..gates.essays import word_count
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_f as task_f_prompt
from ..models import TaskFOutput, TechnicalEssayAssessment


def technical_essay_bonus(out: TaskFOutput, cfg: TechnicalEssayConfig) -> float:
    """Price Task F signals: ``bonus_max · Σ(wᵢ·signalᵢ) / (10·Σwᵢ)``, a weighted mean of the
    three 0–10 signals scaled onto ``[0, bonus_max]``. Gated to 0 by ``on_topic``/``gibberish``."""
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
    client: BaseLLMClient,
    cfg: AppConfig,
    *,
    max_words: int | None = None,
) -> Stage4bResult:
    """Run Stage 4b for one applicant: absent → over-max → Task F. Each rung yields 0 bonus and
    never a rejection, and only the last spends a token.

    ``max_words`` is enforced here because the site does not server-validate optional essays.
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
