"""Unit tests for the ADR-0084 pre-event REJECT/abstain guard (seed 743b).

Two surfaces, both DEFAULT-OFF and ADDITIVE (the deterministic gate stays the
final, immutable authority — this seed only ADDS a reject condition):

  * risk/gate.py: the `in_event_blackout` predicate + the Rule-3.5 silence wired
    into DefaultRiskGate, gated on HERMES_QUANT_EVENT_RISK=1.
  * risk/options_gate.py: the O8 earnings-proximity (long-premium IV-crush)
    check, gated on HERMES_QUANT_EVENT_RISK=1 + a non-None event_risk payload.

Deterministic, offline, no network, no LLM, no clock dependence (every asof is
explicit). Asserts: flag-OFF byte-identical; flag-ON within-window high-impact
=> REJECT; outside-window unaffected; de-risking never blocked; options
earnings-proximity flags a long-premium structure (premium sellers exempt).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
)
from hermes_quant.protocol import (
    AggregatedSignal,
    MarketState,
    Portfolio,
    Position,
)
from hermes_quant.risk.gate import (
    DefaultRiskGate,
    RiskConfig,
    in_event_blackout,
)
from hermes_quant.risk.options_gate import (
    OptionsRiskConfig,
    StructureBucket,
    _earnings_proximity_violation,
    options_gate,
)

ASOF = pd.Timestamp("2026-05-13T00:00:00Z")
ASOF_DT = datetime(2026, 5, 13, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/unit/test_risk_gate.py conventions)
# ---------------------------------------------------------------------------


def _signal(
    *,
    direction: int = 1,
    magnitude: float = 0.02,
    confidence: float = 0.7,
    asset: str = "BTC/USDT",
    asset_class: str = "crypto",
    metadata: dict | None = None,
) -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1h",
        asset_class=asset_class,
        asof=ASOF,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=0.85,
        horizon="4h",
        components=(),
        aggregator="bma",
        metadata=metadata,
    )


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


def _portfolio(*, current_position: float = 0.0, asset: str = "BTC/USDT") -> Portfolio:
    equity = 100_000.0
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


def _event_risk(
    *,
    kind: str = "fomc",
    impact: str = "high",
    offset: timedelta = timedelta(hours=12),
    symbol: str | None = None,
) -> dict:
    """Build a ctx.extras['event_risk']-shaped payload (e3de shape: a forward,
    outcome-free scheduled_for + kind + impact, already asof-filtered upstream)."""
    ev: dict = {
        "kind": kind,
        "impact": impact,
        "scheduled_for": (ASOF_DT + offset).isoformat(),
        "title": f"{kind} test",
        "source": "test",
    }
    if symbol is not None:
        ev["symbol"] = symbol
    return {"decision_asof": ASOF_DT.isoformat(), "events": [ev]}


# ---------------------------------------------------------------------------
# Pure predicate: in_event_blackout (gate.py)
# ---------------------------------------------------------------------------


class TestInEventBlackoutPredicate:
    def test_within_window_high_impact_blocks(self) -> None:
        ok, reason = in_event_blackout(
            _event_risk(offset=timedelta(hours=12)), asof=ASOF_DT, window_days=1.0
        )
        assert ok is True
        assert reason == "event_blackout_fomc_high_impact"

    def test_outside_window_does_not_block(self) -> None:
        ok, reason = in_event_blackout(
            _event_risk(offset=timedelta(days=5)), asof=ASOF_DT, window_days=1.0
        )
        assert ok is False
        assert reason is None

    def test_medium_impact_not_blocked(self) -> None:
        ok, _ = in_event_blackout(
            _event_risk(impact="medium", offset=timedelta(hours=2)),
            asof=ASOF_DT,
            window_days=1.0,
        )
        assert ok is False

    def test_past_event_not_blocked(self) -> None:
        # A schedule strictly in the past is not a PRE-event risk.
        ok, _ = in_event_blackout(
            _event_risk(offset=timedelta(hours=-2)), asof=ASOF_DT, window_days=1.0
        )
        assert ok is False

    def test_none_payload_no_block(self) -> None:
        assert in_event_blackout(None, asof=ASOF_DT, window_days=1.0) == (False, None)

    def test_empty_events_no_block(self) -> None:
        assert in_event_blackout({"events": []}, asof=ASOF_DT, window_days=1.0) == (
            False,
            None,
        )

    def test_malformed_schedule_never_fabricates_blackout(self) -> None:
        payload = {"events": [{"kind": "fomc", "impact": "high", "scheduled_for": "nope"}]}
        assert in_event_blackout(payload, asof=ASOF_DT, window_days=1.0) == (False, None)

    def test_boundary_exactly_at_horizon_blocks(self) -> None:
        ok, _ = in_event_blackout(
            _event_risk(offset=timedelta(days=1)), asof=ASOF_DT, window_days=1.0
        )
        assert ok is True

    def test_event_exactly_at_asof_blocks(self) -> None:
        ok, _ = in_event_blackout(
            _event_risk(offset=timedelta(0)), asof=ASOF_DT, window_days=1.0
        )
        assert ok is True


# ---------------------------------------------------------------------------
# DefaultRiskGate Rule-3.5: flag-OFF byte-identical / flag-ON reject
# ---------------------------------------------------------------------------


class TestGateEventGuardFlagOff:
    """Flag absent / "0" => the guard NEVER runs => byte-identical to today."""

    def test_flag_absent_event_risk_ignored(self, halt_state, monkeypatch) -> None:
        monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
        g = DefaultRiskGate()
        sig = _signal(metadata={"event_risk": _event_risk(offset=timedelta(hours=1))})
        action = g.gate(sig, _market(), _portfolio(), halt_state)
        # A normally-passing signal still passes — the in-window event is ignored.
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_flag_zero_event_risk_ignored(self, halt_state, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "0")
        g = DefaultRiskGate()
        sig = _signal(metadata={"event_risk": _event_risk(offset=timedelta(hours=1))})
        action = g.gate(sig, _market(), _portfolio(), halt_state)
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_flag_off_byte_identical_to_baseline(self, halt_state, monkeypatch) -> None:
        """The action emitted with an in-window event but flag OFF is identical
        to the action with NO event_risk at all — proving zero behavioral
        change when OFF (default-OFF byte-identical rail)."""
        monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
        g1 = DefaultRiskGate()
        with_event = g1.gate(
            _signal(metadata={"event_risk": _event_risk(offset=timedelta(hours=1))}),
            _market(),
            _portfolio(),
            halt_state,
        )
        g2 = DefaultRiskGate()
        without_event = g2.gate(_signal(), _market(), _portfolio(), halt_state)
        assert with_event is not None and without_event is not None
        assert with_event.target_position_pct == without_event.target_position_pct
        assert with_event.reason == without_event.reason
        assert with_event.halt == without_event.halt


class TestGateEventGuardFlagOn:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")

    def test_within_window_high_impact_rejects_fresh_open(self, halt_state) -> None:
        g = DefaultRiskGate()
        sig = _signal(metadata={"event_risk": _event_risk(offset=timedelta(hours=12))})
        # Fresh open (current_position 0): the high-impact in-window event silences.
        action = g.gate(sig, _market(), _portfolio(current_position=0.0), halt_state)
        assert action is None
        assert g.stats()["n_silenced_event_risk"] == 1

    def test_increasing_same_side_rejected(self, halt_state) -> None:
        g = DefaultRiskGate()
        sig = _signal(direction=1, metadata={"event_risk": _event_risk()})
        # Already long, signal wants to go further long => increasing => blocked.
        action = g.gate(sig, _market(), _portfolio(current_position=5.0), halt_state)
        assert action is None
        assert g.stats()["n_silenced_event_risk"] == 1

    def test_de_risking_opposite_side_never_blocked(self, halt_state) -> None:
        """ADR-0084 D-1: the guard NEVER blocks de-risking. A short signal while
        long (reducing toward flat) into an event must still be allowed."""
        g = DefaultRiskGate()
        sig = _signal(direction=-1, metadata={"event_risk": _event_risk()})
        g.gate(sig, _market(), _portfolio(current_position=10.0), halt_state)
        # Not silenced by the event guard (it may still pass/normal-size).
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_outside_window_unaffected(self, halt_state) -> None:
        g = DefaultRiskGate()
        sig = _signal(metadata={"event_risk": _event_risk(offset=timedelta(days=5))})
        action = g.gate(sig, _market(), _portfolio(), halt_state)
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_no_event_risk_metadata_unaffected(self, halt_state) -> None:
        g = DefaultRiskGate()
        action = g.gate(_signal(), _market(), _portfolio(), halt_state)
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_medium_impact_within_window_unaffected(self, halt_state) -> None:
        g = DefaultRiskGate()
        sig = _signal(metadata={"event_risk": _event_risk(impact="medium")})
        action = g.gate(sig, _market(), _portfolio(), halt_state)
        assert action is not None
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_halt_still_takes_priority_over_event_guard(self, halt_state) -> None:
        """The guard is ADDED, not reordered — Rule-0 halt still wins (its
        reason, not the event reason, is recorded)."""
        halt_state.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="manual")
        g = DefaultRiskGate()
        sig = _signal(metadata={"event_risk": _event_risk()})
        action = g.gate(sig, _market(), _portfolio(), halt_state)
        assert action is None
        assert g.stats()["n_silenced_halt"] == 1
        assert g.stats()["n_silenced_event_risk"] == 0

    def test_window_is_config_driven(self, halt_state) -> None:
        # A 7-day window catches an event 5 days out that the default 1-day misses.
        cfg = RiskConfig(event_risk_window_days=7.0)
        g = DefaultRiskGate(cfg)
        sig = _signal(metadata={"event_risk": _event_risk(offset=timedelta(days=5))})
        action = g.gate(sig, _market(), _portfolio(), halt_state)
        assert action is None
        assert g.stats()["n_silenced_event_risk"] == 1


# ---------------------------------------------------------------------------
# options_gate O8: earnings-proximity (long-premium IV-crush)
# ---------------------------------------------------------------------------


def _long_call(symbol: str, *, delta: float, theta: float, vega: float) -> OptionLeg:
    return OptionLeg(
        symbol=symbol,
        side="buy",
        position_intent="buy_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.01, theta=theta, vega=vega, rho=0.01
        ),
    )


def _short_call(symbol: str, *, delta: float, theta: float, vega: float) -> OptionLeg:
    return OptionLeg(
        symbol=symbol,
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.01, theta=theta, vega=vega, rho=0.01
        ),
    )


def _long_premium_debit_vertical() -> list[OptionLeg]:
    """A defined-risk debit vertical that is NET long-premium (net theta < 0 AND
    net vega > 0): the long leg carries richer vega than the short, so the
    structure is exposed to IV-crush. Admits cleanly through O1-O7."""
    return [
        _long_call("NVDA260612C00140000", delta=0.30, theta=-0.05, vega=0.08),
        _short_call("NVDA260612C00150000", delta=0.18, theta=0.02, vega=0.04),
    ]


def _earnings_payload(
    *, symbol: str | None = "NVDA", offset: timedelta = timedelta(days=2), impact: str = "high"
) -> dict:
    ev: dict = {
        "kind": "earnings",
        "impact": impact,
        "scheduled_for": (ASOF_DT + offset).isoformat(),
        "title": f"{symbol} earnings",
        "source": "test",
    }
    if symbol is not None:
        ev["symbol"] = symbol
    return {"decision_asof": ASOF_DT.isoformat(), "events": [ev]}


def _long_premium_kwargs(**over):
    kw = dict(
        strategy_kind="vertical_spread",
        underlying="NVDA",
        spot=150.0,
        nav=1_000_000.0,
        held_shares=0,
        options_buying_power=500_000.0,
        premium_received=0.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=OptionsRiskConfig(),
        strike=150.0,
        width=10.0,
        net_debit=2.0,
        premium_paid=2.0 * 100,
        min_dte=30,
    )
    kw.update(over)
    return kw


class TestO8PurePredicate:
    LONG_PREM = NetGreeks(delta=0.1, gamma=0.01, theta=-5.0, vega=12.0)
    SELLER = NetGreeks(delta=-0.1, gamma=-0.01, theta=5.0, vega=-12.0)

    def test_long_premium_into_earnings_flagged(self) -> None:
        reason = _earnings_proximity_violation(
            _earnings_payload(offset=timedelta(days=2)),
            self.LONG_PREM,
            underlying="NVDA",
            asof=ASOF_DT,
            dte_window=5,
        )
        assert reason == "earnings_proximity_iv_crush"

    def test_premium_seller_exempt(self) -> None:
        # Theta-collecting / vega-short structure HARVESTS the crush — never flagged.
        reason = _earnings_proximity_violation(
            _earnings_payload(),
            self.SELLER,
            underlying="NVDA",
            asof=ASOF_DT,
            dte_window=5,
        )
        assert reason is None

    def test_wrong_symbol_not_flagged(self) -> None:
        reason = _earnings_proximity_violation(
            _earnings_payload(symbol="AAPL"),
            self.LONG_PREM,
            underlying="NVDA",
            asof=ASOF_DT,
            dte_window=5,
        )
        assert reason is None

    def test_outside_dte_window_not_flagged(self) -> None:
        reason = _earnings_proximity_violation(
            _earnings_payload(offset=timedelta(days=20)),
            self.LONG_PREM,
            underlying="NVDA",
            asof=ASOF_DT,
            dte_window=5,
        )
        assert reason is None

    def test_none_payload_no_flag(self) -> None:
        assert (
            _earnings_proximity_violation(
                None, self.LONG_PREM, underlying="NVDA", asof=ASOF_DT, dte_window=5
            )
            is None
        )

    def test_non_earnings_event_ignored(self) -> None:
        macro = {"events": [{"kind": "fomc", "impact": "high",
                             "scheduled_for": (ASOF_DT + timedelta(days=1)).isoformat()}]}
        assert (
            _earnings_proximity_violation(
                macro, self.LONG_PREM, underlying="NVDA", asof=ASOF_DT, dte_window=5
            )
            is None
        )


class TestO8GateIntegration:
    """O8 wired into options_gate(): default-OFF byte-identical + flag-ON reject."""

    def test_event_risk_none_byte_identical(self, monkeypatch) -> None:
        # Even with the master flag ON, a None payload => O8 never runs.
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        res = options_gate(
            _long_premium_debit_vertical(),
            **_long_premium_kwargs(event_risk=None),
        )
        assert res.admitted is True
        assert res.bucket == StructureBucket.DEFINED_RISK

    def test_master_flag_off_byte_identical(self, monkeypatch) -> None:
        # Payload supplied but HERMES_QUANT_EVENT_RISK absent => O8 is a no-op.
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
        res = options_gate(
            _long_premium_debit_vertical(),
            **_long_premium_kwargs(
                event_risk=_earnings_payload(offset=timedelta(days=2)),
                decision_asof=ASOF_DT,
            ),
        )
        assert res.admitted is True

    def test_long_premium_into_earnings_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        res = options_gate(
            _long_premium_debit_vertical(),
            **_long_premium_kwargs(
                event_risk=_earnings_payload(offset=timedelta(days=2)),
                decision_asof=ASOF_DT,
            ),
        )
        assert res.admitted is False
        assert res.reason == "earnings_proximity_iv_crush"

    def test_long_premium_outside_window_admitted(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
        monkeypatch.setenv("HERMES_QUANT_EVENT_RISK", "1")
        res = options_gate(
            _long_premium_debit_vertical(),
            **_long_premium_kwargs(
                event_risk=_earnings_payload(offset=timedelta(days=30)),
                decision_asof=ASOF_DT,
            ),
        )
        assert res.admitted is True
