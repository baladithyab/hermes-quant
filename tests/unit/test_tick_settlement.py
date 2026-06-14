"""Tests for daemon/settlement_loop.py.

Vestigial-daemon-spine deletion: this file originally also covered
``daemon/tick_loop.py`` (compute_market_state + run_one_tick). The
documented daemon → signals.jsonl → freqtrade spine that tick_loop drove is
vestigial — the live spine is cron scripts that call advisor.recommend +
reactors directly — so ``daemon/tick_loop.py`` was removed along with its
TestComputeMarketState / TestRunOneTick coverage. The settlement_loop tests
below (the kill-switch realized-PnL basis) are KEPT.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.daemon.settlement_loop import (
    construct_episode_outcomes,
    construct_realized_outcomes,
    dispatch_settlement,
    find_signals_for_executions,
)
from hermes_quant.daemon.signal_bus import (
    emit_signal_record,
)
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
)

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
