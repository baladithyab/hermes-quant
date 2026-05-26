"""Wave I — walk-forward replay composition."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.backtest import (
    WalkForwardBacktestResult,
    walk_forward_replay,
)


def _bars(n: int = 600, *, seed: int = 42, drift: float = 0.01, vol: float = 0.4):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    closes = 100 + np.cumsum(rng.normal(drift, vol, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": 1000.0,
        }
    )


def _advisor(direction=0):
    def fake_advisor(**kwargs):
        return {
            "as_of": kwargs["as_of"].isoformat(),
            "aggregated_signal": {
                "asset": kwargs.get("symbol", "TEST"),
                "asset_class": kwargs.get("asset_class", "equity"),
                "timeframe": kwargs.get("timeframe", "1h"),
                "direction": direction,
                "magnitude": 0.5 if direction else 0.0,
                "confidence": 0.7 if direction else 0.0,
                "confidence_raw": 0.7 if direction else 0.0,
                "horizon": "1h",
                "aggregator": "bma",
            },
            "risk_gate": {"pass": direction != 0, "kelly_fraction": 0.10 if direction else 0.0},
            "analyst_views": [
                {
                    "analyst": "wf_voice",
                    "direction": direction,
                    "magnitude": 0.5 if direction else 0.0,
                    "confidence": 0.7 if direction else 0.0,
                    "confidence_raw": 0.7 if direction else 0.0,
                    "horizon": "1h",
                },
            ],
        }

    return fake_advisor


def test_walk_forward_replay_returns_expected_fold_count():
    r = walk_forward_replay(
        _bars(900),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=3,
        warmup_bars=20,
        advisor_recommend=_advisor(0),
    )
    assert isinstance(r, WalkForwardBacktestResult)
    assert r.n_splits == 3
    assert len(r.folds) == 3
    assert r.total_decisions == 0


def test_walk_forward_folds_are_out_of_sample_test_slices():
    bars = _bars(900)
    r = walk_forward_replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=3,
        warmup_bars=20,
        advisor_recommend=_advisor(1),
    )
    for fold in r.folds:
        split = fold.split
        assert split.train_end < split.val_start
        assert split.val_end <= split.test_start
        assert fold.n_test_bars > 20
        assert fold.result.n_bars == fold.n_test_bars - min(20, max(1, int(fold.n_test_bars * 0.4)))


def test_walk_forward_aggregates_metrics():
    r = walk_forward_replay(
        _bars(900, drift=0.05),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=3,
        warmup_bars=20,
        advisor_recommend=_advisor(1),
    )
    d = r.to_dict()
    assert "mean_excess_return_vs_buy_hold_pct" in d
    assert "mean_sharpe_delta" in d
    assert "positive_excess_fold_rate" in d
    assert 0.0 <= r.positive_excess_fold_rate <= 1.0
    assert r.total_decisions > 0
    assert r.total_settlements > 0


def test_walk_forward_markdown_report_contains_charter_decision():
    r = walk_forward_replay(
        _bars(900),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=3,
        warmup_bars=20,
        advisor_recommend=_advisor(0),
    )
    md = r.to_markdown_report()
    assert "Walk-forward backtest" in md
    assert "Aggregate out-of-sample summary" in md
    assert "Charter decision" in md
    assert "Fold table" in md


def test_walk_forward_rejects_invalid_split_config():
    with pytest.raises(ValueError):
        walk_forward_replay(
            _bars(300),
            symbol="TEST",
            asset_class="equity",
            timeframe="1h",
            n_splits=3,
            train_pct=0.8,
            val_pct=0.2,
            advisor_recommend=_advisor(0),
        )


def test_walk_forward_does_not_leak_aggregator_between_folds():
    """Each fold gets its own replay default aggregator. If state leaked,
    n_settlements/posterior counts would monotonically carry forward. We assert
    each fold's posterior n_obs is bounded by that fold's own settlements."""
    r = walk_forward_replay(
        _bars(900, drift=0.05),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=3,
        warmup_bars=20,
        advisor_recommend=_advisor(1),
    )
    for fold in r.folds:
        post = fold.result.aggregator_posteriors
        assert post is not None
        stats = post["analyst_stats"].get("wf_voice")
        assert stats is not None
        assert stats["n_observations"] <= fold.result.n_settlements


def test_walk_forward_serialization_has_fold_payloads():
    r = walk_forward_replay(
        _bars(900),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=3,
        warmup_bars=20,
        advisor_recommend=_advisor(0),
    )
    d = r.to_dict()
    assert len(d["folds"]) == 3
    assert "split" in d["folds"][0]
    assert "result" in d["folds"][0]


def test_walk_forward_short_data_raises():
    with pytest.raises(ValueError):
        walk_forward_replay(
            _bars(20),
            symbol="TEST",
            asset_class="equity",
            timeframe="1h",
            n_splits=5,
            advisor_recommend=_advisor(0),
        )
