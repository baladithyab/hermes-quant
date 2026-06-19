#!/usr/bin/env python3
"""aegis-chain-prefetch.py — daily options-chain pre-fetch (agopt3, ADR-0028 D7).

Populates ~/.hermes/quant/option_chains/<u>/<YYYY-MM-DD>.parquet for every
options-eligible watchlist symbol, so the autonomous tick's options PERCEIVE
(iv_rank) and the agmon1/agmon2 SL/TP sweeps have a same-day chain to mark
against (they read ChainSnapshotReader.replay_chain off these parquets).

This is the WRITER half of agopt3; analogous to aegis-gate2-eval.py (bf76b) — a
thin in-repo orchestration script over an already-built primitive. It REUSES
ChainSnapshotReader.fetch_chain_live (agperc3), which itself stamps fetched_at and
writes the parquet atomically. This script only enumerates symbols + drives the
fetch; it adds no new chain logic.

INERT by default / FAIL-SOFT: fetch_chain_live raises LiveChainDisabled unless
HERMES_QUANT_OPTIONS_LIVE_CHAIN=1 AND Alpaca creds are present. When disabled this
script fetches nothing and exits 0 (silence-by-default — it is not an error to run
it with live chains off). A per-symbol fetch failure is logged and SKIPPED (one bad
symbol never aborts the batch). The operator registers it as a daily cron; the agent
owns the script + its test.

SAFETY: read-only market data + parquet writes under ~/.hermes/quant/. No money state,
no orders.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _home_path(home: str | Path | None) -> Path:
    if home is not None:
        return Path(home)
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def prefetch_chains(
    *,
    home: str | Path | None = None,
    reader: object | None = None,
    symbols: list[str] | None = None,
) -> dict:
    """Fetch + persist a chain for each options-eligible watchlist symbol.

    Returns a summary dict {requested, fetched, skipped, disabled, errors:[...]}.
    Pure-ish (no print) so it is unit-testable with an injected reader. FAIL-SOFT:
    LiveChainDisabled => disabled=True (whole batch inert); a per-symbol error is
    counted in `errors` and skipped, never raised.
    """
    from hermes_quant.options.data import ChainSnapshotReader, LiveChainDisabled

    qhome = _home_path(home)
    rdr = reader if reader is not None else ChainSnapshotReader(chains_dir=qhome / "option_chains")

    # Enumerate the optionable watchlist symbols (the perceive/monitor candidate set).
    if symbols is None:
        try:
            from hermes_quant.watchlist import list_watchlist

            symbols = [e.symbol for e in list_watchlist() if getattr(e, "options_eligible", False)]
        except Exception:  # noqa: BLE001 — no watchlist => nothing to prefetch (silence)
            symbols = []

    summary = {"requested": len(symbols), "fetched": 0, "skipped": 0, "disabled": False, "errors": []}
    for sym in symbols:
        try:
            rdr.fetch_chain_live(sym)  # agperc3: stamps fetched_at + writes parquet atomically
            summary["fetched"] += 1
        except LiveChainDisabled:
            # The flag/creds are off for the WHOLE process — no point trying the rest.
            summary["disabled"] = True
            summary["skipped"] = len(symbols) - summary["fetched"]
            break
        except Exception as exc:  # noqa: BLE001 — one bad symbol never aborts the batch
            summary["skipped"] += 1
            summary["errors"].append(f"{sym}: {exc}")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--home", default=None, help="operator home (default HERMES_HOME or ~/.hermes)")
    ap.add_argument("--symbols", default=None, help="comma-separated override (default: optionable watchlist)")
    args = ap.parse_args(argv)

    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    summary = prefetch_chains(home=args.home, symbols=syms)

    print("AEGIS chain prefetch (agopt3)")
    if summary["disabled"]:
        print("  LIVE CHAIN DISABLED (HERMES_QUANT_OPTIONS_LIVE_CHAIN!=1 or no creds) — nothing fetched.")
    else:
        print(f"  requested={summary['requested']} fetched={summary['fetched']} skipped={summary['skipped']}")
        for e in summary["errors"][:10]:
            print(f"  skip: {e}")
    # Exit 0 always: a disabled run / per-symbol skips are valid, not errors.
    return 0


if __name__ == "__main__":
    sys.exit(main())
