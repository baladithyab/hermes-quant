#!/usr/bin/env python3
"""quant-fundamentals-prewarm-daily.py — daily prewarm of the FundamentalsProvider cache.

Per ADR-0064 §D5 + docs/design/v0.6.1-fundamentals-analyst.md §3:

Refreshes the parquet snapshot cache so analyst.analyze() is a pure cache
read on the hot path (no live yfinance during the trading day).

Posture:
- READ-ONLY relative to portfolio state (no orders, no position mutation).
- Fail-soft: yfinance missing or ticker errors do NOT raise; per-ticker
  status strings are reported in the summary.
- Idempotent: a row newer than ttl_hours is skipped (`refresh` returns
  "skipped:fresh"); re-running the same minute is a no-op.

Schedule: 02:00 PT (Mon–Fri) — well before US cash open and after the
prior session's after-hours dust has settled. Cron line:

    0 2 * * 1-5 /home/codeseys/.hermes/hermes-agent/venv/bin/python3 \\
        /mnt/e/CS/github/hermes-quant/scripts/quant-fundamentals-prewarm-daily.py

Exit code:
    0 — completed (regardless of per-ticker success; partial coverage is
        not a system failure — analyst will abstain on missing tickers)
    1 — bootstrap failure (cannot import provider, etc.)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Pin to the hermes-agent venv where hermes-quant is installed (mirrors
# the pattern in scripts/quant-daily-interim.py:30-31).
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

UNIVERSE_PATH = Path.home() / ".hermes" / "scripts" / "quant-universe-interim.txt"
WATCHLIST_PATH = Path.home() / ".hermes" / "quant" / "watchlist" / "play-fit.json"


def load_universe() -> list[str]:
    """Equity-only universe for the fundamentals refresh.

    Mirrors scripts/quant-daily-interim.py:43-94 watchlist union semantics
    BUT keeps only equity tickers (FundamentalsAnalyst is equity-only per
    ADR-0064 §D4): drops anything containing '/' (crypto pair) or ending
    in '=X' (FX pair).

    Returns: deduped list of upper-case tickers.
    """
    seen: set[str] = set()
    rows: list[str] = []

    # Baseline: txt fallback.
    if UNIVERSE_PATH.exists():
        for line in UNIVERSE_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if not parts:
                continue
            ticker = parts[0].upper()
            if "/" in ticker or ticker.endswith("=X"):
                continue  # equity-only
            if ticker not in seen:
                seen.add(ticker)
                rows.append(ticker)

    # Merge: append active watchlist symbols not already present.
    if WATCHLIST_PATH.exists():
        try:
            data = json.loads(WATCHLIST_PATH.read_text())
            for _play, entries in (data.get("plays") or {}).items():
                for entry in entries:
                    if entry.get("state") != "active":
                        continue
                    sym = (entry.get("symbol") or "").upper()
                    if not sym or "/" in sym or sym.endswith("=X"):
                        continue
                    if sym not in seen:
                        seen.add(sym)
                        rows.append(sym)
        except Exception:
            # Watchlist parse failure is non-fatal — txt baseline is fine.
            pass

    return rows


def main() -> int:
    started = datetime.now(timezone.utc)
    try:
        from hermes_quant.data.fundamentals_provider import FundamentalsProvider
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "status": "bootstrap_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                    "started_utc": started.isoformat(),
                }
            )
        )
        return 1

    tickers = load_universe()
    provider = FundamentalsProvider()

    refresh_status: dict[str, str] = {}
    refresh_error: str | None = None
    if tickers:
        try:
            refresh_status = provider.refresh(tickers)
        except Exception as exc:  # noqa: BLE001 — fail-soft per posture
            refresh_error = f"{type(exc).__name__}: {exc}"

    # Roll up per-ticker status into a histogram so the cron log line is
    # bounded regardless of universe size.
    status_counts: dict[str, int] = {}
    for status in refresh_status.values():
        # Bucket the long "error:ExceptionType:msg..." strings under "error:*".
        key = status if not status.startswith("error:") else "error"
        status_counts[key] = status_counts.get(key, 0) + 1

    # Sector-median refresh runs in the same pass — uses cached snapshots
    # only (no extra yfinance calls per ADR-0064 §D6 footnote).
    sector_summary: dict[str, dict] = {}
    sector_error: str | None = None
    if tickers:
        try:
            sector_summary = provider.refresh_sector_medians(tickers)
        except Exception as exc:  # noqa: BLE001
            sector_error = f"{type(exc).__name__}: {exc}"

    finished = datetime.now(timezone.utc)
    summary = {
        "status": "ok" if refresh_error is None else "partial",
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "elapsed_s": round((finished - started).total_seconds(), 2),
        "n_tickers": len(tickers),
        "ticker_status_counts": status_counts,
        "n_sectors": len(sector_summary),
        "sector_summary": {
            sector: {"n": int(info.get("n", 0)), "median_pe": info.get("median_pe")}
            for sector, info in sector_summary.items()
        },
        "refresh_error": refresh_error,
        "sector_error": sector_error,
        "cache_root": str(provider.cache_root),
    }
    print(json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
