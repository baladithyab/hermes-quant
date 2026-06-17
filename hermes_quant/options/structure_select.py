"""hermes_quant.options.structure_select — deterministic stance x IV-regime
structure-selection table (ADR-0082 Part B).

THE RAIL, in one sentence: the DELIBERATION proposes a *coarse* ``structure_intent``
(``ResearchPlan.structure_intent``, ADR-0082 §"Part B"); THIS table + the
``options_gate`` decide the concrete options ``StrategyKind`` — **no LLM ever picks
legs**. This module is a pure, deterministic LOOKUP TABLE (no model, no optimization,
no network, no I/O). It maps

    (direction, structure_intent, IV-regime/IV-rank) -> StrategyKind | None

where ``StrategyKind`` is *exactly* the producer's buildable set
(``options/recipes.py`` ``StrategyKind`` ≡ ``tools._MULTI_LEG_STRATEGIES`` =
``{covered_call, cash_secured_put, wheel, bull_put_spread}``) and ``None`` means
**abstain** (silence-by-default → today's equity path).

What this module is NOT (the rails, ADR-0082 §"Rails preserved"):

  * NOT a sizer — the gate sizes (contract count). This table never emits a size.
  * NOT an authority over the gate — a structure this table selects is STILL run
    through ``options_gate`` by the producer; an inadmissible structure is rejected
    there. The table only narrows the *candidate* ``StrategyKind``; the gate decides
    what (if anything) trades.
  * NOT naked-capable — it only ever emits gate-admissible, collateral-secured /
    defined-risk-income buckets (CC / CSP / wheel / bull_put_spread). Anything
    outside that set (condors, calendars, straddles, naked shorts) is **not
    producible** by the existing producers and therefore resolves to ``None``
    (abstain), never to a structure the producer cannot honestly build.
  * NOT default-on — the selection seam is behind ``HERMES_QUANT_STRUCTURE_SELECT=1``.
    When the flag is OFF (or ``structure_intent`` is absent / ``NONE``, or there is no
    table match, or the intent is a non-producible one), ``select_structure`` returns
    ``None`` and the existing equity / options path is BYTE-IDENTICAL.

IV-REGIME / no-lookahead honesty (ADR-0082 §"IV-rank lookahead"): IV-rank MUST be
computed as-of the decision instant (no future IV in the 52-week window). This module
does not compute IV-rank — it only CLASSIFIES a caller-supplied, as-of-honest IV-rank
into a coarse regime. A caller that feeds a peeked / post-decision IV-rank is a silent
leakage bug at the CALLER, not here; this module is pure given its inputs.

Thresholds (ADR-0082 §"Eval honesty"): the IV-rank cut points are STARTING POINTS to
be eval-gated on hermes' own labeled data, not vendor-backtested ground truth. They are
codified here as a single source of truth so the eval can move them in one place.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hermes_quant.agents.research_debate.schemas import (
        PortfolioRating,
        StructureIntent,
    )
    from hermes_quant.options.recipes import StrategyKind

__all__ = [
    "Direction",
    "IVRegime",
    "STRUCTURE_SELECT_FLAG",
    "VERTICAL_SPREADS_FLAG",
    "classify_iv_regime",
    "direction_from_rating",
    "select_structure",
    "select_structure_for_plan",
    "structure_select_enabled",
    "vertical_spreads_enabled",
]

# The single env flag that gates the SELECTION SEAM (not the pure table). The pure
# ``select_structure`` is always callable for tests; consumers wire through
# ``select_structure_for_plan`` which honours this flag (default-OFF).
STRUCTURE_SELECT_FLAG = "HERMES_QUANT_STRUCTURE_SELECT"

# ADR-0098 Step 2: flag that gates the DEFINED_RISK_CREDIT table rows. DEFAULT-OFF.
# When absent, select_structure NEVER returns 'bull_put_spread' (byte-identical to
# today — only CC/CSP/wheel selectable via the PREMIUM_CAPTURE rows). Only a literal
# "1" enables it (same fail-closed convention as every other money-path flag).
VERTICAL_SPREADS_FLAG = "HERMES_QUANT_VERTICAL_SPREADS"

# IV-rank classification cut points (percent points, 0..100). ADR-0082 STARTING
# POINTS; eval-gated. Half-open so every finite IV-rank lands in exactly one regime:
#   LOW  : iv_rank <  IV_RANK_LOW_MAX            (thin premium; selling is unattractive)
#   MID  : IV_RANK_LOW_MAX <= iv_rank < IV_RANK_HIGH_MIN
#   HIGH : iv_rank >= IV_RANK_HIGH_MIN           (rich premium; selling is favoured)
IV_RANK_LOW_MAX: float = 30.0
IV_RANK_HIGH_MIN: float = 50.0


class IVRegime(StrEnum):
    """Coarse, deterministic implied-volatility regime (from an as-of-honest IV-rank).

    ``StrEnum`` for the same label-stable JSON serialisation reason as the debate
    enums (ADR-0058)."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"


