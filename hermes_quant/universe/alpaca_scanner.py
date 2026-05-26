"""Alpaca-backed daily universe scanner.

Builds a list of US-tradable, fractionable equities filtered on:

  * `tradable=True` (Alpaca will accept orders)
  * `fractionable=True` (Kelly-sized positions of {0.05, 0.10, 0.15, 0.20}*NAV
    on a $200 stock at $5k NAV are sub-share — we need fractional)
  * Last close in [`min_price`, `max_price`]
  * 30-day average dollar volume >= `min_avg_dollar_volume_30d`

Output is a ranked list (descending dollar volume), capped at `max_symbols`,
and atomically written to JSON.

Posture: READ-ONLY against Alpaca. Paper endpoint always. No order flow.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

logger = logging.getLogger(__name__)

# Exchanges we keep. NYSE / NASDAQ / NYSEARCA + AMEX. Drop OTC explicitly.
_KEEP_EXCHANGES = {
    AssetExchange.NYSE,
    AssetExchange.NASDAQ,
    AssetExchange.ARCA,
    AssetExchange.AMEX,
    AssetExchange.BATS,
}
_DROP_EXCHANGES = {AssetExchange.OTC}

# How many symbols to request bars for in one batch. The Alpaca SDK accepts
# arbitrary list sizes but we cap at 100 to keep response payloads sane and
# allow incremental progress logging.
_BATCH_SIZE = 100

# Look back this many calendar days to ensure we have ~20 trading days even
# across weekends/holidays.
_BARS_LOOKBACK_DAYS = 45

# Minimum number of bars before we'll trust the dollar-volume estimate.
_MIN_BARS = 15

# How many trailing bars to average over.
_AVG_WINDOW = 20


def _get_credentials() -> tuple[str, str]:
    """Return (key, secret) from env, with a fallback to the legacy names.

    The task contract says ALPACA_API_KEY / ALPACA_API_SECRET. The user's
    existing scripts use ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (via
    ~/.hermes/secrets/alpaca.env). We accept either so the scanner works
    in both environments.
    """
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET required")
    return key, secret


def _build_trading_client(key: str, secret: str) -> TradingClient:
    return TradingClient(api_key=key, secret_key=secret, paper=True)


def _build_data_client(key: str, secret: str) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(api_key=key, secret_key=secret)


def _list_candidate_assets(client: TradingClient) -> list[Any]:
    """Step 1: pull active US equities, filter to tradable + fractionable, drop OTC."""
    assets = client.get_all_assets(
        GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE,
        )
    )
    out: list[Any] = []
    for a in assets:
        if not getattr(a, "tradable", False):
            continue
        if not getattr(a, "fractionable", False):
            continue
        exch = getattr(a, "exchange", None)
        if exch in _DROP_EXCHANGES:
            continue
        # If the SDK enum check fails (string-typed exchange), accept anything
        # that's not explicitly OTC.
        if exch in _KEEP_EXCHANGES or exch not in _DROP_EXCHANGES:
            out.append(a)
    return out


def _chunks(seq: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _fetch_bars_for_batch(
    data_client: StockHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[Any]]:
    """Return {symbol: [bar, ...]} for the given symbol batch.

    Uses the IEX feed (free with paper accounts). Empty/missing symbols are
    simply absent from the returned dict.
    """
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    try:
        resp = data_client.get_stock_bars(req)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("alpaca bars batch failed for %d syms: %s", len(symbols), exc)
        return {}

    # alpaca-py returns a BarSet whose `.data` is dict[str, list[Bar]]
    data = getattr(resp, "data", None)
    if data is None:
        # Fallback: BarSet supports `.df` and dict-style access on some versions.
        try:
            return {s: list(resp[s]) for s in symbols if s in resp}  # type: ignore[index]
        except Exception:
            return {}
    return {sym: list(bars) for sym, bars in data.items()}


def _avg_dollar_volume(bars: list[Any], window: int = _AVG_WINDOW) -> float:
    """Mean(close * volume) over the trailing `window` bars."""
    if not bars:
        return 0.0
    tail = bars[-window:]
    vals: list[float] = []
    for b in tail:
        close = float(getattr(b, "close", 0.0) or 0.0)
        volume = float(getattr(b, "volume", 0.0) or 0.0)
        vals.append(close * volume)
    if not vals:
        return 0.0
    return statistics.fmean(vals)


def _last_close(bars: list[Any]) -> float:
    if not bars:
        return 0.0
    return float(getattr(bars[-1], "close", 0.0) or 0.0)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def scan_universe(
    *,
    min_price: float = 5.0,
    max_price: float = 500.0,
    min_avg_dollar_volume_30d: float = 5_000_000.0,
    max_symbols: int = 500,
    asset_class: str = "us_equity",
    output_path: Path = Path.home() / ".hermes/quant/universe/alpaca-daily.json",
    trading_client: TradingClient | None = None,
    data_client: StockHistoricalDataClient | None = None,
) -> dict:
    """Build the daily universe and write it to ``output_path``.

    See module docstring for filter semantics. The two ``*_client`` kwargs are
    test seams — production callers should leave them as ``None`` so the
    scanner builds its own paper-mode clients.
    """
    if asset_class != "us_equity":
        raise ValueError(f"only us_equity is supported, got {asset_class!r}")

    if trading_client is None or data_client is None:
        key, secret = _get_credentials()
        if trading_client is None:
            trading_client = _build_trading_client(key, secret)
        if data_client is None:
            data_client = _build_data_client(key, secret)

    # Step 1: assets
    candidates = _list_candidate_assets(trading_client)
    logger.info("alpaca returned %d candidate assets after asset filter", len(candidates))

    # Step 2: bars
    end = datetime.now(UTC)
    start = end - timedelta(days=_BARS_LOOKBACK_DAYS)

    rows: list[dict] = []
    by_symbol = {a.symbol: a for a in candidates}
    symbols = sorted(by_symbol.keys())

    for batch in _chunks(symbols, _BATCH_SIZE):
        bars_by_sym = _fetch_bars_for_batch(data_client, batch, start, end)
        for sym in batch:
            bars = bars_by_sym.get(sym) or []
            if len(bars) < _MIN_BARS:
                continue
            asset = by_symbol[sym]
            close = _last_close(bars)
            advd = _avg_dollar_volume(bars)
            exch_raw = getattr(asset, "exchange", "") or ""
            exch_str = getattr(exch_raw, "value", None) or str(exch_raw)
            rows.append(
                {
                    "symbol": sym,
                    "exchange": exch_str,
                    "last_close": round(close, 4),
                    "avg_dollar_volume_30d": round(advd, 2),
                    "tradable": bool(getattr(asset, "tradable", False)),
                    "shortable": bool(getattr(asset, "shortable", False)),
                    "fractionable": bool(getattr(asset, "fractionable", False)),
                }
            )

    # Step 3: filter + sort + cap
    filtered = [
        r
        for r in rows
        if min_price <= r["last_close"] <= max_price
        and r["avg_dollar_volume_30d"] >= min_avg_dollar_volume_30d
    ]
    filtered.sort(key=lambda r: r["avg_dollar_volume_30d"], reverse=True)
    filtered = filtered[:max_symbols]

    payload = {
        "asof": datetime.now(UTC).isoformat(timespec="seconds"),
        "count": len(filtered),
        "symbols": filtered,
        "filters": {
            "min_price": min_price,
            "max_price": max_price,
            "min_avg_dollar_volume_30d": min_avg_dollar_volume_30d,
            "max_symbols": max_symbols,
            "asset_class": asset_class,
            "output_path": str(output_path),
        },
    }

    # Step 4: atomic write
    _atomic_write_json(Path(output_path), payload)
    logger.info("universe written: %d symbols -> %s", payload["count"], output_path)
    return payload


__all__ = ["scan_universe"]
