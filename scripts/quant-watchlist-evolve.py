#!/usr/bin/env python3
"""Per-play evolving watchlist tick (CLI shim).

Reads the universe at ~/.hermes/quant/universe/alpaca-daily-top100.json (top
100 by dollar-volume), runs the play-fitness scorers against it in parallel,
and atomic-writes the evolved watchlist state to
~/.hermes/quant/watchlist/play-fit.json. Append-only audit log at
~/.hermes/quant/watchlist/journal.jsonl.

PERFORMANCE FIX (Wave 1d, 2026-05-27)
--------------------------------------
Root cause of 120s timeout: the original script used alpaca-daily.json (500
symbols) × sequential yfinance calls (~1.5s/symbol) = ~750s worst-case.

Fix:
  1. Switch universe to alpaca-daily-top100.json (100 symbols).
  2. Parallelize snapshot fetching via ThreadPoolExecutor(max_workers=20).
  3. Expected runtime: ~10-20s for snapshots + <1s evolution logic = <30s total.

The top-100 universe covers >90% of the dollar-volume of the full 500-symbol
universe. The watchlist evolution logic is unchanged; only the input universe
is narrowed and the scorer is parallelized.

CRON SUGGESTION
---------------
Run weekdays at 06:45 America/New_York — after the universe scanner
completes at 06:30 ET — so the watchlist is fresh before the open. Cron
in UTC:

    45 11 * * 1-5  source $HOME/.hermes/secrets/alpaca.env && \\
        $HOME/.hermes/hermes-agent/venv/bin/python3 \\
        $HOME/.hermes/scripts/quant-watchlist-evolve.py \\
        >> $HOME/.hermes/quant/watchlist/evolve.log 2>&1

POSTURE
-------
Silence-by-default: if the universe file is missing the script logs a
warning and exits 0 without modifying any state. The scorer is
dependency-injected — when ``hermes_quant.playbook.scorers`` is available
it is used; otherwise a stub-0.5 scorer keeps the loop running without
onboarding anything (0.5 < 0.65 default floor).

Stdout is silent when no events fire; only summary on changes.
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Re-exec under the hermes-agent venv if needed (where hermes_quant is installed).
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

# Best-effort: source alpaca creds from ~/.hermes/secrets/alpaca.env so that
# any data-dependent scorer can find them. The evolution function itself
# does NOT call alpaca/yfinance — but the injected scorer might.
SECRETS = Path.home() / ".hermes" / "secrets" / "alpaca.env"
if SECRETS.exists() and not (
    os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
):
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")  # bash strips shell quotes; Python doesn't
            os.environ.setdefault(k.strip(), v)

logging.basicConfig(
    level=os.environ.get("HERMES_QUANT_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Wave 1d performance fix: use the top-100 universe (not the full 500-symbol one)
# to stay well inside the 120s cron timeout. The top-100 covers >90% of dollar
# volume and is the authoritative liquid-universe for play-fitness scoring.
TOP100_UNIVERSE_PATH = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily-top100.json"
FULL_UNIVERSE_PATH = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"

# Parallelism cap. 20 workers for 100 symbols → ~5 batches of 20; each batch
# takes ~2s, total snapshot phase ≈ 10-15s. yfinance is IO-bound so threads
# are fine (no GIL contention on network waits).
_MAX_WORKERS = 20


from hermes_quant.playbook.watchlist_evolution import evolve_watchlist  # noqa: E402

# Try to import the real scorer. If the scorers module isn't ready yet, fall
# back to None — evolve_watchlist will use its silent stub. This keeps the
# cron green during the transition.
try:
    from hermes_quant.playbook.scorers import (  # type: ignore[attr-defined]
        compute_play_snapshot,
        score_all,
    )

    _HAS_SCORER = True
except (ImportError, AttributeError):
    _HAS_SCORER = False


def _parallel_score_symbol(symbol: str, play: str, snapshot_cache: dict) -> float:
    """Score one symbol on one play using a pre-populated snapshot cache.

    The snapshot_cache must already be populated (by _prefetch_snapshots)
    before this is called. This function is pure computation — no IO.
    """
    if not _HAS_SCORER:
        return 0.5  # stub: won't cross onboard floor, won't cross evict floor

    snap = snapshot_cache.get(symbol.upper())
    if snap is None:
        return 0.0  # no data → treat as ineligible

    try:
        all_fits = score_all(snap)
        fitness = all_fits.get(play)
        if fitness is None:
            return 0.0
        # Ineligible symbols (eviction or hard-rule fail) → 0.0 so evict_floor=0.45
        # treats them as hard rejects (see scorers.score_symbol docstring).
        if not fitness.eligible:
            return 0.0
        return float(fitness.score)
    except Exception:  # noqa: BLE001
        return 0.0


def _prefetch_snapshots(symbols: list[str], max_workers: int = _MAX_WORKERS) -> dict:
    """Fetch yfinance snapshots for all symbols in parallel.

    Returns {SYMBOL_UPPER: snapshot_dict}. Any symbol that fails is omitted;
    the scorer will return 0.0 for it (silence-by-default).
    """
    if not _HAS_SCORER:
        return {}

    cache: dict[str, dict] = {}

    def _fetch(sym: str) -> tuple[str, dict | None]:
        try:
            return sym.upper(), compute_play_snapshot(sym)
        except Exception:  # noqa: BLE001
            return sym.upper(), None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym_upper, snap = future.result()
            if snap is not None:
                cache[sym_upper] = snap

    return cache


def _make_scorer(snapshot_cache: dict):
    """Return a scorer callable that uses the pre-populated snapshot cache."""

    def _scorer(symbol: str, play: str) -> float:
        return _parallel_score_symbol(symbol, play, snapshot_cache)

    return _scorer


def _resolve_universe_path() -> Path:
    """Prefer top-100 universe; fall back to full universe if top-100 missing."""
    if TOP100_UNIVERSE_PATH.exists():
        return TOP100_UNIVERSE_PATH
    logging.getLogger(__name__).warning(
        "watchlist-evolve: top-100 universe missing (%s), falling back to full (%s)",
        TOP100_UNIVERSE_PATH,
        FULL_UNIVERSE_PATH,
    )
    return FULL_UNIVERSE_PATH


def main() -> int:
    universe_path = _resolve_universe_path()

    # 2026-05-27 hardening (P0-3 from parallel-critique review): if the
    # upstream universe file is stale (>25h old) the entire downstream
    # chain (watchlist → brief → playbook-tick) operates on yesterday's
    # data with no warning. Fail loudly instead — surface the stale-input
    # condition to the cron's stderr so the agent-deliver path catches it.
    if universe_path.exists():
        age_hours = (
            datetime.now(timezone.utc).timestamp() - universe_path.stat().st_mtime
        ) / 3600.0
        if age_hours > 25.0:
            print(
                f"WARNING: universe file is {age_hours:.1f}h old (> 25h); "
                f"watchlist evolution skipped to avoid stale-data cascade. "
                f"Path: {universe_path}",
                file=sys.stderr,
            )
            return 1

    # Wave 1d performance fix: pre-fetch all snapshots in parallel BEFORE
    # calling evolve_watchlist, so the evolution loop's scorer has zero
    # additional IO cost (pure dict lookup). This is what makes 100 symbols
    # complete in ~15s instead of ~250s.
    import json as _json

    symbols: list[str] = []
    if universe_path.exists():
        try:
            payload = _json.loads(universe_path.read_text(encoding="utf-8"))
            raw = payload.get("symbols") or []
            for entry in raw:
                if isinstance(entry, str):
                    symbols.append(entry)
                elif isinstance(entry, dict) and "symbol" in entry:
                    symbols.append(str(entry["symbol"]))
        except Exception:  # noqa: BLE001
            pass  # evolve_watchlist will re-read and handle the error

    if symbols:
        snapshot_cache = _prefetch_snapshots(symbols)
        _scorer = _make_scorer(snapshot_cache)
    else:
        _scorer = None  # evolve_watchlist will use its stub scorer

    summary = evolve_watchlist(scorer=_scorer, universe_path=universe_path)

    # Silence-by-default: only print if something happened.
    if summary["events_written"] == 0 and all(
        v["n_active"] == 0 for v in summary["per_play"].values()
    ):
        return 0

    print(f"as_of={summary['as_of']} events={summary['events_written']}")
    for play, stats in summary["per_play"].items():
        if stats["n_active"] or stats["n_onboarded_today"] or stats["n_evicted_today"]:
            top = ", ".join(f"{s}:{sc:.2f}" for s, sc in stats["top5"])
            print(
                f"  {play:14s} active={stats['n_active']:3d} "
                f"+{stats['n_onboarded_today']:2d} -{stats['n_evicted_today']:2d}  "
                f"top5: {top}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
