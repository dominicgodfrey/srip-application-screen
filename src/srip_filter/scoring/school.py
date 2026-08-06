"""Stage 7 — school bonus (PRD §0.3/§7). Bonus-only and fully deterministic, no LLM.

"High School", blanks, and any below-threshold match resolve to an empty :class:`SchoolMatch`
and a 0 bonus — never negative. Thresholds and point values come from ``AppConfig.school``; the
lists come from ``resources/schools.json`` (committed, non-PII).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz

from ..applicant import ApplicantRow
from ..config import AppConfig, SchoolConfig, project_root
from ..models import SchoolListName, SchoolMatch

_RESOURCES_DIR = project_root() / "resources"
_SCHOOLS_PATH = _RESOURCES_DIR / "schools.json"

# The two ranked lists in schools.json, in canonical order.
_LIST_NAMES: tuple[SchoolListName, ...] = ("us_top20", "intl_top50")

# --- Resource load + normalize + fuzzy match (PRD §7.1 / §13) ---


@dataclass(frozen=True)
class _Candidate:
    """One match target: a school's canonical name or one alias, tagged with its list."""

    text: str  # normalized, used for fuzzy scoring
    canonical_name: str  # identity across lists
    list_name: SchoolListName


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace (PRD §7.1 normalization)."""
    lowered = text.lower()
    no_punct = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", no_punct).strip()


@lru_cache(maxsize=1)
def _load_candidates() -> tuple[_Candidate, ...]:
    """Load ``schools.json`` once and flatten it into normalized match candidates — one per
    canonical name and per alias, so ``MIT``/``UCLA`` match too."""
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
    """Resolve the configured bonus for a list."""
    return {
        "us_top20": cfg.bonus_us_top20,
        "intl_top50": cfg.bonus_intl_top50,
    }[list_name]


def match_school(institution: str, cfg: SchoolConfig) -> SchoolMatch:
    """Fuzzy-match an institution against the curated lists (PRD §7.1); anything scoring below
    ``fuzzy_match_threshold`` returns an empty :class:`SchoolMatch`.

    A school in *both* lists is reported under the higher-bonus one, which keeps
    :attr:`SchoolMatch.list` authoritative and leaves the bonus layer a pure lookup.
    """
    query = _normalize(institution)
    if not query:
        return SchoolMatch()

    # Best score per canonical school across all of its name/alias candidates.
    best_score: dict[str, float] = {}
    for cand in _load_candidates():
        score = fuzz.token_set_ratio(query, cand.text)
        if score > best_score.get(cand.canonical_name, -1.0):
            best_score[cand.canonical_name] = score

    if not best_score:
        return SchoolMatch()

    # Highest score wins; canonical name breaks ties deterministically.
    canonical, score = max(best_score.items(), key=lambda kv: (kv[1], kv[0]))
    if score < cfg.fuzzy_match_threshold:
        return SchoolMatch()

    lists = {c.list_name for c in _load_candidates() if c.canonical_name == canonical}
    chosen = max(lists, key=lambda ln: _bonus_for_list(ln, cfg))
    return SchoolMatch(matched_name=canonical, list=chosen, fuzzy_score=float(score))


# --- School bonus + Stage 7 aggregator (PRD §7.1) ---


@dataclass(frozen=True)
class Stage7Result:
    """Reduced Stage-7 outcome; ``bonus`` is always ≥ 0, and 0 for an unmatched institution."""

    bonus: float
    match: SchoolMatch


def score_school(row: ApplicantRow, cfg: AppConfig) -> Stage7Result:
    """Stage 7 end to end: match the institution and map the matched list to its bonus."""
    match = match_school(row.institution, cfg.school)
    bonus = 0.0 if match.list is None else _bonus_for_list(match.list, cfg.school)
    return Stage7Result(bonus=bonus, match=match)
