"""Unit tests for hermes_quant.data.yfinance_provider.

Network-bound tests are marked @pytest.mark.requires_network and skipped
in normal CI; manually invoke before tagging a release.

Other tests use mocked yfinance — just verify the contract.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from hermes_quant.data.yfinance_provider import YFinanceProvider
from hermes_quant.protocol import (
    DataProvider,
    DataProviderError,
    DataQualityError,
    RateLimitError,
)


def _yfinance_response(n: int = 5):
    """Build a mock yfinance Ticker.history() response."""
    ts = pd.date_range("2026-05-13T00:00:00", periods=n, freq="1h", tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(n)],
            "High": [101.0 + i for i in range(n)],
            "Low": [99.0 + i for i in range(n)],
            "Close": [100.5 + i for i in range(n)],
            "Volume": [1000 + i for i in range(n)],
        },
        index=ts,
    )


class TestYFinanceProtocol:
    def test_satisfies_data_provider_protocol(self):
        p = YFinanceProvider()
        assert isinstance(p, DataProvider)

    def test_metadata_fields(self):
        p = YFinanceProvider()
        assert p.name == "yfinance"
        assert "equity" in p.asset_classes
        assert "etf" in p.asset_classes
        assert "1d" in p.timeframes
        assert p.requires_credentials is False


class TestFetchBarsWithMockedYf:
    def test_basic_fetch(self):
        p = YFinanceProvider(inter_call_sleep_s=0.0)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _yfinance_response(10)
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        out = p.fetch_bars(
            "AAPL", "1h",
            pd.Timestamp("2026-05-12T00:00:00Z"),
            pd.Timestamp("2026-05-14T00:00:00Z"),
        )
        assert len(out) == 10
        assert "timestamp" in out.columns
        assert "open" in out.columns
        # Validation strips tz; output is tz-naive UTC
        assert out["timestamp"].dt.tz is None

    def test_unsupported_timeframe_raises(self):
        p = YFinanceProvider()
        with pytest.raises(DataProviderError, match="not supported"):
            p.fetch_bars(
                "AAPL", "30s",  # invalid
                pd.Timestamp("2026-05-12T00:00:00Z"),
                pd.Timestamp("2026-05-13T00:00:00Z"),
            )

    def test_4h_timeframe_unsupported_in_v011(self):
        """yfinance doesn't natively support 4h; we don't synthesize for v0.1.1."""
        p = YFinanceProvider()
        with pytest.raises(DataProviderError, match="not supported"):
            p.fetch_bars(
                "AAPL", "4h",
                pd.Timestamp("2026-05-12T00:00:00Z"),
                pd.Timestamp("2026-05-13T00:00:00Z"),
            )

    def test_rate_limit_error_translated(self):
        # Phase-9e: zero retry delay so this test stays fast (default
        # backoff would add ~6s).
        p = YFinanceProvider(inter_call_sleep_s=0.0, retry_base_delay_s=0.0)
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = Exception("HTTP 429: Too Many Requests")
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        with pytest.raises(RateLimitError):
            p.fetch_bars(
                "AAPL", "1h",
                pd.Timestamp("2026-05-12T00:00:00Z"),
                pd.Timestamp("2026-05-13T00:00:00Z"),
            )
        # Phase-9e: verify the retry actually fired all 3 attempts before
        # giving up. Without the retry wrapper the count would be 1.
        assert mock_ticker.history.call_count == 3

    def test_other_exception_translated_to_data_provider_error(self):
        p = YFinanceProvider(inter_call_sleep_s=0.0, retry_base_delay_s=0.0)
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = Exception("connection reset")
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        with pytest.raises(DataProviderError):
            p.fetch_bars(
                "AAPL", "1h",
                pd.Timestamp("2026-05-12T00:00:00Z"),
                pd.Timestamp("2026-05-13T00:00:00Z"),
            )
        # DataProviderError is NOT a transient — should fail on first
        # attempt, no retry.
        assert mock_ticker.history.call_count == 1

    # Phase-9e regression (synthesis 2026-05-13 + TradingAgents
    # comparison): rate-limit retries succeed if Yahoo recovers within
    # the budget. This is the main reason for stealing the yf_retry
    # pattern — a 2s transient 429 should NOT cascade to the chain
    # fallback.
    def test_transient_rate_limit_recovers_within_retry_budget(self):
        p = YFinanceProvider(inter_call_sleep_s=0.0, retry_base_delay_s=0.0)
        mock_ticker = MagicMock()
        # First attempt fails with throttle, second attempt succeeds.
        good_df = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Volume": [1000],
            },
            index=pd.DatetimeIndex(
                [pd.Timestamp("2026-05-12T14:00:00Z")], tz="UTC"
            ),
        )
        mock_ticker.history.side_effect = [
            Exception("HTTP 429: Too Many Requests"),  # transient
            good_df,  # recovered
        ]
        # validate_bars requires ≥ 2 bars; let's just make 2 rows
        good_df_2 = pd.DataFrame(
            {
                "Open": [100.0, 100.5],
                "High": [101.0, 101.5],
                "Low": [99.0, 99.5],
                "Close": [100.5, 101.0],
                "Volume": [1000, 1100],
            },
            index=pd.DatetimeIndex(
                [pd.Timestamp("2026-05-12T14:00:00Z"),
                 pd.Timestamp("2026-05-12T15:00:00Z")], tz="UTC"
            ),
        )
        mock_ticker.history.side_effect = [
            Exception("HTTP 429: Too Many Requests"),
            good_df_2,
        ]
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        bars = p.fetch_bars(
            "AAPL", "1h",
            pd.Timestamp("2026-05-12T00:00:00Z"),
            pd.Timestamp("2026-05-13T00:00:00Z"),
        )
        # Recovered: bars returned, retry attempted twice (1 fail + 1 ok)
        assert len(bars) == 2
        assert mock_ticker.history.call_count == 2

    def test_persistent_rate_limit_exhausts_retry_budget(self):
        p = YFinanceProvider(
            inter_call_sleep_s=0.0,
            retry_max_attempts=2,
            retry_base_delay_s=0.0,
        )
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = Exception("rate limited")
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        with pytest.raises(RateLimitError):
            p.fetch_bars(
                "AAPL", "1h",
                pd.Timestamp("2026-05-12T00:00:00Z"),
                pd.Timestamp("2026-05-13T00:00:00Z"),
            )
        # max_attempts=2: should call exactly 2 times
        assert mock_ticker.history.call_count == 2

    def test_empty_response_raises_data_quality(self):
        p = YFinanceProvider(inter_call_sleep_s=0.0)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        with pytest.raises(DataQualityError):
            p.fetch_bars(
                "AAPL", "1h",
                pd.Timestamp("2026-05-12T00:00:00Z"),
                pd.Timestamp("2026-05-13T00:00:00Z"),
            )

    def test_yfinance_not_installed_translated(self):
        """If yfinance import fails, yfinance property raises DataProviderError."""
        p = YFinanceProvider()
        # Force the lazy import to fail
        p._yf = None
        with patch.dict("sys.modules", {"yfinance": None}):
            with pytest.raises(DataProviderError, match="yfinance not installed"):
                _ = p.yf


class TestHealth:
    def test_health_initial(self):
        p = YFinanceProvider()
        h = p.health()
        assert h["provider"] == "yfinance"
        assert h["n_fetches"] == 0
        assert h["n_errors"] == 0
        assert h["yfinance_loaded"] is False

    def test_health_after_fetch(self):
        p = YFinanceProvider(inter_call_sleep_s=0.0)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _yfinance_response(5)
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        p._yf = mock_yf

        p.fetch_bars(
            "AAPL", "1h",
            pd.Timestamp("2026-05-12T00:00:00Z"),
            pd.Timestamp("2026-05-13T00:00:00Z"),
        )
        h = p.health()
        assert h["n_fetches"] == 1
        assert h["n_errors"] == 0
        assert h["yfinance_loaded"] is True


# ---------------------------------------------------------------------------
# Optional network-bound tests
# ---------------------------------------------------------------------------

@pytest.mark.requires_network
@pytest.mark.skip(reason="network-bound; run manually before release")
class TestRealNetwork:
    """Manual-invoke tests against the real Yahoo Finance API."""

    def test_real_fetch_aapl_1d(self):
        p = YFinanceProvider()
        bars = p.fetch_bars(
            "AAPL", "1d",
            pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-02-01T00:00:00Z"),
        )
        assert len(bars) > 5
        assert bars["close"].iloc[-1] > 0
