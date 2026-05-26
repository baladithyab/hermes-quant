"""Tests for daemon/tick_loop.py and daemon/settlement_loop.py.

Uses fake/mock data providers + analysts + aggregators to test the wiring
without requiring network or real entry points.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.settlement_loop import (
    construct_episode_outcomes,
    construct_realized_outcomes,
    dispatch_settlement,
    find_signals_for_executions,
)
from hermes_quant.daemon.signal_bus import (
    emit_signal_record,
)
from hermes_quant.daemon.tick_loop import (
    AssetTask,
    TickLoopState,
    compute_market_state,
    run_one_tick,
)
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    Portfolio,
)
from hermes_quant.risk.gate import DefaultRiskGate, RiskConfig


def _make_bars(n: int = 100, base: float = 100.0, drift: float = 0.001):
    """Build synthetic OHLCV bars with mild drift."""
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-05-13T00:00:00", periods=n, freq="1h")
    closes = base * np.cumprod(1 + drift + rng.normal(0, 0.01, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


def _make_provider(bars: pd.DataFrame):
    p = MagicMock()
    p.name = "mock"
    p.fetch_bars.return_value = bars
    return p


def _make_analyst(direction: int, mag: float = 0.02, conf: float = 0.7, name: str = "mock"):
    a = MagicMock()
    a.name = name
    a.analyze.return_value = AnalystView(
        analyst=name,
        direction=direction,
        magnitude=mag,
        confidence=conf,
        confidence_raw=conf + 0.2,
        horizon="4h",
    )
    return a


def _make_portfolio_factory(equity: float = 100_000.0):
    def portfolio_for(account_id: str, asset_class: str):
        return Portfolio(
            account_id=account_id,
            asset_class=asset_class,
            asof=pd.Timestamp.utcnow(),
            positions={},
            cash=equity,
            equity_total=equity,
            realized_pnl_total=0.0,
            realized_fees_total=0.0,
            peak_equity=equity,
            daily_open_equity=equity,
        )

    return portfolio_for


# ---------------------------------------------------------------------------
# compute_market_state
# ---------------------------------------------------------------------------


class TestComputeMarketState:
    def test_basic(self):
        bars = _make_bars(100)
        ms = compute_market_state(bars, asset="X", asof=pd.Timestamp.utcnow())
        assert ms.volatility > 0
        assert ms.commission > 0

    def test_short_history_uses_bootstrap_default(self):
        bars = _make_bars(5)
        ms = compute_market_state(bars, asset="X", asof=pd.Timestamp.utcnow())
        assert ms.volatility == 0.01

    def test_zero_returns_uses_bootstrap(self):
        ts = pd.date_range("2026-05-13", periods=50, freq="1h")
        bars = pd.DataFrame(
            {
                "timestamp": ts,
                "open": [100.0] * 50,
                "high": [100.0] * 50,
                "low": [100.0] * 50,
                "close": [100.0] * 50,
                "volume": [1000.0] * 50,
            }
        )
        ms = compute_market_state(bars, asset="X", asof=pd.Timestamp.utcnow())
        # Zero stdev → bootstrap default
        assert ms.volatility == 0.01


# ---------------------------------------------------------------------------
# run_one_tick
# ---------------------------------------------------------------------------


class TestRunOneTick:
    def test_emits_signal_when_aligned(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        bars = _make_bars(100, drift=0.002)  # mild uptrend
        provider = _make_provider(bars)
        analysts = [_make_analyst(1, name="a"), _make_analyst(1, name="b")]
        agg = BMAAggregator()
        # ADR-0009 §P0-2 amendment 2026-05-26: cold-start now caps at ~0.375,
        # which floors signed-edge below the cost-gate threshold for typical
        # synthetic bars. Force IdentityCalibrator so the test exercises the
        # tick-emission path (the calibrator behavior is tested separately
        # in test_calibrators.py and test_classical_ta.py).
        from hermes_quant.calibrators import IdentityCalibrator

        agg.calibrator = IdentityCalibrator()
        gate = DefaultRiskGate(
            RiskConfig(
                cost_multiple=0.5,
                min_trade_size=0.0,
                cooldown_after_loss_minutes=0,
                max_position_pct=0.20,
            )
        )
        halt_state = HaltStateSQLite(
            db_path=tmp_path / "halts.db",
            mirror_path=tmp_path / "halt.json",
        )
        state = TickLoopState()

        n = run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h", exchange="binance")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=_make_portfolio_factory(),
            state=state,
            bus_path=bus,
        )
        assert n >= 1
        assert state.n_ticks == 1
        # Bus has the signal
        lines = bus.read_text().splitlines()
        assert len(lines) >= 1
        rec = json.loads(lines[-1])
        assert rec["asset"] == "BTC/USDT"
        assert rec["direction"] in (-1, 1)

    def test_silent_when_analysts_disagree(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        bars = _make_bars(100)
        provider = _make_provider(bars)
        analysts = [_make_analyst(1, name="a"), _make_analyst(-1, name="b")]
        agg = BMAAggregator()
        gate = DefaultRiskGate(RiskConfig(cost_multiple=0.5, min_trade_size=0.0))
        halt_state = HaltStateSQLite(
            db_path=tmp_path / "halts.db",
            mirror_path=tmp_path / "halt.json",
        )
        state = TickLoopState()
        n = run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=_make_portfolio_factory(),
            state=state,
            bus_path=bus,
        )
        # Disagreement → flat → silent
        assert n == 0

    def test_data_quality_error_skips_asset(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        # Bars too short to validate
        bars = _make_bars(1)
        provider = _make_provider(bars)
        analysts = [_make_analyst(1)]
        agg = BMAAggregator()
        gate = DefaultRiskGate()
        halt_state = HaltStateSQLite(
            db_path=tmp_path / "halts.db",
            mirror_path=tmp_path / "halt.json",
        )
        state = TickLoopState()
        n = run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=_make_portfolio_factory(),
            state=state,
            bus_path=bus,
        )
        assert n == 0
        assert state.n_errors >= 1

    # Phase-8 P0-C regression (synthesis 2026-05-13): when the gate's
    # circuit breakers emit an Action(halt=True), the tick loop MUST install
    # the durable halt in the SQLite registry. Without this, the halt is
    # only announced on the bus and lost on daemon restart / next tick.
    def test_drawdown_circuit_breaker_installs_durable_halt(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        bars = _make_bars(100, drift=0.002)
        provider = _make_provider(bars)
        analysts = [_make_analyst(1)]
        agg = BMAAggregator()
        # Tight drawdown limit: 5% drawdown will trip
        gate = DefaultRiskGate(
            RiskConfig(
                max_drawdown_pct=0.05,
                cost_multiple=0.5,
                min_trade_size=0.0,
                cooldown_after_loss_minutes=0,
            )
        )
        halt_state = HaltStateSQLite(
            db_path=tmp_path / "halts.db",
            mirror_path=tmp_path / "halt.json",
        )

        # Build a portfolio with peak=100k, equity=90k → 10% drawdown.
        def portfolio_for_in_drawdown(account_id: str, asset_class: str):
            return Portfolio(
                account_id=account_id,
                asset_class=asset_class,
                asof=pd.Timestamp.utcnow(),
                positions={},
                cash=90_000.0,
                equity_total=90_000.0,
                realized_pnl_total=-10_000.0,
                realized_fees_total=0.0,
                peak_equity=100_000.0,  # 10% drawdown
                daily_open_equity=100_000.0,
            )

        state = TickLoopState()
        n = run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h", exchange="binance")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=portfolio_for_in_drawdown,
            state=state,
            bus_path=bus,
        )

        # The bus should have a halt-flagged signal record
        assert n >= 1
        lines = bus.read_text().splitlines()
        rec = json.loads(lines[-1])
        assert rec["halt"] is True
        # Phase-8 P0-C: the halt MUST also be in the durable registry now.
        # Without this fix, halt_state.is_halted() would return False even
        # though the bus carries halt=True.
        assert halt_state.is_halted(
            account_id="default",
            asset_class="crypto",
            asset="BTC/USDT",
        ), (
            "drawdown circuit breaker emitted Action(halt=True) but "
            "halt_state.add_halt was never called — Phase-8 P0-C regression"
        )

    def test_drawdown_halt_install_is_idempotent_across_ticks(self, tmp_path):
        """Re-running a tick that re-trips the same drawdown circuit breaker
        must NOT crash on the duplicate-halt ValueError."""
        bus = tmp_path / "signals.jsonl"
        bars = _make_bars(100, drift=0.002)
        provider = _make_provider(bars)
        analysts = [_make_analyst(1)]
        agg = BMAAggregator()
        gate = DefaultRiskGate(
            RiskConfig(
                max_drawdown_pct=0.05,
                cost_multiple=0.5,
                min_trade_size=0.0,
                cooldown_after_loss_minutes=0,
            )
        )
        halt_state = HaltStateSQLite(
            db_path=tmp_path / "halts.db",
            mirror_path=tmp_path / "halt.json",
        )

        def portfolio_in_drawdown(account_id: str, asset_class: str):
            return Portfolio(
                account_id=account_id,
                asset_class=asset_class,
                asof=pd.Timestamp.utcnow(),
                positions={},
                cash=90_000.0,
                equity_total=90_000.0,
                realized_pnl_total=-10_000.0,
                realized_fees_total=0.0,
                peak_equity=100_000.0,
                daily_open_equity=100_000.0,
            )

        state = TickLoopState()
        # First tick: installs halt
        run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=portfolio_in_drawdown,
            state=state,
            bus_path=bus,
        )
        # Second tick: gate re-fires (drawdown still > limit), but the
        # halt is already installed. The catch-ValueError-and-pass guard
        # in tick_loop must absorb the duplicate.
        n_errors_before = state.n_errors
        run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=portfolio_in_drawdown,
            state=state,
            bus_path=bus,
        )
        # No new errors: idempotent
        assert state.n_errors == n_errors_before


# ---------------------------------------------------------------------------
# settlement_loop
# ---------------------------------------------------------------------------


class TestSettlementLoop:
    def test_find_signals_for_executions_match(self, tmp_path):
        sig_bus = tmp_path / "signals.jsonl"
        emit_signal_record(
            {
                "schema_version": 1,
                "id": "sig-1",
                "asof": "2026-05-13T00:00:00Z",
                "asset": "BTC/USDT",
                "timeframe": "1h",
                "direction": 1,
                "components": [],
            },
            path=sig_bus,
        )
        executions = [{"signal_id": "sig-1", "side": "buy"}, {"signal_id": "sig-99", "side": "buy"}]
        out = find_signals_for_executions(
            executions,
            signal_bus_path=sig_bus,
            n_signal_records=100,
        )
        assert "sig-1" in out
        assert "sig-99" not in out

    def test_construct_realized_outcomes(self):
        signals = {
            "sig-1": {
                "id": "sig-1",
                "asof": "2026-05-13T00:00:00Z",
                "direction": 1,
                "horizon": "1h",
                "components": [
                    {
                        "analyst": "a",
                        "direction": 1,
                        "magnitude": 0.01,
                        "confidence": 0.7,
                        "confidence_raw": 0.85,
                        "horizon": "1h",
                    },
                ],
            },
        }
        executions = [
            {
                "signal_id": "sig-1",
                "side": "buy",
                "decision_price": 100.0,
                "fill_price": 102.0,  # +2% gain
                "asof": "2026-05-13T01:00:00Z",
            },
        ]
        outcomes = construct_realized_outcomes(executions, signals)
        assert len(outcomes) == 1
        assert outcomes[0].direction_correct is True  # long signal, +2%
        assert outcomes[0].realized_return == pytest.approx(0.02)

    def test_construct_episode_outcomes(self):
        signals = {
            "sig-1": {
                "id": "sig-1",
                "asof": "2026-05-13T00:00:00Z",
                "asset": "BTC/USDT",
                "timeframe": "1h",
                "asset_class": "crypto",
                "direction": 1,
                "magnitude": 0.02,
                "confidence": 0.7,
                "confidence_raw": 0.85,
                "horizon": "4h",
                "aggregator": "bma",
                "components": [
                    {
                        "analyst": "a",
                        "direction": 1,
                        "magnitude": 0.02,
                        "confidence": 0.7,
                        "confidence_raw": 0.85,
                        "horizon": "4h",
                    },
                    {
                        "analyst": "b",
                        "direction": 1,
                        "magnitude": 0.02,
                        "confidence": 0.6,
                        "confidence_raw": 0.75,
                        "horizon": "4h",
                    },
                ],
            },
        }
        executions = [
            {
                "signal_id": "sig-1",
                "side": "buy",
                "decision_price": 100.0,
                "fill_price": 98.0,  # -2% (long signal was wrong)
                "asof": "2026-05-13T01:00:00Z",
            },
        ]
        episodes = construct_episode_outcomes(executions, signals)
        assert len(episodes) == 1
        sig_id, episode = episodes[0]
        assert sig_id == "sig-1"
        assert episode.direction_correct["a"] is False
        assert episode.direction_correct["b"] is False

    def test_dispatch_settlement_calls_updates(self):
        from unittest.mock import MagicMock

        # Build a fake stateful analyst
        analyst = MagicMock()
        analyst.update = MagicMock()
        agg = MagicMock()
        agg.update = MagicMock()

        view = AnalystView(
            analyst="a",
            direction=1,
            magnitude=0.01,
            confidence=0.7,
            confidence_raw=0.85,
            horizon="1h",
        )
        from hermes_quant.protocol import RealizedOutcome

        outcome = RealizedOutcome(
            view=view,
            asof_view=pd.Timestamp("2026-05-13"),
            asof_settlement=pd.Timestamp("2026-05-13T01:00:00Z"),
            realized_return=0.01,
            direction_correct=True,
        )
        sig = AggregatedSignal(
            asset="BTC/USDT",
            timeframe="1h",
            asset_class="crypto",
            asof=pd.Timestamp("2026-05-13"),
            direction=1,
            magnitude=0.01,
            confidence=0.7,
            confidence_raw=0.85,
            horizon="1h",
            components=(view,),
            aggregator="bma",
        )
        from hermes_quant.protocol import EpisodeOutcome

        episode = EpisodeOutcome(
            asset="BTC/USDT",
            timeframe="1h",
            asof=pd.Timestamp("2026-05-13"),
            aggregated_signal=sig,
            realized_returns={"1h": 0.01},
            direction_correct={"a": True},
        )

        stats = dispatch_settlement(
            [outcome],
            [("sig-1", episode)],
            analysts_by_name={"a": analyst},
            aggregator=agg,
        )
        assert stats["n_analyst_updates"] == 1
        assert stats["n_aggregator_updates"] == 1
        analyst.update.assert_called_once()
        agg.update.assert_called_once()
