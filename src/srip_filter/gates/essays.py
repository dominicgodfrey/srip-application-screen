"""Stage 1 — essay deterministic gates (Phase 2).

Cheap, LLM-free checks that run on *both* essays before any token is spent. Per PRD §4 a
*hard* failure on either essay rejects the whole application; *soft* problems (slightly-off
length) are recorded here and carried forward to Stage 4 scoring (§8.3), never a rejection.

This file is built up across Phase 2:
  * 2.1 length gate           — :func:`word_count`, :func:`length_gate`   (this commit)
  * 2.2 profanity gate        — pending
  * 2.3 gibberish heuristics  — pending
  * 2.4 Stage 1 aggregator    — pending

All functions here are pure (no I/O, no globals); thresholds come from ``AppConfig``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import EssayLengthConfig

# PRD §2 word-count rule: tokens are runs of word chars, apostrophes, and hyphens. This is the
# single source of truth for "how long is an essay" across the whole pipeline.
_WORD_RE = re.compile(r"[\w'-]+")


def word_count(text: str) -> int:
    """Count words in an essay per the PRD §2 tokenizer (``re.findall(r"[\\w'-]+")``)."""
    return len(_WORD_RE.findall(text))


@dataclass(frozen=True)
class LengthResult:
    """Outcome of the length check for one essay.

    ``hard_fail`` is the only field that can reject; ``ok`` distinguishes an ideal-length essay
    from one that merely earns a soft penalty. ``length_penalty`` is a float in
    ``[0, len_penalty_max]`` carried to Stage 4 essay scoring (subtracted there, §8.3) — it is
    never a rejection on its own.
    """

    wc: int
    ok: bool  # within the target band [target_min, target_max] — no penalty, no fail
    hard_fail: bool  # outside [hard_min, hard_max] -> REJECTED
    length_penalty: float  # soft penalty, ramps 0 -> len_penalty_max across the off-target band


def _soft_penalty(wc: int, cfg: EssayLengthConfig) -> float:
    """Ramp the soft length penalty from 0 (at the target edge) to ``len_penalty_max``.

    Zero inside ``[target_min, target_max]``. Below ``target_min`` it ramps linearly to the max
    at ``hard_min``; above ``target_max`` it ramps to the max at ``hard_max``. Clamped to the
    max so a hard-fail word count (handled separately) never reports more than ``len_penalty_max``.
    """
    if cfg.target_min <= wc <= cfg.target_max:
        return 0.0
    if wc < cfg.target_min:
        span = cfg.target_min - cfg.hard_min
        frac = 1.0 if span <= 0 else (cfg.target_min - wc) / span
    else:  # wc > target_max
        span = cfg.hard_max - cfg.target_max
        frac = 1.0 if span <= 0 else (wc - cfg.target_max) / span
    return min(float(cfg.len_penalty_max), max(0.0, frac) * cfg.len_penalty_max)


def length_gate(text: str, cfg: EssayLengthConfig) -> LengthResult:
    """Apply the PRD §4.1 length rule to one essay.

    Hard fail when ``wc < hard_min`` or ``wc > hard_max`` (an empty essay hard-fails). Otherwise
    the essay survives; a word count outside ``[target_min, target_max]`` accrues a soft penalty
    that ramps toward ``len_penalty_max`` near the hard bounds. Pure function.
    """
    wc = word_count(text)
    hard_fail = wc < cfg.hard_min or wc > cfg.hard_max
    ok = cfg.target_min <= wc <= cfg.target_max
    return LengthResult(wc=wc, ok=ok, hard_fail=hard_fail, length_penalty=_soft_penalty(wc, cfg))
