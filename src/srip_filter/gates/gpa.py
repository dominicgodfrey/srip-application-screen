"""Stages 2-3 — GPA normalization (raw cell → 4.0 scale) and the GPA gate (PRD §6).

An unresolvable or blank scale is ``NEEDS_REVIEW``, never ``REJECTED`` — false-rejecting the
large international contingent is the failure mode to avoid. Thresholds live in ``config.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..applicant import ApplicantRow
from ..config import AppConfig, GpaConfig, GpaNormalizationConfig
from ..llm.client import BaseLLMClient, LLMParseFailure
from ..llm.prompts import task_a as task_a_prompt
from ..llm.prompts import task_b as task_b_prompt
from ..models import (
    Confidence,
    GpaAssessment,
    GpaGate,
    GpaSource,
    TaskAOutput,
    TaskBOutput,
)

# Internal to this stage: "pass" clears the GPA gate and continues scoring — not yet RANKED.
GpaGateVerdict = Literal["pass", "reject", "needs_review"]

# "a/b" — checked before a bare number so the denominator can pick the scale.
_FRACTION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# First decimal anywhere, so trailing labels ("3.97 GPA") still parse.
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Denominators we recognize deterministically; anything else routes to Task A.
_DENOM_FOUR = 4.0
_DENOM_FIVE = 5.0
_DENOM_TEN = 10.0
_DENOM_HUNDRED = 100.0
_DENOM_TOL = 1e-9


@dataclass(frozen=True)
class GpaNormalization:
    """Stage-2 outcome: exactly one of resolved (``normalized_gpa`` set), ``needs_llm`` (Task A
    decides), or ``requires_manual_review`` (empty cell → NEEDS_REVIEW, no token spent)."""

    normalized_gpa: float | None
    original_scale: str
    conversion_method: str
    confidence: Confidence
    below_threshold: bool | None
    requires_manual_review: bool
    source: GpaSource
    needs_llm: bool


def _percentage_to_gpa(pct: float, ncfg: GpaNormalizationConfig) -> float:
    """Map a 0-100 percentage onto the 4.0 scale via the §6.1 table; below the lowest band it
    scales linearly toward 0."""
    bands = sorted(ncfg.percentage_table, key=lambda b: b.min_pct, reverse=True)
    for band in bands:
        if pct >= band.min_pct:
            return band.gpa
    lowest = bands[-1]
    return pct / lowest.min_pct * lowest.gpa if lowest.min_pct > 0 else 0.0


def _resolved(gpa_value: float, scale: str, method: str, cfg: GpaConfig) -> GpaNormalization:
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
        needs_llm=False,
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
    """Convert a raw GPA cell to the 4.0 scale where possible (PRD §6.1).

    Order: percentage → fraction (scale from the denominator) → bare number on a clean
    ``0..gpa_max`` scale. Anything else routes to Task A; an empty cell goes to manual review.
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
        return _route_to_llm("unknown")  # text but no number (IGCSE letters, "N/A", ...)
    value = float(number.group(0))
    if 0.0 <= value <= cfg.normalization.gpa_max:
        return _resolved(value, "four_point", "clean_4_scale", cfg)
    # Outside the clean band: weighted (>4), a bare percentage/out-of-N, or negative.
    return _route_to_llm("weighted_gt_4" if value > cfg.normalization.gpa_max else "unknown")


# --- Task A fallback + Stage 2 orchestration (LLM) ---


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
    raw: str, client: BaseLLMClient, cfg: AppConfig, *, force_task_a: bool = False
) -> GpaNormalization:
    """Stage 2: normalize a raw GPA, deterministic-first, with Task A as the fallback.

    An unplaceable result or a parse failure becomes ``requires_manual_review``, never a
    rejection. ``force_task_a`` keeps a weighted-only submission off the deterministic fraction
    path — "4.4 / 5.0" *weighted* is not an unweighted /5 scale, so linear conversion would be
    wrong. A blank value still short-circuits to manual review.
    """
    det = normalize_gpa_deterministic(raw, cfg.gpa)
    if not det.needs_llm and not (force_task_a and det.normalized_gpa is not None):
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
        # Keep the deterministic scale guess; mark unscoreable (reason set at the gate).
        return _manual_review_from_llm(det.original_scale, "llm_parse_failure", "low")
    return _from_task_a(out, cfg.gpa)


# --- Points gradient + deterministic gate paths (Stage 3, PRD §8.1 / §6.2) ---


@dataclass(frozen=True)
class GpaGateResult:
    """Stage-3 gate outcome. ``assessment`` and ``gate`` drop straight into the audit record;
    ``gpa_points`` is 0 unless the verdict is "pass"."""

    verdict: GpaGateVerdict
    gpa_points: float
    reason: str  # "" on pass; names the blocker on reject/needs_review
    assessment: GpaAssessment
    gate: GpaGate


def gpa_points(normalized_gpa: float, cfg: GpaConfig) -> float:
    """PRD §8.1 linear gradient over ``[threshold, gpa_max]`` → ``[0, score_max]``, clamped
    at both ends — with the defaults, 3.3 → 0, 3.65 → 20, 4.0 → 40."""
    span = cfg.normalization.gpa_max - cfg.threshold
    if span <= 0:
        return 0.0
    frac = max(0.0, min(1.0, (normalized_gpa - cfg.threshold) / span))
    return round(frac * cfg.score_max, 4)


