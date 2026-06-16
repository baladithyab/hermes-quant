"""quantcore.universe — lean port of hermes-quant's morning universe scan.

hermes-quant (universe/alpaca_scanner.py + playbook/watchlist_evolution.py)
builds the daily watchlist as: liquidity-/price-filtered universe scan ->
ranked by 30d average dollar volume -> capped -> fed to a journaled evolver.

Cowork version: the CANDIDATE POOL is the user's own broker watchlists
(read-only MCP) instead of the full exchange listing; the scan is the same
deterministic filter+rank. No data acquisition here — the caller fetches
bars (yfinance / broker MCP) and passes them in (hermes posture: the scorer
is decoupled from data acquisition).

Output feeds quant-state/config.json watchlist + a journaled scan record at
quant-state/universe/scan-journal.jsonl (append-only).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path

from pydantic import BaseModel

#: hermes defaults, adapted: price band keeps penny/illiquid junk out;
#: dollar-volume floor keeps names tradeable at retail size without impact.
DEFAULT_MIN_PRICE = 3.0
DEFAULT_MAX_PRICE = 2000.0
DEFAULT_MIN_AVG_DOLLAR_VOLUME = 5_000_000.0  # $5M/day (hermes used higher for full-exchange scans)
DEFAULT_MAX_SYMBOLS = 8
MIN_BARS = 15  # don't trust a dollar-volume estimate on fewer bars


class ScanRow(BaseModel):
    symbol: str
    asset_class: str = "equity"
    last_close: float
    avg_dollar_volume: float
    n_bars: int
    admitted: bool
    reason: str


def scan_universe(
    bars_by_symbol: dict[str, list[dict]],
    *,
    asset_class_by_symbol: dict[str, str] | None = None,
    min_price: float = DEFAULT_MIN_PRICE,
    max_price: float = DEFAULT_MAX_PRICE,
    min_avg_dollar_volume: float = DEFAULT_MIN_AVG_DOLLAR_VOLUME,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
) -> list[ScanRow]:
    """Pure scan: filter + rank the candidate pool.

    bars_by_symbol: symbol -> list of {"close": float, "volume": float} dicts
    (chronological, daily, CLOSED bars only — caller enforces asof-honesty).
    Crypto symbols (asset_class 'crypto') skip the price band (BTC > max_price
    is fine) but still need the dollar-volume floor.

    Returns every candidate as a ScanRow (admitted or not, with reason) —
    the journal records the rejects too (hermes journaled-evolution posture).
    Admitted rows are ranked by avg_dollar_volume descending, capped at
    max_symbols (crypto counted separately, cap 2).
    """
    classes = asset_class_by_symbol or {}
    rows: list[ScanRow] = []
    for symbol in sorted(bars_by_symbol):
        bars = [
            b
            for b in bars_by_symbol[symbol]
            if b.get("close") and b.get("volume") is not None
            and b["close"] > 0 and b["volume"] >= 0
        ]
        ac = classes.get(symbol, "equity")
        if len(bars) < MIN_BARS:
            rows.append(
                ScanRow(
                    symbol=symbol, asset_class=ac, last_close=bars[-1]["close"] if bars else 0.0,
                    avg_dollar_volume=0.0, n_bars=len(bars), admitted=False,
                    reason=f"insufficient_bars_{len(bars)}<{MIN_BARS}",
                )
            )
            continue
        last_close = float(bars[-1]["close"])
        window = bars[-30:]
        adv = sum(float(b["close"]) * float(b["volume"]) for b in window) / len(window)
        if ac != "crypto" and not (min_price <= last_close <= max_price):
            rows.append(
                ScanRow(
                    symbol=symbol, asset_class=ac, last_close=last_close,
                    avg_dollar_volume=adv, n_bars=len(bars), admitted=False,
                    reason=f"price_band_{last_close:.2f}",
                )
            )
            continue
        if adv < min_avg_dollar_volume:
            rows.append(
                ScanRow(
                    symbol=symbol, asset_class=ac, last_close=last_close,
                    avg_dollar_volume=adv, n_bars=len(bars), admitted=False,
                    reason=f"dollar_volume_{adv:,.0f}<{min_avg_dollar_volume:,.0f}",
                )
            )
            continue
        rows.append(
            ScanRow(
                symbol=symbol, asset_class=ac, last_close=last_close,
                avg_dollar_volume=adv, n_bars=len(bars), admitted=True, reason="admitted",
            )
        )

    # Rank admitted by dollar volume desc; cap equities and crypto separately.
    admitted_eq = sorted(
        (r for r in rows if r.admitted and r.asset_class != "crypto"),
        key=lambda r: -r.avg_dollar_volume,
    )
    admitted_cr = sorted(
        (r for r in rows if r.admitted and r.asset_class == "crypto"),
        key=lambda r: -r.avg_dollar_volume,
    )
    keep = {r.symbol for r in admitted_eq[:max_symbols]} | {r.symbol for r in admitted_cr[:2]}
    for r in rows:
        if r.admitted and r.symbol not in keep:
            r.admitted = False
            r.reason = "capped_by_rank"
    return rows


def journal_scan(state_dir: Path, rows: list[ScanRow], *, source: str) -> dict:
    """Append the scan to quant-state/universe/scan-journal.jsonl (append-only)
    and return the summary record. fsync'd like the ledger."""
    udir = Path(state_dir) / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    path = udir / "scan-journal.jsonl"
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "admitted": [r.symbol for r in rows if r.admitted],
        "rows": [r.model_dump() for r in rows],
    }
    with open(path, "a", buffering=1) as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return record
