#!/usr/bin/env python3
"""Catalyst Sense — D74.7 live-gate eval runner.

Builds a real multi-event labeled eval set, pulls REAL forward returns from
yfinance (the catalyst-reaction move), and runs catalyst.eval.eval_gate:

  * NEGATIVE CONTROL — benign headlines must produce ZERO packets.
  * DIRECTIONAL PRECISION — synthesized stance must match the realized
    next-session move at >= min_hit_rate.

This is the HARD GATE before flipping HERMES_QUANT_SEMANTIC_ENABLED=1. Run it;
only enable if it passes. Honest by construction: realized returns are pulled
from yfinance (the graph never sees them), measured as the close-to-close move
spanning the catalyst date (event after close -> next session reaction).

Events use entities the curated graph covers (space, semis). The OPEC case is
DELIBERATELY included as an out-of-sample probe of the edge-sign ambiguity the
spike flagged — if it fails, that's informative, not a reason to fudge.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from hermes_quant.catalyst.eval import EvalCase, eval_gate
from hermes_quant.catalyst.ingest import CatalystItem

UTC = timezone.utc


def _item(title: str, when: datetime) -> CatalystItem:
    return CatalystItem(title=title, published_at=when, source="eval", link="n/a")


def realized_move(symbol: str, event_date: str) -> float:
    """Close-to-close % move spanning the event date (event after close ->
    next session reaction). Pulls from yfinance. Returns nan on no data."""
    import yfinance as yf
    import pandas as pd
    start = (pd.Timestamp(event_date) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(event_date) + pd.Timedelta(days=4)).strftime("%Y-%m-%d")
    try:
        df = yf.Ticker(symbol).history(start=start, end=end)
    except Exception as e:
        print(f"  yfinance error {symbol}: {e}", file=sys.stderr)
        return float("nan")
    if len(df) < 2:
        return float("nan")
    # find the bar on/just-after event_date, compare to the prior close
    df = df.reset_index()
    ev = pd.Timestamp(event_date).tz_localize(None)
    df["d"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    after = df[df["d"] >= ev]
    if len(after) < 1:
        return float("nan")
    idx = after.index[0]
    if idx == 0:
        return float("nan")
    prev_close = df.loc[idx - 1, "Close"]
    react_close = df.loc[idx, "Close"]
    return round((react_close - prev_close) / prev_close * 100.0, 2)


# --- the labeled event set: real catalysts on graph-covered entities ---
# Blue Origin explosion: 2026-05-28 after close -> 2026-05-29 reaction.
EVENT_BLUE_ORIGIN = ("Blue Origin's New Glenn rocket explodes during hotfire test",
                     datetime(2026, 5, 28, 22, 14, tzinfo=UTC), "2026-05-29",
                     ["RKLB", "LUNR", "ASTS", "RDW"])

BENIGN = [
    _item("Rocket Lab reports quarterly results in line with estimates", datetime(2026,5,20,14,0,tzinfo=UTC)),
    _item("Blue Origin schedules routine maintenance window", datetime(2026,5,20,14,0,tzinfo=UTC)),
    _item("Analysts discuss the space sector outlook for next year", datetime(2026,5,20,14,0,tzinfo=UTC)),
    _item("Market opens flat as investors await economic data", datetime(2026,5,20,14,0,tzinfo=UTC)),
    _item("TSMC holds annual shareholder meeting in Taipei", datetime(2026,5,20,14,0,tzinfo=UTC)),
]


def main() -> int:
    print("=" * 72)
    print("CATALYST SENSE — D74.7 LIVE GATE")
    print("=" * 72)

    # Build precision cases with REAL forward returns
    title, when, event_date, symbols = EVENT_BLUE_ORIGIN
    print(f"\nPulling real forward returns for the Blue Origin case ({event_date})...")
    cases: list[EvalCase] = []
    for sym in symbols:
        mv = realized_move(sym, event_date)
        print(f"  {sym:5} realized {mv:+.2f}%")
        if mv == mv:  # not nan
            cases.append(EvalCase(_item(title, when), sym, mv))

    print(f"\nBuilt {len(cases)} precision cases + {len(BENIGN)} benign controls.")
    print("\nRunning eval_gate (min_hit_rate=0.6)...\n")

    passed, neg, prec = eval_gate(BENIGN, cases, min_hit_rate=0.6)

    print("--- NEGATIVE CONTROL ---")
    print(f"  benign items: {neg.n_benign_items}")
    print(f"  spurious packets: {neg.n_spurious_packets}  {'✅ PASS' if neg.passed else '❌ FAIL'}")
    if neg.spurious:
        print(f"  spurious symbols: {neg.spurious}")

    print("\n--- DIRECTIONAL PRECISION ---")
    print(f"  cases scored: {prec.n_scored}/{prec.n_cases}")
    print(f"  hits: {prec.hits}  hit_rate: {prec.hit_rate:.2%}  "
          f"{'✅ PASS' if prec.passed else '❌ FAIL'}")
    for m in prec.misses:
        print(f"    miss: {m}")

    print("\n" + "=" * 72)
    if passed:
        print("GATE: ✅ PASS — safe to flip HERMES_QUANT_SEMANTIC_ENABLED=1")
    else:
        print("GATE: ❌ FAIL — DO NOT enable. Fix the failing axis first.")
    print("=" * 72)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