def build_assessment(
    raw: str,
    norm: GpaNormalization,
    explanation_eval: TaskBOutput | None = None,
    explanation: str = "",
) -> GpaAssessment:
    """Project a :class:`GpaNormalization` onto the audit ``GpaAssessment`` block (PRD §9).

    ``explanation`` is carried verbatim so the audit UI can show the text that rescued (or
    failed to rescue) a sub-threshold GPA; ``explanation_eval`` is set only when Task B ran.
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
        explanation_text=explanation.strip(),
        explanation_eval=explanation_eval,
    )


def gpa_gate_deterministic(
    raw: str, norm: GpaNormalization, explanation: str, cfg: GpaConfig
) -> GpaGateResult | None:
    """Decide the GPA gate branches that need no LLM; return ``None`` when Task B must judge
    (sub-threshold GPA with an explanation present)."""
    if norm.normalized_gpa is None or norm.requires_manual_review:
        # An empty GPA cell with no explanation is an affirmative non-answer, not an
        # unresolvable scale — reject it. Anything else unresolved stays human-reviewed.
        if not raw.strip() and not explanation.strip():
            reason = "No GPA provided and no explanation given"
            return GpaGateResult(
                verdict="reject",
                gpa_points=0.0,
                reason=reason,
                assessment=build_assessment(raw, norm, explanation=explanation),
                gate=GpaGate(passed=False, reason=reason),
            )
        reason = "GPA scale could not be normalized"
        return GpaGateResult(
            verdict="needs_review",
            gpa_points=0.0,
            reason=reason,
            assessment=build_assessment(raw, norm, explanation=explanation),
            gate=GpaGate(passed=False, reason=reason),
        )

    g = norm.normalized_gpa
    if g < cfg.hard_floor:
        reason = (
            f"GPA {g} below the hard floor of {cfg.hard_floor} — "
            "not eligible regardless of explanation"
        )
        return GpaGateResult(
            verdict="reject",
            gpa_points=0.0,
            reason=reason,
            assessment=build_assessment(raw, norm, explanation=explanation),
            gate=GpaGate(passed=False, reason=reason),
        )
    if g >= cfg.threshold:
        points = gpa_points(g, cfg)
        return GpaGateResult(
            verdict="pass",
            gpa_points=points,
            reason="",
            assessment=build_assessment(raw, norm, explanation=explanation),
            gate=GpaGate(passed=True, reason=f"normalized {g} >= {cfg.threshold}"),
        )

    if not explanation.strip():
        reason = f"GPA below {cfg.threshold}, no explanation"
        return GpaGateResult(
            verdict="reject",
            gpa_points=0.0,
            reason=reason,
            assessment=build_assessment(raw, norm, explanation=explanation),
            gate=GpaGate(passed=False, reason=reason),
        )

    return None  # < threshold with an explanation -> Task B


# --- Task B low-GPA adequacy + Stage 2-3 aggregator (LLM) ---


def _with_detail(gate_reason: str, rationale: str) -> str:
    """``gate_reason``, plus the model's rationale when it wrote one. Never model-only."""
    detail = rationale.strip()
    return f"{gate_reason} — {detail}" if detail else gate_reason


def _task_b_result(
    out: TaskBOutput,
    raw: str,
    norm: GpaNormalization,
    normalized_gpa: float,
    explanation: str,
    cfg: GpaConfig,
) -> GpaGateResult:
    """Turn a Task B verdict into a gate result; store the eval in the assessment either way.

    The reason is composed deterministically and only *then* extended with the model's prose:
    as the whole reason, an empty rationale left a rejection that could not name its gate.
    """
    assessment = build_assessment(raw, norm, out, explanation=explanation)
    if out.recommended_outcome == "rank":
        # Below threshold -> points clamp to 0: deficit reflected, never erased (§8.1).
        return GpaGateResult(
            verdict="pass",
            gpa_points=gpa_points(normalized_gpa, cfg),
            reason="",
            assessment=assessment,
            gate=GpaGate(passed=True, reason=_with_detail(
                f"GPA below {cfg.threshold}; explanation accepted", out.rationale
            )),
        )
    reason = _with_detail(
        f"GPA below {cfg.threshold}; explanation judged inadequate", out.rationale
    )
    return GpaGateResult(
        verdict="reject",
        gpa_points=0.0,
        reason=reason,
        assessment=assessment,
        gate=GpaGate(passed=False, reason=reason),
    )


async def assess_gpa(
    row: ApplicantRow,
    client: BaseLLMClient,
    cfg: AppConfig,
    *,
    force_task_a: bool = False,
) -> GpaGateResult:
    """Stages 2-3 end to end: normalize the GPA, then gate it (PRD §6).

    Only one branch reaches Task B — a sub-threshold GPA with an explanation. A parse failure
    routes to ``needs_review``; an unscoreable applicant is never rejected.
    """
    norm = await normalize_gpa(row.gpa, client, cfg, force_task_a=force_task_a)
    det = gpa_gate_deterministic(row.gpa, norm, row.gpa_explanation, cfg.gpa)
    if det is not None:
        return det

    normalized_gpa = norm.normalized_gpa
    assert normalized_gpa is not None  # guaranteed by gpa_gate_deterministic returning None
    gap = round(cfg.gpa.threshold - normalized_gpa, 4)
    try:
        out = await client.complete(
            "task_b",
            system=task_b_prompt.SYSTEM,
            user=task_b_prompt.user_prompt(normalized_gpa, gap, row.gpa_explanation),
            schema=TaskBOutput,
        )
    except LLMParseFailure:
        reason = "LLM_PARSE_FAILURE"
        return GpaGateResult(
            verdict="needs_review",
            gpa_points=0.0,
            reason=reason,
            assessment=build_assessment(row.gpa, norm, explanation=row.gpa_explanation),
            gate=GpaGate(passed=False, reason=reason),
        )
    return _task_b_result(out, row.gpa, norm, normalized_gpa, row.gpa_explanation, cfg.gpa)
