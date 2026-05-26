"""Regression tests for Phase-8 P0-A.3 calibration-quality gating.

Per docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md §P0-A.3: outcomes
constructed from a single fill's slippage formula carry the
`_calibration_quality = "slippage_only"` tag, and `dispatch_settlement`
MUST skip them so analyst Beta posteriors don't get corrupted with
non-directional data.

These tests pin the gate so a v0.1.2 mistake (e.g. removing the tag from
construct_realized_outcomes but forgetting to update construct_episode_outcomes
or vice versa, or removing the gate from dispatch_settlement) would fail
loudly.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.daemon.settlement_loop import (
    CALIBRATION_QUALITY_HORIZON_RETURN,
    CALIBRATION_QUALITY_SLIPPAGE_ONLY,
    construct_episode_outcomes,
    construct_realized_outcomes,
    dispatch_settlement,
)
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    EpisodeOutcome,
    RealizedOutcome,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_signal(sig_id: str = "sig-1", direction: int = 1) -> dict:
    return {
        "schema_version": 1,
        "id": sig_id,
        "asof": "2026-05-13T14:00:00.000000Z",
        "asset": "BTC/USDT",
        "exchange": "binance",
        "timeframe": "1h",
        "asset_class": "crypto",
        "direction": direction,
        "magnitude": 0.005,
        "confidence": 0.62,
        "confidence_raw": 0.70,
        "horizon": "4h",
        "decision_price": 50_000.0,
        "target_position_pct": 0.10,
        "reason": "edge=0.012",
        "halt": False,
        "halt_scope": None,
        "halt_until": None,
        "components": [
            {
                "analyst": "classical_ta",
                "direction": direction,
                "magnitude": 0.005,
                "confidence": 0.62,
                "confidence_raw": 0.70,
                "horizon": "4h",
            },
        ],
        "aggregator": "bma",
    }


def _make_exec(
    sig_id: str = "sig-1",
    side: str = "buy",
    fill_price: float = 50_500.0,
    decision_price: float = 50_000.0,
) -> dict:
    return {
        "schema_version": 1,
        "exec_id": "exec-1",
        "asof": "2026-05-13T14:01:00.000000Z",
        "asset": "BTC/USDT",
        "side": side,
        "qty": 0.01,
        "fill_price": fill_price,
        "decision_price": decision_price,
        "fees": 0.5,
        "account_id": "freqtrade",
        "asset_class": "crypto",
        "signal_id": sig_id,
    }


# ---------------------------------------------------------------------------
# construct_realized_outcomes tags every outcome
# ---------------------------------------------------------------------------


class TestRealizedOutcomeQualityTagging:
    def test_buy_fill_outcome_tagged_slippage_only(self):
        sig = _make_signal()
        exec_rec = _make_exec(side="buy", fill_price=50_500.0, decision_price=50_000.0)
        outcomes = construct_realized_outcomes([exec_rec], {sig["id"]: sig})

        assert len(outcomes) == 1
        # Phase-8 P0-A.3: the per-fill slippage formula doesn't yield a
        # horizon-return; the tag MUST be present so dispatch skips this.
        assert outcomes[0].view.metadata is not None
        assert (
            outcomes[0].view.metadata["_calibration_quality"] == CALIBRATION_QUALITY_SLIPPAGE_ONLY
        )

    def test_sell_fill_outcome_tagged_slippage_only(self):
        sig = _make_signal(direction=-1)
        exec_rec = _make_exec(side="sell", fill_price=49_500.0, decision_price=50_000.0)
        outcomes = construct_realized_outcomes([exec_rec], {sig["id"]: sig})

        assert len(outcomes) == 1
        assert (
            outcomes[0].view.metadata["_calibration_quality"] == CALIBRATION_QUALITY_SLIPPAGE_ONLY
        )

    def test_realized_return_is_per_fill_slippage_not_horizon_return(self):
        """Document that v0.1.1's `realized_return` is slippage."""
        sig = _make_signal()
        # Buy at 50_500, decision was 50_000. Slippage = 1% (paid 1% more).
        exec_rec = _make_exec(side="buy", fill_price=50_500.0, decision_price=50_000.0)
        outcomes = construct_realized_outcomes([exec_rec], {sig["id"]: sig})
        # The "realized_return" stored is slippage, NOT horizon return.
        # (fill_price - decision_price) / decision_price = 0.01
        assert outcomes[0].realized_return == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# construct_episode_outcomes tags AggregatedSignal.metadata
# ---------------------------------------------------------------------------


class TestEpisodeOutcomeQualityTagging:
    def test_episode_outcome_aggregated_signal_tagged(self):
        sig = _make_signal()
        exec_rec = _make_exec()
        episodes = construct_episode_outcomes([exec_rec], {sig["id"]: sig})

        assert len(episodes) == 1
        sig_id, episode = episodes[0]
        # The AggregatedSignal in EpisodeOutcome inherits the slippage-only
        # tag so dispatch_settlement's aggregator.update() guard skips it.
        agg_meta = episode.aggregated_signal.metadata or {}
        assert agg_meta.get("_calibration_quality") == CALIBRATION_QUALITY_SLIPPAGE_ONLY


# ---------------------------------------------------------------------------
# dispatch_settlement gates correctly
# ---------------------------------------------------------------------------


