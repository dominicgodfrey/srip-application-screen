"""Stage 1 — essay deterministic gates (PRD v3 §4).

Cheap, LLM-free checks that run on every essay before any token is spent:

  * :func:`word_count`      — audit data only; **no length rule rejects anyone**
  * :func:`profanity_gate`  — all essays; a hit routes to NEEDS_REVIEW (+ wordlist loader)
  * :func:`gibberish_gate`  — required essays; a hit REJECTS
  * :func:`run_essay_gates_v3` — the aggregator the pipeline calls

The gibberish math is pure; the profanity gate depends on a loaded wordlist (file I/O at
construction only), so it takes its matcher as an argument or lazily builds a cached
default. Thresholds come from ``AppConfig``.
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

# resources/profanity.txt lives at the project root (see config.project_root — the source
# tree and an installed package resolve it differently).
DEFAULT_PROFANITY_PATH = project_root() / "resources" / "profanity.txt"
_ALLOW_PREFIX = "ALLOW:"

# PRD §2 word-count rule: tokens are runs of word chars, apostrophes, and hyphens. This is the
# single source of truth for "how long is an essay" across the whole pipeline.
_WORD_RE = re.compile(r"[\w'-]+")


def word_count(text: str) -> int:
    """Count words in an essay per the PRD §2 tokenizer (``re.findall(r"[\\w'-]+")``)."""
    return len(_WORD_RE.findall(text))


# ================================================================================================
# 2.2 — Profanity gate (PRD §4.2)
# ================================================================================================
# Built on better-profanity (whole-token matching, case-insensitive, light leetspeak via its
# CHARS_MAPPING). The matcher = better-profanity's DEFAULT list + our curated BLOCK terms − our
# medical/anatomical ALLOW terms, so clinical vocabulary in a good-faith extenuating-circumstances
# explanation never trips the gate. The curated lists currently live as an inert placeholder in
# resources/profanity.txt; until it is filled, the gate behaves as the default list.


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


def profanity_terms(text: str, matcher: Profanity | None = None) -> tuple[str, ...]:
    """Return the distinct tokens in ``text`` that individually trip the profanity matcher.

    Used for the audit trail (and the audit-UI highlight) when :func:`profanity_gate` hits —
    a human auditor must be able to see *which* word caused a rejection. Tokenized with the
    same PRD §2 word rule as everything else; lowercased, order of first appearance.
    """
    if not text.strip():
        return ()
    m = matcher or _default_matcher()
    seen: dict[str, None] = {}
    for token in _WORD_RE.findall(text):
        lowered = token.lower()
        if lowered not in seen and m.contains_profanity(token):
            seen[lowered] = None
    return tuple(seen)


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


# ================================================================================================
# Stage 1 aggregator (PRD v3 §4)
# ================================================================================================
# Runs the deterministic checks over the essays and reduces them to a single verdict. Every
# check here is token-free, so all of them are computed (a complete audit Gates block) rather
# than short-circuited; fail-fast applies to the *LLM* stages downstream.


@dataclass(frozen=True)
class Stage1Result:
    """Reduced outcome of Stage 1 for one application.

    ``rejected``/``needs_review``/``primary_reason`` drive the pipeline; the three audit
    blocks (``length_gate``/``profanity``/``gibberish``) drop straight into
    ``AuditRecord.gates``. ``length_gate`` carries word counts as audit data only — no
    length rule decides anything (see the aggregator below).

    A profanity hit sets ``needs_review`` rather than ``rejected`` (owner, 2026-07-29): it
    routes to a human. Where both fire, ``rejected`` wins — a definite reject outranks a
    review (PRD §0.7).
    """

    rejected: bool
    primary_reason: str  # "" unless flagged; names the deciding gate (PRD §12 invariant)
    length_gate: EssayLengthGate
    profanity: HitGate
    gibberish: HitGate
    needs_review: bool = False


# Scope notes, all owner decisions:
#   * NO length gate. The site server-validates word bounds at submit (400, the submission
#     never lands), so a violation cannot reach us from a real applicant — only from our own
#     stale config, i.e. as a false positive on a good-faith one (2026-07-28). Word counts
#     are still reported for the audit record.
#   * profanity checks ALL essays including the optional one (2026-07-04).
#   * gibberish heuristics run on the REQUIRED essays only — essay-3 gibberish merely zeroes
#     its bonus via Task F, staying bonus-only even though the live form makes it mandatory
#     to submit (2026-07-28).


def run_essay_gates_v3(
    applicant: WebhookApplicant, cfg: AppConfig, matcher: Profanity | None = None
) -> Stage1Result:
    """Stage 1 over a webhook applicant: profanity(all essays) + gibberish(required essays).

    Gibberish in a required essay is ``rejected``; a profanity hit sets ``needs_review``
    instead (owner, 2026-07-29) — the matcher is a word list, and a word list cannot tell
    "the transatlantic slave trade" from an insult. A human confirms every flag, so a false
    positive costs a review, not an application.
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

    # Gibberish is checked first because it is the only Stage-1 gate that still rejects
    # outright; profanity routes to a human (owner, 2026-07-29), so where both fire the
    # rejection is the deciding verdict and must be the one named.
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
        # Word counts are audit data only — ok/hard_fail are pinned True/False because the
        # length gate retired (bounds are enforced by the site at submit).
        length_gate=EssayLengthGate(
            e1_wc=b1_wc, e2_wc=b2_wc, e1_ok=True, e2_ok=True, hard_fail=False
        ),
        profanity=HitGate(hit=profanity_hit, terms=list(profane_terms)),
        gibberish=HitGate(hit=gibberish_hit, terms=gib_terms),
    )
