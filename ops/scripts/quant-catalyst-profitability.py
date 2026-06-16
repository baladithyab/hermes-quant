"""quant-catalyst-profitability.py — measure catalyst-signal edge on live data.

Joins the propagation log against realized yfinance forward returns, grouped by
relation class (brand_self consumer-trend vs sector edges), and prints whether each
class is paying. This is the feedback loop that decides whether to RAISE the
consumer-trend confidence haircut or prune the edges — verify profitability, never assume.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    import os; os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

from hermes_quant.catalyst.profitability import (
    MIN_SAMPLE,
    format_report,
    measure_profitability,
)

_FWD_WINDOW_DAYS = 21  # ~1 month forward; catalyst horizon is days-to-weeks

# State baseline for the change-detecting no_agent watchdog (mirrors the coverage
# probe pattern from commit e4ecad5). Persisted per-relation: {cleared, verdict}.
_BASELINE = Path.home() / ".hermes" / "quant" / "catalyst" / "profitability-baseline.json"


def _yf_forward_return(symbol: str, asof: date) -> float | None:
    import warnings; warnings.filterwarnings("ignore")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            import pandas as pd
            import yfinance as yf
            df = yf.download(symbol, start=str(asof - timedelta(days=4)),
                             end=str(asof + timedelta(days=_FWD_WINDOW_DAYS + 6)),
                             interval="1d", auto_adjust=True, progress=False)
        except Exception:
            return None
    if df is None or len(df) == 0:
        return None
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close = close.dropna()
    import pandas as pd
    # ar29: enter at the NEXT bar STRICTLY AFTER asof, not the same-day bar. The asof is
    # the signal's PUBLICATION time; using `>= asof` picks the bar ON asof when one
    # exists, scoring an intraday-published signal against close[D] (the publication-day
    # move it could not have captured) — a same-bar LOOKAHEAD. The consuming module's
    # contract is explicit (catalyst/profitability.py:11-12, graph_mining.py:278: "the
    # NEXT bar after asof, lookahead-honest"). `>` makes the entry the first tradeable
    # bar after publication, matching that contract; this forward return drives the live
    # CONSUMER_TREND_CONFIDENCE_HAIRCUT raise/prune decision, so the bias is load-bearing.
    entry = close.index[close.index > pd.Timestamp(asof)]
    if len(entry) == 0:
        return None
    entry_px = float(close.loc[entry[0]])
    exit_target = entry[0] + pd.Timedelta(days=_FWD_WINDOW_DAYS)
    ex = close.index[close.index >= exit_target]
    exit_px = float(close.loc[ex[0]]) if len(ex) else float(close.iloc[-1])
    return (exit_px / entry_px - 1) * 100


def _load_baseline() -> dict[str, dict]:
    """Load the per-relation watchdog baseline. Missing/corrupt -> {} (first run)."""
    try:
        return json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_baseline(state: dict[str, dict]) -> None:
    """Persist the per-relation watchdog baseline. Best-effort (never raises)."""
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(state, sort_keys=True))
    except OSError:
        pass


def _current_state(stats: dict) -> dict[str, dict]:
    """Project RelationStats -> {relation: {cleared, verdict}} for diffing.

    ``cleared`` means the class crossed n_scored >= MIN_SAMPLE (it just became
    trustworthy); ``verdict`` is the profitability verdict string.
    """
    return {
        r: {"cleared": s.n_scored >= MIN_SAMPLE, "verdict": s.verdict}
        for r, s in stats.items()
    }


def _transitions(cur: dict[str, dict], baseline: dict[str, dict]) -> list[str]:
    """Pure state-transition diff: emit a line ONLY when a relation class crosses
    MIN_SAMPLE for the first time, or a cleared class flips verdict.

    Standing-state (cleared + unchanged verdict) produces nothing -> the cron
    stays silent (no_agent contract). New-but-uncleared classes are also silent
    (their hit-rate isn't trustworthy yet).
    """
    out: list[str] = []
    for r, c in cur.items():
        b = baseline.get(r)
        if b is None:
            if c["cleared"]:
                out.append(f"{r} CLEARED MIN_SAMPLE ({c['verdict']})")
        else:
            if c["cleared"] and not b.get("cleared"):
                out.append(f"{r} CLEARED MIN_SAMPLE ({c['verdict']})")
            elif c["cleared"] and c["verdict"] != b.get("verdict"):
                out.append(f"{r} verdict {b.get('verdict')} -> {c['verdict']}")
    return out


def main() -> int:
    verbose = "--verbose" in sys.argv
    # Cap fetches: measuring every sector edge every run is slow + redundant. The
    # decision this loop informs is the CONSUMER-TREND haircut, so cap rows; the
    # cron accumulates verdicts over days. max_rows bounds the yfinance fan-out.
    stats = measure_profitability(_yf_forward_return, max_rows=120)
    if not stats:
        return 0  # silence-by-default: no scored data yet (no_agent cron)

    cur = _current_state(stats)
    baseline = _load_baseline()
    transitions = _transitions(cur, baseline)
    _save_baseline(cur)

    # --verbose always shows the full picture (on-demand operator pull).
    if verbose:
        print("📊 " + format_report(stats))
        return 0
    # Standing state, unchanged -> silent (no_agent watchdog: empty stdout).
    if not transitions:
        return 0
    # Something changed: announce the transition(s), then the full table.
    print("📊 catalyst-profitability: " + "; ".join(transitions))
    print(format_report(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
