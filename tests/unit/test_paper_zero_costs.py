"""Unit tests for the paper-mode-only cost-gate override.

Covers `RiskConfig.paper_zero_costs` (added per
docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md) and its
interaction with the existing risk-gate rules:

  - The override forces the cost-gate threshold to 0.0 (the
    `cost_multiple × round_trip_cost` math is bypassed).
  - The edge-sign alignment guard (Rule 5a, Phase-8 P0-B) is NEVER
    bypassed; negatively-edged signals still silence.
  - With the override and a positive-edge signal at low confidence
    (which would fail the live cost gate), the gate now passes and
    target_position_pct is non-zero.
  - The default value is False (live-mode behavior unchanged).

Live mode is unaffected by these tests — the autonomous loop's
fail-closed guard against non-paper reactors is enforced separately
in `hermes_quant.autonomous._react`.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.protocol import (
    AggregatedSignal,
    MarketState,
    Portfolio,
    Position,
)
from hermes_quant.risk.gate import DefaultRiskGate, RiskConfig

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_risk_gate.py shape so the two suites stay aligned)
# ---------------------------------------------------------------------------


def _signal(
    direction: int = 1,
    magnitude: float = 0.02,
    confidence: float = 0.7,
    asset: str = "BTC/USDT",
) -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp("2026-05-26T00:00:00Z"),
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence,
        horizon="4h",
        components=(),
        aggregator="bma",
    )


def _market(
    *,
    volatility: float = 0.02,
    commission: float = 0.001,
    spread: float = 0.0008,
    slippage: float = 0.0012,
    tz: str = "UTC",
) -> MarketState:
    return MarketState(
        asset="BTC/USDT",
        asof=pd.Timestamp("2026-05-26T00:00:00Z"),
        volatility=volatility,
        commission=commission,
        spread=spread,
        slippage_estimate=slippage,
        tz=tz,
    )


def _portfolio(
    *,
    drawdown: float = 0.0,
    daily_loss: float = 0.0,
    current_position: float = 0.0,
    equity: float = 100_000.0,
    asset: str = "BTC/USDT",
    account_id: str = "alpaca-paper",
    asset_class: str = "crypto",
) -> Portfolio:
    peak = equity / max(1e-9, 1 - drawdown)
    daily_open = equity / max(1e-9, 1 - daily_loss)

    qty = current_position * equity / 100.0
    positions = {}
    if abs(current_position) > 0:
        positions[asset] = Position(
            asset=asset,
            qty=qty,
            avg_entry_price=100.0,
            mark_price=100.0,
            unrealized_pnl=0.0,
            realized_fees=0.0,
        )

    return Portfolio(
        account_id=account_id,
        asset_class=asset_class,
        asof=pd.Timestamp("2026-05-26T00:00:00Z"),
        positions=positions,
        cash=equity,
        equity_total=equity,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=peak,
        daily_open_equity=daily_open,
    )


@pytest.fixture()
def halt_state(tmp_path: Path) -> HaltStateSQLite:
    return HaltStateSQLite(
        db_path=tmp_path / "halts.db",
        mirror_path=tmp_path / "halts.json",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_paper_zero_costs_default_false():
    """RiskConfig() must default paper_zero_costs to False — silence-by-default
    on the live path is non-negotiable."""
    cfg = RiskConfig()
    assert cfg.paper_zero_costs is False


def test_paper_zero_costs_threshold_is_zero(halt_state):
    """With paper_zero_costs=True, the cost-gate threshold is 0.0 regardless
    of market.commission/spread/slippage. We verify this by emitting a tiny
    positive-edge signal whose |edge| would be FAR below the live cost-gate
    threshold (cost_multiple=10) but trivially > 0.0."""
    # Live-mode equivalent (control): identical inputs MUST silence at
    # this commission/spread/slippage level so the contrast is meaningful.
    live_cfg = RiskConfig(
        cost_multiple=10.0,
        min_trade_size=0.0,
        max_position_pct=0.20,
        paper_zero_costs=False,
    )
    g_live = DefaultRiskGate(live_cfg)
    live_action = g_live.gate(
        _signal(direction=1, magnitude=0.005, confidence=0.55),
        _market(volatility=0.02, commission=0.001, spread=0.0008, slippage=0.0012),
        _portfolio(),
        halt_state,
    )
    assert live_action is None, (
        "control: live mode must still silence this signal — "
        "if not, the test isn't actually exercising the override"
    )

    # Paper override path: same threshold-driving inputs, but
    # paper_zero_costs=True forces threshold→0.0, so the signal clears
    # the cost gate and a non-zero target emits.
    paper_cfg = RiskConfig(
        cost_multiple=10.0,  # would force live-mode silence; ignored here
        min_trade_size=0.0,
        max_position_pct=0.20,
        paper_zero_costs=True,
    )
    g_paper = DefaultRiskGate(paper_cfg)
    paper_action = g_paper.gate(
        _signal(direction=1, magnitude=0.005, confidence=0.55),
        _market(volatility=0.02, commission=0.001, spread=0.0008, slippage=0.0012),
        _portfolio(),
        halt_state,
    )
    assert paper_action is not None, (
        "paper_zero_costs=True must zero the cost-gate threshold so a "
        "tiny positive-edge signal clears the gate"
    )
    assert paper_action.target_position_pct != 0.0
    # And no cost-gate silence should have been recorded.
    assert g_paper._n_silenced_cost_gate == 0


def test_paper_zero_costs_still_silences_negative_edge(halt_state):
    """THE EDGE SIGN GUARD IS NEVER BYPASSED.

    A signal with calibrated probability < 0.5 in the requested direction
    has negative expected_signed_edge. Even with paper_zero_costs=True
    (threshold=0), the edge-sign alignment guard MUST silence it — we
    never hold a negative-edge position regardless of the cost regime.
    """
    cfg = RiskConfig(
        cost_multiple=0.5,  # irrelevant since override is on
        min_trade_size=0.0,
        max_position_pct=0.20,
        paper_zero_costs=True,
    )
    g = DefaultRiskGate(cfg)
    # confidence 0.40 with direction=+1 → expected_signed_edge < 0
    action = g.gate(
        _signal(direction=1, magnitude=0.10, confidence=0.40),
        _market(volatility=0.02, commission=0.0, spread=0.0, slippage=0.0),
        _portfolio(),
        halt_state,
    )
    assert action is None, (
        "negative-signed-edge signals MUST silence even with "
        "paper_zero_costs=True — the edge-sign guard is non-negotiable"
    )
    # Confirm it was the cost_gate_edge_sign rule that silenced it
    # (not, say, min_trade_size or some other rule).
    assert g._n_silenced_cost_gate >= 1


def test_paper_zero_costs_clears_at_low_confidence(halt_state):
    """With paper_zero_costs=True AND positive-edge AND prob=0.40 above
    the no-edge floor (here we use prob=0.55 with magnitude high enough
    to give a small positive signed edge), the gate now passes and
    target_position_pct is non-zero — exactly the cold-start unblocking
    intent of the override. Same configuration with paper_zero_costs=False
    silences (control)."""
    # Note: prob=0.40 with direction=+1 yields a NEGATIVE edge — that's
    # the previous test. To validate "low confidence still clears," use
    # prob=0.55 (above the 0.5 no-edge floor, below the live cost-gate
    # threshold for the configured commission/spread/slippage).
    common_market = _market(
        volatility=0.02,
        commission=0.0008,
        spread=0.0006,
        slippage=0.0008,
    )
    common_signal = _signal(direction=1, magnitude=0.01, confidence=0.55)

    # Control: live-mode silences this exact signal.
    live_cfg = RiskConfig(
        cost_multiple=2.0,
        min_trade_size=0.0,
        max_position_pct=0.20,
        paper_zero_costs=False,
    )
    g_live = DefaultRiskGate(live_cfg)
    live_action = g_live.gate(common_signal, common_market, _portfolio(), halt_state)
    assert live_action is None, (
        "control: live mode must silence this low-confidence signal "
        "(otherwise the override has nothing to unblock)"
    )

    # Paper override clears the same signal.
    paper_cfg = RiskConfig(
        cost_multiple=2.0,
        min_trade_size=0.0,
        max_position_pct=0.20,
        paper_zero_costs=True,
    )
    g_paper = DefaultRiskGate(paper_cfg)
    paper_action = g_paper.gate(common_signal, common_market, _portfolio(), halt_state)
    assert paper_action is not None, (
        "paper_zero_costs=True must let positive-edge low-confidence "
        "signals through (this is the unblocking intent)"
    )
    assert paper_action.target_position_pct != 0.0
    assert paper_action.halt is False
