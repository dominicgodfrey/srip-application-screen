"""Stage 7 — school bonus (Phase 6.1/6.2).

A **bonus-only** stage (PRD §0.3/§7): it adds to ``final_score``, never subtracts, and can never
change a ``REJECTED``/``NEEDS_REVIEW`` outcome. Fully **deterministic** — no LLM — so there is no
isolate-the-LLM split; Phase 6 splits along the two stages instead (the resume stub lives in
``resume.py``):

  * 6.1 resource load + normalize + fuzzy match — :func:`match_school`
  * 6.2 Stage 7 aggregator (list → bonus)       — :func:`score_school`

"High School" (364/466 applicants), blanks, and any below-threshold match resolve to an empty
:class:`SchoolMatch` and a 0 bonus — **never negative** (the §0.3 "absence is neutral" invariant).

Thresholds and point values come from ``AppConfig.school``; the school lists come from
``resources/schools.json`` (committed, non-PII). No magic numbers here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from ..config import AppConfig, SchoolConfig
from ..ingest import ApplicantRow
from ..models import SchoolListName, SchoolMatch

_RESOURCES_DIR = Path(__file__).resolve().parents[3] / "resources"
_SCHOOLS_PATH = _RESOURCES_DIR / "schools.json"

# The two ranked lists in schools.json, in canonical order.
_LIST_NAMES: tuple[SchoolListName, ...] = ("us_top20", "intl_top50")

# ================================================================================================
# 6.1 — Resource load + normalize + fuzzy match (PRD §7.1 / §13)
# ================================================================================================


@dataclass(frozen=True)
class _Candidate:
    """One match target: a school's canonical name or one alias, tagged with its list."""

    text: str  # normalized name/alias used for fuzzy scoring
    canonical_name: str  # the school's canonical ``name`` (identity across lists)
    list_name: SchoolListName


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace (PRD §7.1 normalization)."""
    lowered = text.lower()
    no_punct = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", no_punct).strip()


@lru_cache(maxsize=1)
def _load_candidates() -> tuple[_Candidate, ...]:
    """Load ``schools.json`` once and flatten it into normalized match candidates.

    Each school contributes its canonical ``name`` plus every alias as a separate candidate
    (so ``MIT``/``UCLA`` match), all tagged with the school's canonical name + list. Cached for
    the process; mirrors the profanity-matcher pattern (Phase 2.2).
    """
    with _SCHOOLS_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    candidates: list[_Candidate] = []
    for list_name in _LIST_NAMES:
        for school in data.get(list_name, []):
            canonical = school["name"]
            for raw in [canonical, *school.get("aliases", [])]:
                norm = _normalize(raw)
                if norm:
                    candidates.append(
                        _Candidate(text=norm, canonical_name=canonical, list_name=list_name)
                    )
    return tuple(candidates)


def _bonus_for_list(list_name: SchoolListName, cfg: SchoolConfig) -> float:
    """Resolve the configured bonus for a list (used for the both-lists tiebreak + scoring)."""
    return {
        "us_top20": cfg.bonus_us_top20,
        "intl_top50": cfg.bonus_intl_top50,
    }[list_name]


def match_school(institution: str, cfg: SchoolConfig) -> SchoolMatch:
    """Fuzzy-match an institution string against the curated lists (PRD §7.1).

    Normalizes the input, scores it against every school name/alias with ``rapidfuzz``
    token-set ratio, and keeps the best canonical school scoring at or above
    ``fuzzy_match_threshold``. A school present in **both** lists is reported under the list with
    the higher configured bonus, so :attr:`SchoolMatch.list` is authoritative and the bonus layer
    (6.2) is a pure lookup. Blank / "High School" / any below-threshold input → empty
    :class:`SchoolMatch` (``matched_name=None, list=None, fuzzy_score=0``).
    """
    query = _normalize(institution)
    if not query:
        return SchoolMatch()

    # Best score seen per canonical school across all of its name/alias candidates.
    best_score: dict[str, float] = {}
    for cand in _load_candidates():
        score = fuzz.token_set_ratio(query, cand.text)
        if score > best_score.get(cand.canonical_name, -1.0):
            best_score[cand.canonical_name] = score

    if not best_score:
        return SchoolMatch()

    # Pick the highest-scoring school; deterministic tiebreak on canonical name for equal scores.
    canonical, score = max(best_score.items(), key=lambda kv: (kv[1], kv[0]))
    if score < cfg.fuzzy_match_threshold:
        return SchoolMatch()

    # Both-lists tiebreak: report under whichever list that school sits in with the higher bonus.
    lists = {c.list_name for c in _load_candidates() if c.canonical_name == canonical}
    chosen = max(lists, key=lambda ln: _bonus_for_list(ln, cfg))
    return SchoolMatch(matched_name=canonical, list=chosen, fuzzy_score=float(score))


# ================================================================================================
# 6.2 — School bonus + Stage 7 aggregator (PRD §7.1)
# ================================================================================================


@dataclass(frozen=True)
class Stage7Result:
    """Reduced outcome of Stage 7 for one application.

    ``bonus`` drops into ``Scores.school_bonus`` and ``match`` into ``AuditRecord.school_match``.
    ``bonus`` is always ≥ 0 — an unmatched/"High School"/blank institution contributes 0.
    """

    bonus: float
    match: SchoolMatch


def score_school(row: ApplicantRow, cfg: AppConfig) -> Stage7Result:
    """Stage 7 end to end: match the institution and map the matched list to its bonus.

    ``us_top20`` → ``bonus_us_top20``; ``intl_top50`` → ``bonus_intl_top50``; no match → 0.
    Bonus-only and never negative — this can never manufacture or rescue a ``REJECTED`` outcome
    (rejections are gated before scoring; §12 invariant).
    """
    match = match_school(row.institution, cfg.school)
    bonus = 0.0 if match.list is None else _bonus_for_list(match.list, cfg.school)
    return Stage7Result(bonus=bonus, match=match)
