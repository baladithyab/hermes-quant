#!/usr/bin/env python3
"""quant-bootstrap-calibrator.py — fit IsotonicCalibrator from historical Alpaca bars.

This is the offline counterpart to the live calibration loop. It replays the
hermes-quant advisor pipeline against ~1y of historical Alpaca paper bars and
fits an IsotonicCalibrator on the (raw_confidence → direction_correct) pairs
that the analysts emit.

Posture (preserved from AGENTS.md):
- READ-ONLY against Alpaca. paper=True. NO order paths anywhere.
- Silence-by-default: an analyst exception → log + skip the bar (not the run).
- Reproducibility: deterministic seed. Output is atomic-replaced.

Why this exists: cold-start ColdStartCalibrator (Beta(2,5)) caps calibrated
confidence at 0.375, and the risk gate's signed-edge filter silences every
signal at that level. Without trading, no settled outcomes accrue → the
IsotonicCalibrator can never reach its N>=200 threshold via live data alone.
The bootstrap synthesizes those (raw, correct) pairs from history so the
aggregator can switch to a fitted calibrator on the next process restart.
See docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Re-exec into the hermes-agent venv where hermes-quant + alpaca-py are installed.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])


UNIVERSE_PATH = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"
ALPACA_ENV = Path.home() / ".hermes" / "secrets" / "alpaca.env"
DEFAULT_OUTPUT = Path.home() / ".hermes" / "quant" / "calibrators" / "isotonic.pkl"


def _source_alpaca_env() -> None:
    """Source ~/.hermes/secrets/alpaca.env into os.environ if creds aren't already set."""
    if os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY"):
        return
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET"):
        return
    if not ALPACA_ENV.exists():
        print(
            f"WARNING: {ALPACA_ENV} not found and no ALPACA_API_KEY* in env",
            file=sys.stderr,
        )
        return
    for line in ALPACA_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _load_universe(top_n: int) -> list[str]:
    if not UNIVERSE_PATH.exists():
        raise SystemExit(
            f"universe file not found: {UNIVERSE_PATH}\n"
            "Run the daily Alpaca scanner first (universe.alpaca_scanner.scan_universe)."
        )
    payload = json.loads(UNIVERSE_PATH.read_text())
    rows = payload.get("symbols", [])
    syms = [r["symbol"] for r in rows[:top_n] if "symbol" in r]
    if not syms:
        raise SystemExit(f"no symbols loaded from {UNIVERSE_PATH}")
    return syms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=100, help="how many top symbols to use (default 100)")
    parser.add_argument("--days", type=int, default=365, help="lookback days (default 365)")
    parser.add_argument(
        "--horizon-bars",
        type=int,
        default=4,
        help="forward-return window in bars (default 4 ≈ 4 trading days)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output pickle path (default {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=200,
        help="min (raw, correct) pairs to actually fit the calibrator (default 200)",
    )
    parser.add_argument(
        "--include-kronos",
        action="store_true",
        help="include KronosAnalyst (slow ~75s/symbol; default off)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG-level logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    _source_alpaca_env()

    syms = _load_universe(args.top)
    print(f"bootstrap: replaying {len(syms)} symbols, {args.days}d lookback, "
          f"horizon={args.horizon_bars} bars")
    print(f"bootstrap: first 10 syms: {syms[:10]}")

    from hermes_quant.training.bootstrap_calibrator import bootstrap_calibrator

    result = bootstrap_calibrator(
        symbols=syms,
        days=args.days,
        timeframe="1d",
        horizon_bars=args.horizon_bars,
        output_path=args.output,
        min_samples=args.min_samples,
        include_kronos=args.include_kronos,
    )

    print()
    print("=" * 60)
    print("BOOTSTRAP RESULT")
    print("=" * 60)
    print(json.dumps(result, indent=2, default=str))

    if result.get("fitted"):
        # Reload to confirm + show calibrated mappings at standard probes.
        import pickle

        with open(args.output, "rb") as f:
            cal = pickle.load(f)
        print()
        print("calibrator.status():", cal.status())
        print()
        print("RAW → CALIBRATED mapping (fitted isotonic):")
        for raw in (0.1, 0.3, 0.5, 0.7, 0.9):
            print(f"   {raw:.2f}  →  {cal.calibrate(raw):.4f}")
    else:
        print()
        print("calibrator was NOT fitted (n_samples<min_samples or fit error).")
        print("re-run with more symbols or longer --days to accumulate samples.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
