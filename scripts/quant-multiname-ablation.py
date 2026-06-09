#!/usr/bin/env python3
"""Multi-name flag-ablation runner — fair verdicts on a representative universe.

The single-symbol-SPY flag ablations (EVENT_RISK, OVERNIGHT_DRIFT) both returned
HOLD, but the verdict docs flagged that as a likely-to-fail, unrepresentative
universe — the documented re-open condition for BOTH was "multi-name /
cohort-weighted universe". The harness (run_flag_ablation -> WalkForwardEngine ->
AdvisorStrategy) already supports a multi-name universe via a (field, symbol)
MultiIndex-column ohlcv frame; the single-symbol runs were a choice, not a limit.

This builds that frame from real yfinance bars and runs the ablation for a chosen
flag over a chosen cohort, printing the promote/HOLD card. It is committed (not a
/tmp throwaway) so any future flag verdict can be re-run fairly.

Usage:
    python scripts/quant-multiname-ablation.py FLAG SYM1,SYM2,... [START] [END]

Examples:
    # OVERNIGHT_DRIFT on the high-retail-attention / high-beta cohort the research cites
    python scripts/quant-multiname-ablation.py HERMES_QUANT_OVERNIGHT_DRIFT \
        QQQ,ARKK,TSLA,NVDA,GME,COIN 2023-01-01 2024-12-31

    # EVENT_RISK (FOMC blackout) on a rate-sensitive multi-name set
    python scripts/quant-multiname-ablation.py HERMES_QUANT_EVENT_RISK \
        SPY,QQQ,TLT,XLF,IWM 2023-01-01 2024-12-31

Run under: UV_LINK_MODE=copy DISABLE_KRONOS=1 HERMES_QUANT_RUN_BACKTEST=1 uv run --no-sync python scripts/quant-multiname-ablation.py ...
"""
from __future__ import annotations

import json
import sys

import pandas as pd

from hermes_quant.backtest.ablation import run_flag_ablation
from hermes_quant.backtest.engine import WalkForwardConfig
from hermes_quant.backtest.strategy import AdvisorStrategy
from hermes_quant.data.yfinance_provider import YFinanceProvider

# For EVENT_RISK the carrier must be injected (the offline AdvisorStrategy path
# does not populate signal.metadata['event_risk']) — so that flag is measured via
# the dedicated EventRiskAblationStrategy with a real FOMC calendar. OVERNIGHT_DRIFT
# gates the loadout, so plain AdvisorStrategy(analysts=None) measures it directly.
EVENT_RISK_FLAG = "HERMES_QUANT_EVENT_RISK"


def _assemble_multiindex(frames: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, list[str]]:
    """Pure assembly: per-symbol OHLCV frames -> a (field, symbol) MultiIndex-column
    frame on the UNION of trading days (forward-filled within each symbol so a
    missing day for one name does not drop the row for the cohort).

    Split out from the network fetch so it is unit-testable without yfinance.
    """
    if len(frames) < 2:
        raise ValueError(f"need >=2 symbols with data; got {list(frames)}")
    union_idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    cols = {}
    for sym, f in frames.items():
        f2 = f.reindex(union_idx).ffill()
        for field in ["open", "high", "low", "close", "volume"]:
            cols[(field, sym)] = f2[field]
    ohlcv = pd.DataFrame(cols, index=union_idx)
    ohlcv.columns = pd.MultiIndex.from_tuples(ohlcv.columns)
    return ohlcv, list(frames)


def _build_multiname_ohlcv(symbols: list[str], start: str, end: str) -> tuple[pd.DataFrame, list[str]]:
    """Fetch per-symbol daily bars (yfinance) and assemble the MultiIndex frame."""
    provider = YFinanceProvider()
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        bars = provider.fetch_bars(sym, "1d", pd.Timestamp(start), pd.Timestamp(end))
        if bars is None or len(bars) < 100:
            print(f"# WARN: {sym} insufficient bars ({0 if bars is None else len(bars)}) — skipping")
            continue
        if "timestamp" in bars.columns:
            bars = bars.set_index(pd.DatetimeIndex(pd.to_datetime(bars["timestamp"]))).drop(
                columns=["timestamp"]
            )
        bars.index = pd.DatetimeIndex(bars.index).tz_localize(None)
        frames[sym] = bars[["open", "high", "low", "close", "volume"]]
    return _assemble_multiindex(frames)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    flag = sys.argv[1]
    symbols = [s.strip().upper() for s in sys.argv[2].split(",") if s.strip()]
    start = sys.argv[3] if len(sys.argv) > 3 else "2023-01-01"
    end = sys.argv[4] if len(sys.argv) > 4 else "2024-12-31"

    ohlcv, present = _build_multiname_ohlcv(symbols, start, end)
    idx = pd.DatetimeIndex(ohlcv.index)
    print(f"# universe: {present}  bars: {len(idx)}  {idx[0].date()} -> {idx[-1].date()}")

    split = min(max(60, len(idx) // 3), len(idx) - 2)
    config = WalkForwardConfig(
        train_start=idx[0],
        train_end=idx[split - 1],
        holdout_start=idx[split],
        holdout_end=idx[-1],
        step_days=1,
        lookback_days=400,
        initial_nav=100_000.0,
    )

    if flag == EVENT_RISK_FLAG:
        # Carrier-injecting strategy + real FOMC calendar (gate-seam flag).
        from hermes_quant.backtest.event_risk_ablation import (
            EventRiskAblationStrategy,
            historical_fomc_calendar,
        )

        cal = historical_fomc_calendar()

        def _factory():
            return EventRiskAblationStrategy(present, calendar=cal, learn_from_fills=True)
    else:
        # Loadout-gating flag (e.g. OVERNIGHT_DRIFT): plain advisor loadout reads
        # the flag inside each leg's env-override.
        def _factory():
            return AdvisorStrategy(present, analysts=None, learn_from_fills=True)

    result = run_flag_ablation(flag, strategy_factory=_factory, universe=present, ohlcv=ohlcv, config=config)
    print(json.dumps({"flag": flag, "universe": present, "window": f"{start} -> {end}", **result.to_dict()}, indent=2, default=str))


if __name__ == "__main__":
    main()
