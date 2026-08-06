"""Task D — essay grading: gibberish backstop, relevance gate, quality (PRD §4, §8.3).

Runs once per essay on Stage 1-3 survivors, adding the judgment the deterministic gates cannot
make. Two of its outputs are gates that disqualify the whole application; the rest feed the
additive essay score.

``prompt_text`` is the question exactly as delivered in the payload — never a frozen copy in
config, so it cannot drift from the live form.
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
    "3. QUALITY: if it is on-topic and genuine, score 0-15 on clarity, specificity, coherence, "
    "and overall saliency (does it make a compelling, concrete case?). Reward concrete detail "
    "and genuine motivation over generic filler.\n\n"
    "Grammar/spelling: apply only a SLIGHT penalty (0-3) for genuine errors. Never penalize "
    "ESL phrasing, accent-of-writing, or simple vocabulary — penalize real mistakes only.\n"
    "Be fair: a short, plain, honest essay that answers the prompt is on-topic and scoreable, "
    "not a rejection.\n"
    "Return ONLY JSON matching the required schema. No markdown, no preamble."
)

TARGET_RANGE = "100-350"


def user_prompt(
    prompt_text: str, word_count: int, essay_text: str, target_range: str = TARGET_RANGE
) -> str:
    """Build the Task D user message for one essay (PRD §8.3 template).

    ``prompt_text`` is the essay question exactly as the applicant saw it; ``word_count``
    is the Stage 1 tokenizer count; ``essay_text`` is the raw essay. v3 passes the
    per-essay ``target_range`` from the webhook payload's min/max metadata; the default
    keeps the v2 form's fixed band for the replay/calibration path.
    """
    return (
        f'PROMPT: """{prompt_text}"""\n'
        f"WORD_COUNT: {word_count}\n"
        f"TARGET_RANGE: {target_range}\n"
        f'ESSAY: """{essay_text}"""'
    )
