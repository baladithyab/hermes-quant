#!/usr/bin/env python3
"""factor-oracle-evaluate.py — CLI wrapper for the FactorOracle.

Evaluates one or all registered alpha factors against a cached OHLCV CSV
and prints a rank-sorted production-readiness table.

Every verdict is automatically persisted to
    ~/.hermes/quant/factors/factor_verdicts.jsonl
(APPEND-ONLY; re-evaluation adds new rows, history preserved).

Usage
-----
    python scripts/factor-oracle-evaluate.py \\
        --bars /path/to/cached_bars.csv \\
        [--factor-id FACTOR_ID] \\
        [--profile premium|standard|experimental] \\
        [--fwd-horizon 5]

Output columns
--------------
    factor_id | name | tier | icir | ic_mean | hit_rate | production_ready

Exit codes
----------
    0 — success (at least one verdict produced)
    1 — error (bad args, missing file, no factors registered, etc.)

Notes
-----
- The CSV must have a DatetimeIndex (column "date" or index) and at minimum
  columns: open, high, low, close, volume.
- If --factor-id is omitted, ALL factors registered in the AlphaZoo are
  evaluated and printed sorted by ICIR descending.
- --profile filters the output table to show only factors at that tier or above
  (e.g. --profile standard shows premium + standard rows).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Module imports — resolved relative to the repo root
# ---------------------------------------------------------------------------
try:
    from hermes_quant.factors.alpha_zoo import AlphaZoo
    from hermes_quant.factors.factor_oracle import FactorOracle, ProductionReadinessThresholds
    from hermes_quant.factors.starter_set import register_starter_set as register_starter_factors
except ImportError as exc:
    print(f"ERROR: could not import hermes_quant: {exc}", file=sys.stderr)
    print(
        "Make sure you have activated the venv and are running from the repo root.",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Tier ordering (higher = better)
# ---------------------------------------------------------------------------
_TIER_ORDER = {"premium": 3, "standard": 2, "experimental": 1, "rejected": 0}


def _load_bars(path: Path) -> pd.DataFrame:
    """Load OHLCV bars from a CSV file.

    Expects a 'date' column (or an existing DatetimeIndex) and columns:
    open, high, low, close, volume.
    """
    df = pd.read_csv(path, parse_dates=True)
    # Detect date column
    if "date" in df.columns:
        df = df.set_index("date")
    elif "Date" in df.columns:
        df = df.set_index("Date")
    df.index = pd.to_datetime(df.index)
    df.columns = [c.lower() for c in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        print(f"ERROR: bars CSV is missing columns: {missing}", file=sys.stderr)
        sys.exit(1)
    df = df.sort_index()
    return df


def _build_thresholds(profile: str) -> ProductionReadinessThresholds:
    """Return default thresholds (profile arg affects output filter, not thresholds)."""
    return ProductionReadinessThresholds()


def _print_table(
    rows: list[tuple],
    profile_filter: str | None,
) -> None:
    """Print a formatted rank-sorted table."""
    if not rows:
        print("No verdicts produced.")
        return

    # Filter by profile if requested
    if profile_filter:
        min_rank = _TIER_ORDER.get(profile_filter, 0)
        rows = [r for r in rows if _TIER_ORDER.get(r[3], 0) >= min_rank]

    if not rows:
        print(f"No factors at or above the '{profile_filter}' tier.")
        return

    # Header
    header = f"{'RANK':<5} {'FACTOR_ID':<20} {'NAME':<35} {'TIER':<14} {'ICIR':>7} {'IC_MEAN':>8} {'HIT_RT':>7} {'READY':<6}"
    print(header)
    print("-" * len(header))

    for i, (factor_id, name, icir, tier, ic_mean, hit_rate, production_ready) in enumerate(
        rows, start=1
    ):
        def _fmt(v: object) -> str:
            if isinstance(v, float):
                if v != v:
                    return "   NaN"
                return f"{v:7.4f}"
            return str(v)

        ready_str = "✓" if production_ready else "✗"
        print(
            f"{i:<5} {factor_id:<20} {name[:34]:<35} {tier:<14} "
            f"{_fmt(icir):>7} {_fmt(ic_mean):>8} {_fmt(hit_rate):>7} {ready_str:<6}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate alpha factors via the FactorOracle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--bars",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to OHLCV CSV file.",
    )
    parser.add_argument(
        "--factor-id",
        dest="factor_id",
        default=None,
        metavar="FID",
        help="Evaluate a single factor by ID. Omit to evaluate all.",
    )
    parser.add_argument(
        "--profile",
        choices=["premium", "standard", "experimental"],
        default=None,
        help="Filter output table to this tier and above.",
    )
    parser.add_argument(
        "--fwd-horizon",
        dest="fwd_horizon",
        type=int,
        default=5,
        metavar="N",
        help="Forward-return horizon in days (default: 5).",
    )

    args = parser.parse_args(argv)

    # ---- Load bars ----
    if not args.bars.exists():
        print(f"ERROR: bars file not found: {args.bars}", file=sys.stderr)
        return 1
    print(f"Loading bars from {args.bars} …")
    try:
        bars = _load_bars(args.bars)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to load bars: {exc}", file=sys.stderr)
        return 1
    print(f"  Loaded {len(bars)} bars ({bars.index[0]} → {bars.index[-1]})")

    # ---- Build AlphaZoo (loads all registered factors) ----
    zoo = AlphaZoo()

    # Auto-populate starter factors if the zoo is empty
    if not zoo.list_all():
        print("No factors registered; auto-loading starter set …")
        try:
            register_starter_factors(zoo)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: starter set registration failed: {exc}", file=sys.stderr)

    all_factors = zoo.list_all()
    if not all_factors:
        print("ERROR: no factors registered in the AlphaZoo.", file=sys.stderr)
        return 1
    print(f"  {len(all_factors)} factor(s) registered.")

    # ---- Build FactorOracle ----
    thresholds = _build_thresholds(args.profile or "experimental")
    oracle = FactorOracle(zoo, thresholds=thresholds)

    # ---- Evaluate ----
    if args.factor_id:
        if zoo.read(args.factor_id) is None:
            print(f"ERROR: factor {args.factor_id!r} not found in registry.", file=sys.stderr)
            return 1
        print(f"Evaluating factor {args.factor_id!r} …")
        try:
            verdict = oracle.evaluate(args.factor_id, bars, fwd_horizon_days=args.fwd_horizon)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: evaluation failed: {exc}", file=sys.stderr)
            return 1
        rows = [(
            verdict.factor_id,
            verdict.name,
            verdict.ic_panel.get("icir"),
            verdict.tier,
            verdict.ic_panel.get("ic_mean"),
            verdict.ic_panel.get("hit_rate"),
            verdict.production_ready,
        )]
    else:
        print(f"Evaluating all {len(all_factors)} factor(s) …")
        try:
            ranked = oracle.rank(bars, fwd_horizon_days=args.fwd_horizon)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: evaluation failed: {exc}", file=sys.stderr)
            return 1
        rows = [
            (
                fid,
                v.name,
                v.ic_panel.get("icir"),
                v.tier,
                v.ic_panel.get("ic_mean"),
                v.ic_panel.get("hit_rate"),
                v.production_ready,
            )
            for fid, v in ranked
        ]

    print()
    _print_table(rows, args.profile)
    print(f"\nVerdicts persisted to: {oracle._verdicts_path}")  # noqa: SLF001
    return 0


if __name__ == "__main__":
    sys.exit(main())
