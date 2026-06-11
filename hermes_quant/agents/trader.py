"""hermes_quant.agents.trader — TraderProposal schema + TraderNode v0.1 (deterministic)
                                + TraderNodeLLM v0.2 (feature-flagged, silence-by-default).

ADR-0044: Wave 2 — Trader stage + structured output.
ADR-0054: LLM-Caller Foundation & TraderNode v0.2.

TraderNode v0.1 is DETERMINISTIC — no LLM call.
It maps a 5-tier research recommendation → concrete TraderProposal with:
  - explicit entry_price (current close from the advisor signal)
  - stop_loss (2 × ATR from the signal metadata)
  - position_sizing_pct (deterministic ladder per rating)
  - time_horizon_days (conservative default per rating)
  - confidence (passed through from ResearchPlan)

TraderNodeLLM v0.2 wraps v0.1 with an optional LLM upgrade path:
  - Feature-flagged via HERMES_QUANT_TRADER_LLM=1 (default OFF / 0).
  - Falls back to v0.1 if: flag is OFF, no API key, LLM call fails, or
    structured output is invalid.
  - Audit log records which path fired.
  - NEVER raises on LLM error (ADR-0031 silence-by-default contract).

Usage:
    # v0.1 (always safe, deterministic):
    node = TraderNode()
    proposal = node(research_plan_dict, advisor_signal_dict)

    # v0.2 (LLM upgrade with v0.1 fallback):
    from hermes_quant.agents.llm_caller import LLMCaller
    node = TraderNodeLLM(llm_caller=LLMCaller())
    proposal = node(research_plan_dict, advisor_signal_dict)

Inputs:
    research_plan_dict — output of ResearchPlan.model_dump() or equivalent
        keys: recommendation (str), confidence (float), rationale (str),
              strategic_actions (str), horizon_emphasis (str|None)
    advisor_signal_dict — output of advisor.recommend() or sub-dict
        keys: direction (int), confidence (float), magnitude (float),
              metadata (dict with atr_relative, close, etc.)
              data_quality (dict with bars_received, last_close, etc.)

Returns:
    TraderProposal instance.

Graceful fallback:
    If required fields are missing from either input, the node returns
    a conservative proposal with size_fraction=0.05, confidence=0.5,
    and warning_message set to explain the failure mode.
"""

from __future__ import annotations

import logging
import math
import os
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field, model_validator

# ADR-0065 (v0.6.1): TraderProposal is the consumer of the ResearchDebateStage's
# ResearchPlan output. Importing PortfolioRating at module load is safe because
# `hermes_quant.agents.research_debate.schemas` imports from
# `hermes_quant.aggregators.llm_committee` (BullBearTurn), and llm_committee.py
# does not import from trader.py — no cycle.
from hermes_quant.agents.research_debate.schemas import PortfolioRating

if TYPE_CHECKING:
    from hermes_quant.agents.llm_caller import LLMCaller

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rating → sizing ladder (deterministic v0.1)
# ---------------------------------------------------------------------------

_RATING_SIZE_FRACTION: dict[str, float] = {
    "Buy": 0.20,
    "Overweight": 0.10,
    "Hold": 0.00,
    "Underweight": -0.10,
    "Sell": -0.20,
}

_RATING_HORIZON_DAYS: dict[str, int] = {
    "Buy": 30,
    "Overweight": 21,
    "Hold": 14,
    "Underweight": 21,
    "Sell": 30,
}

# Default ATR multiplier for stop placement
_ATR_STOP_MULTIPLIER: float = 2.0

# Default percentage stop used when ATR is unavailable but we DO have a price.
# Root-cause fix (deep-review 2026-06-07): the June-4 ASTS loss ran with
# stop_loss=None because ATR was missing and the trader left the stop unset. A
# stopless position has no invalidation level, so a -21% move ran uncapped. With
# a price but no ATR we now place a default 8% stop (a reasonable single-name
# equity invalidation band) rather than emitting None. Tunable per-instance.
_DEFAULT_STOP_PCT: float = 0.08

# Conservative fallback values when inputs are incomplete
_FALLBACK_SIZE: float = 0.05
_FALLBACK_CONF: float = 0.50


# ---------------------------------------------------------------------------
# TraderAction enum — str mixin ensures JSON serialization works out of box
# ---------------------------------------------------------------------------


class TraderAction(str, Enum):
    """Canonical trade action.

    str mixin: json.dumps({"action": TraderAction.BUY}) → {"action": "BUY"}
    without needing a custom encoder.
    """

    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


