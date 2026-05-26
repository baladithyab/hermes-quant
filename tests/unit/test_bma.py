"""Unit tests for hermes_quant.aggregators.bma — BMAAggregator."""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.protocol import (
    Aggregator,
    AnalystView,
    EpisodeOutcome,
    MarketContext,
)


def _ctx() -> MarketContext:
    ts = pd.date_range("2026-05-13", periods=2, freq="1h")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000.0, 1000.0],
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=101.5,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _view(
    name: str,
    direction: int,
    mag: float = 0.01,
    conf: float = 0.7,
    conf_raw: float = 0.85,
    horizon: str = "1h",
) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=mag,
        confidence=conf,
        confidence_raw=conf_raw,
        horizon=horizon,
    )


class TestProtocolContract:
    def test_satisfies_aggregator_protocol(self):
        a = BMAAggregator()
        assert isinstance(a, Aggregator)


class TestAggregate:
    def test_empty_views_returns_flat_signal(self):
        a = BMAAggregator()
        sig = a.aggregate([], _ctx())
        assert sig.direction == 0
        assert sig.magnitude == 0.0
        assert sig.confidence == 0.0
        assert sig.aggregator == "bma"

    def test_unanimous_long_returns_long(self):
        a = BMAAggregator()
        views = [
            _view("classical-ta", 1, mag=0.012, conf=0.7),
            _view("microstructure", 1, mag=0.008, conf=0.6),
            _view("kronos", 1, mag=0.015, conf=0.8),
        ]
        sig = a.aggregate(views, _ctx())
        assert sig.direction == 1
        assert sig.magnitude > 0
        # Pre-calibration: cold-start shrinkage of 0.20
        assert sig.confidence_raw > sig.confidence
        # Agreement bonus applied (all 3 agree)
        assert sig.confidence_raw > 0.5

    def test_unanimous_short_returns_short(self):
        a = BMAAggregator()
        views = [_view("a", -1, conf=0.7), _view("b", -1, conf=0.6)]
        sig = a.aggregate(views, _ctx())
        assert sig.direction == -1

    def test_majority_wins(self):
        a = BMAAggregator()
        views = [
            _view("a", 1, conf=0.8),
            _view("b", 1, conf=0.7),
            _view("c", -1, conf=0.6),
        ]
        sig = a.aggregate(views, _ctx())
        assert sig.direction == 1
        # Agreement bonus NOT applied (mixed); confidence_raw is just vote_share
        # vote_share = |0.8+0.7-0.6| / (0.8+0.7+0.6) = 0.9 / 2.1 ≈ 0.429
        assert 0.4 < sig.confidence_raw < 0.5

    def test_pure_disagreement_returns_flat(self):
        a = BMAAggregator()
        views = [
            _view("a", 1, conf=0.5),
            _view("b", -1, conf=0.5),
        ]
        sig = a.aggregate(views, _ctx())
        # Pre-calibration weights are uniform (0.5), so views perfectly cancel
        assert sig.direction == 0

    def test_components_preserved(self):
        """ADR-0009 §P1-10: components tuple required for joint-state replay."""
        a = BMAAggregator()
        views = [_view("a", 1), _view("b", 1)]
        sig = a.aggregate(views, _ctx())
        assert len(sig.components) == 2
        assert sig.components[0].analyst == "a"

    def test_metadata_includes_weights(self):
        a = BMAAggregator()
        views = [_view("a", 1, conf=0.7), _view("b", 1, conf=0.8)]
        sig = a.aggregate(views, _ctx())
        assert "weights" in sig.metadata
        assert sig.metadata["n_contributing"] == 2
        assert sig.metadata["n_views"] == 2


