"""Tests for hermes_quant.data.alphavantage_provider (B22, R-B22 2026-05-31).

Offline / deterministic — NO live network. A FakeSession returns canned JSON
dicts (recorded TIME_SERIES_DAILY shape) so the parser, rate-limit
classification, fail-closed key handling, and no-lookahead as_of filter are all
exercised without hitting Alpha Vantage.

Covers:
  - happy path: canned TIME_SERIES_DAILY -> validated bars, string->float/int
    casts, US/Eastern dates -> tz-naive UTC.
  - {"Note": ...} -> RateLimitError (legacy per-minute throttle).
  - {"Information": "...rate limit..."} -> RateLimitError (daily quota / premium).
  - {"Error Message": ...} -> DataProviderError (bad symbol/params).
  - missing key (arg None + env unset) -> DataProviderError at FETCH (not ctor).
  - as_of filter drops future bars (no-lookahead).
  - empty / <2 bars -> DataQualityError.
  - determinism: same canned input -> identical DataFrame.
  - integration: fetch_with_chain([ratelimited_yf, av]) falls through yf -> AV.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.data.alphavantage_provider import AlphaVantageProvider
from hermes_quant.data.base import fetch_with_chain
from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)

# ---------------------------------------------------------------------------
# Recorded sample payload (TIME_SERIES_DAILY, compact) — offline fixture.
# Numbered string keys, all values are STRINGS (as AV returns them).
# ---------------------------------------------------------------------------
SAMPLE_DAILY = {
    "Meta Data": {
        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
        "2. Symbol": "IBM",
        "3. Last Refreshed": "2026-05-29",
        "4. Output Size": "Compact",
        "5. Time Zone": "US/Eastern",
    },
    "Time Series (Daily)": {
        "2026-05-29": {
            "1. open": "134.49",
            "2. high": "135.79",
            "3. low": "132.80",
            "4. close": "135.09",
            "5. volume": "5428898",
        },
        "2026-05-28": {
            "1. open": "133.10",
            "2. high": "134.90",
            "3. low": "132.50",
            "4. close": "134.20",
            "5. volume": "4012345",
        },
        "2026-05-27": {
            "1. open": "131.00",
            "2. high": "133.50",
            "3. low": "130.75",
            "4. close": "133.05",
            "5. volume": "3899001",
        },
    },
}

NOTE_BODY = {
    "Note": (
        "Thank you for using Alpha Vantage! Our standard API call frequency is "
        "5 calls per minute and 500 calls per day."
    )
}

INFORMATION_RATE_LIMIT_BODY = {
    "Information": (
        "We have detected your API key ... and our standard API rate limit is "
        "25 requests per day. Please subscribe to a premium plan."
    )
}

ERROR_MESSAGE_BODY = {
    "Error Message": (
        "Invalid API call. Please retry or visit the documentation for "
        "TIME_SERIES_DAILY."
    )
}


# ---------------------------------------------------------------------------
# Fake requests.Session seam
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: Any, status: int = 200):
        self._body = body
        self._status = status

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class FakeSession:
    """Returns a canned JSON body for every .get(). Records calls."""

    def __init__(self, body: Any, status: int = 200):
        self._body = body
        self._status = status
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(self._body, self._status)


def _provider(body: Any, *, status: int = 200, api_key: str | None = "TESTKEY") -> AlphaVantageProvider:
    return AlphaVantageProvider(
        api_key=api_key,
        session=FakeSession(body, status=status),
        retry_base_delay_s=0.001,  # keep retry sleeps negligible in tests
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_parses_sample_payload():
    p = _provider(SAMPLE_DAILY)
    bars = p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))
    assert list(bars.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(bars) == 3
    # Ascending by timestamp (validate_bars sorts).
    assert bars["timestamp"].is_monotonic_increasing
    # String -> numeric casts.
    assert bars["open"].dtype.kind == "f"
    assert bars["volume"].dtype.kind in ("i", "f")
    last = bars.iloc[-1]
    assert last["close"] == pytest.approx(135.09)
    assert int(last["volume"]) == 5428898
    # tz-naive UTC after validate_bars.
    assert bars["timestamp"].dt.tz is None


def test_us_eastern_dates_normalized_to_utc():
    """A US/Eastern calendar date becomes tz-naive UTC; the UTC date may roll
    to the next calendar day (EDT is UTC-4), which is the expected,
    conservative behavior."""
    p = _provider(SAMPLE_DAILY)
    bars = p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))
    # 2026-05-29 00:00 US/Eastern (EDT, UTC-4) -> 2026-05-29 04:00 UTC.
    assert bars["timestamp"].iloc[-1] == pd.Timestamp("2026-05-29T04:00:00")


def test_determinism_same_input_same_output():
    p1 = _provider(SAMPLE_DAILY)
    p2 = _provider(SAMPLE_DAILY)
    b1 = p1.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))
    b2 = p2.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))
    pd.testing.assert_frame_equal(b1, b2)


# ---------------------------------------------------------------------------
# Rate-limit classification (HTTP 200 + JSON body)
# ---------------------------------------------------------------------------


def test_note_body_raises_rate_limit_error():
    p = _provider(NOTE_BODY)
    with pytest.raises(RateLimitError):
        p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


def test_information_rate_limit_raises_rate_limit_error():
    p = _provider(INFORMATION_RATE_LIMIT_BODY)
    with pytest.raises(RateLimitError):
        p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


def test_error_message_raises_data_provider_error():
    p = _provider(ERROR_MESSAGE_BODY)
    with pytest.raises(DataProviderError):
        p.fetch_bars("ZZZZ", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


def test_missing_time_series_key_raises_data_provider_error():
    p = _provider({"Meta Data": {"2. Symbol": "IBM"}})
    with pytest.raises(DataProviderError, match="missing"):
        p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


# ---------------------------------------------------------------------------
# Fail-closed: missing API key
# ---------------------------------------------------------------------------


def test_missing_key_raises_at_fetch_not_construction(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    # Construction must NOT raise (discovery/daemons without a key must boot).
    p = AlphaVantageProvider(api_key=None, session=FakeSession(SAMPLE_DAILY))
    with pytest.raises(DataProviderError, match="ALPHA_VANTAGE_API_KEY not set"):
        p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


def test_key_from_env_is_resolved(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "ENVKEY")
    p = AlphaVantageProvider(api_key=None, session=FakeSession(SAMPLE_DAILY))
    bars = p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))
    assert len(bars) == 3
    # The key is sent in params but never logged/returned.
    assert p._session.calls[0]["params"]["apikey"] == "ENVKEY"


def test_health_never_exposes_key_value(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "SECRET")
    p = AlphaVantageProvider(api_key=None)
    h = p.health()
    assert h["has_key"] is True
    assert "SECRET" not in str(h)
    assert "key" not in [k for k in h if "value" in k.lower()]


# ---------------------------------------------------------------------------
# No-lookahead as_of filter
# ---------------------------------------------------------------------------


def test_as_of_drops_future_bars():
    p = _provider(SAMPLE_DAILY)
    # as_of = 2026-05-28 12:00 UTC. The 2026-05-29 bar (timestamp 05-29 04:00
    # UTC) is in the future -> dropped. 05-27 and 05-28 bars survive.
    as_of = pd.Timestamp("2026-05-28T12:00:00Z")
    bars = p.fetch_bars(
        "IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"), as_of=as_of
    )
    assert len(bars) == 2
    assert bars["timestamp"].max() <= pd.Timestamp("2026-05-28T12:00:00")


def test_as_of_tz_naive_input_handled():
    """A tz-naive as_of must not raise (pandas 2.x tz-aware vs naive compare)."""
    p = _provider(SAMPLE_DAILY)
    as_of = pd.Timestamp("2026-05-28T12:00:00")  # tz-naive
    bars = p.fetch_bars(
        "IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"), as_of=as_of
    )
    assert len(bars) == 2


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


def test_empty_series_raises_data_quality_error():
    body = {"Meta Data": {}, "Time Series (Daily)": {}}
    p = _provider(body)
    with pytest.raises(DataQualityError):
        p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


def test_single_bar_raises_data_quality_error():
    body = {
        "Meta Data": {"5. Time Zone": "US/Eastern"},
        "Time Series (Daily)": {
            "2026-05-29": {
                "1. open": "134.49",
                "2. high": "135.79",
                "3. low": "132.80",
                "4. close": "135.09",
                "5. volume": "5428898",
            }
        },
    }
    p = _provider(body)
    with pytest.raises(DataQualityError):
        p.fetch_bars("IBM", "1d", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


# ---------------------------------------------------------------------------
# Timeframe / protocol
# ---------------------------------------------------------------------------


def test_unsupported_timeframe_raises():
    p = _provider(SAMPLE_DAILY)
    with pytest.raises(DataProviderError, match="timeframe"):
        p.fetch_bars("IBM", "1h", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-01"))


def test_provider_attributes_conform_to_protocol():
    p = AlphaVantageProvider(api_key="X")
    assert p.name == "alphavantage"
    assert p.asset_classes == ["equity", "etf"]
    assert p.timeframes == ["1d"]
    assert p.requires_credentials is True


# ---------------------------------------------------------------------------
# Integration: fetch_with_chain falls through yfinance -> AlphaVantage
# ---------------------------------------------------------------------------


def _good_bars(n: int) -> pd.DataFrame:
    ts = pd.date_range("2026-05-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        }
    )


def test_chain_falls_through_yfinance_to_alphavantage():
    """A rate-limited yfinance tier -> AlphaVantage serves the bars.

    Mirrors the MagicMock provider pattern in test_data_base.py. The AV
    provider is REAL (offline FakeSession); yfinance is a mock that always
    rate-limits so the chain backs off and falls through.
    """
    yf = MagicMock()
    yf.name = "yfinance"
    yf.fetch_bars.side_effect = [
        RateLimitError("throttled"),
        RateLimitError("throttled"),
        RateLimitError("throttled"),
    ]
    av = _provider(SAMPLE_DAILY)
    out = fetch_with_chain(
        [yf, av],
        "IBM",
        "1d",
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-06-01"),
        max_retries=2,
    )
    # AV's 3 sample bars come through.
    assert len(out) == 3
    assert yf.fetch_bars.call_count == 3  # 1 + 2 retries
    # AV was constructed with the canonical signature fetch_with_chain calls.
    assert av._session.calls, "AlphaVantage was never called by the chain"


def test_chain_skips_alphavantage_when_key_absent(monkeypatch):
    """Fail-closed: a key-less AV tier raises DataProviderError -> the chain
    treats it as transient-fall-through. With AV as the LAST tier and no
    successful provider, the chain raises 'all providers failed' (NOT a crash).
    """
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    yf = MagicMock()
    yf.name = "yfinance"
    yf.fetch_bars.side_effect = [
        DataProviderError("yf down"),
        DataProviderError("yf down"),
        DataProviderError("yf down"),
    ]
    av = AlphaVantageProvider(api_key=None, session=FakeSession(SAMPLE_DAILY))
    with pytest.raises(DataProviderError, match="all providers failed"):
        fetch_with_chain(
            [yf, av],
            "IBM",
            "1d",
            pd.Timestamp("2026-01-01"),
            pd.Timestamp("2026-06-01"),
            max_retries=2,
        )
