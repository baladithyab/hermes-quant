"""hermes_quant.data.bar_alignment — still-forming-bar discipline (ADR-0069).

When the BMA runs mid-session at the daily timeframe, yfinance returns an
OHLCV frame whose last row is today's still-forming bar. Reading the close
of that row as `last_close` is technically info-available (the latest
intraday tick is real-time public data) but breaks several invariants the
rest of the system relies on:

1. Replay equality: two runs on the same `bar_ts` produce different numbers
   because the still-forming bar's close has shifted between calls.
2. Calibration drift: volatility estimators (`ATR / last_close`, σ from
   daily closes) compute biased numbers when one of the "closes" is a
   partial-bar tick.
3. Slippage attribution corruption: paper fills at intraday-tick prices
   produce 0-bps slippage that live execution cannot reproduce.

The fix is to drop the last bar IF the operator's wall-clock falls before
that bar's session-close cutoff. After the close cutoff, the bar is
settled and we keep it.

This is *not* a lookahead-prevention fix. It is a semantic-clarity fix.
Lookahead prevention is what ADR-0050 (lookahead sentinel) does. This
ADR-0069 helper ensures `last_close` means what every downstream consumer
assumes it means: a settled bar close.

For analysts that genuinely want the latest intraday tick, the dropped
values are surfaced in `extras["still_forming_close"]`, etc.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


# Per-asset-class session-close cutoffs in UTC.
# US equities: 16:00 ET = 20:00 UTC (EDT) or 21:00 UTC (EST).
# Use America/New_York timezone with daylight-saving awareness rather than
# hardcoding a UTC offset that's wrong half the year.
_ET = ZoneInfo("America/New_York")
_EQUITY_CLOSE_ET = time(16, 0)


def _us_equity_session_close(bar_date_utc: pd.Timestamp) -> datetime:
    """Return the UTC datetime of the 16:00 ET session-close on bar_date_utc.

    Handles EDT/EST automatically. yfinance daily bars stamp at 00:00 UTC of
    the trading day's UTC-aligned date — but 00:00 UTC = 20:00 ET *previous
    day* in EDT, which causes the naive .astimezone() approach to land on
    the wrong session. Use the raw UTC date as the trading-day date instead;
    that's the convention yfinance follows for US equities (the bar dated
    "2026-05-28" is the 5/28 NYSE session, regardless of UTC hour).
    """
    trading_date = bar_date_utc.date()  # UTC date == NYSE session date for daily yfinance bars
    et_close = datetime.combine(trading_date, _EQUITY_CLOSE_ET, tzinfo=_ET)
    return et_close.astimezone(timezone.utc)


def drop_still_forming_bar(
    bars: pd.DataFrame,
    timeframe: str,
    asset_class: str,
    *,
    now: datetime | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Drop the last bar if it represents a still-forming session.

    Args:
        bars: OHLCV DataFrame. Either timestamp-as-index or timestamp-as-column.
            Must be sorted ascending by time.
        timeframe: one of the canonical timeframe strings (e.g. "1d", "1h").
            Only "1d" daily-aligned timeframes are subject to this discipline;
            intraday timeframes (5m, 15m, 1h, 4h) and crypto pass through.
        asset_class: "equity" | "crypto" | etc. Crypto bars are continuous
            and have no session-close concept — they pass through.
        now: override for testing. Defaults to datetime.now(timezone.utc).

    Returns:
        (trimmed_bars, info) where:
          - trimmed_bars: original bars or bars without the last row
          - info: dict with keys:
              - still_forming_dropped: bool
              - still_forming_close: float | None  (the dropped bar's close)
              - still_forming_high: float | None
              - still_forming_low: float | None
              - still_forming_volume: float | None
              - reason: str  (human-readable explanation for the decision)

    The function is a no-op (returns bars unchanged + info["still_forming_dropped"]=False)
    in any of these cases:
      - timeframe is not "1d" (intraday bars close on wall-clock; "still
        forming" is a transient-by-design concept, not a discipline issue)
      - asset_class is "crypto" (24/7 continuous bars)
      - the last bar is already past its session-close cutoff (settled)
      - the DataFrame is empty
    """
    if now is None:
        now = datetime.now(timezone.utc)

    info: dict[str, Any] = {
        "still_forming_dropped": False,
        "still_forming_close": None,
        "still_forming_high": None,
        "still_forming_low": None,
        "still_forming_volume": None,
        "reason": "",
    }

    if bars is None or len(bars) == 0:
        info["reason"] = "empty_bars"
        return bars, info

    if timeframe != "1d":
        info["reason"] = f"non_daily_timeframe={timeframe}"
        return bars, info

    if asset_class == "crypto":
        info["reason"] = "crypto_continuous_bars"
        return bars, info

    # Identify the last-bar timestamp. Support both timestamp-as-index and
    # timestamp-as-column (the canonical hermes_quant shape per ADR-0009 §P0).
    if "timestamp" in bars.columns:
        raw_last_ts = bars["timestamp"].iloc[-1]
    else:
        raw_last_ts = bars.index[-1]

    last_ts = pd.Timestamp(raw_last_ts)  # type: ignore[arg-type]
    if last_ts.tz is None:
        last_ts = last_ts.tz_localize("UTC")

    # For US equities, the bar is settled once we're past 16:00 ET on the
    # bar's date. Other asset classes can be added here as needed.
    if asset_class == "equity":
        cutoff = _us_equity_session_close(last_ts)
    else:
        # Conservative default: treat as 24h-after-bar-start cutoff.
        cutoff = (last_ts + pd.Timedelta(days=1)).to_pydatetime()

    if now >= cutoff:
        info["reason"] = (
            f"bar_settled now={now.isoformat()} >= cutoff={cutoff.isoformat()}"
        )
        return bars, info

    # Drop the still-forming bar. Surface its values in info for analysts
    # that opt in to using them.
    last_row = bars.iloc[-1]
    info["still_forming_dropped"] = True
    info["still_forming_close"] = float(last_row.get("close", float("nan")))
    info["still_forming_high"] = float(last_row.get("high", float("nan")))
    info["still_forming_low"] = float(last_row.get("low", float("nan")))
    info["still_forming_volume"] = float(last_row.get("volume", float("nan")))
    info["reason"] = (
        f"dropped now={now.isoformat()} < cutoff={cutoff.isoformat()} "
        f"bar_date={last_ts.date().isoformat()}"
    )

    trimmed = bars.iloc[:-1].copy()
    return trimmed, info
