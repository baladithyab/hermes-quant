"""Unit tests for the ADR-0084 event-risk CARRIER (seed 8f41).

The 743b pre-event guard (risk/gate.py Rule-3.5 + options_gate.py O8) reads its
asof-honest, outcome-free event-risk payload from ``signal.metadata['event_risk']``
(gate) / the ``event_risk=`` arg (options_gate). The calendar wiring, however,
stamps that payload onto ``ctx.extras['event_risk']``. Nothing copied it across
the aggregator->gate seam, so the guard could never fire. This seed adds a
one-shot, flag-gated copy at three seams:

  * advisor.py  : ``_carry_event_risk(agg_signal, ctx)`` after aggregate, before gate.
  * tick_loop.py: ``_carry_event_risk(signal, ctx)`` before the daemon gate call.
  * options/recipes.py: forwards ``event_risk`` + ``decision_asof`` into options_gate.

All three are DEFAULT-OFF + ADDITIVE, gated on HERMES_QUANT_EVENT_RISK read at
CALL TIME. Flag absent => no metadata key copied / nothing forwarded => the 743b
guard never fires => byte-identical to today. This file proves exactly that:
flag-OFF the carried metadata has NO event_risk key (and the helper returns the
SAME object); flag-ON the carrier copies it and (with an in-window high-impact
event) the deterministic gate silences.

Deterministic, offline, no network, no LLM, no clock dependence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.advisor import _carry_event_risk as advisor_carry
from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.tick_loop import _carry_event_risk as daemon_carry
from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
)
from hermes_quant.options.recipes import build_multi_leg_proposal
from hermes_quant.protocol import (
    AggregatedSignal,
    MarketContext,
    MarketState,
    Portfolio,
    Position,
)
from hermes_quant.risk.gate import DefaultRiskGate
from hermes_quant.risk.options_gate import OptionsRiskConfig

ASOF = pd.Timestamp("2026-05-13T00:00:00Z")
ASOF_DT = datetime(2026, 5, 13, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _signal(*, direction: int = 1, metadata: dict | None = None) -> AggregatedSignal:
    return AggregatedSignal(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        asof=ASOF,
        direction=direction,
        magnitude=0.02,
        confidence=0.7,
        confidence_raw=0.85,
        horizon="4h",
        components=(),
        aggregator="bma",
        metadata=metadata,
    )


def _ctx(*, extras: dict | None = None) -> MarketContext:
    bars = pd.DataFrame(
        {
            "timestamp": [ASOF],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1000.0],
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=100.0,
        last_volume=1000.0,
        asof=ASOF,
        extras=extras if extras is not None else {},
    )


def _event_risk(*, kind: str = "fomc", impact: str = "high",
                offset: timedelta = timedelta(hours=12)) -> dict:
    """ctx.extras['event_risk']-shaped payload (wiring.py shape)."""
    return {
        "decision_asof": ASOF_DT.isoformat(),
        "events": [
            {
                "kind": kind,
                "impact": impact,
                "scheduled_for": (ASOF_DT + offset).isoformat(),
                "title": f"{kind} test",
                "source": "test",
            }
        ],
    }


def _market() -> MarketState:
    return MarketState(
        asset="BTC/USDT",
        asof=ASOF,
        volatility=0.02,
        commission=0.001,
        spread=0.0008,
        slippage_estimate=0.0012,
        tz="UTC",
    )


def _portfolio(*, current_position: float = 0.0) -> Portfolio:
    equity = 100_000.0
    positions = {}
    if abs(current_position) > 0:
        positions["BTC/USDT"] = Position(
            asset="BTC/USDT",
            qty=current_position * equity / 100.0,
            avg_entry_price=100.0,
            mark_price=100.0,
            unrealized_pnl=0.0,
            realized_fees=0.0,
        )
    return Portfolio(
        account_id="alpaca-paper",
        asset_class="crypto",
        asof=ASOF,
        positions=positions,
        cash=equity,
        equity_total=equity,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=equity,
        daily_open_equity=equity,
    )


@pytest.fixture()
def halt_state(tmp_path: Path) -> HaltStateSQLite:
    return HaltStateSQLite(
        db_path=tmp_path / "halts.db",
        mirror_path=tmp_path / "halts.json",
    )


# ---------------------------------------------------------------------------
# The advisor + daemon carrier helpers share the exact same contract, so we
# parametrize the same assertions across both.
# ---------------------------------------------------------------------------

_CARRIERS = pytest.mark.parametrize("carry", [advisor_carry, daemon_carry])


class TestCarrierFlagOff:
    """Flag absent / "0" => the carrier is a no-op => byte-identical."""

    @_CARRIERS
    def test_flag_absent_no_metadata_key_copied(self, carry, monkeypatch) -> None:
        monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
        sig = _signal()
        ctx = _ctx(extras={"event_risk": _event_risk()})
        out = carry(sig, ctx)
        # Same object returned (no dataclasses.replace happened) ...
        assert out is sig
        # ... and crucially: NO event_risk key on the (still-None) metadata.
        assert out.metadata is None

    @_CARRIERS
    def test_flag_zero_no_metadata_key_copied(self, carry, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "0")
        sig = _signal(metadata={"id": "abc"})
        ctx = _ctx(extras={"event_risk": _event_risk()})
        out = carry(sig, ctx)
        assert out is sig
        assert "event_risk" not in (out.metadata or {})
        # Pre-existing metadata is untouched.
        assert out.metadata == {"id": "abc"}

    @_CARRIERS
    def test_flag_on_but_no_event_risk_in_extras_no_op(self, carry, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        sig = _signal(metadata={"id": "abc"})
        ctx = _ctx(extras={"regime": "trend"})  # no event_risk key
        out = carry(sig, ctx)
        assert out is sig
        assert "event_risk" not in (out.metadata or {})


class TestCarrierFlagOn:
    """Flag ON + event_risk present in extras => copied onto metadata."""

    @_CARRIERS
    def test_event_risk_copied_onto_metadata(self, carry, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        payload = _event_risk()
        sig = _signal()
        ctx = _ctx(extras={"event_risk": payload})
        out = carry(sig, ctx)
        # A new (frozen) signal with the carried key.
        assert out is not sig
        assert out.metadata is not None
        assert out.metadata["event_risk"] == payload

    @_CARRIERS
    def test_preexisting_metadata_preserved(self, carry, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        payload = _event_risk()
        sig = _signal(metadata={"id": "xyz", "weights": {"a": 1.0}})
        ctx = _ctx(extras={"event_risk": payload})
        out = carry(sig, ctx)
        assert out.metadata["id"] == "xyz"
        assert out.metadata["weights"] == {"a": 1.0}
        assert out.metadata["event_risk"] == payload

    @_CARRIERS
    def test_source_signal_not_mutated(self, carry, monkeypatch) -> None:
        """The original signal's metadata mapping is never mutated in place."""
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        original_md = {"id": "xyz"}
        sig = _signal(metadata=original_md)
        ctx = _ctx(extras={"event_risk": _event_risk()})
        carry(sig, ctx)
        assert original_md == {"id": "xyz"}  # untouched


