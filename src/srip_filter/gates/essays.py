"""Stage 1 — essay deterministic gates (PRD v3 §4).

LLM-free checks run before any token is spent: profanity over all essays (a hit routes to
NEEDS_REVIEW), gibberish over the required ones (a hit rejects), and word counts as audit data
only — **no length rule rejects anyone**. Thresholds come from ``AppConfig``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from better_profanity import Profanity

from ..config import AppConfig, GibberishConfig, project_root
from ..models import EssayLengthGate, HitGate

if TYPE_CHECKING:  # circular-import guard: ingest_webhook is a pure-mapping consumer
    from ..ingest_webhook import WebhookApplicant

DEFAULT_PROFANITY_PATH = project_root() / "resources" / "profanity.txt"
_ALLOW_PREFIX = "ALLOW:"

# PRD §2 tokenizer — the single source of truth for "how long is an essay".
_WORD_RE = re.compile(r"[\w'-]+")


def word_count(text: str) -> int:
    """Count words in an essay per the PRD §2 tokenizer (``re.findall(r"[\\w'-]+")``)."""
    return len(_WORD_RE.findall(text))


# --- Profanity gate (PRD §4.2) ---
# matcher = better-profanity's default list + curated BLOCK terms − medical/anatomical ALLOW
# terms, so clinical vocabulary in a good-faith explanation never trips the gate. The curated
# lists in resources/profanity.txt are still an inert placeholder.


@dataclass(frozen=True)
class ProfanityWordlist:
    """Parsed ``resources/profanity.txt``: BLOCK terms to add, ALLOW terms to exempt."""

    block: tuple[str, ...]
    allow: tuple[str, ...]


def load_profanity_wordlist(path: str | Path = DEFAULT_PROFANITY_PATH) -> ProfanityWordlist:
    """Parse the wordlist into BLOCK and ALLOW terms (see the file's own header for the format).

    A missing file yields empty lists — the gate then equals better-profanity's default.
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

    ``CENSOR_WORDSET`` entries are ``VaryingString``s, which compare equal to a plain string —
    that is what lets the ALLOW filter be a straight comprehension.
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
    """Build the matcher from the default wordlist path once per run."""
    return build_profanity_matcher()


def profanity_gate(text: str, matcher: Profanity | None = None) -> bool:
    """Return ``True`` if ``text`` trips the profanity matcher (PRD §4.2); blank text never does."""
    if not text.strip():
        return False
    return (matcher or _default_matcher()).contains_profanity(text)


def profanity_terms(text: str, matcher: Profanity | None = None) -> tuple[str, ...]:
    """Distinct tokens in ``text`` that individually trip the matcher, in order of first
    appearance — the auditor has to see *which* word caused the flag."""
    if not text.strip():
        return ()
    m = matcher or _default_matcher()
    seen: dict[str, None] = {}
    for token in _WORD_RE.findall(text):
        lowered = token.lower()
        if lowered not in seen and m.contains_profanity(token):
            seen[lowered] = None
    return tuple(seen)


# --- Gibberish heuristics (PRD §4.2) ---
# The PRD's dictionary-hit-ratio check is deliberately dropped (see the decisions log): no
# English-dictionary dependency, far lower ESL false-positive risk, and Task D catches the
# subtler cases. A hit needs >= 2 signals together, which ordinary awkward prose never trips.

_VOWELS = frozenset("aeiouy")  # 'y' counts as a vowel so "rhythm" isn't a consonant run


@dataclass(frozen=True)
class GibberishResult:
    """Which signals fired; only ``hit`` gates the pipeline, the rest are the audit trail."""

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
    """Flag keyboard-mashing via four independent signals, hitting only when at least
    ``cfg.min_signals`` fire — the ESL safeguard. Text under ``cfg.min_chars`` is never flagged.
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


# --- Stage 1 aggregator (PRD v3 §4) ---
# Every check here is token-free, so all of them run (a complete audit Gates block) rather than
# short-circuiting; fail-fast applies to the LLM stages downstream.


@dataclass(frozen=True)
class Stage1Result:
    """Reduced Stage-1 outcome. The three gate blocks drop straight into ``AuditRecord.gates``;
    ``length_gate`` is audit data only. Where both gates fire, ``rejected`` wins (PRD §0.7)."""

    rejected: bool
    primary_reason: str  # "" unless flagged; names the deciding gate (PRD §12 invariant)
    length_gate: EssayLengthGate
    profanity: HitGate
    gibberish: HitGate
    needs_review: bool = False


# Scope, all owner decisions: no length gate (the site server-validates bounds at submit, so a
# check here could only false-positive on a good-faith applicant, 2026-07-28); profanity covers
# ALL essays including the optional one (2026-07-04); gibberish covers the REQUIRED ones only,
# since essay-3 gibberish merely zeroes its bonus via Task F (2026-07-28).


def run_essay_gates_v3(
    applicant: WebhookApplicant, cfg: AppConfig, matcher: Profanity | None = None
) -> Stage1Result:
    """Stage 1: profanity (all essays) + gibberish (required essays).

    Gibberish rejects; profanity only sets ``needs_review`` (owner, 2026-07-29) — a word list
    cannot tell "the transatlantic slave trade" from an insult, so a false positive must cost a
    review rather than an application.
    """
    row = applicant.row
    b1_wc, b2_wc = word_count(row.essay1), word_count(row.essay2)

    all_essays = [("1", row.essay1), ("2", row.essay2)]
    if row.essay3.strip():
        all_essays.append(("3", row.essay3))
    profane_terms: tuple[str, ...] = ()
    profanity_hit = any(profanity_gate(text, matcher) for _, text in all_essays)
    if profanity_hit:
        profane_terms = tuple(
            dict.fromkeys(
                term
                for n, text in all_essays
                for term in (f"e{n}:{t}" for t in profanity_terms(text, matcher))
            )
        )

    gib1 = gibberish_gate(row.essay1, cfg.gibberish)
    gib2 = gibberish_gate(row.essay2, cfg.gibberish)
    gibberish_hit = gib1.hit or gib2.hit
    gib_terms = [
        f"e{n}:{signal}"
        for n, res in ((1, gib1), (2, gib2))
        if res.hit
        for signal, fired in (
            ("consonant_run", res.consonant_run),
            ("low_entropy", res.low_entropy),
            ("repeat_run", res.repeat_run),
            ("low_unique_ratio", res.low_unique_ratio),
        )
        if fired
    ]

    # Gibberish first: it is the only Stage-1 gate that rejects, so where both fire it is the
    # deciding verdict and must be the one named.
    if gibberish_hit:
        reason = "Essay flagged as gibberish by deterministic heuristics"
    elif profanity_hit:
        reason = "Profanity flagged in an essay — needs human confirmation"
    else:
        reason = ""

    return Stage1Result(
        rejected=gibberish_hit,
        needs_review=profanity_hit,
        primary_reason=reason,
        # ok/hard_fail are pinned: the length gate retired, word counts are audit data only.
        length_gate=EssayLengthGate(
            e1_wc=b1_wc, e2_wc=b2_wc, e1_ok=True, e2_ok=True, hard_fail=False
        ),
        profanity=HitGate(hit=profanity_hit, terms=list(profane_terms)),
        gibberish=HitGate(hit=gibberish_hit, terms=gib_terms),
    )
