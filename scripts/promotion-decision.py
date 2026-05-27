#!/usr/bin/env python3
"""scripts/promotion-decision.py — One-shot promotion evaluation CLI (ADR-0052).

Runs the STOCKBENCH harness for the named strategy + window, feeds the result
through :class:`PromotionGate`, and prints a human-readable summary table plus
the full PromotionRecord JSON.

Exit codes
----------
0   PromotionDecision.promote = True  (strategy passed all gate criteria)
1   PromotionDecision.promote = False (one or more criteria failed)
2   Unexpected error (stack trace printed to stderr)

Usage
-----
.. code-block:: bash

    # Evaluate buy-and-hold over a 3-month window:
    python scripts/promotion-decision.py \\
        --strategy buyhold \\
        --universe AAPL,MSFT \\
        --window 2025-06-01:2025-08-31

    # Evaluate with a hypothesis reference and auto-record:
    python scripts/promotion-decision.py \\
        --strategy buyhold \\
        --universe AAPL,MSFT \\
        --window 2025-06-01:2025-08-31 \\
        --auto-record \\
        --hypothesis-id hyp_AAPL_20250601_abc123

    # Use a hypothesis ID to drive the window/universe (if registered):
    python scripts/promotion-decision.py \\
        --hypothesis-id hyp_AAPL_20250601_abc123

NOTE: This script does NOT call any external LLM.  It uses the built-in
      synthetic price source and the named strategy only.  For real price data,
      configure a PriceSourceProtocol-compatible adapter and wire it in via
      --price-source (future extension).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------


def _load_strategy(name: str):
    """Return a strategy instance by short name."""
    name_lower = name.lower().replace("-", "").replace("_", "")
    if name_lower in ("buyhold", "buyandholdstrategy"):
        from hermes_quant.eval.stockbench import _BuyAndHoldStrategy
        return _BuyAndHoldStrategy()
    raise ValueError(
        f"Unknown strategy name {name!r}.  "
        "Currently supported: 'buyhold'.  "
        "To add a custom strategy, implement StrategyProtocol and register it here."
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_PASS = "✅ PASS"
_FAIL = "❌ FAIL"


def _fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def _fmt_float(v) -> str:
    if v is None:
        return "nan"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v)


def _print_summary_table(record) -> None:
    """Print a human-readable summary to stdout."""
    s = record.stockbench_result_summary
    d = record.decision

    promote = d.get("promote", False)
    reasons = d.get("reasons", [])
    suggested = d.get("suggested_action", "")

    bar = "=" * 60
    print(bar)
    print(f"  PROMOTION DECISION  —  {record.strategy_name}")
    print(bar)
    print(f"  Window     : {record.window_start} → {record.window_end}")
    print(f"  Universe   : {', '.join(s.get('universe', []))}")
    print(f"  Record ID  : {record.record_id}")
    if record.hypothesis_id:
        print(f"  Hypothesis : {record.hypothesis_id}")
    print()

    # Metrics table
    alpha = s.get("vs_buyhold_alpha", 0.0)
    sortino = s.get("sortino")
    drawdown = s.get("max_drawdown", 0.0)
    cum_ret = s.get("cumulative_return", 0.0)
    bh_ret = s.get("buyhold_cumulative_return", 0.0)

    alpha_ok = isinstance(alpha, (int, float)) and alpha > 0
    sortino_ok = isinstance(sortino, (int, float)) and sortino > 0.5
    drawdown_ok = isinstance(drawdown, (int, float)) and drawdown > -0.20

    rows = [
        ("Cumulative Return", _fmt_pct(cum_ret), ""),
        ("Buy-and-Hold Return", _fmt_pct(bh_ret), ""),
        ("vs Buy-and-Hold Alpha", _fmt_pct(alpha), _PASS if alpha_ok else _FAIL),
        ("Sortino Ratio", _fmt_float(sortino), _PASS if sortino_ok else _FAIL),
        ("Max Drawdown", _fmt_pct(drawdown), _PASS if drawdown_ok else _FAIL),
        ("Contamination Guard", str(s.get("contamination_guard_fired", False)), ""),
        ("N Decisions", str(s.get("n_decisions", 0)), ""),
    ]

    col_w = [max(len(r[0]) for r in rows) + 2, 14, 12]
    header = (
        f"  {'Metric':<{col_w[0]}} {'Value':>{col_w[1]}} {'Status':>{col_w[2]}}"
    )
    sep = "  " + "-" * (col_w[0] + col_w[1] + col_w[2] + 4)
    print(header)
    print(sep)
    for label, value, status in rows:
        print(f"  {label:<{col_w[0]}} {value:>{col_w[1]}} {status:>{col_w[2]}}")
    print()

    # Gate verdict
    verdict_label = "PROMOTE ✅" if promote else "DO NOT PROMOTE ❌"
    print(f"  Gate Verdict: {verdict_label}")
    print()
    if reasons:
        print("  Failing criteria:")
        for r in reasons:
            print(f"    • {r}")
        print()
    print(f"  Suggested action:\n    {suggested}")
    print(bar)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="promotion-decision.py",
        description=(
            "Run STOCKBENCH harness + PromotionGate for a strategy and print "
            "the PromotionDecision.  Exit 0 = promote, 1 = no-promote, 2 = error."
        ),
    )
    p.add_argument(
        "--strategy",
        default="buyhold",
        metavar="NAME",
        help="Strategy short name (default: 'buyhold').  Supported: buyhold.",
    )
    p.add_argument(
        "--universe",
        default="AAPL,MSFT,NVDA,GOOG,META",
        metavar="TICKERS",
        help="Comma-separated ticker list (default: AAPL,MSFT,NVDA,GOOG,META).",
    )
    p.add_argument(
        "--window",
        default=None,
        metavar="FROM:TO",
        help="Evaluation window as ISO dates FROM:TO (e.g. 2025-06-01:2025-08-31). "
             "Required unless --hypothesis-id is given (future: auto-lookup).",
    )
    p.add_argument(
        "--hypothesis-id",
        default=None,
        metavar="HYP_ID",
        help="Optional hypothesis ID to embed in the PromotionRecord.  "
             "Operators use this to look up the hypothesis and call "
             "HypothesisRegistry.update_status() after reviewing the decision.",
    )
    p.add_argument(
        "--auto-record",
        action="store_true",
        default=False,
        help="Append the PromotionRecord to promotion_decisions.jsonl.",
    )
    p.add_argument(
        "--recorded-by",
        default="system",
        metavar="IDENTITY",
        help="Identity string for the audit trail (default: 'system').",
    )
    p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        default=False,
        help="Output only the PromotionRecord JSON (no summary table).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        from hermes_quant.eval.promotion_orchestrator import PromotionOrchestrator
        from hermes_quant.eval.stockbench import STOCKBENCHHarness

        # --- Parse window ---
        if args.window is None:
            parser.error(
                "--window FROM:TO is required.  "
                "Example: --window 2025-06-01:2025-08-31"
            )
        try:
            from_str, to_str = args.window.split(":")
            window_start = date.fromisoformat(from_str.strip())
            window_end = date.fromisoformat(to_str.strip())
        except ValueError as exc:
            parser.error(f"Invalid --window format: {exc}")

        # --- Parse universe ---
        universe = [t.strip().upper() for t in args.universe.split(",") if t.strip()]

        # --- Load strategy ---
        try:
            strategy = _load_strategy(args.strategy)
        except ValueError as exc:
            parser.error(str(exc))

        # --- Build orchestrator with non-strict contamination for operator use ---
        harness = STOCKBENCHHarness(strict_contamination=False)
        orchestrator = PromotionOrchestrator(harness=harness)

        # --- Run ---
        record = orchestrator.run(
            strategy=strategy,
            universe=universe,
            window_start=window_start,
            window_end=window_end,
            hypothesis_id=args.hypothesis_id,
            strategy_name=args.strategy,
            auto_record=args.auto_record,
            recorded_by=args.recorded_by,
        )

        # --- Output ---
        if args.output_json:
            print(record.model_dump_json(indent=2))
        else:
            _print_summary_table(record)
            print()
            print("Full record JSON:")
            print(record.model_dump_json(indent=2))

        promote = record.decision.get("promote", False)
        return 0 if promote else 1

    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
