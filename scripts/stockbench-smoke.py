#!/usr/bin/env python
"""scripts/stockbench-smoke.py — STOCKBENCH smoke harness CLI.

Usage::

    python scripts/stockbench-smoke.py \
        --window 2025-06-01:2025-08-31 \
        --universe AAPL,MSFT,NVDA,GOOG,META \
        --benchmark SPY

Outputs the STOCKBENCHResult as JSON + a summary table.

Uses StubLLMCommittee / synthetic price data so it does NOT burn API budget.
The evaluation window must be post-knowledge-cutoff (default: 2025-01-01).

The Contamination guard is enforced: passing a pre-cutoff window will exit
with a non-zero status code and a clear error message.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Stub strategy (no LLM API calls)
# ---------------------------------------------------------------------------


class _StubLLMCommitteeStrategy:
    """Lightweight stub that simulates a committee decision without LLM calls.

    Decision logic: simple momentum — long when last return > 0, short otherwise.
    This is intentionally naïve; the point is to measure the harness, not the model.
    """

    def decide(self, ticker: str, as_of: date, price_history) -> float:  # noqa: ANN001
        import numpy as np

        prices = price_history
        if len(prices) < 2:
            return 0.0
        last_return = (prices[-1] - prices[-2]) / prices[-2]
        if last_return > 0.005:
            return 1.0
        elif last_return < -0.005:
            return -1.0
        return 0.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="STOCKBENCH smoke harness — contamination-safe eval",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--window",
        default="2025-06-01:2025-08-31",
        help="Evaluation window as START:END (ISO dates). Default: 2025-06-01:2025-08-31",
    )
    parser.add_argument(
        "--universe",
        default="AAPL,MSFT,NVDA,GOOG,META",
        help="Comma-separated ticker list (default: AAPL,MSFT,NVDA,GOOG,META)",
    )
    parser.add_argument(
        "--benchmark",
        default="SPY",
        help="Benchmark ticker for buy-and-hold comparison (default: SPY)",
    )
    parser.add_argument(
        "--json-out",
        metavar="FILE",
        help="Write JSON result to FILE instead of stdout",
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="Warn instead of error on contaminated windows",
    )
    return parser.parse_args(argv)


def _parse_window(window_str: str) -> tuple[date, date]:
    parts = window_str.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"Window must be in START:END format, got: {window_str!r}"
        )
    return date.fromisoformat(parts[0].strip()), date.fromisoformat(parts[1].strip())


def _fmt_pct(v: float) -> str:
    if not math.isfinite(v):
        return str(v)
    return f"{v:+.2%}"


def _fmt_f2(v: float) -> str:
    if not math.isfinite(v):
        return str(v)
    return f"{v:+.4f}"


def _print_summary_table(result: Any) -> None:
    col_w = 30
    sep = "=" * (col_w * 2 + 3)
    print(sep)
    print("STOCKBENCH Evaluation Summary")
    print(sep)
    rows = [
        ("Universe", ", ".join(result.universe)),
        ("Window", f"{result.window_start} → {result.window_end}"),
        ("Benchmark", result.benchmark),
        ("Cumulative Return", _fmt_pct(result.cumulative_return)),
        ("Buy-and-Hold Return", _fmt_pct(result.metadata.get("buyhold_cumulative_return", float("nan")))),
        ("vs. Buy-and-Hold Alpha", _fmt_pct(result.vs_buyhold_alpha)),
        ("Max Drawdown", _fmt_pct(result.max_drawdown)),
        ("Sortino Ratio", _fmt_f2(result.sortino)),
        ("Total Decisions", str(result.n_decisions)),
        ("Decisions / Day", f"{result.decisions_per_day_avg:.3f}"),
        ("Contamination Guard Fired", str(result.contamination_guard_fired)),
    ]
    for label, value in rows:
        print(f"  {label:<{col_w}}{value}")
    print(sep)

    # PromotionGate decision
    try:
        from hermes_quant.eval.promotion_gate import PromotionGate
        gate = PromotionGate()
        decision = gate.check(result)
        print()
        status = "✅ PROMOTE" if decision.promote else "❌ DO NOT PROMOTE"
        print(f"Promotion Gate: {status}")
        if decision.reasons:
            print("Reasons:")
            for r in decision.reasons:
                print(f"  • {r}")
        print(f"Suggested action: {decision.suggested_action}")
        print()
    except Exception as exc:  # noqa: BLE001
        print(f"(PromotionGate unavailable: {exc})")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        window_start, window_end = _parse_window(args.window)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    universe = [t.strip().upper() for t in args.universe.split(",") if t.strip()]

    try:
        from hermes_quant.eval.stockbench import STOCKBENCHHarness, ContaminationError
    except ImportError as exc:
        print(f"ERROR: hermes_quant.eval not available: {exc}", file=sys.stderr)
        return 3

    harness = STOCKBENCHHarness(strict_contamination=not args.lenient)
    strategy = _StubLLMCommitteeStrategy()

    try:
        result = harness.run(
            strategy,
            universe=universe,
            window_start=window_start,
            window_end=window_end,
            benchmark=args.benchmark,
        )
    except ContaminationError as exc:
        print(f"CONTAMINATION ERROR: {exc}", file=sys.stderr)
        print(
            "Hint: Pass --lenient to warn instead of error, or use a post-2025 window.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR during evaluation: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 4

    _print_summary_table(result)

    result_dict = result.to_dict()
    json_str = json.dumps(result_dict, indent=2, default=str)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(json_str)
        print(f"\nJSON written to: {args.json_out}")
    else:
        print("\nJSON output:")
        print(json_str)

    return 0


if __name__ == "__main__":
    sys.exit(main())
