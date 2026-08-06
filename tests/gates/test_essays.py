"""Tests for Stage 1 essay gates. Synthetic text only — no applicant content.

Covers the word-count tokenizer (audit data — no length rule rejects anyone), the profanity
wordlist loader and gate, the gibberish heuristics, and the ``run_essay_gates_v3``
aggregator that reduces them to one verdict.
"""

from __future__ import annotations

from pathlib import Path

from better_profanity import Profanity

from srip_filter.applicant import ApplicantRow
from srip_filter.config import AppConfig, GibberishConfig
from srip_filter.gates.essays import (
    Stage1Result,
    build_profanity_matcher,
    gibberish_gate,
    load_profanity_wordlist,
    profanity_gate,
    profanity_terms,
    run_essay_gates_v3,
    word_count,
)
from srip_filter.ingest_webhook import WebhookApplicant


def _write_wordlist(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "profanity.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# Distinct, letter-varied real words, so a synthesized essay of any length reads as
# non-gibberish: high entropy, high unique-word ratio.
_WORD_POOL = (
    "the quick brown fox jumps over a lazy dog while bright morning sunlight covers green "
    "valleys near an old river where many curious children gladly play music during warm "
    "summer breaks and slowly learn about modern science history human language through "
    "thoughtful questions asked every single ordinary day before quiet evening stars appear "
    "above silent mountains beyond distant golden fields toward hopeful future work"
).split()


def _varied_essay(n: int) -> str:
    """A clean, varied essay of exactly ``n`` words (cycles the pool; high unique ratio)."""
    repeats = (n // len(_WORD_POOL)) + 1
    return " ".join((_WORD_POOL * repeats)[:n])


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


# ------------------------------------------------------------------ Stage 1 aggregator

APP_CFG = AppConfig()


def _app(essay1: str, essay2: str, essay3: str = "") -> WebhookApplicant:
    """Minimal WebhookApplicant carrying just the essays the aggregator reads."""
    row = ApplicantRow(
        submission_id="id1",
        first_name="Ann",
        last_name="Lee",
        email="a@b.com",
        essay1=essay1,
        essay2=essay2,
        essay3=essay3,
    )
    return WebhookApplicant(row=row)


def test_clean_application_passes_all_gates():
    result = run_essay_gates_v3(_app(_varied_essay(200), _varied_essay(180)), APP_CFG)
    assert isinstance(result, Stage1Result)
    assert result.rejected is False and result.needs_review is False
    assert result.primary_reason == ""
    assert result.profanity.hit is False
    assert result.gibberish.hit is False
    assert result.length_gate.e1_wc == 200
    assert result.length_gate.e2_wc == 180


def test_word_counts_are_recorded_but_never_decide_anything():
    """No length rule rejects: the site validates bounds at submit and sends none."""
    for e1, e2 in ((_varied_essay(3), _varied_essay(200)),
                   (_varied_essay(200), _varied_essay(5000)),
                   ("", "")):
        result = run_essay_gates_v3(_app(e1, e2), APP_CFG)
        assert result.rejected is False, (len(e1.split()), len(e2.split()))
        assert result.length_gate.hard_fail is False
        assert "length" not in result.primary_reason.lower()


def test_profanity_in_either_required_essay_needs_review_not_rejection():
    matcher = build_profanity_matcher(Path("no_such_file.txt"))  # default list
    matcher.add_censor_words(["frobslur"])
    result = run_essay_gates_v3(
        _app(_varied_essay(200), _varied_essay(180) + " frobslur"), APP_CFG, matcher=matcher
    )
    assert result.rejected is False          # a word list cannot judge context
    assert result.needs_review is True
    assert result.profanity.hit is True
    assert "profanity" in result.primary_reason.lower()


def test_profanity_in_the_optional_essay_also_flags_the_application():
    matcher = build_profanity_matcher(Path("no_such_file.txt"))
    matcher.add_censor_words(["frobslur"])
    result = run_essay_gates_v3(
        _app(_varied_essay(200), _varied_essay(180), _varied_essay(120) + " frobslur"),
        APP_CFG,
        matcher=matcher,
    )
    assert result.needs_review is True
    assert any(term.startswith("e3:") for term in result.profanity.terms)


def test_gibberish_in_a_required_essay_rejects():
    # 70 "asdf" tokens: trips entropy + unique-ratio together.
    result = run_essay_gates_v3(_app(" ".join(["asdf"] * 70), _varied_essay(180)), APP_CFG)
    assert result.rejected is True
    assert result.gibberish.hit is True
    assert "gibberish" in result.primary_reason.lower()


def test_gibberish_in_the_optional_essay_is_not_a_stage1_finding():
    """Essay 3 is bonus-only — Task F zeroes its bonus; Stage 1 must not touch the outcome."""
    result = run_essay_gates_v3(
        _app(_varied_essay(200), _varied_essay(180), " ".join(["asdf"] * 70)), APP_CFG
    )
    assert result.rejected is False and result.needs_review is False
    assert result.gibberish.hit is False


def test_where_both_fire_the_rejection_is_the_named_verdict():
    """A definite reject outranks a review, and primary_reason must name the decider."""
    matcher = build_profanity_matcher(Path("no_such_file.txt"))
    matcher.add_censor_words(["frobslur"])
    result = run_essay_gates_v3(
        _app(" ".join(["asdf"] * 70), _varied_essay(180) + " frobslur"),
        APP_CFG,
        matcher=matcher,
    )
    assert result.rejected is True
    assert result.profanity.hit is True       # still recorded for the audit trail
    assert "gibberish" in result.primary_reason.lower()


def test_a_flagged_application_never_carries_a_silent_reason():
    for result in (
        run_essay_gates_v3(_app(" ".join(["asdf"] * 70), _varied_essay(180)), APP_CFG),
    ):
        assert result.rejected or result.needs_review
        assert result.primary_reason != ""


# ------------------------------------------------------------ curated allowlist (real resources/)


def test_default_matcher_allows_clinical_terms_from_resources_file():
    # Regression for real false positives in the reference dataset: the default list flags
    # these clinical words, and resources/profanity.txt allowlists them.
    matcher = build_profanity_matcher()  # the real committed wordlist
    for phrase in (
        "apps that support stroke awareness campaigns",
        "I volunteered for an organ donation drive",
        "my project uses facial recognition",
        "I gave an oral presentation about algorithms",
        "the thrust of my argument is simple",
        "sex-based differences in clinical medicine",
    ):
        assert profanity_gate(phrase, matcher) is False, phrase


def test_default_matcher_still_flags_real_profanity():
    matcher = build_profanity_matcher()
    assert profanity_gate("this program is fucking great", matcher) is True


# ------------------------------------------------------------ profanity_terms (audit highlight)


def test_profanity_terms_names_the_offending_tokens(tmp_path):
    path = _write_wordlist(tmp_path, ["frobslur", "blortcuss"])
    matcher = build_profanity_matcher(path)
    terms = profanity_terms("you frobslur and Blortcuss and frobslur again", matcher)
    assert terms == ("frobslur", "blortcuss")  # deduped, lowercased, first-appearance order


def test_profanity_terms_empty_on_clean_text(tmp_path):
    matcher = build_profanity_matcher(_write_wordlist(tmp_path, ["frobslur"]))
    assert profanity_terms("a perfectly clean sentence", matcher) == ()


def test_stage1_records_profanity_terms_and_gibberish_signals():
    matcher = build_profanity_matcher(Path("no_such_file.txt"))
    matcher.add_censor_words(["frobslur"])
    result = run_essay_gates_v3(
        _app(_varied_essay(200) + " frobslur", " ".join(["asdf"] * 70)), APP_CFG, matcher=matcher
    )
    assert result.rejected is True
    assert result.profanity.terms == ["e1:frobslur"]
    assert result.gibberish.hit is True
    assert all(t.startswith("e2:") for t in result.gibberish.terms)
    assert result.gibberish.terms  # the fired signals are named for the audit trail
