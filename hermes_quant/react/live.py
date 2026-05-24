"""hermes_quant.react.live — Live multi-leg broker stub (ADR-0029 patched D7).

This module is intentionally inert at runtime: no broker call lands here in v0.1.x.
It exists to make the type system the gate. Importing LiveBroker without a
LiveTradingApproval gives you a class with no `submit_mleg_order` method.

Follow ADR-0029 patched D7 thresholds verbatim. Do NOT alter.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator


class LiveTradingApproval(BaseModel):
    """Constructed only by passing every promotion gate. Cannot be instantiated
    by mistake. Per ADR-0029 patched D7 (Amendment 2026-05-24).
    """

    approval_id: str
    issued_at: datetime
    paper_outcomes_count: int
    rolling_30d_realized_sharpe: float
    sharpe_95ci_lower: float
    rolling_30d_max_drawdown_pct: float
    no_killswitch_in_trailing_14d: bool
    immutable_breaches_in_window: int
    weekly_retro_evidence_ids: list[str]
    promoter_human_id: str

    @model_validator(mode="after")
    def _enforce_thresholds(self) -> LiveTradingApproval:
        if self.paper_outcomes_count < 100:
            raise ValueError("paper_outcomes_count must be >= 100")
        if self.sharpe_95ci_lower < 1.0:
            raise ValueError("sharpe_95ci_lower must be >= 1.0")
        if self.rolling_30d_max_drawdown_pct > 0.01:
            raise ValueError("rolling_30d_max_drawdown_pct must be <= 0.01 (1%)")
        if not self.no_killswitch_in_trailing_14d:
            raise ValueError("kill-switch trigger in trailing 14d disqualifies promotion")
        if self.immutable_breaches_in_window != 0:
            raise ValueError("any immutable-rule breach disqualifies promotion")
        return self


# Threshold constants for cross-module imports. Two key naming conventions are
# supported on this same dict because two consumers exist:
#   - hermes_quant.governance.promotion uses `min_*` / `max_*` prefixes
#   - this module's docs use `*_min` / `*_max` suffixes
# The dict carries BOTH spellings pointing at the same numeric values so neither
# consumer needs to know about the other's naming style. ADR-0029 D7 is the
# single source of truth for the numbers; key names are an integration detail.
LIVE_APPROVAL_THRESHOLDS: dict[str, Any] = {
    # Suffix style (this module's primary naming):
    "paper_outcomes_count_min": 100,
    "sharpe_95ci_lower_min": 1.0,
    "rolling_30d_max_drawdown_pct_max": 0.01,
    "killswitch_window_days": 14,
    "immutable_breaches_max": 0,
    "calibrator_drift_max": 0.05,
    # Prefix style (governance.promotion._LATE_BIND_THRESHOLDS naming):
    "min_paper_outcomes": 100,
    "min_sharpe_95ci_lower": 1.0,
    "max_rolling_30d_drawdown_pct": 0.01,
    "max_calibrator_drift": 0.05,
    "immutable_breach_window_days": 30,
}


class LiveBroker:
    """Live multi-leg requires LiveTradingApproval at construction time.

    `submit_mleg_order` is bound to the INSTANCE only when an approval is
    passed; the class itself has no such attribute. This is correctness-by-
    construction — no runtime boolean flip can authorize live trading.
    """

    def __init__(self, approval: LiveTradingApproval):
        if not isinstance(approval, LiveTradingApproval):
            raise TypeError(
                "LiveBroker requires a LiveTradingApproval object. "
                "PaperBroker.submit_mleg_order remains the only path until "
                "the approval contract lands."
            )
        self._approval = approval

        # CRITICAL: bind submit_mleg_order to instance, not class.
        # Class-level inspection (inspect.getmembers(LiveBroker)) will not
        # show submit_mleg_order; only instances constructed with a valid
        # approval have it.
        self.submit_mleg_order = self._submit_mleg_order_impl

    def _submit_mleg_order_impl(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Stub. Raises NotImplementedError until a future ADR-00XX defines the
        live multi-leg submission contract. Until then, paper is the only path.
        """
        raise NotImplementedError(
            "LiveBroker.submit_mleg_order is not implemented in v0.1.x. "
            "PaperBroker.submit_mleg_order is the only multi-leg execution path. "
            "See ADR-0029 patched D7."
        )
