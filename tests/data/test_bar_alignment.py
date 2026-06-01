"""Tests for hermes_quant.data.bar_alignment (ADR-0069).

These tests pin the still-forming-bar discipline: daily-timeframe equity reads
mid-session drop the still-forming last bar; reads after market close keep it;
crypto and intraday timeframes pass through unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from hermes_quant.data.bar_alignment import drop_still_forming_bar


@pytest.fixture
def fixture_bars() -> pd.DataFrame:
    """Two daily bars: 5/27 settled, 5/28 still-forming (close=102.5 = intraday tick)."""
    return pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2026-05-27", tz="UTC"),
                pd.Timestamp("2026-05-28", tz="UTC"),
            ],
            "open": [100.0, 102.0],
            "high": [101.5, 103.0],
            "low": [99.5, 101.0],
            "close": [101.0, 102.5],
            "volume": [1_000_000.0, 500_000.0],
        }
    )


# --- Daily equity, EDT (March-November) ---


def test_mid_session_drops_still_forming_bar(fixture_bars: pd.DataFrame) -> None:
    """5/28 14:00 ET = 5/28 18:00 UTC (EDT). Bar not yet settled."""
    now = datetime(2026, 5, 28, 18, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 1
    assert float(trimmed["close"].iloc[-1]) == 101.0  # 5/27 settled close
    assert info["still_forming_dropped"] is True
    assert info["still_forming_close"] == 102.5
    assert info["still_forming_high"] == 103.0
    assert info["still_forming_low"] == 101.0
    assert info["still_forming_volume"] == 500_000.0
    assert "dropped" in info["reason"]


def test_post_close_keeps_settled_bar(fixture_bars: pd.DataFrame) -> None:
    """5/28 17:00 ET = 5/28 21:00 UTC (EDT). Bar is now settled."""
    now = datetime(2026, 5, 28, 21, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False
    assert "bar_settled" in info["reason"]


def test_at_session_close_keeps_settled_bar(fixture_bars: pd.DataFrame) -> None:
    """16:00 ET sharp is the boundary. Treat as settled (>= cutoff)."""
    now = datetime(2026, 5, 28, 20, 0, tzinfo=UTC)  # 16:00 ET in EDT

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False


def test_pre_open_drops_bar(fixture_bars: pd.DataFrame) -> None:
    """Before the open of 5/28, the 5/28 bar is also still-forming (no print)."""
    now = datetime(2026, 5, 28, 13, 0, tzinfo=UTC)  # 09:00 ET

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 1
    assert info["still_forming_dropped"] is True


# --- Pass-through cases ---


def test_crypto_passthrough(fixture_bars: pd.DataFrame) -> None:
    """Crypto bars are continuous; no session-close concept."""
    now = datetime(2026, 5, 28, 18, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "crypto", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False
    assert info["reason"] == "crypto_continuous_bars"


# --- Intraday timeframe-aware boundary (ADR-0083 Phase 0a) ---


@pytest.fixture
def intraday_1h_bars() -> pd.DataFrame:
    """Three 1h bars stamped at period START (yfinance/ccxt convention).

    14:00 and 15:00 are closed hours; 16:00 is the current still-forming hour.
    """
    return pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2026-05-28 14:00", tz="UTC"),
                pd.Timestamp("2026-05-28 15:00", tz="UTC"),
                pd.Timestamp("2026-05-28 16:00", tz="UTC"),
            ],
            "open": [100.0, 101.0, 102.0],
            "high": [101.5, 102.5, 103.0],
            "low": [99.5, 100.5, 101.0],
            "close": [101.0, 102.0, 102.5],
            "volume": [1e6, 9e5, 5e5],
        }
    )


def test_1h_mid_hour_drops_current_incomplete_hour(
    intraday_1h_bars: pd.DataFrame,
) -> None:
    """At 16:30 UTC the 16:00 hour is still forming (closes 17:00); drop it,
    but keep the closed 14:00 and 15:00 hours."""
    now = datetime(2026, 5, 28, 16, 30, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(intraday_1h_bars, "1h", "equity", now=now)

    assert len(trimmed) == 2
    assert [pd.Timestamp(t).hour for t in trimmed["timestamp"]] == [14, 15]
    assert float(trimmed["close"].iloc[-1]) == 102.0  # last CLOSED hour (15:00)
    assert info["still_forming_dropped"] is True
    assert info["still_forming_close"] == 102.5  # the dropped 16:00 bar
    assert info["still_forming_volume"] == 5e5
    assert "dropped" in info["reason"]


def test_1h_after_hour_close_keeps_all_bars(intraday_1h_bars: pd.DataFrame) -> None:
    """At 17:00 UTC the 16:00 hour has closed (cutoff == 17:00, settled at >=);
    keep every closed hour."""
    now = datetime(2026, 5, 28, 17, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(intraday_1h_bars, "1h", "equity", now=now)

    assert len(trimmed) == 3
    assert info["still_forming_dropped"] is False
    assert "bar_settled" in info["reason"]


def test_1h_crypto_intraday_period_still_forms() -> None:
    """Intraday crypto is NOT a daily continuous pass-through: a 1h crypto bar
    is still forming until the hour closes (the boundary is asset-class
    independent for intraday)."""
    bars = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2026-05-28 14:00", tz="UTC"),
                pd.Timestamp("2026-05-28 15:00", tz="UTC"),
            ],
            "open": [100.0, 101.0],
            "high": [101.5, 102.5],
            "low": [99.5, 100.5],
            "close": [101.0, 102.0],
            "volume": [1e6, 9e5],
        }
    )
    now = datetime(2026, 5, 28, 15, 30, tzinfo=UTC)  # 15:00 hour forming

    trimmed, info = drop_still_forming_bar(bars, "1h", "crypto", now=now)

    assert len(trimmed) == 1
    assert info["still_forming_dropped"] is True


@pytest.mark.parametrize(
    "tf,minutes",
    [("5m", 5), ("15m", 15), ("30m", 30), ("4h", 240)],
)
def test_intraday_timeframes_use_own_period_boundary(tf: str, minutes: int) -> None:
    """Each intraday TF's still-forming boundary is bar_start + its own period."""
    bar_start = pd.Timestamp("2026-05-28 14:00", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": [bar_start - pd.Timedelta(minutes=minutes), bar_start],
            "open": [100.0, 101.0],
            "high": [101.5, 102.5],
            "low": [99.5, 100.5],
            "close": [101.0, 102.0],
            "volume": [1e6, 9e5],
        }
    )

    # One second before the period closes -> still forming -> dropped.
    pre = (bar_start + pd.Timedelta(minutes=minutes) - pd.Timedelta(seconds=1)).to_pydatetime()
    trimmed_pre, info_pre = drop_still_forming_bar(bars, tf, "equity", now=pre)
    assert len(trimmed_pre) == 1
    assert info_pre["still_forming_dropped"] is True

    # At the period close -> settled (>= cutoff) -> kept.
    at = (bar_start + pd.Timedelta(minutes=minutes)).to_pydatetime()
    trimmed_at, info_at = drop_still_forming_bar(bars, tf, "equity", now=at)
    assert len(trimmed_at) == 2
    assert info_at["still_forming_dropped"] is False


