"""quantcore.gate — the deterministic risk gate. FINAL AUTHORITY (rail #2).

Port of hermes_quant.risk.gate.DefaultRiskGate (ADR-0004 + ADR-0009 §P0-5 +
ADR-0084), datetime-native, ledger-audited. Rule sequence (highest first):

  Rule 0:   halt check                        -> silence
  Rule 0.5: committee-degeneracy check        -> silence (cowork addition:
            fewer than min_distinct_analysts views => no single-voice trades)
  Rule 1:   drawdown circuit breaker          -> flatten + durable halt
  Rule 2:   daily-loss circuit breaker        -> flatten + halt until next session
  Rule 3:   flat / zero-confidence signal     -> silence
  Rule 3.5: pre-event blackout (ADR-0084; config-ON, opening/increasing only)
  Rule 4:   post-loss cooldown                -> silence
  Rule 5:   cost gate + edge-sign alignment   -> silence
  Rule 6:   quarter-Kelly size, clipped + snapped to the ladder
  Rule 6.5: portfolio caps (ADR-0087: gross exposure + concurrent positions)
            -> silence. REJECT-ONLY at the single seam: never resizes the
            target, and de-risking is never blocked.
  Rule 7:   minimum-trade-size guard          -> silence

The gate NEVER raises on bad portfolio state: non-finite NAV/drawdown fails
closed (flatten + halt). It returns a GateDecision in all cases — callers
append it to the ledger for the audit trail.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
UTC = timezone.utc
from typing import Any, Literal

from pydantic import BaseModel

from quantcore.config import RiskConfig
from quantcore.kelly import (
    cost_gate_threshold,
    expected_signed_edge,
    quarter_kelly_size,
)
from quantcore.schemas import CommitteeSignal, MarketCosts, PortfolioState


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _parse_event_ts(s: Any) -> datetime | None:
    """Coerce an event timestamp to tz-aware UTC. None on ANY failure —
    malformed data can never fabricate a blackout (ADR-0084)."""
    try:
        if isinstance(s, datetime):
            dt = s
        elif isinstance(s, str):
            v = s.strip()
            if not v:
                return None
            dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
        else:
            return None
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def in_event_blackout(
    events: list[dict] | None,
    *,
    asof: datetime,
    window_days: float,
    high_impact_only: bool = True,
) -> tuple[bool, str | None]:
    """Pure predicate: is asof inside a pre-event blackout window?

    Fires iff some event is high-impact, scheduled_for >= asof, and
    scheduled_for - asof <= window_days. Missing/malformed => never fires.
    """
    if not events:
        return False, None
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    horizon = asof + timedelta(days=window_days)
    for ev in events:
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").strip().lower()
        if high_impact_only and impact != "high":
            continue
        scheduled = _parse_event_ts(ev.get("scheduled_for"))
        if scheduled is None or scheduled < asof:
            continue
        if scheduled <= horizon:
            kind = str(ev.get("kind") or "event").strip().lower() or "event"
            return True, f"event_blackout_{kind}_high_impact"
    return False, None


class GateDecision(BaseModel):
    """Always returned; always ledger-appended. verdict='silence' is the default."""

    verdict: Literal["action", "silence", "flatten_halt"]
    rule: str
    reason: str
    target_position_pct: float = 0.0
    current_position_pct: float = 0.0
    edge: float | None = None
    cost_threshold: float | None = None
    halt: bool = False
    halt_until: datetime | None = None
    asof: datetime


class RiskGate:
    """Deterministic gate. Stateless across calls except via PortfolioState
    (cooldown/halt live in the ledger-reconstructed portfolio, not in-memory —
    Cowork sessions are ephemeral, so ALL state must be on disk)."""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig.conservative()

    def gate(
        self,
        signal: CommitteeSignal,
        costs: MarketCosts,
        portfolio: PortfolioState,
    ) -> GateDecision:
        cfg = self.config
        asof = signal.asof_decision

        def silence(rule: str, reason: str, **kw: Any) -> GateDecision:
            return GateDecision(
                verdict="silence", rule=rule, reason=reason, asof=asof, **kw
            )

        def flatten(rule: str, reason: str, halt_until: datetime | None = None) -> GateDecision:
            return GateDecision(
                verdict="flatten_halt",
                rule=rule,
                reason=reason,
                target_position_pct=0.0,
                halt=True,
                halt_until=halt_until,
                asof=asof,
            )

        # Rule 0: halt check (highest priority)
        if portfolio.halted:
            if portfolio.halt_until is None or asof < portfolio.halt_until:
                return silence("rule0_halt", f"halt_active_{portfolio.halt_reason or 'unspecified'}")

        # Rule 0.5: committee degeneracy — no single-voice trades
        if signal.n_distinct_analysts < cfg.min_distinct_analysts:
            return silence(
                "rule0_5_degenerate_committee",
                f"n_distinct_analysts={signal.n_distinct_analysts}<"
                f"{cfg.min_distinct_analysts}",
            )

        # Non-finite portfolio state fails CLOSED
        if not _is_finite(portfolio.nav) or not _is_finite(portfolio.drawdown_pct) or not _is_finite(
            portfolio.daily_loss_pct
        ):
            return flatten("rule_nonfinite", "non_finite_portfolio_state")

        # Rule 1: drawdown circuit breaker (durable halt — explicit resume only)
        if portfolio.drawdown_pct > cfg.max_drawdown_pct:
            return flatten(
                "rule1_drawdown",
                f"drawdown_circuit_breaker_{portfolio.drawdown_pct:.4f}",
            )

        # Rule 2: daily-loss circuit breaker (halt until next session)
        if portfolio.daily_loss_pct > cfg.max_daily_loss_pct:
            return flatten(
                "rule2_daily_loss",
                f"daily_loss_circuit_breaker_{portfolio.daily_loss_pct:.4f}",
                halt_until=_next_session_open(costs.tz, asof),
            )

        # Rule 3: flat or zero-confidence
        if signal.direction == 0 or signal.confidence < 1e-6:
            return silence("rule3_flat", "flat_or_zero_confidence")

        current = portfolio.current_position_pct(signal.asset)
        if not _is_finite(current):
            return flatten("rule_nonfinite", "non_finite_position_state")

        # Rule 3.5: pre-event blackout (opening/increasing only; never blocks de-risking)
        if cfg.event_risk_enabled:
            is_opening_or_increasing = signal.direction * current >= 0
            if is_opening_or_increasing:
                blackout, reason = in_event_blackout(
                    signal.event_risk,
                    asof=asof,
                    window_days=cfg.event_risk_window_days,
                )
                if blackout:
                    return silence("rule3_5_event_blackout", reason or "event_blackout")

        # Rule 4: post-loss cooldown
        if portfolio.last_loss_at is not None:
            elapsed_min = (asof - portfolio.last_loss_at).total_seconds() / 60.0
            if elapsed_min < cfg.cooldown_after_loss_minutes:
                return silence("rule4_cooldown", "post_loss_cooldown")

        # Rule 5: cost gate (same edge feeds the sizer — single source of truth)
        edge = expected_signed_edge(
            direction=signal.direction,
            probability=signal.confidence,
            magnitude=abs(signal.magnitude),
        )
        if not all(
            _is_finite(v)
            for v in (edge, costs.commission, costs.spread, costs.slippage_estimate, costs.volatility)
        ):
            return silence("rule5_cost_gate", "non_finite_risk_input")
        threshold = (
            0.0
            if cfg.paper_zero_costs
            else cost_gate_threshold(
                market_commission=costs.commission,
                market_spread=costs.spread,
                market_slippage=costs.slippage_estimate,
                cost_multiple=cfg.cost_multiple,
            )
        )
        # Edge-sign alignment guard — NEVER bypassed, even with paper_zero_costs
        if edge * signal.direction <= 0:
            return silence("rule5_cost_gate", "cost_gate_edge_sign", edge=edge)
        if abs(edge) < threshold:
            return silence(
                "rule5_cost_gate", "cost_gate_below_threshold", edge=edge, cost_threshold=threshold
            )

        # Rule 6: quarter-Kelly size (variance = stdev^2)
        variance = float(costs.volatility) ** 2
        if not _is_finite(variance):
            return silence("rule5_cost_gate", "non_finite_risk_input")
        target = quarter_kelly_size(
            edge=edge,
            variance=variance,
            quarter_kelly=cfg.quarter_kelly,
            max_position_pct=cfg.max_position_pct,
            action_step=cfg.action_step,
            direction=signal.direction,
        )

        # Rule 6.5: portfolio caps (ADR-0087) — cap at the single seam,
        # REJECT-ONLY. The gate never silently resizes the target: a capped
        # trade is rejected outright so the human sees why. De-risking
        # (|target| < |current|, i.e. the target moves toward 0) is NEVER
        # blocked — the rule is skipped entirely.
        is_derisking = abs(target) < abs(current) - 1e-12
        if not is_derisking:
            net_by_asset: dict[str, float] = {}
            for pos in portfolio.positions:
                net_by_asset[pos.asset] = net_by_asset.get(pos.asset, 0.0) + pos.position_pct
            others_gross = sum(
                abs(v) for a, v in net_by_asset.items() if a != signal.asset
            )
            prospective_gross = others_gross + abs(target)
            if prospective_gross > cfg.max_gross_exposure_pct + 1e-12:
                return silence(
                    "rule6_5_portfolio_caps",
                    f"gross_exposure_cap_{prospective_gross:.4f}",
                    target_position_pct=target,
                    current_position_pct=current,
                )
            if abs(current) < 1e-12:  # opening a NEW asset
                n_open_others = sum(
                    1
                    for a, v in net_by_asset.items()
                    if a != signal.asset and abs(v) > 1e-12
                )
                if n_open_others >= cfg.max_concurrent_positions:
                    return silence(
                        "rule6_5_portfolio_caps",
                        "max_concurrent_positions",
                        target_position_pct=target,
                        current_position_pct=current,
                    )

        # Rule 7: minimum trade size (anti-churn)
        delta = target - current
        if abs(delta) < cfg.min_trade_size:
            return silence("rule7_min_trade", "min_trade_size", target_position_pct=target)

        return GateDecision(
            verdict="action",
            rule="rule6_kelly",
            reason=(
                f"signal_dir={signal.direction}_conf={signal.confidence:.3f}_"
                f"edge={edge:.5f}_kelly_size={target:.3f}"
            ),
            target_position_pct=target,
            current_position_pct=current,
            edge=edge,
            cost_threshold=threshold,
            asof=asof,
        )


def _next_session_open(tz: str, now: datetime) -> datetime:
    """Crypto (UTC): next UTC midnight. Sessioned markets: now + 24h
    (avoids the after-hours auto-clear re-trip window — hermes-quant P1-delta)."""
    if tz.upper() == "UTC":
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return nxt
    return now + timedelta(days=1)
