"""hermes_quant.risk.gate — Concrete risk gate implementation.

Per ADR-0004 + ADR-0009 §P0-1 + §P0-5 + synthesis-v2 §P0-A:

Sequence (HIGHEST priority FIRST per ADR-0009 §P0-5):
  Rule 0: halt check (any active halt covering scope → silence)
  Rule 1: drawdown circuit breaker (>max_drawdown_pct → flatten + halt)
  Rule 2: daily-loss circuit breaker (>max_daily_loss_pct → flatten + halt-until-session)
  Rule 3: silence on flat or zero-confidence signal
  Rule 4: post-loss cooldown (last loss < cooldown_minutes → silence)
  Rule 5: cost gate (|expected_signed_edge| < cost_multiple × round_trip_cost → silence)
  Rule 6: position size from quarter-Kelly (uses expected_signed_edge / σ²)
  Rule 7: minimum-trade-size guard (|delta| < min_trade_size → silence)

Per synthesis-v2 §P0-A: BOTH the cost-gate AND the Kelly sizer use the
SAME expected_signed_edge formula (single source of truth from
hermes_quant.risk.kelly).

Per ADR-0004 §Configuration profiles: ships three named profiles
(conservative, moderate, aggressive) loaded from
~/.hermes/config.yaml::quant.risk.profile.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    HaltState,
    MarketState,
    Portfolio,
    RiskGate,
)
from hermes_quant.risk.kelly import (
    cost_gate_threshold,
    expected_signed_edge,
    quarter_kelly_size,
    round_to_step,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskConfig:
    """Per ADR-0004 + ADR-0009 §P0-5."""
    max_position_pct: float = 0.20
    """Hard cap on absolute target position fraction. Default 20% NAV."""

    action_step: float = 0.05
    """Discrete action step (anti-leverage-gambling). Positions in
    {0, ±0.05, ±0.10, ±0.15, ±0.20} of NAV."""

    cost_multiple: float = 2.0
    """Edge must be ≥ N × round-trip transaction cost."""

    max_drawdown_pct: float = 0.15
    """Drawdown circuit breaker — flatten + durable halt above this."""

    max_daily_loss_pct: float = 0.05
    """Daily-loss circuit breaker — flatten + halt-until-session."""

    min_trade_size: float = 0.02
    """Minimum |target - current| to act on (anti-churn)."""

    quarter_kelly: float = 0.25
    """Kelly multiplier (0.25 = quarter-Kelly per literature consensus)."""

    cooldown_after_loss_minutes: int = 60
    """Cooldown window after a realized loss (heuristic; v0.2 may
    config-default-off)."""

    @classmethod
    def conservative(cls) -> "RiskConfig":
        return cls(
            max_position_pct=0.10,
            action_step=0.05,
            cost_multiple=3.0,
            max_drawdown_pct=0.10,
            max_daily_loss_pct=0.03,
        )

    @classmethod
    def moderate(cls) -> "RiskConfig":
        return cls()  # all defaults

    @classmethod
    def aggressive(cls) -> "RiskConfig":
        return cls(
            max_position_pct=0.40,
            action_step=0.10,
            cost_multiple=1.5,
            max_drawdown_pct=0.20,
            max_daily_loss_pct=0.10,
        )


PROFILES = {
    "conservative": RiskConfig.conservative,
    "moderate": RiskConfig.moderate,
    "aggressive": RiskConfig.aggressive,
}


# ---------------------------------------------------------------------------
# Per-asset state (cooldown timers, last-loss tracking)
# ---------------------------------------------------------------------------

@dataclass
class _AssetCooldownState:
    """Cooldown timers per (account, asset_class, asset)."""
    last_loss_at: Optional[pd.Timestamp] = None


# ---------------------------------------------------------------------------
# DefaultRiskGate
# ---------------------------------------------------------------------------

class DefaultRiskGate:
    """Concrete risk gate implementation.

    Implements the RiskGate Protocol from hermes_quant.protocol.

    Per synthesis-v2 §P0-A: cost gate AND Kelly sizer use expected_signed_edge.
    Per synthesis-v2 §P0-D ordering: halt FIRST, then any other check.
    """

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._cooldowns: dict[tuple[str, str, str], _AssetCooldownState] = {}
        # Action stats for observability
        self._n_actions = 0
        self._n_silenced_halt = 0
        self._n_silenced_drawdown = 0
        self._n_silenced_daily_loss = 0
        self._n_silenced_flat = 0
        self._n_silenced_cooldown = 0
        self._n_silenced_cost_gate = 0
        self._n_silenced_min_trade = 0

    def gate(
        self,
        signal: AggregatedSignal,
        market: MarketState,
        portfolio: Portfolio,
        halt_state: HaltState,
    ) -> Action | None:
        """Enforce the 8-rule sequence. Returns None for silence."""

        # Rule 0: Halt check (HIGHEST PRIORITY per synthesis-v2 §P0-D ordering)
        if halt_state.is_halted(
            portfolio.account_id, portfolio.asset_class, signal.asset
        ):
            self._n_silenced_halt += 1
            return None

        # Rule 1: Drawdown circuit breaker
        if portfolio.drawdown_pct > self.config.max_drawdown_pct:
            self._n_silenced_drawdown += 1
            return Action(
                target_position_pct=0.0,
                reason=f"drawdown_circuit_breaker_{portfolio.drawdown_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=None,  # explicit resume only
            )

        # Rule 2: Daily-loss circuit breaker
        if portfolio.daily_loss_pct > self.config.max_daily_loss_pct:
            self._n_silenced_daily_loss += 1
            return Action(
                target_position_pct=0.0,
                reason=f"daily_loss_circuit_breaker_{portfolio.daily_loss_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=_next_session_open(market.tz, portfolio.asof),
            )

        # Rule 3: Silence on flat or zero-confidence signal
        if signal.direction == 0 or signal.confidence < 1e-6:
            self._n_silenced_flat += 1
            return None

        # Rule 4: Post-loss cooldown
        cooldown_key = (portfolio.account_id, portfolio.asset_class, signal.asset)
        cooldown = self._cooldowns.get(cooldown_key)
        if cooldown is not None and cooldown.last_loss_at is not None:
            elapsed_minutes = (
                portfolio.asof - cooldown.last_loss_at
            ).total_seconds() / 60.0
            if elapsed_minutes < self.config.cooldown_after_loss_minutes:
                self._n_silenced_cooldown += 1
                return None

        # Rule 5: Cost gate (synthesis-v2 §P0-A: uses expected_signed_edge)
        edge = expected_signed_edge(
            direction=signal.direction,
            probability=signal.confidence,
            magnitude=abs(signal.magnitude),
        )
        threshold = cost_gate_threshold(
            market_commission=market.commission,
            market_spread=market.spread,
            market_slippage=market.slippage_estimate,
            cost_multiple=self.config.cost_multiple,
        )
        if abs(edge) < threshold:
            self._n_silenced_cost_gate += 1
            return None

        # Rule 6: Position size from quarter-Kelly
        # variance = volatility² (volatility per ADR-0009 §P0-1 fix is stdev)
        variance = market.volatility ** 2
        target_size = quarter_kelly_size(
            edge=edge,
            variance=variance,
            quarter_kelly=self.config.quarter_kelly,
            max_position_pct=self.config.max_position_pct,
            action_step=self.config.action_step,
            direction=signal.direction,
        )

        # Rule 7: Minimum trade size guard (anti-churn)
        current = portfolio.current_position_pct(signal.asset)
        delta = target_size - current
        if abs(delta) < self.config.min_trade_size:
            self._n_silenced_min_trade += 1
            return None

        self._n_actions += 1
        return Action(
            target_position_pct=target_size,
            reason=(
                f"signal_dir={signal.direction}_conf={signal.confidence:.3f}_"
                f"edge={edge:.5f}_kelly_size={target_size:.3f}"
            ),
            signal_id=signal.metadata.get("id") if signal.metadata else None,
            halt=False,
        )

    def record_loss(
        self,
        account_id: str,
        asset_class: str,
        asset: str,
        loss_at: pd.Timestamp,
    ) -> None:
        """Settlement loop calls this on a realized loss to start cooldown."""
        key = (account_id, asset_class, asset)
        if key not in self._cooldowns:
            self._cooldowns[key] = _AssetCooldownState()
        self._cooldowns[key].last_loss_at = loss_at

    def stats(self) -> dict:
        return {
            "n_actions": self._n_actions,
            "n_silenced_halt": self._n_silenced_halt,
            "n_silenced_drawdown": self._n_silenced_drawdown,
            "n_silenced_daily_loss": self._n_silenced_daily_loss,
            "n_silenced_flat": self._n_silenced_flat,
            "n_silenced_cooldown": self._n_silenced_cooldown,
            "n_silenced_cost_gate": self._n_silenced_cost_gate,
            "n_silenced_min_trade": self._n_silenced_min_trade,
        }


def _next_session_open(tz: str, now: pd.Timestamp) -> pd.Timestamp:
    """Next session open per asset's tz. UTC (24/7 crypto) → 0000 next day.

    For equities, tz='America/New_York' would give 09:30 next session day;
    v0.1.1 simplification: just next-UTC-day for any non-UTC tz too.
    """
    # Crypto: next UTC day 0000
    if tz.upper() == "UTC":
        next_day = (now + pd.Timedelta(days=1)).normalize()
        return next_day
    # Equities (etc.): bump to next calendar day (simplification for v0.1.1;
    # v0.1.2 will use trading_calendars for proper session boundaries)
    next_day = (now + pd.Timedelta(days=1)).normalize()
    return next_day
