"""Task D — essay grading: gibberish backstop + relevance gate + quality (PRD §4, §8.3).

Runs once per essay on Stage 1-3 survivors. Two of its outputs are *gates* that disqualify the
whole application — ``is_gibberish`` (the LLM backstop to the cheap Stage 1 heuristics) and
``on_topic`` (the relevance gate) — while the remaining fields feed the additive essay score.
Length/profanity/the cheap gibberish heuristics already passed deterministically (Stage 1);
this task adds the judgment those cannot make. Output parses into
:class:`srip_filter.models.TaskDOutput`.

``prompt_text`` is the *resolved CSV essay-question header* — the exact prompt the applicant
answered — supplied by the orchestrator from ``HeaderResolution.role_to_header``. It is never a
frozen copy in config, so it cannot drift from the form per cycle.
"""

from __future__ import annotations

SYSTEM = (
    "You grade a single application essay for a selective high-school / undergraduate "
    "software-engineering program. Many applicants are non-native English speakers.\n\n"
    "Do three things, in this order:\n"
    "1. GIBBERISH: decide if the essay is keyboard-mashing or a good-faith failure (random "
    "characters, copy-paste noise, content unrelated to writing an essay). Set is_gibberish "
    "true ONLY for genuine non-writing. Awkward, simple, or ESL-accented prose is NOT "
    "gibberish.\n"
    "2. RELEVANCE: decide if the essay actually responds to the given PROMPT. An off-topic "
    "essay (answers a different question, or is generic boilerplate that ignores the prompt) is "
    "disqualifying — set on_topic false.\n"
    "3. QUALITY: if it is on-topic and genuine, score 0-20 on clarity, specificity, coherence, "
    "and overall saliency (does it make a compelling, concrete case?). Reward concrete detail "
    "and genuine motivation over generic filler.\n\n"
    "Grammar/spelling: apply only a SLIGHT penalty (0-3) for genuine errors. Never penalize "
    "ESL phrasing, accent-of-writing, or simple vocabulary — penalize real mistakes only.\n"
    "Be fair: a short, plain, honest essay that answers the prompt is on-topic and scoreable, "
    "not a rejection.\n"
    "Return ONLY JSON matching the required schema. No markdown, no preamble."
)

TARGET_RANGE = "100-350"


def user_prompt(prompt_text: str, word_count: int, essay_text: str) -> str:
    """Build the Task D user message for one essay (PRD §8.3 template).

    ``prompt_text`` is the resolved CSV essay-question header (the prompt the applicant
    answered); ``word_count`` is the Stage 1 tokenizer count; ``essay_text`` is the raw essay.
    """
    return (
        f'PROMPT: """{prompt_text}"""\n'
        f"WORD_COUNT: {word_count}\n"
        f"TARGET_RANGE: {TARGET_RANGE}\n"
        f'ESSAY: """{essay_text}"""'
    )