def test_unknown_timeframe_passthrough(fixture_bars: pd.DataFrame) -> None:
    """A timeframe we cannot bound passes through (never drop what we can't bound)."""
    now = datetime(2026, 5, 28, 18, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(fixture_bars, "7m", "equity", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False
    assert "unbounded_timeframe=7m" in info["reason"]


def test_empty_bars_passthrough() -> None:
    """Empty input returns empty output; no error."""
    empty = pd.DataFrame(
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

    trimmed, info = drop_still_forming_bar(empty, "1d", "equity")

    assert len(trimmed) == 0
    assert info["still_forming_dropped"] is False
    assert info["reason"] == "empty_bars"


# --- Edge cases ---


def test_index_based_timestamps(fixture_bars: pd.DataFrame) -> None:
    """Some callers pass timestamps as the DataFrame index."""
    indexed = fixture_bars.set_index("timestamp")
    now = datetime(2026, 5, 28, 18, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(indexed, "1d", "equity", now=now)

    assert len(trimmed) == 1
    assert info["still_forming_dropped"] is True


def test_naive_timestamps_treated_as_utc(fixture_bars: pd.DataFrame) -> None:
    """Tz-naive timestamps from validate_bars are localized to UTC implicitly."""
    naive = fixture_bars.copy()
    naive["timestamp"] = pd.to_datetime(naive["timestamp"]).dt.tz_localize(None)
    now = datetime(2026, 5, 28, 18, 0, tzinfo=UTC)

    trimmed, info = drop_still_forming_bar(naive, "1d", "equity", now=now)

    # Whether-tz-aware shouldn't change the drop decision
    assert len(trimmed) == 1
    assert info["still_forming_dropped"] is True


def test_winter_est_handles_dst_correctly() -> None:
    """In January, ET is UTC-5 (EST). 16:00 ET = 21:00 UTC, not 20:00."""
    bars = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2026-01-15", tz="UTC"),
                pd.Timestamp("2026-01-16", tz="UTC"),
            ],
            "open": [100.0, 102.0],
            "high": [101.5, 103.0],
            "low": [99.5, 101.0],
            "close": [101.0, 102.5],
            "volume": [1e6, 5e5],
        }
    )

    # 1/16 15:00 ET (EST) = 20:00 UTC — still mid-session in winter (close is 21:00 UTC)
    pre_close_winter = datetime(2026, 1, 16, 20, 0, tzinfo=UTC)
    trimmed, info = drop_still_forming_bar(bars, "1d", "equity", now=pre_close_winter)
    assert info["still_forming_dropped"] is True

    # 1/16 17:00 ET (EST) = 22:00 UTC — post-close in winter
    post_close_winter = datetime(2026, 1, 16, 22, 0, tzinfo=UTC)
    trimmed, info = drop_still_forming_bar(bars, "1d", "equity", now=post_close_winter)
    assert info["still_forming_dropped"] is False
