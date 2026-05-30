#!/usr/bin/env python3
"""quant-calibrator-drift.py — weekly calibrator drift check (B11).

Replays the bootstrap walk over the configured universe to collect recent
``(raw_confidence, direction_correct)`` pairs, computes the population-level
drift of the live IsotonicCalibrator, appends an alert row to the drift log,
and OPTIONALLY auto-refits the calibrator.

Suggested schedule: ``0 7 * * 1`` (Monday 07:00 UTC).

Posture (preserved from AGENTS.md):
- READ-ONLY against Alpaca. paper=True. NO order paths anywhere.
- Auto-refit is DEFAULT-OFF behind HERMES_QUANT_CALIBRATOR_AUTO_REFIT=1. When
  the flag is unset the cron ONLY alerts and never touches the live pickle.
- Silence-by-default: any exception → log + exit 0. The cron NEVER crashes.

ADR refs: ADR-0009 §P0-2 (calibration drift surfaced in quant_doctor).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Re-exec into the hermes-agent venv where hermes-quant + alpaca-py are installed.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])


UNIVERSE_PATH = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"
ALPACA_ENV = Path.home() / ".hermes" / "secrets" / "alpaca.env"
DEFAULT_CALIBRATOR = Path.home() / ".hermes" / "quant" / "calibrators" / "isotonic.pkl"

_AUTO_REFIT_ENV = "HERMES_QUANT_CALIBRATOR_AUTO_REFIT"


def _source_alpaca_env() -> None:
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
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _load_universe(top_n: int) -> list[str]:
    if not UNIVERSE_PATH.exists():
        raise SystemExit(f"universe file not found: {UNIVERSE_PATH}")
    payload = json.loads(UNIVERSE_PATH.read_text())
    rows = payload.get("symbols", [])
    syms = [r["symbol"] for r in rows[:top_n] if "symbol" in r]
    if not syms:
        raise SystemExit(f"no symbols loaded from {UNIVERSE_PATH}")
    return syms


def _collect_pairs(symbols: list[str], days: int, horizon_bars: int):
    """Replay the bootstrap walk to collect recent (raw, correct) pairs.

    Reuses bootstrap_calibrator internals (no order path, read-only Alpaca).
    Returns (raw_scores, direction_correct).
    """
    from hermes_quant.training.bootstrap_calibrator import (
        _MIN_CONTEXT_BARS,
        _build_analysts,
        _build_data_client,
        _fetch_bars,
        _walk_bars_for_symbol,
    )

    client = _build_data_client()
    analysts = _build_analysts(include_kronos=False)
    end = datetime.now(UTC)
    start = end - timedelta(days=days + 30)

    raw_scores: list[float] = []
    direction_correct: list[bool] = []
    for symbol in symbols:
        bars = _fetch_bars(client, symbol, start, end)
        if bars.empty or len(bars) <= _MIN_CONTEXT_BARS + horizon_bars:
            continue
        per_symbol = _walk_bars_for_symbol(
            bars, symbol, analysts, horizon_bars=horizon_bars,
            min_context_bars=_MIN_CONTEXT_BARS,
        )
        for pairs in per_symbol.values():
            for raw, correct in pairs:
                raw_scores.append(float(raw))
                direction_correct.append(bool(correct))
    return raw_scores, direction_correct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=100, help="top N universe symbols")
    parser.add_argument("--days", type=int, default=365, help="lookback days")
    parser.add_argument("--horizon-bars", type=int, default=4)
    parser.add_argument("--threshold", type=float, default=0.05)
    parser.add_argument("--calibrator", type=Path, default=DEFAULT_CALIBRATOR)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    auto_refit = os.environ.get(_AUTO_REFIT_ENV) == "1"  # DEFAULT-OFF

    # Silence-by-default: any failure logs + exits 0 (never crashes the cron).
    try:
        _source_alpaca_env()
        from hermes_quant.training.calibrator_drift import run_drift_check

        symbols = _load_universe(args.top)
        print(f"drift-check: collecting pairs over {len(symbols)} symbols, {args.days}d")
        raw_scores, correct = _collect_pairs(symbols, args.days, args.horizon_bars)
        print(f"drift-check: collected {len(raw_scores)} (raw, correct) pairs")

        result = run_drift_check(
            calibrator_path=args.calibrator,
            pairs=(raw_scores, correct),
            auto_refit=auto_refit,
            threshold=args.threshold,
            refit_kwargs={"symbols": symbols, "days": args.days,
                          "horizon_bars": args.horizon_bars},
        )
        print("=" * 60)
        print("DRIFT RESULT")
        print("=" * 60)
        from dataclasses import asdict
        print(json.dumps(asdict(result), indent=2, default=str))
        print(f"auto_refit flag ({_AUTO_REFIT_ENV}): {'ON' if auto_refit else 'OFF'}")
        if result.should_alert:
            print(f"ALERT: calibrator drift {result.drift:.4f} exceeds "
                  f"threshold {result.threshold:.4f}")
    except Exception as exc:  # noqa: BLE001
        logging.warning("drift-check failed (%s); exiting 0 (silence-by-default).", exc)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
