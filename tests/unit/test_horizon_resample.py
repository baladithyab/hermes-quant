"""Tests for horizon_cache resample logic (ADR-0036)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.data.horizon_cache import resample_to_horizon


def _make_daily_df(n_days: int = 5 * 252, start: str = "2021-01-04") -> pd.DataFrame:
    """Build a synthetic 5-year daily OHLCV DataFrame on business-days."""
    idx = pd.bdate_range(start=start, periods=n_days)
    rng = np.random.default_rng(seed=42)
    close = 100.0 + rng.normal(0, 1, size=n_days).cumsum()
    open_ = close + rng.normal(0, 0.2, size=n_days)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.3, size=n_days))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.3, size=n_days))
    volume = rng.integers(1_000_000, 5_000_000, size=n_days).astype(float)
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


class TestResampleToHorizon:
    def test_1d_passthrough(self):
        df = _make_daily_df()
        out = resample_to_horizon(df, "1d")
        assert len(out) == len(df), "1d should be a passthrough"
        # Same OHLCV columns
        assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_1w_bar_count(self):
        df = _make_daily_df(n_days=5 * 252, start="2021-01-04")
        out = resample_to_horizon(df, "1w")
        # 5 years of weekly bars ≈ 5 * 52 = 260, allow ±5 for partials
        assert 250 <= len(out) <= 275, f"expected ~260 weekly bars, got {len(out)}"
        # Open/High/Low/Close aggregation correctness on the first week
        first_week_end = out["timestamp"].iloc[0]
        first_week_bars = df[df["timestamp"] <= first_week_end]
        assert out["open"].iloc[0] == pytest.approx(first_week_bars["open"].iloc[0])
        assert out["close"].iloc[0] == pytest.approx(first_week_bars["close"].iloc[-1])
        assert out["high"].iloc[0] == pytest.approx(first_week_bars["high"].max())
        assert out["low"].iloc[0] == pytest.approx(first_week_bars["low"].min())
        assert out["volume"].iloc[0] == pytest.approx(first_week_bars["volume"].sum())

    def test_1m_bar_count(self):
        df = _make_daily_df(n_days=5 * 252)
        out = resample_to_horizon(df, "1M")
        # 5 years ≈ 60 monthly bars
        assert 55 <= len(out) <= 65, f"expected ~60 monthly bars, got {len(out)}"

    def test_1q_bar_count(self):
        df = _make_daily_df(n_days=5 * 252)
        out = resample_to_horizon(df, "1Q")
        # 5 years ≈ 20 quarterly bars (ADR §"Default horizon set" wants
        # ~40 from 10y; with 5y synthetic input we get ~20)
        assert 18 <= len(out) <= 22, f"expected ~20 quarterly bars, got {len(out)}"

    def test_volume_sums_correctly(self):
        df = _make_daily_df()
        out_w = resample_to_horizon(df, "1w")
        # Volume across all weekly bars must equal volume across all daily bars
        # (resample is a partition; nothing is dropped except partial trailing
        # weeks if the index ends mid-week). Allow tiny floating drift.
        # Use last fully-covered weekly bar's timestamp as cutoff.
        last_w = out_w["timestamp"].iloc[-1]
        df_truncated = df[df["timestamp"] <= last_w]
        assert out_w["volume"].sum() == pytest.approx(df_truncated["volume"].sum())

    def test_ohlc_invariants(self):
        df = _make_daily_df()
        for h in ("1w", "1M", "1Q"):
            out = resample_to_horizon(df, h)
            # high >= low, high >= max(open, close), low <= min(open, close)
            assert (out["high"] >= out["low"]).all(), f"{h}: high < low"
            assert (out["high"] >= out[["open", "close"]].max(axis=1)).all(), (
                f"{h}: high < max(open, close)"
            )
            assert (out["low"] <= out[["open", "close"]].min(axis=1)).all(), (
                f"{h}: low > min(open, close)"
            )

    def test_unknown_horizon_raises(self):
        df = _make_daily_df(n_days=10)
        with pytest.raises(ValueError, match="unsupported horizon"):
            resample_to_horizon(df, "1y")

    def test_empty_input_returns_empty(self):
        empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        out = resample_to_horizon(empty, "1w")
        assert len(out) == 0
        assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