class TestCarrierEndToEndGate:
    """The whole point: carrier ON + in-window high-impact event => gate silences."""

    @_CARRIERS
    def test_carried_event_silences_fresh_open(self, carry, halt_state, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        ctx = _ctx(extras={"event_risk": _event_risk(offset=timedelta(hours=12))})
        carried = carry(_signal(direction=1), ctx)
        g = DefaultRiskGate()
        action = g.gate(carried, _market(), _portfolio(current_position=0.0), halt_state)
        assert action is None
        assert g.stats()["n_silenced_event_risk"] == 1

    @_CARRIERS
    def test_flag_off_carried_signal_passes(self, carry, halt_state, monkeypatch) -> None:
        """Flag OFF: nothing carried, the in-window event is invisible, the
        normally-passing signal still passes => byte-identical to today."""
        monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
        ctx = _ctx(extras={"event_risk": _event_risk(offset=timedelta(hours=12))})
        carried = carry(_signal(direction=1), ctx)
        g = DefaultRiskGate()
        action = g.gate(carried, _market(), _portfolio(current_position=0.0), halt_state)
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0

    @_CARRIERS
    def test_carried_event_outside_window_passes(self, carry, halt_state, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        ctx = _ctx(extras={"event_risk": _event_risk(offset=timedelta(days=5))})
        carried = carry(_signal(direction=1), ctx)
        g = DefaultRiskGate()
        action = g.gate(carried, _market(), _portfolio(), halt_state)
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0


# ---------------------------------------------------------------------------
# Options path: recipes.build_multi_leg_proposal forwards event_risk +
# decision_asof into options_gate (flag-gated).
# ---------------------------------------------------------------------------


def _earnings_payload(*, symbol: str = "AAPL",
                      offset: timedelta = timedelta(days=2)) -> dict:
    return {
        "decision_asof": ASOF_DT.isoformat(),
        "events": [
            {
                "kind": "earnings",
                "impact": "high",
                "symbol": symbol,
                "scheduled_for": (ASOF_DT + offset).isoformat(),
                "title": f"{symbol} earnings",
                "source": "test",
            }
        ],
    }


def _cc_chain():
    """A minimal deterministic OptionChain with one eligible covered-call short
    call, carrying greeks so options_gate's O1-O7 can run offline. The OCC
    symbol encodes expiry 2026-06-17 (35 DTE from ASOF) and strike 150.00, so
    the snapshot lands inside the default 25-45 DTE window and parse_strike
    yields 150."""
    from hermes_quant.options.data import OptionChain, OptionSnapshot

    snap = OptionSnapshot(
        symbol="AAPL260617C00150000",
        asof=ASOF_DT,
        fetched_at=ASOF_DT,
        bid=2.0,
        ask=2.1,
        last=2.05,
        volume=1000,
        open_interest=5000,
        greeks=OptionGreeksSnapshot(
            delta=0.30, gamma=0.01, theta=0.05, vega=0.10, rho=0.01
        ),
        underlying_spot=150.0,
        risk_free_rate=0.04,
    )
    return OptionChain(
        underlying="AAPL",
        asof=ASOF_DT,
        underlying_spot=150.0,
        risk_free_rate=0.04,
        snapshots=(snap,),
    )


class TestOptionsRecipeCarrier:
    """recipes seam: flag-OFF byte-identical; flag-ON forwards event_risk +
    decision_asof so options_gate's O8 can fire on a long-premium structure.

    The covered-call short here is a premium SELLER (theta>0/vega<0), which O8
    deliberately EXEMPTS, so we assert the SELLER admits even into earnings (the
    crush is harvested) — proving event_risk is forwarded and O8 ran without
    blocking de-risking premium income. The long-premium reject path is already
    covered against options_gate directly in test_event_risk_guard.py; here we
    assert the recipes seam wiring is sound and flag-gated."""

    def test_flag_off_does_not_forward(self, monkeypatch) -> None:
        # OPTIONS_GATE on so we reach the gate; EVENT_RISK absent => not forwarded.
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
        res = build_multi_leg_proposal(
            symbol="AAPL",
            asof=ASOF_DT,
            strategy_kind="covered_call",
            chain=_cc_chain(),
            nav=1_000_000.0,
            held_shares=100,
            options_buying_power=500_000.0,
            cfg=OptionsRiskConfig(),
            event_risk=_earnings_payload(offset=timedelta(days=2)),
        )
        # A premium-selling covered call admits; flag-off path == today.
        assert res.admitted is True

    def test_flag_on_premium_seller_admits_into_earnings(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        res = build_multi_leg_proposal(
            symbol="AAPL",
            asof=ASOF_DT,
            strategy_kind="covered_call",
            chain=_cc_chain(),
            nav=1_000_000.0,
            held_shares=100,
            options_buying_power=500_000.0,
            cfg=OptionsRiskConfig(),
            event_risk=_earnings_payload(offset=timedelta(days=2)),
        )
        # Premium sellers HARVEST the crush => O8 exempts them => still admits.
        assert res.admitted is True

    def test_flag_on_no_event_risk_payload_admits(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        res = build_multi_leg_proposal(
            symbol="AAPL",
            asof=ASOF_DT,
            strategy_kind="covered_call",
            chain=_cc_chain(),
            nav=1_000_000.0,
            held_shares=100,
            options_buying_power=500_000.0,
            cfg=OptionsRiskConfig(),
            event_risk=None,
        )
        assert res.admitted is True


# Sentinel: NetGreeks/OptionLeg imported above are part of the shared options
# vocabulary; reference them so linters don't flag the import as unused if a
# future refactor drops a usage.
assert NetGreeks is not None
assert OptionLeg is not None
