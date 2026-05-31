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
- no_agent silence contract (scheduler: empty stdout ⇒ silent run, no delivery).
  As a weekly watchdog this cron must NOT spam the operator every Monday with
  zero drift. It is a state-baseline change-detecting watchdog (mirrors the
  sibling quant-catalyst-coverage.py / quant-catalyst-profitability.py probes):
  it emits NOTHING unless the alert state TRANSITIONS (clean→drift or drift→
  clean) vs the persisted baseline. Standing-clean and standing-drift both stay
  silent. --verbose forces the full picture for on-demand operator pulls. The
  drift computation, the durable drift-log append, and the auto-refit behavior
  ALL run unconditionally every tick — only the stdout EMIT is change-gated.

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

# State baseline for the change-detecting no_agent watchdog (mirrors the coverage
# / profitability probes). Persists the last observed alert state so the cron can
# emit ONLY on a transition (clean<->drift), never on a standing state.
_BASELINE = Path.home() / ".hermes" / "quant" / "calibrators" / "drift-baseline.json"


def _load_baseline() -> dict:
    """Load the watchdog baseline. Missing/corrupt -> {} (treated as first run)."""
    try:
        return json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_baseline(state: dict) -> None:
    """Persist the watchdog baseline. Best-effort (never raises).

    Atomic write (AGENTS.md N-rule for no_agent watchdog baselines): serialize
    to a ``.tmp`` sibling then ``os.replace`` so a crash mid-write can never
    leave a truncated JSON that the next run's _load_baseline reads as corrupt.
    os.replace is atomic on the same filesystem.
    """
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BASELINE.with_suffix(_BASELINE.suffix + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True))
        os.replace(tmp, _BASELINE)
    except OSError:
        pass


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
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="force the full DRIFT RESULT picture even when nothing changed "
             "(on-demand operator pull; bypasses the change-detection gate)",
    )
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
        raw_scores, correct = _collect_pairs(symbols, args.days, args.horizon_bars)

        # The drift computation + durable drift-log append + (gated) auto-refit
        # ALWAYS run — only the stdout EMIT below is change-gated. run_drift_check
        # appends the audit row to drift-log.jsonl regardless of what we print.
        result = run_drift_check(
            calibrator_path=args.calibrator,
            pairs=(raw_scores, correct),
            auto_refit=auto_refit,
            threshold=args.threshold,
            refit_kwargs={"symbols": symbols, "days": args.days,
                          "horizon_bars": args.horizon_bars},
        )
        return _emit(result, verbose=args.verbose, auto_refit=auto_refit)
    except Exception as exc:  # noqa: BLE001
        logging.warning("drift-check failed (%s); exiting 0 (silence-by-default).", exc)
        return 0


def _full_report(result, *, auto_refit: bool) -> str:
    """Render the headline + full DRIFT RESULT JSON block."""
    from dataclasses import asdict
    lines = [
        "=" * 60,
        "DRIFT RESULT",
        "=" * 60,
        json.dumps(asdict(result), indent=2, default=str),
        f"auto_refit flag ({_AUTO_REFIT_ENV}): {'ON' if auto_refit else 'OFF'}",
    ]
    if result.should_alert:
        lines.append(
            f"ALERT: calibrator drift {result.drift:.4f} exceeds "
            f"threshold {result.threshold:.4f}"
        )
    return "\n".join(lines)


def _emit(result, *, verbose: bool, auto_refit: bool) -> int:
    """no_agent change-detection gate over the stdout EMIT only.

    Emits the headline + full DRIFT RESULT JSON ONLY when the alert state
    TRANSITIONS vs the persisted baseline:
      * clean -> drift  (should_alert flips True): the headline + table.
      * drift -> clean  (should_alert flips False): one "drift cleared" note.
    Standing-clean and standing-drift both stay SILENT (empty stdout ⇒ no
    Discord message; don't cry wolf every Monday). --verbose forces the full
    picture regardless (on-demand operator pull). The 'no_samples' /
    'no_calibrator' degraded paths are never an alert and are silent unless
    --verbose. The baseline is updated every run so a transition fires once.
    """
    prev = _load_baseline()
    prev_alert = bool(prev.get("should_alert", False)) if prev else None
    cur_alert = bool(result.should_alert)
    _save_baseline({"should_alert": cur_alert})

    if verbose:
        print(_full_report(result, auto_refit=auto_refit))
        return 0

    # No state change since last run -> silent (no_agent contract).
    if prev_alert is not None and prev_alert == cur_alert:
        return 0

    if cur_alert:
        # clean -> drift (or first-ever run that is already drifting): full picture.
        print(_full_report(result, auto_refit=auto_refit))
    elif prev_alert:
        # drift -> clean: one transition note, then silent on subsequent clean runs.
        print(
            f"✅ calibrator-drift: drift {result.drift:.4f} back within "
            f"threshold {result.threshold:.4f} (was alerting) — cleared."
        )
    # else: first-ever run that is already clean -> silent (no transition to report).
    return 0


if __name__ == "__main__":
    sys.exit(main())
