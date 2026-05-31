"""quant-catalyst-graph-mine.py — W5 B10 learned-graph miner (catalyst edges).

Joins the propagation log against realized yfinance forward returns, grouped PER
EDGE (the layer below the per-relation profitability cron), and PROPOSES per-edge
FLIP_SIGN / DOWNWEIGHT / PRUNE candidates to graph-mine-candidates.json for operator
review. It NEVER auto-edits the seed/live YAML — the advisory plane only (ADR-0080
D80.1). DEFAULT-OFF behind HERMES_QUANT_GRAPH_MINING; flag-OFF is a silent no-op.

Change-detecting no_agent watchdog: prints ONLY on a state transition (an edge
crossing MIN_SAMPLE for the first time, or a cleared edge flipping verdict).
Standing state -> silent. Mirrors quant-catalyst-profitability.py structurally.
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

from hermes_quant.catalyst.graph_mining import (
    MIN_SAMPLE,
    format_report,
    mine_graph,
    write_candidates,
)

_FWD_WINDOW_DAYS = 21  # ~1 month forward; catalyst horizon is days-to-weeks

# State baseline for the change-detecting no_agent watchdog (per-edge analog of
# profitability-baseline.json). Persisted per-edge: {cleared, verdict}.
_BASELINE = Path.home() / ".hermes" / "quant" / "catalyst" / "graph-mine-baseline.json"
_CANDIDATES = Path.home() / ".hermes" / "quant" / "catalyst" / "graph-mine-candidates.json"


def _yf_forward_return(symbol: str, asof: date) -> float | None:
    import warnings; warnings.filterwarnings("ignore")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
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
    entry = close.index[close.index >= pd.Timestamp(asof)]
    if len(entry) == 0:
        return None
    entry_px = float(close.loc[entry[0]])
    exit_target = entry[0] + pd.Timedelta(days=_FWD_WINDOW_DAYS)
    ex = close.index[close.index >= exit_target]
    exit_px = float(close.loc[ex[0]]) if len(ex) else float(close.iloc[-1])
    return (exit_px / entry_px - 1) * 100


def _edge_key_str(ev) -> str:
    """Join the (source, target, relation) tuple as a stable baseline-dict key."""
    return f"{ev.source}|{ev.target_symbol}|{ev.relation}"


def _load_baseline() -> dict[str, dict]:
    """Load the per-edge watchdog baseline. Missing/corrupt -> {} (first run)."""
    try:
        return json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_baseline(state: dict[str, dict]) -> None:
    """Persist the per-edge watchdog baseline. Best-effort (never raises)."""
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(state, sort_keys=True))
    except OSError:
        pass


def _current_state(evidence: dict) -> dict[str, dict]:
    """Project EdgeEvidence -> {edge_key_str: {cleared, verdict}} for diffing.

    ``cleared`` means the edge crossed n_scored >= MIN_SAMPLE (it just became
    trustworthy); ``verdict`` is the per-edge verdict string.
    """
    return {
        _edge_key_str(ev): {"cleared": ev.n_scored >= MIN_SAMPLE, "verdict": ev.verdict}
        for ev in evidence.values()
    }


def _transitions(cur: dict[str, dict], baseline: dict[str, dict]) -> list[str]:
    """Pure state-transition diff: emit a line ONLY when an edge crosses MIN_SAMPLE
    for the first time, or a cleared edge flips verdict.

    Standing-state (cleared + unchanged verdict) produces nothing -> the cron stays
    silent (no_agent contract). New-but-uncleared edges are also silent (their
    hit-rate isn't trustworthy yet). Byte-for-byte the profitability transition logic.
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
    # Cap the yfinance fan-out: measuring every edge every run is slow + redundant.
    # The cron accumulates verdicts over days. max_rows bounds the fetch count.
    # (mine_graph returns {} when HERMES_QUANT_GRAPH_MINING is unset -> silent.)
    evidence = mine_graph(_yf_forward_return, max_rows=120)
    if not evidence:
        return 0  # silence-by-default: flag-OFF, or no scored data yet (no_agent cron)

    cur = _current_state(evidence)
    baseline = _load_baseline()
    transitions = _transitions(cur, baseline)
    _save_baseline(cur)

    # --verbose always shows the full picture (on-demand operator pull).
    if verbose:
        print("📊 " + format_report(evidence))
        return 0
    # Standing state, unchanged -> silent (no_agent watchdog: empty stdout).
    if not transitions:
        return 0
    # Something changed: write the candidate diff (advisory plane) + announce.
    n = write_candidates(evidence)
    print("📊 catalyst-graph-mine: " + "; ".join(transitions))
    if n:
        print(f"  -> {n} candidate edge edit(s) written to {_CANDIDATES} (operator review)")
    print(format_report(evidence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
