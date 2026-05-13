"""Unit tests for hermes_quant.data.base — validation gates + provider chain."""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.data.base import (
    MIN_VALID_BARS,
    REQUIRED_COLUMNS,
    fetch_with_chain,
    validate_bars,
)
from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)


def _good_bars(n: int = 10, start: str = "2026-05-13T00:00:00") -> pd.DataFrame:
    ts = pd.date_range(start, periods=n, freq="1h")
    return pd.DataFrame({
        "timestamp": ts,
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    })


class TestValidateBars:
    def test_passes_clean_input(self):
        bars = _good_bars(10)
        out = validate_bars(bars)
        assert len(out) == 10
        for c in REQUIRED_COLUMNS:
            assert c in out.columns

    def test_drops_nan_ohlc(self):
        bars = _good_bars(10)
        bars.loc[3, "close"] = float("nan")
        bars.loc[5, "open"] = float("nan")
        out = validate_bars(bars)
        assert len(out) == 8

    def test_drops_zero_volume(self):
        bars = _good_bars(10)
        bars.loc[2, "volume"] = 0
        bars.loc[7, "volume"] = -1  # negative also dropped
        out = validate_bars(bars)
        assert len(out) == 8

    def test_dedupes_timestamp_keep_last(self):
        bars = _good_bars(10)
        # Make rows 3 and 5 share a timestamp; row 5 should win (latest in source order)
        bars.loc[3, "timestamp"] = bars.loc[5, "timestamp"]
        out = validate_bars(bars)
        assert len(out) == 9

    def test_sorts_ascending(self):
        bars = _good_bars(10)
        # Reverse the input
        bars = bars.iloc[::-1].reset_index(drop=True)
        out = validate_bars(bars)
        assert (out["timestamp"].diff().dropna() > pd.Timedelta(0)).all()

    def test_min_bars_enforced(self):
        bars = _good_bars(1)
        with pytest.raises(DataQualityError, match="min 2"):
            validate_bars(bars)

    def test_empty_input_raises(self):
        with pytest.raises(DataQualityError, match="empty"):
            validate_bars(pd.DataFrame())

    def test_missing_columns_raises(self):
        bad = _good_bars(10).drop(columns=["volume"])
        with pytest.raises(DataQualityError, match="missing required"):
            validate_bars(bad)

    def test_timestamp_normalized_to_utc(self):
        """tz-aware → UTC; tz-naive treated as UTC."""
        ts_aware = pd.date_range("2026-05-13", periods=3, freq="1h", tz="America/New_York")
        bars = pd.DataFrame({
            "timestamp": ts_aware,
            "open": [100, 101, 102.0],
            "high": [101, 102, 103.0],
            "low": [99, 100, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [100, 100, 100],
        })
        out = validate_bars(bars)
        # Output is tz-naive UTC
        assert out["timestamp"].dt.tz is None
        # And the values are shifted from NY to UTC (NY is UTC-4 or UTC-5)
        assert out["timestamp"].iloc[0].hour in (4, 5)


class TestFetchWithChain:
    def _make_provider(self, name: str, behavior):
        p = MagicMock()
        p.name = name
        p.fetch_bars.side_effect = behavior
        return p

    def test_first_provider_succeeds(self):
        bars = _good_bars(10)
        p1 = self._make_provider("p1", [bars])
        p2 = self._make_provider("p2", [bars])
        out = fetch_with_chain(
            [p1, p2], "BTC/USDT", "1h",
            pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-13"),
        )
        assert len(out) == 10
        p1.fetch_bars.assert_called_once()
        p2.fetch_bars.assert_not_called()

    def test_falls_back_on_rate_limit(self):
        bars = _good_bars(10)
        # p1 always rate-limited; p2 succeeds
        p1 = self._make_provider("p1", [
            RateLimitError("throttled"),
            RateLimitError("throttled"),
            RateLimitError("throttled"),  # exhausts retries
        ])
        p2 = self._make_provider("p2", [bars])
        out = fetch_with_chain(
            [p1, p2], "BTC/USDT", "1h",
            pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-13"),
            max_retries=2,
        )
        assert len(out) == 10
        assert p1.fetch_bars.call_count == 3
        assert p2.fetch_bars.call_count == 1

    def test_falls_back_on_transient(self):
        bars = _good_bars(10)
        p1 = self._make_provider("p1", [
            DataProviderError("network blip"),
            DataProviderError("network blip"),
            DataProviderError("network blip"),
        ])
        p2 = self._make_provider("p2", [bars])
        out = fetch_with_chain(
            [p1, p2], "BTC/USDT", "1h",
            pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-13"),
            max_retries=2,
        )
        assert len(out) == 10

    def test_data_quality_error_propagates(self):
        """DataQualityError doesn't fall back — data isn't going to fix itself."""
        bad = _good_bars(1)  # < MIN_VALID_BARS
        p1 = self._make_provider("p1", [bad])
        p2 = self._make_provider("p2", [_good_bars(10)])
        with pytest.raises(DataQualityError):
            fetch_with_chain(
                [p1, p2], "BTC/USDT", "1h",
                pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-13"),
            )
        # p2 was NOT tried because data-quality error propagates immediately
        p2.fetch_bars.assert_not_called()

    def test_all_providers_fail_raises(self):
        p1 = self._make_provider("p1", [
            DataProviderError("p1 down"), DataProviderError("p1 down"),
            DataProviderError("p1 down"),
        ])
        p2 = self._make_provider("p2", [
            DataProviderError("p2 down"), DataProviderError("p2 down"),
            DataProviderError("p2 down"),
        ])
        with pytest.raises(DataProviderError, match="all providers failed"):
            fetch_with_chain(
                [p1, p2], "BTC/USDT", "1h",
                pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-13"),
                max_retries=2,
            )

    def test_empty_provider_list_raises(self):
        with pytest.raises(DataProviderError, match="no providers"):
            fetch_with_chain(
                [], "BTC/USDT", "1h",
                pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-13"),
            )
