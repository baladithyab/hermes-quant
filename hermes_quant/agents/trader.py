"""hermes_quant.agents.trader — TraderProposal schema + TraderNode v0.1 (deterministic).

ADR-0044: Wave 2 — Trader stage + structured output.

TraderNode v0.1 is DETERMINISTIC — no LLM call.
It maps a 5-tier research recommendation → concrete TraderProposal with:
  - explicit entry_price (current close from the advisor signal)
  - stop_loss (2 × ATR from the signal metadata)
  - position_sizing_pct (deterministic ladder per rating)
  - time_horizon_days (conservative default per rating)
  - confidence (passed through from ResearchPlan)

LLM-driven v0.2 is deferred; this makes Wave 2 testable cheaply without
incurring any LLM cost.

Usage:
    node = TraderNode()
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
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

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

    def __init__(self, atr_multiplier: float = _ATR_STOP_MULTIPLIER) -> None:
        self.atr_multiplier = atr_multiplier

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
        horizon_days = (
            _RATING_HORIZON_DAYS.get(recommendation)
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