class TestUpdate:
    def test_update_increments_alpha_on_correct(self):
        a = BMAAggregator()
        views = [_view("a", 1)]
        sig = a.aggregate(views, _ctx())

        outcome = EpisodeOutcome(
            asset="BTC/USDT",
            timeframe="1h",
            asof=pd.Timestamp("2026-05-13"),
            aggregated_signal=sig,
            realized_returns={"1h": 0.01},
            direction_correct={"a": True},
        )
        a.update(outcome)
        stats = a._stats["a"]
        assert stats.alpha == pytest.approx(a.prior_alpha + 1.0)
        assert stats.beta == pytest.approx(a.prior_beta)
        assert stats.n_observations == 1

    def test_update_increments_beta_on_incorrect(self):
        a = BMAAggregator()
        views = [_view("a", 1)]
        sig = a.aggregate(views, _ctx())
        outcome = EpisodeOutcome(
            asset="BTC/USDT",
            timeframe="1h",
            asof=pd.Timestamp("2026-05-13"),
            aggregated_signal=sig,
            realized_returns={"1h": -0.01},
            direction_correct={"a": False},
        )
        a.update(outcome)
        stats = a._stats["a"]
        assert stats.alpha == pytest.approx(a.prior_alpha)
        assert stats.beta == pytest.approx(a.prior_beta + 1.0)

    def test_weights_evolve_after_observations(self):
        """Per-analyst posterior accuracy reflects track record."""
        a = BMAAggregator(n_min_observations=5, prior_alpha=1.0, prior_beta=1.0)
        # Analyst 'a' is right 8 of 10 times, 'b' is right 4 of 10 times
        for outcome_pair in [(True, False)] * 8 + [(False, True)] * 2:
            views = [_view("a", 1), _view("b", 1)]
            sig = a.aggregate(views, _ctx())
            outcome = EpisodeOutcome(
                asset="BTC/USDT",
                timeframe="1h",
                asof=pd.Timestamp("2026-05-13"),
                aggregated_signal=sig,
                realized_returns={"1h": 0.01},
                direction_correct={"a": outcome_pair[0], "b": outcome_pair[1]},
            )
            a.update(outcome)
        # After 10 observations, posteriors:
        #  a: Beta(1+8, 1+2) → mean 9/12 = 0.75
        #  b: Beta(1+2, 1+8) → mean 3/12 = 0.25
        wa = a._weight_for("a")
        wb = a._weight_for("b")
        assert wa > wb
        assert wa > 0.6
        assert wb < 0.4

    def test_update_ignores_views_not_in_outcome_map(self):
        a = BMAAggregator()
        views = [_view("a", 1), _view("b", 1)]
        sig = a.aggregate(views, _ctx())
        outcome = EpisodeOutcome(
            asset="BTC/USDT",
            timeframe="1h",
            asof=pd.Timestamp("2026-05-13"),
            aggregated_signal=sig,
            realized_returns={"1h": 0.01},
            direction_correct={"a": True},  # b not present
        )
        a.update(outcome)
        # 'b' stats untouched
        assert "b" not in a._stats or a._stats.get("b") is None or a._stats["b"].n_observations == 0


class TestUniformPreCalibration:
    """Per ADR-0009 §P1-12: uniform weights until n_min_observations."""

    def test_pre_calibration_uniform_weights(self):
        a = BMAAggregator(n_min_observations=100)
        # Even with one analyst that's been wrong 99/100 times, weights are uniform
        # because it hasn't reached the threshold (we need 100, give 99)
        for _ in range(99):
            views = [_view("bad", 1)]
            sig = a.aggregate(views, _ctx())
            outcome = EpisodeOutcome(
                asset="BTC/USDT",
                timeframe="1h",
                asof=pd.Timestamp("2026-05-13"),
                aggregated_signal=sig,
                realized_returns={"1h": 0.01},
                direction_correct={"bad": False},
            )
            a.update(outcome)
        # Still below n_min_observations (99 < 100) → uniform 0.5
        assert a._weight_for("bad") == 0.5


class TestStatus:
    def test_status_initial(self):
        a = BMAAggregator()
        s = a.status()
        assert s["name"] == "bma"
        assert s["n_aggregated"] == 0
        assert s["analyst_stats"] == {}

    def test_status_after_aggregate(self):
        a = BMAAggregator()
        views = [_view("a", 1), _view("b", 1)]
        a.aggregate(views, _ctx())
        s = a.status()
        assert s["n_aggregated"] == 1
        assert s["last_aggregated_at"] is not None