# ---------------------------------------------------------------------------
# TraderProposal — the core Pydantic v2 schema
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Concrete, actionable trade proposal produced by TraderNode.

    Every approval that reaches the risk gate MUST carry a TraderProposal
    embedded in advisor_result['trader_proposal'] so that stop_loss,
    entry_price, and time_horizon are never None in the audit trail.

    Design decisions:
    - action and size_fraction are REQUIRED (no silent default sizing).
    - entry_price / stop_loss / target_price are Optional because the v0.1
      deterministic node may not have current-price data; callers should
      treat None as "price data unavailable" and log a warning.
    - Cross-field validation: for BUY stop_loss < entry_price; for SELL
      stop_loss > entry_price (stops are always loss-side).
    - rationale is capped at 2048 chars to bound context usage.
    - warning_message signals graceful-fallback activation.
    """

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    action: TraderAction
    size_fraction: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Fraction of available capital (0–1). Sign of intent is captured by action.",
    )
    entry_price: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Suggested entry price (current close). None if price data unavailable.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "Hard stop price. For BUY: stop < entry. For SELL: stop > entry. "
            "None if ATR data unavailable."
        ),
    )
    target_price: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Price target (1R or analyst-derived). None if unavailable.",
    )
    time_horizon_days: Optional[int] = Field(
        default=None,
        ge=1,
        le=365,
        description="Expected holding period in calendar days.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence in this proposal (0–1).",
    )
    rationale: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="2–4 sentence rationale anchored in the research plan.",
    )
    warning_message: Optional[str] = Field(
        default=None,
        description=(
            "Non-None when graceful fallback was triggered. "
            "Callers should surface this in the brief."
        ),
    )

    # ADR-0065 (v0.6.1): optional links back to the ResearchDebateStage's
    # judge output. Both default to None so EVERY existing call-site (and the
    # ``extra='forbid'`` posture) keeps working unchanged when the bull/bear
    # debate flag is OFF. When the stage runs, the wiring layer (llm_committee
    # dispatch site) is expected to populate these so the audit trail can join
    # a TraderProposal back to the ResearchPlan that justified its sizing.
    research_plan_recommendation: Optional[PortfolioRating] = Field(
        default=None,
        description=(
            "5-tier rating produced by the ResearchManager judge in the "
            "Bull/Bear debate stage (ADR-0065). None when the legacy parallel-"
            "emit committee path is taken (HERMES_QUANT_RESEARCH_DEBATE unset)."
        ),
    )
    research_plan_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable identifier joining this TraderProposal to the audit row "
            "emitted by ``run_research_debate`` (kind='research_debate'). "
            "Mirrors the ``proposal_id`` field on that audit payload."
        ),
    )

    @model_validator(mode="after")
    def _validate_stop_vs_entry(self) -> "TraderProposal":
        """Cross-field: stop_loss must be on the losing side of entry."""
        entry = self.entry_price
        stop = self.stop_loss

        if entry is None or stop is None:
            return self  # can't validate without both prices

        if self.action == TraderAction.BUY:
            if stop >= entry:
                raise ValueError(
                    f"For BUY action stop_loss ({stop}) must be < entry_price ({entry}). "
                    "Stops must be on the losing side."
                )
        elif self.action == TraderAction.SELL:
            if stop <= entry:
                raise ValueError(
                    f"For SELL action stop_loss ({stop}) must be > entry_price ({entry}). "
                    "Stops must be on the losing side."
                )
        # HOLD: stop_loss is informational; no directional constraint

        return self


# ---------------------------------------------------------------------------
# TraderNode — deterministic v0.1
# ---------------------------------------------------------------------------


class TraderNode:
    """Translate a ResearchPlan + advisor signal into a TraderProposal.

    v0.1: Deterministic mapping. No LLM calls.

    v0.2 (deferred, ADR-0044 §4): LLM-driven Trader will replace the
    deterministic ladder with a prompted deep-tier call that can reason
    about support/resistance and entry timing.

    Args:
        atr_multiplier: ATR multiplier for stop placement (default: 2.0).
            Stop = entry ± (atr_multiplier × ATR_absolute).
    """

    def __init__(
        self,
        atr_multiplier: float = _ATR_STOP_MULTIPLIER,
        default_stop_pct: float = _DEFAULT_STOP_PCT,
    ) -> None:
        self.atr_multiplier = atr_multiplier
        self.default_stop_pct = default_stop_pct

    def __call__(
        self,
        research_plan: dict[str, Any],
        advisor_signal: dict[str, Any] | None = None,
    ) -> TraderProposal:
        """Produce a TraderProposal from research plan + optional signal data.

        Args:
            research_plan: Dict with at minimum keys:
                recommendation (str), confidence (float), rationale (str),
                strategic_actions (str)
            advisor_signal: Optional signal dict from advisor.recommend().
                Expected keys: metadata (dict), data_quality (dict),
                direction (int), magnitude (float), confidence (float).
                Metadata sub-keys used: atr_relative, last_close.

        Returns:
            TraderProposal — never raises; returns warning fallback on error.
        """
        try:
            return self._build(research_plan, advisor_signal or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TraderNode._build failed (%s); returning graceful fallback.",
                exc,
                exc_info=True,
            )
            return self._fallback(str(exc))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build(
        self,
        research_plan: dict[str, Any],
        advisor_signal: dict[str, Any],
    ) -> TraderProposal:
        """Core deterministic mapping — may raise; caller wraps with try/except."""
        # --- Extract required fields ---
        recommendation = research_plan.get("recommendation")
        if recommendation not in _RATING_SIZE_FRACTION:
            raise ValueError(
                f"research_plan.recommendation must be one of "
                f"{list(_RATING_SIZE_FRACTION)}; got {recommendation!r}"
            )

        plan_confidence = research_plan.get("confidence")
        if plan_confidence is None or not isinstance(plan_confidence, (int, float)):
            raise ValueError(
                f"research_plan.confidence must be a float; got {plan_confidence!r}"
            )

        rationale_parts = [
            research_plan.get("rationale") or "",
            research_plan.get("strategic_actions") or "",
        ]
        rationale = " ".join(p for p in rationale_parts if p).strip()
        if not rationale:
            raise ValueError("research_plan must have non-empty rationale or strategic_actions.")
        rationale = rationale[:2048]  # enforce cap

        # --- Derive sizing and action ---
        size_fraction = abs(_RATING_SIZE_FRACTION[recommendation])
        action = _rating_to_action(recommendation)
        # Wave 5c (horizon contract): the holding horizon must follow the
        # aggregate's DETECTION horizon when the advisor signal carries one — a
        # 5m-detected signal must not be positioned for a 30-day "Buy" hold just
        # because the rating ladder says so (the horizon-contract mismatch). The
        # aggregate's AggregatedSignal.horizon rides through advisor_signal as a
        # canonical label ("5m".."1Q"); map it to a representative day-count.
        # Precedence: aggregate horizon -> rating ladder -> horizon_emphasis text.
        # When the signal has no horizon (every legacy call shape), this is
        # byte-identical to the prior ladder-first behavior.
        horizon_days = (
            _aggregate_horizon_to_days(advisor_signal.get("horizon"))
            or _RATING_HORIZON_DAYS.get(recommendation)
            or _parse_horizon(research_plan.get("horizon_emphasis"))
        )

        # --- Price / ATR data from advisor signal ---
        entry_price, stop_loss, target_price = self._price_levels(
            action=action,
            recommendation=recommendation,
            advisor_signal=advisor_signal,
        )

        return TraderProposal(
            action=action,
            size_fraction=size_fraction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_price=target_price,
            time_horizon_days=horizon_days,
            confidence=float(plan_confidence),
            rationale=rationale,
            warning_message=None,
        )

    def _price_levels(
        self,
        action: TraderAction,
        recommendation: str,
        advisor_signal: dict[str, Any],
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Derive entry, stop, target from signal metadata.

        Returns (entry_price, stop_loss, target_price). Any may be None
        if the required data is missing or invalid.
        """
        metadata = advisor_signal.get("metadata") or {}
        data_quality = advisor_signal.get("data_quality") or {}

        # Prefer metadata.last_close, then data_quality.last_close, then None
        last_close: Optional[float] = _coerce_positive_float(
            metadata.get("last_close")
            or data_quality.get("last_close")
            or advisor_signal.get("close")
        )

        # atr_relative is ATR(14) / close (dimensionless); we need ATR in $
        atr_relative: Optional[float] = _coerce_positive_float(
            metadata.get("atr_relative")
        )

        entry_price: Optional[float] = last_close
        stop_loss: Optional[float] = None
        target_price: Optional[float] = None

        if last_close is not None and atr_relative is not None:
            atr_abs = atr_relative * last_close
            stop_dist = self.atr_multiplier * atr_abs

            if action == TraderAction.BUY:
                raw_stop = last_close - stop_dist
                stop_loss = max(raw_stop, 0.01)  # can't be ≤ 0
                # 1R target: entry + stop_dist (symmetric)
                target_price = last_close + stop_dist
            elif action == TraderAction.SELL:
                stop_loss = last_close + stop_dist
                target_price = last_close - stop_dist
            # HOLD: no meaningful stop/target from ATR alone
        elif last_close is not None and action in (TraderAction.BUY, TraderAction.SELL):
            # Root-cause fix (deep-review 2026-06-07): ATR missing but we have a
            # price — fall back to a default PERCENTAGE stop rather than leaving
            # stop_loss=None. A stopless position has no invalidation level (the
            # June-4 ASTS -21% loss ran uncapped for exactly this reason). The
            # default band is wider/cruder than an ATR stop but bounds the loss.
            stop_dist = self.default_stop_pct * last_close
            if action == TraderAction.BUY:
                stop_loss = max(last_close - stop_dist, 0.01)
                target_price = last_close + stop_dist
            else:  # SELL
                stop_loss = last_close + stop_dist
                target_price = max(last_close - stop_dist, 0.01)

        return entry_price, stop_loss, target_price

    @staticmethod
    def _fallback(reason: str) -> TraderProposal:
        """Return a conservative fallback proposal when _build fails."""
        msg = f"TraderNode graceful fallback — {reason[:500]}"
        return TraderProposal(
            action=TraderAction.HOLD,
            size_fraction=_FALLBACK_SIZE,
            entry_price=None,
            stop_loss=None,
            target_price=None,
            time_horizon_days=None,
            confidence=_FALLBACK_CONF,
            rationale=(
                "Graceful fallback activated: required fields were missing or invalid. "
                "Conservative HOLD with reduced sizing applied. Review warning_message."
            ),
            warning_message=msg,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _rating_to_action(recommendation: str) -> TraderAction:
    """Map 5-tier rating to TraderAction enum."""
    if recommendation in ("Buy", "Overweight"):
        return TraderAction.BUY
    if recommendation in ("Sell", "Underweight"):
        return TraderAction.SELL
    return TraderAction.HOLD


def _coerce_positive_float(val: Any) -> Optional[float]:
    """Try to cast val to float; return None if it's missing, NaN, or ≤ 0."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


# Aggregate (AggregatedSignal.horizon) canonical label -> representative
# holding-period day-count. Intraday/daily detection windows map to a 1-day
# hold (the position is opened against a same-day signal); weekly/monthly/
# quarterly scale up. Wave 5c: keeps the trader's holding horizon aligned to the
# detection horizon the BMA actually aggregated, instead of a rating default.
_AGGREGATE_HORIZON_DAYS: dict[str, int] = {
    "1m": 1,
    "5m": 1,
    "15m": 1,
    "30m": 1,
    "1h": 1,
    "4h": 1,
    "1d": 1,
    "1w": 7,
    "1M": 30,
    "1Q": 90,
}


def _aggregate_horizon_to_days(horizon: Any) -> Optional[int]:
    """Map an aggregate signal's canonical horizon label to a day-count.

    Returns None for a missing/unknown label so the caller falls back to the
    rating ladder — an unrecognized horizon must never crash or zero the hold.
    """
    if not isinstance(horizon, str):
        return None
    return _AGGREGATE_HORIZON_DAYS.get(horizon)


def _parse_horizon(horizon_emphasis: Any) -> Optional[int]:
    """Try to extract a day-count from horizon_emphasis string.

    e.g. 'medium-term (30–60 days)' → 45, 'short-term' → 14.
    Returns None on parse failure; caller falls back to rating ladder.
    """
    if not isinstance(horizon_emphasis, str):
        return None
    import re  # local import to avoid top-level dependency in the hot path

    m = re.search(r"(\d+)\s*(?:–|-|to)\s*(\d+)\s*day", horizon_emphasis, re.IGNORECASE)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        mid = (lo + hi) // 2
        return max(1, min(365, mid))
    m2 = re.search(r"(\d+)\s*day", horizon_emphasis, re.IGNORECASE)
    if m2:
        return max(1, min(365, int(m2.group(1))))
    if "short" in horizon_emphasis.lower():
        return 14
    if "long" in horizon_emphasis.lower():
        return 90
    return None


# ---------------------------------------------------------------------------
# Feature-flag helper
# ---------------------------------------------------------------------------


def _trader_llm_enabled() -> bool:
    """Return True iff HERMES_QUANT_TRADER_LLM=1 (default OFF)."""
    return os.environ.get("HERMES_QUANT_TRADER_LLM", "0").strip() == "1"


# ---------------------------------------------------------------------------
# TraderNodeLLM — v0.2 (LLM-driven with v0.1 fallback)
# ---------------------------------------------------------------------------

# Prompt templates used by TraderNodeLLM v0.2.
_SYSTEM_PROMPT = """\
You are a disciplined quantitative trading analyst for hermes-quant.
Your task is to translate a research plan and an advisor signal into a \
precise, risk-controlled TraderProposal.

Rules:
- Always respect the research plan's recommendation (Buy/Overweight/Hold/\
Underweight/Sell).
- For BUY actions, stop_loss MUST be strictly less than entry_price.
- For SELL actions, stop_loss MUST be strictly greater than entry_price.
- size_fraction must be in [0.0, 1.0]; confidence must be in [0.0, 1.0].
- Provide a 2–4 sentence rationale anchored in the research plan.
- Return ONLY valid JSON conforming to the TraderProposal schema.
"""

_USER_PROMPT_TEMPLATE = """\
Research plan:
{research_plan_json}

Advisor signal:
{advisor_signal_json}

Emit a TraderProposal JSON object. Do not include any prose outside the JSON.
"""


class TraderNodeLLM:
    """TraderNode v0.2 — LLM-driven with silence-by-default v0.1 fallback.

    ADR-0054: wraps v0.1 deterministic node with an optional LLM upgrade.

    Feature flag: HERMES_QUANT_TRADER_LLM=1 enables LLM path (default OFF).

    Fallback chain:
        1. Flag OFF                         → v0.1 deterministic
        2. Flag ON, llm_caller.available()  → LLM call attempted
           a. LLM returns valid proposal    → v0.2 path (audit: v02_llm_succeeded)
           b. LLM returns None / invalid    → v0.1 (audit: v02_llm_fallback_to_v01)
           c. LLM call raises               → v0.1 (audit: v02_llm_fallback_to_v01)
        3. Flag ON, not available()         → v0.1 (audit: v02_llm_fallback_to_v01)

    Args:
        llm_caller:      LLMCaller instance. None → always falls back to v0.1.
        atr_multiplier:  Passed to the underlying TraderNode (default 2.0).
    """

    def __init__(
        self,
        *,
        llm_caller: Optional["LLMCaller"] = None,
        atr_multiplier: float = _ATR_STOP_MULTIPLIER,
    ) -> None:
        self._llm_caller = llm_caller
        self._v01_node = TraderNode(atr_multiplier=atr_multiplier)
        self.atr_multiplier = atr_multiplier

    def __call__(
        self,
        research_plan: dict[str, Any],
        advisor_signal: dict[str, Any] | None = None,
    ) -> TraderProposal:
        """Produce a TraderProposal, routing to LLM or v0.1 per flag + availability.

        Never raises — v0.1 is always the safety net.
        """
        import json as _json

        advisor_signal = advisor_signal or {}

        # --- Path A: flag OFF or no caller → pure v0.1 ---
        if not _trader_llm_enabled() or self._llm_caller is None:
            proposal = self._v01_node(research_plan, advisor_signal)
            self._record_path("v01_deterministic", proposal)
            return proposal

        # --- Path B: flag ON, check availability ---
        if not self._llm_caller.available():
            logger.warning(
                "TraderNodeLLM: LLMCaller not available (no API key); "
                "falling back to v0.1 deterministic."
            )
            proposal = self._v01_node(research_plan, advisor_signal)
            self._record_path("v02_llm_fallback_to_v01", proposal, reason="llm_not_available")
            return proposal

        # --- Path C: attempt LLM call ---
        try:
            system_prompt = _SYSTEM_PROMPT
            user_prompt = _USER_PROMPT_TEMPLATE.format(
                research_plan_json=_json.dumps(research_plan, default=str),
                advisor_signal_json=_json.dumps(advisor_signal, default=str),
            )
            obj, raw = self._llm_caller.call(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=TraderProposal,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TraderNodeLLM: LLM call raised unexpectedly (%s); "
                "falling back to v0.1.",
                exc,
            )
            proposal = self._v01_node(research_plan, advisor_signal)
            self._record_path(
                "v02_llm_fallback_to_v01", proposal, reason=f"llm_raised: {exc}"
            )
            return proposal

        # --- Validate returned object ---
        if isinstance(obj, TraderProposal):
            # ADR-4665 §5.3/§7.4 (Gate-1 byte-identical gap): the LLM may
            # influence DIRECTION and QUALITATIVE fields, but its numeric
            # entry/stop/target MUST NOT reach the risk gate un-recomputed.
            # Re-run the SAME deterministic helper v0.1 uses and OVERWRITE the
            # price triple, so a hallucinated stop/target can never flow
            # downstream as if it were the deterministic value. NEVER raise —
            # on any failure fall back to the pure v0.1 deterministic proposal.
            try:
                grounded = self._overwrite_price_levels_deterministic(
                    obj, research_plan, advisor_signal
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "TraderNodeLLM: deterministic price-level recompute failed "
                    "(%s); falling back to v0.1.",
                    exc,
                )
                proposal = self._v01_node(research_plan, advisor_signal)
                self._record_path(
                    "v02_llm_fallback_to_v01",
                    proposal,
                    reason=f"recompute_failed: {exc}",
                )
                return proposal

            logger.info(
                "TraderNodeLLM: v0.2 LLM path succeeded "
                "(numeric stop/target deterministically re-grounded)."
            )
            self._record_path("v02_llm_succeeded", grounded)
            return grounded

        # LLM returned None or non-TraderProposal — fall back
        logger.warning(
            "TraderNodeLLM: LLM returned %r (not a TraderProposal); "
            "falling back to v0.1.",
            type(obj).__name__,
        )
        proposal = self._v01_node(research_plan, advisor_signal)
        self._record_path(
            "v02_llm_fallback_to_v01",
            proposal,
            reason="llm_parse_failed",
        )
        return proposal

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _overwrite_price_levels_deterministic(
        self,
        llm_proposal: TraderProposal,
        research_plan: dict[str, Any],
        advisor_signal: dict[str, Any],
    ) -> TraderProposal:
        """Re-ground a v0.2 LLM proposal's numeric price levels.

        ADR-4665 §5.3/§7.4. The LLM's raw entry_price / stop_loss /
        target_price are DISCARDED and replaced with the deterministic
        recomputation produced by the EXACT same ``TraderNode._price_levels``
        helper that the v0.1 path feeds to the risk gate. Everything else the
        LLM produced (action, size_fraction, time_horizon_days, confidence,
        rationale, research-plan links) is preserved.

        We rebuild a fresh ``TraderProposal`` rather than ``model_copy`` so the
        cross-field model-validator (stop must be on the losing side of entry)
        re-runs against the deterministic numbers — the LLM cannot smuggle a
        wrong-side or absurd stop past the producing seam.

        Returns a NEW TraderProposal. May raise (caller wraps with try/except
        → v0.1 fallback); never mutates the input.
        """
        # Recompute from the LLM's chosen ACTION so the deterministic stop sits
        # on the correct losing side for the direction the LLM proposed. The
        # magnitude/side come entirely from advisor-signal metadata (2×ATR from
        # last_close), never from the LLM's numbers. ``recommendation`` carries
        # the rating-domain value from the research plan (NOT action.value) so
        # the helper's contract — ``_build`` passes "Buy"/"Sell"/… — is honored
        # even if ``_price_levels`` is later extended to consume the rating.
        recommendation = research_plan.get("recommendation")
        det_entry, det_stop, det_target = self._v01_node._price_levels(
            action=llm_proposal.action,
            recommendation=recommendation,
            advisor_signal=advisor_signal,
        )

        data = llm_proposal.model_dump()
        data["entry_price"] = det_entry
        data["stop_loss"] = det_stop
        data["target_price"] = det_target
        return TraderProposal(**data)

    def _record_path(
        self,
        path: str,
        proposal: TraderProposal,
        reason: str = "",
    ) -> None:
        """Append a trader_llm_call audit event recording which path fired."""
        from hermes_quant.agents.llm_caller import _audit_append

        payload: dict[str, Any] = {
            "path": path,
            "reason": reason,
            "action": proposal.action.value,
            "size_fraction": proposal.size_fraction,
            "confidence": proposal.confidence,
            "warning_message": proposal.warning_message,
        }
        _audit_append(
            kind="trader_llm_call",
            source="hermes_quant.agents.trader.TraderNodeLLM",
            payload=payload,
        )
