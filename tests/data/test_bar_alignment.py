"""Tests for hermes_quant.data.bar_alignment (ADR-0069).

These tests pin the still-forming-bar discipline: daily-timeframe equity reads
mid-session drop the still-forming last bar; reads after market close keep it;
crypto and intraday timeframes pass through unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    now = datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)

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
    now = datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc)

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False
    assert "bar_settled" in info["reason"]


def test_at_session_close_keeps_settled_bar(fixture_bars: pd.DataFrame) -> None:
    """16:00 ET sharp is the boundary. Treat as settled (>= cutoff)."""
    now = datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc)  # 16:00 ET in EDT

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False


def test_pre_open_drops_bar(fixture_bars: pd.DataFrame) -> None:
    """Before the open of 5/28, the 5/28 bar is also still-forming (no print)."""
    now = datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc)  # 09:00 ET

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "equity", now=now)

    assert len(trimmed) == 1
    assert info["still_forming_dropped"] is True


# --- Pass-through cases ---


def test_crypto_passthrough(fixture_bars: pd.DataFrame) -> None:
    """Crypto bars are continuous; no session-close concept."""
    now = datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)

    trimmed, info = drop_still_forming_bar(fixture_bars, "1d", "crypto", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False
    assert info["reason"] == "crypto_continuous_bars"


@pytest.mark.parametrize("tf", ["5m", "15m", "1h", "4h"])
def test_intraday_timeframes_passthrough(fixture_bars: pd.DataFrame, tf: str) -> None:
    """Intraday bars close on wall-clock; 'still forming' is by design."""
    now = datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)

    trimmed, info = drop_still_forming_bar(fixture_bars, tf, "equity", now=now)

    assert len(trimmed) == 2
    assert info["still_forming_dropped"] is False
    assert tf in info["reason"]


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
    now = datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)

    trimmed, info = drop_still_forming_bar(indexed, "1d", "equity", now=now)

    assert len(trimmed) == 1
    assert info["still_forming_dropped"] is True


def test_naive_timestamps_treated_as_utc(fixture_bars: pd.DataFrame) -> None:
    """Tz-naive timestamps from validate_bars are localized to UTC implicitly."""
    naive = fixture_bars.copy()
    naive["timestamp"] = pd.to_datetime(naive["timestamp"]).dt.tz_localize(None)
    now = datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)

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
    pre_close_winter = datetime(2026, 1, 16, 20, 0, tzinfo=timezone.utc)
    trimmed, info = drop_still_forming_bar(bars, "1d", "equity", now=pre_close_winter)
    assert info["still_forming_dropped"] is True

    # 1/16 17:00 ET (EST) = 22:00 UTC — post-close in winter
    post_close_winter = datetime(2026, 1, 16, 22, 0, tzinfo=timezone.utc)
    trimmed, info = drop_still_forming_bar(bars, "1d", "equity", now=post_close_winter)
    assert info["still_forming_dropped"] is False