class _RecordingAnalyst:
    """Minimal analyst that records every update() call."""

    def __init__(self, name: str = "classical_ta"):
        self.name = name
        self.updates: list[RealizedOutcome] = []

    def emit(self, ctx):  # pragma: no cover — protocol satisfaction
        raise NotImplementedError

    def update(self, outcome: RealizedOutcome) -> None:
        self.updates.append(outcome)


class _RecordingAggregator:
    """Minimal aggregator that records every update() call."""

    def __init__(self):
        self.updates: list[EpisodeOutcome] = []

    def aggregate(self, views, ctx):  # pragma: no cover
        raise NotImplementedError

    def update(self, episode: EpisodeOutcome) -> None:
        self.updates.append(episode)


class TestDispatchGating:
    def test_slippage_only_outcomes_skip_analyst_update(self):
        """The whole point of P0-A.3: slippage-only RealizedOutcomes must NOT
        flow into analyst.update()."""
        sig = _make_signal()
        exec_rec = _make_exec()

        outcomes = construct_realized_outcomes([exec_rec], {sig["id"]: sig})
        episodes = construct_episode_outcomes([exec_rec], {sig["id"]: sig})

        analyst = _RecordingAnalyst()
        agg = _RecordingAggregator()
        stats = dispatch_settlement(
            outcomes,
            episodes,
            analysts_by_name={"classical_ta": analyst},
            aggregator=agg,
        )

        assert stats["n_realized"] == 1
        assert stats["n_episodes"] == 1
        # The whole point: ZERO updates dispatched.
        assert stats["n_analyst_updates"] == 0
        assert stats["n_aggregator_updates"] == 0
        # Both should be counted as skipped.
        assert stats["n_skipped_slippage_only"] == 2
        # Recording analyst/aggregator confirms.
        assert analyst.updates == []
        assert agg.updates == []

    def test_horizon_return_outcomes_DO_dispatch(self):
        """Forward-compat: when v0.1.2 lifts the gate by tagging outcomes
        with CALIBRATION_QUALITY_HORIZON_RETURN, dispatch MUST dispatch."""
        view = AnalystView(
            analyst="classical_ta",
            direction=1,
            magnitude=0.005,
            confidence=0.62,
            confidence_raw=0.70,
            horizon="4h",
            metadata={"_calibration_quality": CALIBRATION_QUALITY_HORIZON_RETURN},
        )
        outcome = RealizedOutcome(
            view=view,
            asof_view=pd.Timestamp("2026-05-13T14:00:00Z"),
            asof_settlement=pd.Timestamp("2026-05-13T18:00:00Z"),
            realized_return=0.012,
            direction_correct=True,
        )

        # Make a horizon-quality AggregatedSignal so the episode also
        # passes the aggregator gate.
        agg_signal = AggregatedSignal(
            asset="BTC/USDT",
            timeframe="1h",
            asset_class="crypto",
            asof=pd.Timestamp("2026-05-13T14:00:00Z"),
            direction=1,
            magnitude=0.005,
            confidence=0.62,
            confidence_raw=0.70,
            horizon="4h",
            components=(view,),
            aggregator="bma",
            metadata={"_calibration_quality": CALIBRATION_QUALITY_HORIZON_RETURN},
        )
        episode = EpisodeOutcome(
            asset="BTC/USDT",
            timeframe="1h",
            asof=pd.Timestamp("2026-05-13T14:00:00Z"),
            aggregated_signal=agg_signal,
            realized_returns={"4h": 0.012},
            direction_correct={"classical_ta": True},
            realized_net_pnl=0.0,
        )

        analyst = _RecordingAnalyst()
        agg = _RecordingAggregator()
        stats = dispatch_settlement(
            [outcome],
            [("sig-1", episode)],
            analysts_by_name={"classical_ta": analyst},
            aggregator=agg,
        )

        assert stats["n_analyst_updates"] == 1
        assert stats["n_aggregator_updates"] == 1
        assert stats["n_skipped_slippage_only"] == 0
        assert len(analyst.updates) == 1
        assert len(agg.updates) == 1

    def test_unknown_quality_tag_is_treated_as_skip_safe(self):
        """Defensive: outcomes WITHOUT the slippage_only tag (e.g. None
        metadata) are NOT auto-skipped — they fall through to the regular
        analyst lookup. This preserves backward-compat for any code path
        constructing outcomes without explicit metadata."""
        view = AnalystView(
            analyst="classical_ta",
            direction=1,
            magnitude=0.005,
            confidence=0.62,
            confidence_raw=0.70,
            horizon="4h",
            metadata=None,
        )
        outcome = RealizedOutcome(
            view=view,
            asof_view=pd.Timestamp("2026-05-13T14:00:00Z"),
            asof_settlement=pd.Timestamp("2026-05-13T18:00:00Z"),
            realized_return=0.012,
            direction_correct=True,
        )

        analyst = _RecordingAnalyst()
        agg = _RecordingAggregator()
        stats = dispatch_settlement(
            [outcome],
            [],
            analysts_by_name={"classical_ta": analyst},
            aggregator=agg,
        )
        # No tag = no slippage_only skip, dispatched normally.
        assert stats["n_skipped_slippage_only"] == 0
        assert stats["n_analyst_updates"] == 1