class Direction(StrEnum):
    """Directional stance distilled from the judge's ``PortfolioRating``.

    The table keys on this 3-valued stance, not the 5-tier rating, because the
    structure choice only depends on long/short/neutral lean (ADR-0082 stance x
    IV-regime matrix)."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def structure_select_enabled() -> bool:
    """True iff the structure-selection seam is enabled (``=1``). Default-OFF.

    Fail-closed: any value other than the literal ``"1"`` is treated as OFF, so a
    typo / partial config never silently enables a money-path structural seam."""
    return os.environ.get(STRUCTURE_SELECT_FLAG, "0") == "1"


def vertical_spreads_enabled() -> bool:
    """True iff the defined-risk credit vertical seam is enabled (``=1``). Default-OFF.

    ADR-0098 Step 2: gates the DEFINED_RISK_CREDIT rows in ``_STRUCTURE_TABLE``.
    When OFF, ``select_structure`` NEVER returns ``'bull_put_spread'`` (the table
    rows for defined_risk_credit are masked at read time). A literal ``"1"`` is the
    ONLY enabling value — fail-closed, same convention as every money-path flag."""
    return os.environ.get(VERTICAL_SPREADS_FLAG, "0") == "1"


def classify_iv_regime(iv_rank: float) -> IVRegime | None:
    """Classify an as-of-honest IV-rank (0..100) into a coarse ``IVRegime``.

    Returns ``None`` (abstain) for a missing / non-finite / out-of-range IV-rank —
    fail-closed: an unknown vol regime must never imply a structure. The caller is
    responsible for as-of honesty (no future IV in the IV-rank window); this function
    is pure given its input."""
    # NaN/inf and Nones are inadmissible: an unknown regime abstains.
    try:
        r = float(iv_rank)
    except (TypeError, ValueError):
        return None
    if r != r:  # NaN
        return None
    if r < 0.0 or r > 100.0:
        return None
    if r < IV_RANK_LOW_MAX:
        return IVRegime.LOW
    if r < IV_RANK_HIGH_MIN:
        return IVRegime.MID
    return IVRegime.HIGH


def direction_from_rating(rating: PortfolioRating) -> Direction:
    """Distil a 5-tier ``PortfolioRating`` into the 3-valued directional stance the
    table keys on, via the rating's own ``signed_intensity`` (deterministic):

        +2 / +1 (BUY / OVERWEIGHT) -> BULLISH
        -2 / -1 (SELL / UNDERWEIGHT) -> BEARISH
         0      (HOLD)                -> NEUTRAL
    """
    s = rating.signed_intensity
    if s > 0:
        return Direction.BULLISH
    if s < 0:
        return Direction.BEARISH
    return Direction.NEUTRAL


# ---------------------------------------------------------------------------
# The deterministic selection table.
# ---------------------------------------------------------------------------
#
# Keyed by (Direction, StructureIntent-value, IVRegime). The VALUE is a producible
# ``StrategyKind`` (a ``tools._MULTI_LEG_STRATEGIES`` member) or ``None`` (abstain).
#
# Design principles (ADR-0082):
#   * The existing producer (``recipes.build_multi_leg_proposal``) can build ONLY
#     covered_call / cash_secured_put / wheel — all collateral-secured INCOME
#     structures. So the only ``structure_intent`` that can map to a producible kind
#     is PREMIUM_CAPTURE (income / premium-selling). Defined-risk credit/debit
#     (verticals/condors) and long_premium (straddles/calendars) have no producer
#     yet -> they resolve to None (abstain), never to a kind the producer cannot
#     honestly build. (ADR-0082: "Out-of-table / non-defined-risk -> none -> silence.")
#   * Premium selling is only attractive when premium is RICH: LOW IV-regime ->
#     abstain (None) even for PREMIUM_CAPTURE; MID/HIGH IV-regime -> select.
#   * Stance picks the income leg side:
#       - BULLISH premium-capture  -> cash_secured_put  (get-paid-to-go-long; the
#         short put's assignment leaves the operator long at a discount).
#       - BEARISH premium-capture  -> covered_call      (capped-upside income against
#         held shares; the gate rejects it unless the shares are actually held).
#       - NEUTRAL premium-capture  -> wheel             (CSP -> assignment -> CC cycle;
#         the gate / producer validate the held-share / cash collateral).
#   * Everything not explicitly tabulated below is ABSENT and resolves to None via
#     ``.get(...)`` — the silence-by-default rail.
#
# NOTE: this dict is the SINGLE SOURCE OF TRUTH for the matrix; the eval moves cells
# here, never in scattered call sites.
_STRUCTURE_TABLE: dict[tuple[Direction, str, IVRegime], str] = {
    # --- PREMIUM_CAPTURE: the only intent the current producer can satisfy ---
    # Bullish income: cash-secured put (only when premium is worth selling).
    (Direction.BULLISH, "premium_capture", IVRegime.MID): "cash_secured_put",
    (Direction.BULLISH, "premium_capture", IVRegime.HIGH): "cash_secured_put",
    # Bearish income: covered call against held shares.
    (Direction.BEARISH, "premium_capture", IVRegime.MID): "covered_call",
    (Direction.BEARISH, "premium_capture", IVRegime.HIGH): "covered_call",
    # Neutral income: the wheel (CSP <-> CC cycle).
    (Direction.NEUTRAL, "premium_capture", IVRegime.MID): "wheel",
    (Direction.NEUTRAL, "premium_capture", IVRegime.HIGH): "wheel",
    # LOW IV-regime PREMIUM_CAPTURE is intentionally ABSENT for every stance
    # (thin premium -> abstain).
    # --- DEFINED_RISK_CREDIT: ADR-0098 Step 2 (bull_put_spread). ---
    # Bullish defined-risk credit: bull put spread — collect credit for a bullish
    # view while capping max loss via the long protection leg. Only MID/HIGH IV
    # (thin premium at LOW IV makes the spread uneconomical). BEARISH / NEUTRAL
    # defined-risk-credit rows are intentionally ABSENT until a bear-call-spread
    # producer exists.
    # IMPORTANT: these rows are MASKED by select_structure when
    # HERMES_QUANT_VERTICAL_SPREADS != "1" — the flag gate lives in select_structure,
    # not the table literal, so the table remains a single source of truth and the
    # gate can be eval-toggled without editing the table.
    (Direction.BULLISH, "defined_risk_credit", IVRegime.MID): "bull_put_spread",
    (Direction.BULLISH, "defined_risk_credit", IVRegime.HIGH): "bull_put_spread",
    # DEFINED_RISK_DEBIT / LONG_PREMIUM are intentionally ABSENT for every
    # (stance, regime) because no producer for them exists yet; they resolve to
    # None (abstain) until a producer exists.
}


def select_structure(
    *,
    direction: Direction,
    structure_intent: StructureIntent | None,
    iv_rank: float | None = None,
    iv_regime: IVRegime | None = None,
) -> StrategyKind | None:
    """Pure, deterministic structure selection. Same inputs => same output.

    Returns a producible ``StrategyKind`` (a ``_MULTI_LEG_STRATEGIES`` member) or
    ``None`` (ABSTAIN -> silence -> equity path). It NEVER returns naked or a kind the
    producer cannot build, NEVER sizes, and NEVER consults the gate (the producer runs
    the gate downstream; an inadmissible structure is still rejected there).

    Either an ``iv_regime`` (already classified) or an ``iv_rank`` (0..100, as-of
    honest) must be supplied; if both are given, ``iv_regime`` wins. A missing /
    unclassifiable IV input -> None (abstain), per the silence-by-default rail.

    Abstains (returns ``None``) when:
      * ``structure_intent`` is absent or ``NONE`` (silence-by-default), or
      * the IV regime is unknown / unclassifiable, or
      * the ``(direction, intent, regime)`` triple is not in the table (e.g. LOW-IV
        premium capture, or any defined-risk / long-premium intent the producer
        cannot build yet).
    """
    # Lazy import to avoid a hard import cycle (schemas import from many places);
    # also keeps this module importable with zero side effects.
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    # silence-by-default: absent / NONE intent -> equity path.
    if structure_intent is None or structure_intent == StructureIntent.NONE:
        return None

    regime = iv_regime if iv_regime is not None else (
        classify_iv_regime(iv_rank) if iv_rank is not None else None
    )
    if regime is None:
        return None  # unknown vol regime -> abstain (fail-closed).

    result = _STRUCTURE_TABLE.get((direction, structure_intent.value, regime))

    # ADR-0098 Step 2: mask defined_risk_credit table results when the vertical
    # spreads flag is OFF (default). This preserves byte-identity: the only change
    # relative to today is that a DEFINED_RISK_CREDIT intent now maps to
    # 'bull_put_spread' in the table, but the selection is suppressed to None
    # (abstain -> equity path) until the operator sets HERMES_QUANT_VERTICAL_SPREADS=1.
    # A PREMIUM_CAPTURE / DEFINED_RISK_DEBIT / LONG_PREMIUM result is unaffected.
    if result is not None and structure_intent.value == "defined_risk_credit":
        if not vertical_spreads_enabled():
            return None  # flag OFF -> abstain (byte-identical to today).

    return result


def select_structure_for_plan(
    plan,  # noqa: ANN001 — ResearchPlan (avoid the import cycle at module load)
    *,
    iv_rank: float | None = None,
    iv_regime: IVRegime | None = None,
) -> StrategyKind | None:
    """Flag-honouring convenience wrapper over :func:`select_structure`.

    This is the CONSUMER seam: it reads ``HERMES_QUANT_STRUCTURE_SELECT`` and returns
    ``None`` (abstain -> byte-identical equity path) whenever the flag is OFF, so a
    caller can unconditionally call this and get today's behaviour until the operator
    flips the flag. With the flag ON it distils ``plan.recommendation`` to a
    ``Direction`` and delegates to the pure table.

    ``plan`` is a ``ResearchPlan`` (duck-typed to ``.recommendation`` /
    ``.structure_intent``)."""
    if not structure_select_enabled():
        return None  # default-OFF: byte-identical equity path.

    intent = getattr(plan, "structure_intent", None)
    rating = getattr(plan, "recommendation", None)
    if rating is None:
        return None  # malformed plan -> abstain (fail-closed).

    return select_structure(
        direction=direction_from_rating(rating),
        structure_intent=intent,
        iv_rank=iv_rank,
        iv_regime=iv_regime,
    )
