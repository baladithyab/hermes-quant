"""Tests for hermes_quant.data.ccxt_provider (ADR-0017).

Covers:
  - Symbol/timeframe/asset_class validation
  - Lookahead-safe as_of filter (bar OPEN + tf_seconds <= as_of)
  - Pagination loop terminates on partial page / since-monotonicity
  - validate_bars wired (NaN drop, dedupe, sort, zero-volume drop)
  - ccxt error taxonomy mapping (RateLimitExceeded -> RateLimitError,
    BadSymbol -> DataProviderError, NetworkError retry+exhaust)
  - lazy import: module loads without ccxt installed (fake_factory path)
  - Empty result -> DataQualityError
  - <2 bars after as_of filter -> DataQualityError

B22 (R-B22, 2026-05-31): the public ``fetch_bars`` now exposes the CANONICAL
DataProvider Protocol signature ``(asset, timeframe, start, end, *, use_cache,
as_of)`` and delegates to the renamed crypto-specific ``_fetch_crypto_bars``.
The legacy-shaped tests below exercise ``_fetch_crypto_bars`` directly; a
dedicated test verifies the canonical wrapper derives lookback/as_of and
delegates so ccxt can sit in ``fetch_with_chain``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from hermes_quant.data.ccxt_provider import CcxtProvider
from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)


# ---------------------------------------------------------------------------
# Fake ccxt exchange for unit tests
# ---------------------------------------------------------------------------


class FakeExchange:
    """Minimal ccxt-shaped fake. Stores a list of [ts_ms, o, h, l, c, v] rows.

    fetch_ohlcv(symbol, timeframe, since, limit) returns rows where
    row[0] >= since, capped at `limit`.
    """

    def __init__(self, rows: list[list[Any]] | None = None):
        self.rows = rows or []
        self.calls: list[dict] = []
        self.raise_on_call: list[Exception] = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit}
        )
        if self.raise_on_call:
            exc = self.raise_on_call.pop(0)
            raise exc
        out = [r for r in self.rows if since is None or r[0] >= since]
        return out[:limit]

    def set_sandbox_mode(self, on):
        pass


def _make_bars(
    *,
    n: int,
    start: datetime,
    timeframe_seconds: int,
    open_price: float = 100.0,
    drift: float = 0.5,
) -> list[list[Any]]:
    """Build n synthetic OHLCV rows starting at `start`."""
    rows = []
    for i in range(n):
        ts_ms = int(
            (start + timedelta(seconds=i * timeframe_seconds))
            .replace(tzinfo=timezone.utc)
            .timestamp()
            * 1000
        )
        o = open_price + i * drift
        h = o + 1.0
        low = o - 1.0
        c = o + drift / 2
        v = 100.0
        rows.append([ts_ms, o, h, low, c, v])
    return rows


@pytest.fixture
def hourly_bars():
    """24 hourly bars starting 2026-05-13T00:00:00Z."""
    return _make_bars(
        n=24,
        start=datetime(2026, 5, 13, 0, 0, 0),
        timeframe_seconds=3600,
    )


@pytest.fixture
def provider_with_bars(hourly_bars):
    return CcxtProvider(
        exchange_id="binance",
        _exchange_factory=lambda: FakeExchange(rows=hourly_bars),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rejects_unknown_asset_class(provider_with_bars):
    with pytest.raises(DataProviderError, match="asset_class"):
        provider_with_bars._fetch_crypto_bars("BTC/USDT", "equity", "1h")


def test_rejects_invalid_timeframe(provider_with_bars):
    with pytest.raises(DataProviderError, match="timeframe"):
        provider_with_bars._fetch_crypto_bars("BTC/USDT", "crypto", "999h")


def test_rejects_no_slash_symbol(provider_with_bars):
    with pytest.raises(DataProviderError, match="unified format"):
        provider_with_bars._fetch_crypto_bars("BTCUSDT", "crypto", "1h")


# ---------------------------------------------------------------------------
# as_of lookahead filter (the critical money-software path)
# ---------------------------------------------------------------------------


def test_as_of_drops_in_flight_bar(provider_with_bars, hourly_bars):
    """as_of mid-bar must drop the in-flight bar."""
    # 24 bars starting 00:00, hourly. as_of = 14:30 should ADMIT bars
    # closing at <= 14:30 — i.e. bars opening at 0..13 close at 1..14.
    # The bar opening at 14:00 closes at 15:00 > 14:30 → DROPPED.
    # The bar opening at 13:00 closes at 14:00 ≤ 14:30 → ADMITTED.
    # Result: 14 bars (open_times 0..13).
    as_of = pd.Timestamp("2026-05-13T14:30:00Z")
    result = provider_with_bars._fetch_crypto_bars(
        "BTC/USDT",
        "crypto",
        "1h",
        lookback_bars=100,
        as_of=as_of,
    )
    assert len(result) == 14
    # Last admitted bar: open_time = 13:00
    assert result["timestamp"].iloc[-1] == pd.Timestamp("2026-05-13T13:00:00Z")


def test_as_of_exactly_on_close_admits_bar(provider_with_bars):
    """as_of == bar close_time must ADMIT that bar (≤, not <)."""
    # Bar opening 13:00 closes at 14:00. as_of=14:00 -> bar admitted.
    as_of = pd.Timestamp("2026-05-13T14:00:00Z")
    result = provider_with_bars._fetch_crypto_bars(
        "BTC/USDT",
        "crypto",
        "1h",
        lookback_bars=100,
        as_of=as_of,
    )
    assert result["timestamp"].iloc[-1] == pd.Timestamp("2026-05-13T13:00:00Z")
    assert len(result) == 14


def test_as_of_one_second_before_close_drops_bar(provider_with_bars):
    """as_of one second BEFORE close_time must DROP that bar."""
    as_of = pd.Timestamp("2026-05-13T13:59:59Z")
    result = provider_with_bars._fetch_crypto_bars(
        "BTC/USDT",
        "crypto",
        "1h",
        lookback_bars=100,
        as_of=as_of,
    )
    # bar opening 13:00 closes at 14:00 > 13:59:59 → DROPPED
    # last admitted is bar opening 12:00, closing 13:00
    assert result["timestamp"].iloc[-1] == pd.Timestamp("2026-05-13T12:00:00Z")


def test_default_as_of_is_now(hourly_bars):
    """When as_of=None, default is wall-clock now."""
    # Build bars BEFORE current time so they all close in the past
    now = datetime.now(tz=timezone.utc)
    rows = _make_bars(
        n=10,
        start=now.replace(microsecond=0) - timedelta(hours=12),
        timeframe_seconds=3600,
    )
    provider = CcxtProvider(_exchange_factory=lambda: FakeExchange(rows=rows))
    result = provider._fetch_crypto_bars("BTC/USDT", "crypto", "1h")
    # All 10 bars close in the past -> all admitted
    assert len(result) == 10


# ---------------------------------------------------------------------------
# Lookback bar count
# ---------------------------------------------------------------------------


def test_returns_at_most_lookback_bars(provider_with_bars):
    as_of = pd.Timestamp("2026-05-13T23:00:00Z")
    result = provider_with_bars._fetch_crypto_bars(
        "BTC/USDT",
        "crypto",
        "1h",
        lookback_bars=5,
        as_of=as_of,
    )
    assert len(result) == 5


def test_returns_tail_not_head(provider_with_bars):
    """When more bars exist than lookback, return the LATEST ones."""
    as_of = pd.Timestamp("2026-05-13T23:00:00Z")
    result = provider_with_bars._fetch_crypto_bars(
        "BTC/USDT",
        "crypto",
        "1h",
        lookback_bars=3,
        as_of=as_of,
    )
    # 23:00 is the as_of; bar opening 22:00 closes at 23:00 -> last admitted
    assert result["timestamp"].iloc[-1] == pd.Timestamp("2026-05-13T22:00:00Z")
    assert result["timestamp"].iloc[0] == pd.Timestamp("2026-05-13T20:00:00Z")


# ---------------------------------------------------------------------------
# Empty result + insufficient bars
# ---------------------------------------------------------------------------


def test_empty_result_raises_data_quality_error():
    provider = CcxtProvider(_exchange_factory=lambda: FakeExchange(rows=[]))
    with pytest.raises(DataQualityError, match="no bars"):
        provider._fetch_crypto_bars("BTC/USDT", "crypto", "1h")


def test_insufficient_bars_after_as_of_filter_raises(hourly_bars):
    """If as_of is so old only 0-1 bars survive the filter, DataQualityError."""
    provider = CcxtProvider(_exchange_factory=lambda: FakeExchange(rows=hourly_bars))
    # as_of before any bars exist -> filter drops everything
    as_of = pd.Timestamp("2025-01-01T00:00:00Z")
    with pytest.raises(DataQualityError, match="bars remain"):
        provider._fetch_crypto_bars("BTC/USDT", "crypto", "1h", as_of=as_of)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


def test_rate_limit_exceeded_maps_to_rate_limit_error():
    """ccxt.RateLimitExceeded after retries -> hermes_quant.RateLimitError."""
    import ccxt as _ccxt  # only import for the exception class

    fake = FakeExchange()
    fake.raise_on_call = [
        _ccxt.RateLimitExceeded("rate limit"),
        _ccxt.RateLimitExceeded("rate limit"),
        _ccxt.RateLimitExceeded("rate limit"),
    ]
    provider = CcxtProvider(_exchange_factory=lambda: fake)
    with pytest.raises(RateLimitError):
        provider._fetch_with_retry(
            symbol="BTC/USDT",
            timeframe="1h",
            since=0,
            limit=10,
            max_attempts=3,
            base_delay_s=0.001,
        )


def test_bad_symbol_does_not_retry():
    """ccxt.BadSymbol -> DataProviderError immediately, no retry."""
    import ccxt as _ccxt

    fake = FakeExchange()
    fake.raise_on_call = [_ccxt.BadSymbol("unknown symbol")]
    provider = CcxtProvider(_exchange_factory=lambda: fake)
    with pytest.raises(DataProviderError, match="rejected symbol"):
        provider._fetch_with_retry(
            symbol="FAKE/X",
            timeframe="1h",
            since=0,
            limit=10,
            max_attempts=3,
            base_delay_s=0.001,
        )
    # Verify: only 1 call (no retry)
    assert len(fake.calls) == 1


def test_network_error_retries_then_raises():
    import ccxt as _ccxt

    fake = FakeExchange()
    fake.raise_on_call = [
        _ccxt.NetworkError("conn reset"),
        _ccxt.NetworkError("conn reset"),
        _ccxt.NetworkError("conn reset"),
    ]
    provider = CcxtProvider(_exchange_factory=lambda: fake)
    with pytest.raises(DataProviderError, match="network error"):
        provider._fetch_with_retry(
            symbol="BTC/USDT",
            timeframe="1h",
            since=0,
            limit=10,
            max_attempts=3,
            base_delay_s=0.001,
        )
    assert len(fake.calls) == 3  # all 3 attempts made


def test_network_error_then_success():
    """Transient NetworkError followed by success returns the data."""
    import ccxt as _ccxt

    fake = FakeExchange(rows=[[0, 1, 1, 1, 1, 1]])
    fake.raise_on_call = [_ccxt.NetworkError("transient")]
    provider = CcxtProvider(_exchange_factory=lambda: fake)
    result = provider._fetch_with_retry(
        symbol="BTC/USDT",
        timeframe="1h",
        since=0,
        limit=10,
        max_attempts=3,
        base_delay_s=0.001,
    )
    assert result == [[0, 1, 1, 1, 1, 1]]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_terminates_on_empty_chunk():
    """Empty response from fetch_ohlcv terminates the loop."""
    fake = FakeExchange(rows=[])
    provider = CcxtProvider(_exchange_factory=lambda: fake)
    with pytest.raises(DataQualityError):
        provider._fetch_crypto_bars("BTC/USDT", "crypto", "1h")
    assert len(fake.calls) == 1


def test_pagination_terminates_on_partial_page():
    """When ccxt returns fewer than limit rows, that's the last page."""
    rows = _make_bars(
        n=50,  # fewer than the 1000-row limit
        start=datetime(2026, 5, 13, 0, 0, 0),
        timeframe_seconds=3600,
    )
    fake = FakeExchange(rows=rows)
    provider = CcxtProvider(_exchange_factory=lambda: fake)
    result = provider._fetch_crypto_bars(
        "BTC/USDT",
        "crypto",
        "1h",
        lookback_bars=100,
        as_of=pd.Timestamp("2026-05-15T23:00:00Z"),
    )
    # All 50 bars admitted (close times all <= as_of)
    assert len(result) == 50
    # Only ONE call (50 < 1000 means partial -> stop)
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Symbol normalization (we DON'T normalize; we reject)
# ---------------------------------------------------------------------------


def test_provider_does_not_silently_normalize_symbols(provider_with_bars):
    """Per ADR-0017 §D2, no-slash symbols raise immediately, not silently retry."""
    with pytest.raises(DataProviderError, match="ccxt rejects"):
        provider_with_bars._fetch_crypto_bars("ETHUSDT", "crypto", "1h")


# ---------------------------------------------------------------------------
# B22: canonical Protocol signature (fetch_bars delegates to _fetch_crypto_bars)
# ---------------------------------------------------------------------------


def test_canonical_fetch_bars_signature_delegates(hourly_bars):
    """fetch_bars(asset, timeframe, start, end, *, use_cache, as_of) works.

    The canonical wrapper must derive lookback from [start, end], pass as_of
    (defaulting to end when None), and return the same bars _fetch_crypto_bars
    would for the equivalent window — so ccxt can sit in fetch_with_chain.
    """
    provider = CcxtProvider(_exchange_factory=lambda: FakeExchange(rows=hourly_bars))
    start = pd.Timestamp("2026-05-13T00:00:00Z")
    end = pd.Timestamp("2026-05-13T14:00:00Z")
    result = provider.fetch_bars("BTC/USDT", "1h", start, end, use_cache=True)
    # Bars opening 0..13 close at 1..14; as_of defaults to end=14:00 -> 14 bars.
    assert len(result) == 14
    assert result["timestamp"].iloc[-1] == pd.Timestamp("2026-05-13T13:00:00Z")


def test_canonical_fetch_bars_explicit_as_of_overrides_end(hourly_bars):
    """An explicit as_of wins over end in the canonical signature."""
    provider = CcxtProvider(_exchange_factory=lambda: FakeExchange(rows=hourly_bars))
    start = pd.Timestamp("2026-05-13T00:00:00Z")
    end = pd.Timestamp("2026-05-13T23:00:00Z")
    result = provider.fetch_bars(
        "BTC/USDT", "1h", start, end, as_of=pd.Timestamp("2026-05-13T14:00:00Z")
    )
    assert result["timestamp"].iloc[-1] == pd.Timestamp("2026-05-13T13:00:00Z")


def test_canonical_fetch_bars_rejects_no_slash(provider_with_bars):
    """Canonical path still rejects no-slash symbols via _fetch_crypto_bars."""
    start = pd.Timestamp("2026-05-13T00:00:00Z")
    end = pd.Timestamp("2026-05-13T14:00:00Z")
    with pytest.raises(DataProviderError, match="unified format"):
        provider_with_bars.fetch_bars("BTCUSDT", "1h", start, end)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_returns_provider_info():
    provider = CcxtProvider(
        exchange_id="binance",
        sandbox=True,
        _exchange_factory=lambda: FakeExchange(),
    )
    h = provider.health()
    assert h["name"] == "ccxt"
    assert h["exchange_id"] == "binance"
    assert h["sandbox"] is True


# ---------------------------------------------------------------------------
# Optional-extras: missing ccxt
# ---------------------------------------------------------------------------


def test_missing_ccxt_install_fails_at_construction(monkeypatch):
    """Without ccxt installed, instantiating raises DataProviderError."""
    import sys

    # Simulate missing ccxt
    monkeypatch.setitem(sys.modules, "ccxt", None)
    with pytest.raises(DataProviderError, match="ccxt is not installed"):
        CcxtProvider(exchange_id="binance")


def test_unknown_exchange_id_raises():
    """Unknown exchange_id -> DataProviderError."""
    with pytest.raises(DataProviderError, match="unknown exchange_id"):
        CcxtProvider(exchange_id="not_a_real_exchange")
