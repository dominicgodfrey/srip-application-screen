"""Stage 1 — essay deterministic gates (Phase 2).

Cheap, LLM-free checks that run on *both* essays before any token is spent. Per PRD §4 a
*hard* failure on either essay rejects the whole application; *soft* problems (slightly-off
length) are recorded here and carried forward to Stage 4 scoring (§8.3), never a rejection.

This file is built up across Phase 2:
  * 2.1 length gate           — :func:`word_count`, :func:`length_gate`
  * 2.2 profanity gate        — :func:`profanity_gate` (+ wordlist loader)
  * 2.3 gibberish heuristics  — :func:`gibberish_gate`   (this commit)
  * 2.4 Stage 1 aggregator    — pending

The length/gibberish math is pure; the profanity gate depends on a loaded wordlist (file I/O
at construction only), so it takes its matcher as an argument or lazily builds a cached default.
Thresholds come from ``AppConfig``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from better_profanity import Profanity

from ..config import EssayLengthConfig, GibberishConfig

# resources/profanity.txt lives at the project root (this file is src/srip_filter/gates/...).
DEFAULT_PROFANITY_PATH = Path(__file__).resolve().parents[3] / "resources" / "profanity.txt"
_ALLOW_PREFIX = "ALLOW:"

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


# ================================================================================================
# 2.2 — Profanity gate (PRD §4.2)
# ================================================================================================
# Built on better-profanity (whole-token matching, case-insensitive, light leetspeak via its
# CHARS_MAPPING). The matcher = better-profanity's DEFAULT list + our curated BLOCK terms − our
# medical/anatomical ALLOW terms, so clinical vocabulary in a good-faith extenuating-circumstances
# explanation never trips the gate. The curated lists currently live as an inert placeholder in
# resources/profanity.txt (openissue.md #3); until filled, the gate behaves as the default list.


@dataclass(frozen=True)
class ProfanityWordlist:
    """Parsed ``resources/profanity.txt``: BLOCK terms to add, ALLOW terms to exempt."""

    block: tuple[str, ...]
    allow: tuple[str, ...]


def load_profanity_wordlist(path: str | Path = DEFAULT_PROFANITY_PATH) -> ProfanityWordlist:
    """Parse the profanity wordlist file into BLOCK and ALLOW term tuples.

    Format (see the file's own header): blank lines and ``#`` comments are ignored; a line
    starting with ``ALLOW:`` is a medical/anatomical exemption; every other non-comment line is
    a term to block. Terms are lowercased for case-insensitive matching. A missing file yields
    empty lists (the gate then == better-profanity's default list) rather than raising.
    """
    file_path = Path(path)
    if not file_path.exists():
        return ProfanityWordlist(block=(), allow=())
    block: list[str] = []
    allow: list[str] = []
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith(_ALLOW_PREFIX):
            term = line[len(_ALLOW_PREFIX) :].strip().lower()
            if term:
                allow.append(term)
        else:
            block.append(line.lower())
    return ProfanityWordlist(block=tuple(block), allow=tuple(allow))


def build_profanity_matcher(path: str | Path = DEFAULT_PROFANITY_PATH) -> Profanity:
    """Build a configured :class:`Profanity` matcher: default list + BLOCK − ALLOW.

    Loads better-profanity's built-in list, adds our curated BLOCK terms, then drops any entry
    (default or added) matching an ALLOW term so clinical/anatomical words are exempt.
    ``CENSOR_WORDSET`` is a plain list, so the allow filter is a straightforward comprehension;
    ``VaryingString`` compares equal to a plain string, which is what powers the match.
    """
    wordlist = load_profanity_wordlist(path)
    matcher = Profanity()
    if wordlist.block:
        matcher.add_censor_words(list(wordlist.block))
    if wordlist.allow:
        allow = set(wordlist.allow)
        matcher.CENSOR_WORDSET = [
            entry for entry in matcher.CENSOR_WORDSET if not any(entry == a for a in allow)
        ]
    return matcher


@lru_cache(maxsize=1)
def _default_matcher() -> Profanity:
    """Lazily build and cache the matcher from the default wordlist path (built once per run)."""
    return build_profanity_matcher()


def profanity_gate(text: str, matcher: Profanity | None = None) -> bool:
    """Return ``True`` if ``text`` contains profanity/a slur (a hard-reject signal, PRD §4.2).

    Empty/whitespace text is never a hit. Pass an explicit ``matcher`` (e.g. in tests) or rely
    on the cached default built from ``resources/profanity.txt``.
    """
    if not text.strip():
        return False
    return (matcher or _default_matcher()).contains_profanity(text)


# ================================================================================================
# 2.3 — Gibberish heuristics (PRD §4.2, no dictionary)
# ================================================================================================
# Cheap deterministic signals only — the dictionary-hit-ratio check from the PRD is intentionally
# dropped (see PLAN decisions log) so there is no English-dictionary dependency and far lower ESL
# false-positive risk; subtler gibberish is caught later by LLM Task D. A hit requires >= 2 of the
# signals below to fire together, so ordinary awkward/ESL prose (which trips at most one) passes.

_VOWELS = frozenset("aeiouy")  # 'y' counted as a vowel to avoid false consonant runs (rhythm)


@dataclass(frozen=True)
class GibberishResult:
    """Which cheap signals fired, and whether their count crosses ``min_signals``.

    The individual booleans are kept for the audit/debug trail; only ``hit`` gates the pipeline.
    """

    hit: bool
    consonant_run: bool
    low_entropy: bool
    repeat_run: bool
    low_unique_ratio: bool
    signal_count: int


def _longest_consonant_run(text: str) -> int:
    """Length of the longest run of consecutive consonant letters (case-insensitive)."""
    longest = current = 0
    for ch in text.lower():
        if ch.isalpha() and ch not in _VOWELS:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _longest_repeat_run(text: str) -> int:
    """Length of the longest run of one identical non-space character (e.g. ``aaaaaa`` -> 6)."""
    longest = current = 0
    prev: str | None = None
    for ch in text.lower():
        if ch.isspace():
            prev, current = None, 0
            continue
        current = current + 1 if ch == prev else 1
        prev = ch
        longest = max(longest, current)
    return longest


def _char_entropy(letters: list[str]) -> float:
    """Shannon entropy (bits) of a letter sequence; ``asdfasdf``/``aaaaaa`` score very low."""
    if not letters:
        return 0.0
    total = len(letters)
    return -sum((n / total) * math.log2(n / total) for n in Counter(letters).values())


def gibberish_gate(text: str, cfg: GibberishConfig) -> GibberishResult:
    """Flag keyboard-mashing / good-faith-failure essays via cheap deterministic signals.

    Computes up to four independent signals (long consonant run, low letter entropy, a long
    identical-char run, a low unique-word ratio) and reports a hit only when at least
    ``cfg.min_signals`` of them fire — the ESL safeguard. Text with too few letters
    (``< cfg.min_chars``) carries too little signal and is never flagged. Pure function.
    """
    letters = [c for c in text.lower() if c.isalpha()]
    if len(letters) < cfg.min_chars:
        return GibberishResult(False, False, False, False, False, 0)

    words = [w.lower() for w in _WORD_RE.findall(text)]
    consonant_run = _longest_consonant_run(text) > cfg.max_consonant_run
    low_entropy = _char_entropy(letters) < cfg.min_char_entropy
    repeat_run = _longest_repeat_run(text) >= cfg.max_repeat_run
    low_unique_ratio = (
        len(words) >= cfg.min_words_for_ratio
        and len(set(words)) / len(words) < cfg.min_unique_word_ratio
    )

    count = sum((consonant_run, low_entropy, repeat_run, low_unique_ratio))
    return GibberishResult(
        hit=count >= cfg.min_signals,
        consonant_run=consonant_run,
        low_entropy=low_entropy,
        repeat_run=repeat_run,
        low_unique_ratio=low_unique_ratio,
        signal_count=count,
    )
