"""Tests for Stage 1 essay gates (Phase 2). Synthetic text only — no applicant content.

2.1 covers the length gate; later sub-tasks append profanity / gibberish / aggregator tests.
"""

from __future__ import annotations

from pathlib import Path

from better_profanity import Profanity

from srip_filter.config import EssayLengthConfig, GibberishConfig
from srip_filter.gates.essays import (
    LengthResult,
    build_profanity_matcher,
    gibberish_gate,
    length_gate,
    load_profanity_wordlist,
    profanity_gate,
    word_count,
)


def _write_wordlist(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "profanity.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _essay(n: int) -> str:
    """A synthetic essay of exactly ``n`` words (space-joined tokens)."""
    return " ".join(["word"] * n)


# ------------------------------------------------------------------ word_count tokenizer


def test_word_count_basic():
    assert word_count("hello world") == 2


def test_word_count_empty_and_whitespace():
    assert word_count("") == 0
    assert word_count("   \n\t ") == 0


def test_word_count_keeps_apostrophes_and_hyphens():
    # "don't" and "well-known" are each one token per the §2 rule, not split on '/-.
    assert word_count("I don't like well-known clichés") == 5


def test_word_count_ignores_punctuation_as_separators():
    assert word_count("one, two; three. four!") == 4


# ------------------------------------------------------------------ length gate bands

CFG = EssayLengthConfig()  # PRD defaults: target 100-350, hard 60-500, penalty max 5


def test_in_target_band_ok_no_penalty():
    r = length_gate(_essay(200), CFG)
    assert r == LengthResult(wc=200, ok=True, hard_fail=False, length_penalty=0.0)


def test_target_edges_inclusive():
    assert length_gate(_essay(100), CFG).ok is True
    assert length_gate(_essay(350), CFG).ok is True
    assert length_gate(_essay(100), CFG).length_penalty == 0.0
    assert length_gate(_essay(350), CFG).length_penalty == 0.0


def test_below_target_but_above_hard_min_is_soft_only():
    r = length_gate(_essay(80), CFG)
    assert r.hard_fail is False
    assert r.ok is False
    assert 0.0 < r.length_penalty <= CFG.len_penalty_max


def test_above_target_but_below_hard_max_is_soft_only():
    r = length_gate(_essay(400), CFG)
    assert r.hard_fail is False
    assert r.ok is False
    assert 0.0 < r.length_penalty <= CFG.len_penalty_max


def test_penalty_grows_toward_hard_min():
    # Closer to hard_min => larger soft penalty.
    near_target = length_gate(_essay(95), CFG).length_penalty
    near_hard = length_gate(_essay(65), CFG).length_penalty
    assert near_hard > near_target


def test_penalty_capped_at_max_just_inside_hard_bounds():
    # wc == hard_min / hard_max are still survivors, at the maximum soft penalty.
    assert length_gate(_essay(CFG.hard_min), CFG).length_penalty == CFG.len_penalty_max
    assert length_gate(_essay(CFG.hard_max), CFG).length_penalty == CFG.len_penalty_max
    assert length_gate(_essay(CFG.hard_min), CFG).hard_fail is False
    assert length_gate(_essay(CFG.hard_max), CFG).hard_fail is False


def test_below_hard_min_hard_fails():
    r = length_gate(_essay(59), CFG)
    assert r.hard_fail is True
    assert r.ok is False


def test_above_hard_max_hard_fails():
    r = length_gate(_essay(501), CFG)
    assert r.hard_fail is True
    assert r.ok is False


def test_empty_essay_hard_fails():
    r = length_gate("", CFG)
    assert r.wc == 0
    assert r.hard_fail is True


def test_penalty_never_exceeds_max_below_hard_min():
    # Even past the hard bound the reported penalty is clamped (hard_fail carries the rejection).
    assert length_gate(_essay(10), CFG).length_penalty == CFG.len_penalty_max


# ------------------------------------------------------------------ profanity wordlist loader


def test_load_wordlist_parses_block_and_allow(tmp_path):
    path = _write_wordlist(
        tmp_path,
        [
            "# a comment",
            "",
            "FrobSlur",
            "another-term",
            "ALLOW: breast",
            "allow: rectal",  # case-insensitive prefix
            "   ",  # blank-ish, ignored
        ],
    )
    wl = load_profanity_wordlist(path)
    assert wl.block == ("frobslur", "another-term")  # lowercased, comments/blanks dropped
    assert wl.allow == ("breast", "rectal")


def test_load_wordlist_missing_file_is_empty(tmp_path):
    wl = load_profanity_wordlist(tmp_path / "does_not_exist.txt")
    assert wl.block == ()
    assert wl.allow == ()


# ------------------------------------------------------------------ profanity gate behaviour


def test_gate_clean_text_no_hit():
    matcher = build_profanity_matcher(Path("does_not_exist.txt"))  # == default list
    assert profanity_gate("I research breast cancer biology in my free time", matcher) is False


def test_gate_empty_or_whitespace_no_hit():
    matcher = Profanity()
    assert profanity_gate("", matcher) is False
    assert profanity_gate("   \n\t ", matcher) is False


def test_block_term_is_flagged(tmp_path):
    path = _write_wordlist(tmp_path, ["frobslur"])
    matcher = build_profanity_matcher(path)
    assert profanity_gate("you are a frobslur", matcher) is True
    assert profanity_gate("you are fine", matcher) is False


def test_block_term_matches_whole_token_only(tmp_path):
    path = _write_wordlist(tmp_path, ["frob"])
    matcher = build_profanity_matcher(path)
    # "frob" as a standalone token hits; embedded in a longer word it does not.
    assert profanity_gate("what a frob", matcher) is True
    assert profanity_gate("this is frobnication", matcher) is False


def test_block_term_leetspeak_normalized(tmp_path):
    path = _write_wordlist(tmp_path, ["frobslur"])
    matcher = build_profanity_matcher(path)
    assert profanity_gate("you fr0bslur", matcher) is True


def test_allow_term_exempts_default_clinical_word(tmp_path):
    # 'anal' is in better-profanity's default list but is also a clinical/anatomical prefix.
    assert Profanity().contains_profanity("anal") is True  # sanity: default flags it

    path = _write_wordlist(tmp_path, ["ALLOW: anal"])
    matcher = build_profanity_matcher(path)
    assert profanity_gate("anal fissure recovery affected my term", matcher) is False


# ------------------------------------------------------------------ gibberish heuristics

GIB = GibberishConfig()

# A genuine, varied paragraph (high entropy, high unique-word ratio, no long runs).
CLEAN_ESSAY = (
    "I want to join this program because building software lets me solve real problems "
    "for people in my community. Last summer I wrote a small app that helped my school "
    "track recycling, and seeing classmates actually use it made me eager to learn more "
    "about engineering, testing, and working on a team toward a shared goal."
)

# Awkward / ESL phrasing but composed entirely of real words — must NOT be flagged.
ESL_ESSAY = (
    "I am very much wanting the joining of this good program because the computer and the "
    "making of program is my big passion since long time. In my country I am study hard the "
    "mathematics and also the coding, and I hope very strongly to be improving my skill more "
    "and to be helping the peoples with the technology in the future days."
)


def test_clean_essay_not_gibberish():
    assert gibberish_gate(CLEAN_ESSAY, GIB).hit is False


def test_esl_essay_not_gibberish():
    result = gibberish_gate(ESL_ESSAY, GIB)
    assert result.hit is False
    assert result.signal_count < GIB.min_signals


def test_repeated_token_mash_is_gibberish():
    text = " ".join(["asdf"] * 25)  # low entropy + low unique-word ratio
    result = gibberish_gate(text, GIB)
    assert result.hit is True
    assert result.signal_count >= GIB.min_signals


def test_repeated_single_char_is_gibberish():
    result = gibberish_gate("a" * 40, GIB)  # zero entropy + long repeat run
    assert result.hit is True
    assert result.low_entropy is True
    assert result.repeat_run is True


def test_single_signal_does_not_fire():
    # A clean essay plus one absurd all-consonant token trips ONLY the consonant-run signal.
    text = CLEAN_ESSAY + " bcdfghjklmnpqrst"
    result = gibberish_gate(text, GIB)
    assert result.consonant_run is True
    assert result.signal_count == 1
    assert result.hit is False


def test_short_text_below_min_chars_never_flagged():
    result = gibberish_gate("asdf jkl", GIB)  # only 7 letters, below min_chars
    assert result.hit is False
    assert result.signal_count == 0


def test_long_consonant_run_token_detected():
    text = CLEAN_ESSAY + " qwrtznbvfg"
    assert gibberish_gate(text, GIB).consonant_run is True
