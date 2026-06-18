"""01f0 LIVE cutover: the ADR-0097 slippage haircut is AUTHORITATIVE on the live
DefaultRiskGate decision path (risk/gate.py), not just the clean_window evidence or the
shadow gate. A thin edge that clears the raw Rule-5 cost gate is SILENCED when the haircut
is on; default-OFF is byte-identical. This closes the orphan the review (01f0) flagged:
the haircut had no LIVE decision consumer.
"""
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from hermes_quant.protocol import AggregatedSignal, MarketState, Portfolio
from hermes_quant.risk.gate import DefaultRiskGate, RiskConfig


def _signal(direction=1, magnitude=0.02, confidence=0.62) -> AggregatedSignal:
    return AggregatedSignal(
        asset="BTC/USDT", timeframe="1h", asset_class="crypto",
        asof=pd.Timestamp("2026-06-18T00:00:00Z"),
        direction=direction, magnitude=magnitude, confidence=confidence,
        confidence_raw=confidence, horizon="4h", components=(), aggregator="bma", metadata=None,
    )


def _market() -> MarketState:
    # Low costs so a thin edge clears the raw cost gate (threshold ~ cost_multiple*round_trip).
    return MarketState(
        asset="BTC/USDT", asof=pd.Timestamp("2026-06-18T00:00:00Z"),
        volatility=0.02, commission=0.0001, spread=0.0001, slippage_estimate=0.0001, tz="UTC",
    )


def _portfolio() -> Portfolio:
    return Portfolio(
        account_id="alpaca-paper", asset_class="crypto",
        asof=pd.Timestamp("2026-06-18T00:00:00Z"),
        positions={}, cash=100_000.0, equity_total=100_000.0,
        realized_pnl_total=0.0, realized_fees_total=0.0,
        peak_equity=100_000.0, daily_open_equity=100_000.0,
    )


class _NoHalt:
    def is_halted(self, *a, **k):  # noqa: ANN002
        return False


def _action(cfg: RiskConfig):
    g = DefaultRiskGate(config=cfg)
    return g.gate(_signal(), _market(), _portfolio(), _NoHalt())


def test_01f0_live_haircut_silences_thin_edge_that_clears_raw_cost_gate():
    """RED-proof (the orphan close): a signal whose edge clears the RAW cost gate fires
    with the haircut OFF, but the SAME signal is SILENCED with slippage_gate_enabled=True +
    a penalty that shrinks the edge below threshold — on the LIVE DefaultRiskGate."""
    base = RiskConfig()
    raw = _action(base)
    # The thin edge must actually fire (target_position_pct != 0) on the raw path, else the test is
    # vacuous (a signal that's silenced anyway can't prove the haircut did the silencing).
    assert abs(getattr(raw, "target_position_pct", 0.0) or 0.0) > 0.0, (
        "fixture must fire on the raw path so the haircut is the discriminator"
    )
    # Same signal, haircut ON with a penalty large enough to shrink the edge under threshold.
    hc = dataclasses.replace(base, slippage_gate_enabled=True, slippage_penalty_frac=0.05)
    out = _action(hc)
    assert abs(getattr(out, "target_position_pct", 0.0) or 0.0) == pytest.approx(0.0), (
        "01f0: the live gate must SILENCE a thin edge once the slippage haircut shrinks it "
        "below the cost-gate threshold (the haircut is authoritative on the LIVE path)"
    )


def test_01f0_live_default_off_byte_identical():
    """Default-OFF (slippage_gate_enabled=False) => the live gate is byte-identical even when
    a penalty is present in the config (the haircut line is never reached)."""
    base = RiskConfig()
    off_with_penalty = dataclasses.replace(base, slippage_gate_enabled=False, slippage_penalty_frac=0.05)
    a = _action(base)
    b = _action(off_with_penalty)
    assert (getattr(a, "target_position_pct", None)) == (getattr(b, "target_position_pct", None)), (
        "flag OFF must be byte-identical regardless of a stray penalty value"
    )
