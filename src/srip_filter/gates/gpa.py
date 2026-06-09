"""Stages 2-3 — GPA normalization and gate (Phase 3).

Stage 2 converts a raw GPA cell to a 4.0-scale equivalent; Stage 3 turns that into a gate
verdict (PRD §6). The work is split so the LLM-touching parts stay isolated and the
deterministic majority is fully testable with zero API spend:

  * 3.1 deterministic normalizer  — :func:`normalize_gpa_deterministic`   (this commit)
  * 3.2 Task A fallback + orchestration                                   (next)
  * 3.3 points gradient + deterministic gate paths
  * 3.4 Task B low-GPA adequacy + Stage 2-3 aggregator

Hard line (PRD §1/§6.2): an unresolvable or blank scale is ``NEEDS_REVIEW``, *never*
``REJECTED`` — false-rejecting the large international contingent is the failure mode to avoid.
This deterministic pass therefore never decides a rejection; it either resolves a value, flags
it for LLM Task A (``needs_llm``), or — for a truly empty cell — flags it for manual review.

The §6.1 percentage→4.0 table and the clean-scale ceiling live in ``config.yaml``
(``gpa.normalization``); this module hard-codes no thresholds. Pure functions, no I/O, no LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..config import AppConfig, GpaConfig, GpaNormalizationConfig
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_a as task_a_prompt
from ..models import Confidence, GpaAssessment, GpaGate, GpaSource, TaskAOutput

# Internal Stage-3 verdict. Distinct from the final Outcome: "pass" means the GPA gate is
# cleared and scoring continues (essays still run) — it is not yet RANKED.
GpaGateVerdict = Literal["pass", "reject", "needs_review"]

# A fraction "a/b" (e.g. 85/100, 4.5/5, 3.8/4.0). Checked before a bare number so the
# denominator can pick the scale.
_FRACTION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
# An explicit percentage "92%", "95.2 %".
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# First signed/unsigned decimal anywhere in the string (handles trailing labels: "3.97 GPA").
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Denominators we recognize deterministically; anything else routes to Task A.
_DENOM_FOUR = 4.0
_DENOM_FIVE = 5.0
_DENOM_TEN = 10.0
_DENOM_HUNDRED = 100.0
_DENOM_TOL = 1e-9


@dataclass(frozen=True)
class GpaNormalization:
    """Stage-2 normalization outcome for one applicant (PRD §6.1 output shape + a routing flag).

    Exactly one of three dispositions holds:

    * **resolved** — ``normalized_gpa`` is set, ``needs_llm`` and ``requires_manual_review`` both
      False. ``below_threshold`` reflects ``normalized_gpa < threshold``.
    * **route to LLM** — ``needs_llm`` True, ``normalized_gpa`` None. Stage 3.2 will call Task A.
      No decision is made here.
    * **manual review** — ``requires_manual_review`` True (empty cell). Stage 3 sends it to
      ``NEEDS_REVIEW`` without spending a token.

    Stage 3.3 maps this onto the audit ``GpaAssessment`` block.
    """

    normalized_gpa: float | None
    original_scale: str
    conversion_method: str
    confidence: Confidence
    below_threshold: bool | None
    requires_manual_review: bool
    source: GpaSource
    needs_llm: bool


def _percentage_to_gpa(pct: float, ncfg: GpaNormalizationConfig) -> float:
    """Map a 0-100 percentage onto the 4.0 scale via the §6.1 table.

    A percentage at or above a band's ``min_pct`` takes that band's GPA; below the lowest band
    the value scales linearly toward 0, anchored on the lowest band's ``(min_pct, gpa)`` point.
    """
    bands = sorted(ncfg.percentage_table, key=lambda b: b.min_pct, reverse=True)
    for band in bands:
        if pct >= band.min_pct:
            return band.gpa
    lowest = bands[-1]
    return pct / lowest.min_pct * lowest.gpa if lowest.min_pct > 0 else 0.0


def _resolved(
    gpa_value: float, scale: str, method: str, cfg: GpaConfig
) -> GpaNormalization:
    """Build a resolved deterministic result, capped at ``gpa_max`` and rounded."""
    capped = round(min(gpa_value, cfg.normalization.gpa_max), 4)
    return GpaNormalization(
        normalized_gpa=capped,
        original_scale=scale,
        conversion_method=method,
        confidence="high",
        below_threshold=capped < cfg.threshold,
        requires_manual_review=False,
        source="deterministic",
        needs_llm=False,  # resolved: never routed
    )


def _route_to_llm(scale: str) -> GpaNormalization:
    """Flag a non-blank value the deterministic parser cannot confidently place for Task A."""
    return GpaNormalization(
        normalized_gpa=None,
        original_scale=scale,
        conversion_method="route_to_task_a",
        confidence="low",
        below_threshold=None,
        requires_manual_review=False,
        source="deterministic",
        needs_llm=True,
    )


def _manual_review(scale: str, method: str) -> GpaNormalization:
    """Flag an empty cell for manual review without spending an LLM token (PRD §6.1)."""
    return GpaNormalization(
        normalized_gpa=None,
        original_scale=scale,
        conversion_method=method,
        confidence="low",
        below_threshold=None,
        requires_manual_review=True,
        source="deterministic",
        needs_llm=False,
    )


def _from_fraction(num: float, denom: float, cfg: GpaConfig) -> GpaNormalization:
    """Resolve an ``a/b`` value using the denominator to pick the scale."""
    ncfg = cfg.normalization
    if abs(denom - _DENOM_HUNDRED) < _DENOM_TOL:
        if num > ncfg.percentage_max:
            return _route_to_llm("percentage")
        return _resolved(_percentage_to_gpa(num, ncfg), "percentage", "fraction_over_100", cfg)
    if abs(denom - _DENOM_TEN) < _DENOM_TOL:
        if num > _DENOM_TEN:
            return _route_to_llm("out_of_10")
        return _resolved(_percentage_to_gpa(num * 10, ncfg), "out_of_10", "out_of_10_table", cfg)
    if abs(denom - _DENOM_FIVE) < _DENOM_TOL:
        if num > _DENOM_FIVE:
            return _route_to_llm("out_of_5")
        return _resolved(num / _DENOM_FIVE * ncfg.gpa_max, "out_of_5", "out_of_5_linear", cfg)
    if abs(denom - _DENOM_FOUR) < _DENOM_TOL:
        if num > ncfg.gpa_max:  # weighted on a 4-scale -> Task A
            return _route_to_llm("weighted_gt_4")
        return _resolved(num, "four_point", "fraction_over_4", cfg)
    return _route_to_llm("unknown")


def normalize_gpa_deterministic(raw: str, cfg: GpaConfig) -> GpaNormalization:
    """Convert a raw GPA cell to the 4.0 scale deterministically where possible (PRD §6.1).

    Resolution order: explicit percentage (``%``) → explicit fraction (``a/b``, scale from the
    denominator) → bare number on a clean ``0..gpa_max`` scale. A bare value above ``gpa_max``
    (weighted, or a bare percentage/out-of-N with no denominator) and any string without a
    parseable number route to LLM Task A (``needs_llm=True``). A genuinely empty cell goes to
    manual review without a token. Pure function — no decision/rejection is made here.
    """
    text = raw.strip()
    if not text:
        return _manual_review("blank", "blank")

    percent = _PERCENT_RE.search(text)
    if percent:
        pct = float(percent.group(1))
        if pct > cfg.normalization.percentage_max:
            return _route_to_llm("percentage")
        gpa = _percentage_to_gpa(pct, cfg.normalization)
        return _resolved(gpa, "percentage", "percent_sign", cfg)

    fraction = _FRACTION_RE.search(text)
    if fraction:
        num, denom = float(fraction.group(1)), float(fraction.group(2))
        if denom <= 0:
            return _route_to_llm("unknown")
        return _from_fraction(num, denom, cfg)

    number = _FLOAT_RE.search(text)
    if not number:
        return _route_to_llm("unknown")  # text present but no number (IGCSE letters, "N/A", ...)
    value = float(number.group(0))
    if 0.0 <= value <= cfg.normalization.gpa_max:
        return _resolved(value, "four_point", "clean_4_scale", cfg)
    # Out of the clean 0..4.0 band: weighted (>4), a bare percentage/out-of-N, or negative.
    return _route_to_llm("weighted_gt_4" if value > cfg.normalization.gpa_max else "unknown")


# ================================================================================================
# 3.2 — Task A fallback + Stage 2 orchestration (LLM)
# ================================================================================================
# normalize_gpa runs the deterministic path first and calls Task A *only* for the values it
# flagged (needs_llm). Task A's estimate is capped at gpa_max; a value Task A cannot safely place
# (requires_manual_review, or a null estimate) becomes requires_manual_review=True -> NEEDS_REVIEW
# at the gate. An LLM parse failure routes to manual review too — never a rejection (PRD §8).


def _manual_review_from_llm(scale: str, method: str, confidence: Confidence) -> GpaNormalization:
    """A value Task A (or a parse failure) could not place -> manual review, source=llm."""
    return GpaNormalization(
        normalized_gpa=None,
        original_scale=scale,
        conversion_method=method,
        confidence=confidence,
        below_threshold=None,
        requires_manual_review=True,
        source="llm",
        needs_llm=False,
    )


def _from_task_a(out: TaskAOutput, cfg: GpaConfig) -> GpaNormalization:
    """Map a Task A output onto :class:`GpaNormalization`, capping the estimate at ``gpa_max``."""
    if out.requires_manual_review or out.normalized_gpa is None:
        return _manual_review_from_llm(out.original_scale, out.conversion_method, out.confidence)
    capped = round(min(out.normalized_gpa, cfg.normalization.gpa_max), 4)
    return GpaNormalization(
        normalized_gpa=capped,
        original_scale=out.original_scale,
        conversion_method=out.conversion_method,
        confidence=out.confidence,
        below_threshold=capped < cfg.threshold,
        requires_manual_review=False,
        source="llm",
        needs_llm=False,
    )


async def normalize_gpa(
    raw: str, client: BaseLLMClient, cfg: AppConfig
) -> GpaNormalization:
    """Stage 2: normalize a raw GPA, deterministic-first, with LLM Task A as the fallback.

    Resolves and returns immediately for any value the deterministic parser handled or sent to
    manual review. Only a ``needs_llm`` value reaches Task A; its estimate is capped at
    ``gpa_max`` and an unplaceable result (or an :class:`LLMParseFailure` after the client's
    retry) becomes ``requires_manual_review`` — i.e. ``NEEDS_REVIEW``, never a rejection. The raw
    string is used as ``cache_text`` so identical GPAs dedup within a run.
    """
    det = normalize_gpa_deterministic(raw, cfg.gpa)
    if not det.needs_llm:
        return det
    try:
        out = await client.complete(
            "task_a",
            system=task_a_prompt.SYSTEM,
            user=task_a_prompt.user_prompt(raw),
            schema=TaskAOutput,
            cache_text=raw,
        )
    except LLMParseFailure:
        # Keep the deterministic scale guess; mark unscoreable for a human (reason set at gate).
        return _manual_review_from_llm(det.original_scale, "llm_parse_failure", "low")
    return _from_task_a(out, cfg.gpa)


# ================================================================================================
# 3.3 — GPA points gradient + deterministic gate paths (Stage 3, PRD §8.1 / §6.2)
# ================================================================================================
# gpa_points is the pure §8.1 gradient (3.0 -> 0, 3.7 -> 28, 4.0 -> 40). gpa_gate_deterministic
# decides the branches that need no LLM: an unresolved/manual-review scale -> NEEDS_REVIEW (never
# REJECTED); >= threshold -> PASS + points; < threshold with a blank explanation -> REJECTED. The
# remaining branch (< threshold WITH an explanation) needs LLM Task B and is wired in Phase 3.4;
# this function returns None for it.


@dataclass(frozen=True)
class GpaGateResult:
    """Stage-3 GPA gate outcome for one applicant.

    ``verdict`` drives the pipeline ("pass" continues to essay scoring; "reject"/"needs_review"
    are terminal for this stage). ``assessment`` and ``gate`` drop straight into the audit record
    (``AuditRecord.gpa`` and ``AuditRecord.gates.gpa_gate``). ``gpa_points`` is 0 unless passed.
    """

    verdict: GpaGateVerdict
    gpa_points: float
    reason: str  # "" on pass; names the blocker on reject/needs_review
    assessment: GpaAssessment
    gate: GpaGate


def gpa_points(normalized_gpa: float, cfg: GpaConfig) -> float:
    """PRD §8.1 linear gradient over ``[threshold, gpa_max]`` → ``[0, score_max]``, clamped.

    3.0 → 0, 3.7 → 28, 4.0 → 40 with the defaults. Below the threshold clamps to 0; above
    ``gpa_max`` clamps to ``score_max`` (the normalizer already caps GPA at ``gpa_max``). Pure.
    """
    span = cfg.normalization.gpa_max - cfg.threshold
    if span <= 0:
        return 0.0
    frac = max(0.0, min(1.0, (normalized_gpa - cfg.threshold) / span))
    return round(frac * cfg.score_max, 4)


def build_assessment(
    raw: str, norm: GpaNormalization, explanation_eval: object | None = None
) -> GpaAssessment:
    """Project a :class:`GpaNormalization` onto the audit ``GpaAssessment`` block (PRD §9).

    ``explanation_eval`` is the Task B output, populated only when Task B ran (Phase 3.4).
    """
    return GpaAssessment(
        raw=raw or None,
        normalized_gpa=norm.normalized_gpa,
        original_scale=norm.original_scale,
        conversion_method=norm.conversion_method,
        confidence=norm.confidence,
        below_threshold=norm.below_threshold,
        requires_manual_review=norm.requires_manual_review,
        source=norm.source,
        explanation_eval=explanation_eval,  # type: ignore[arg-type]
    )


def gpa_gate_deterministic(
    raw: str, norm: GpaNormalization, explanation: str, cfg: GpaConfig
) -> GpaGateResult | None:
    """Decide the GPA gate branches that need no LLM; return ``None`` if Task B is required.

    * unresolved scale (null GPA or ``requires_manual_review``) → ``needs_review`` (PRD §6.2:
      never a rejection — protects the international contingent);
    * GPA ≥ ``threshold`` → ``pass`` with gradient points;
    * GPA < ``threshold`` with a blank explanation → ``reject``;
    * GPA < ``threshold`` with an explanation present → ``None`` (Phase 3.4 calls Task B).
    """
    if norm.normalized_gpa is None or norm.requires_manual_review:
        reason = "GPA scale could not be normalized"
        return GpaGateResult(
            verdict="needs_review",
            gpa_points=0.0,
            reason=reason,
            assessment=build_assessment(raw, norm),
            gate=GpaGate(passed=False, reason=reason),
        )

    g = norm.normalized_gpa
    if g >= cfg.threshold:
        points = gpa_points(g, cfg)
        return GpaGateResult(
            verdict="pass",
            gpa_points=points,
            reason="",
            assessment=build_assessment(raw, norm),
            gate=GpaGate(passed=True, reason=f"normalized {g} >= {cfg.threshold}"),
        )

    if not explanation.strip():
        reason = f"GPA below {cfg.threshold}, no explanation"
        return GpaGateResult(
            verdict="reject",
            gpa_points=0.0,
            reason=reason,
            assessment=build_assessment(raw, norm),
            gate=GpaGate(passed=False, reason=reason),
        )

    return None  # < threshold with an explanation -> LLM Task B (Phase 3.4)
