#!/usr/bin/env python3
"""Catalyst Sense — propagation-graph ↔ trading-universe coverage probe.

Surfaces DEAD-ON-ARRIVAL graph edges: catalyst targets that aren't in the
advisor's tradeable universe, so their packets are produced but never consumed.
A silent gap (found 2026-05-29: AMD/LCID/LUNR/SPR were outside the 500-name
liquidity universe) — this probe makes it visible after any graph edit or
universe refresh.

Reads the live universe at ~/.hermes/quant/universe/alpaca-daily.json. Prints a
compact tiered summary (silence-friendly): clean coverage -> one line; dead edges
-> a flagged block with per-symbol provenance + the keep/prune/onboard decision
prompt. Exit 0 always (informational; never blocks the pipeline).

Run on demand, or wire as a daily no_agent cron after the universe-scan job.
"""
from __future__ import annotations

import json
import pathlib
import sys

from hermes_quant.catalyst.propagation import coverage_against_universe, load_graph

_UNIVERSE = pathlib.Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"


def load_universe_symbols(path: pathlib.Path = _UNIVERSE) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    syms = data.get("symbols", []) if isinstance(data, dict) else data
    out: set[str] = set()
    for x in syms or []:
        s = x.get("symbol") if isinstance(x, dict) else x
        if isinstance(s, str):
            out.add(s)
    return out


def main() -> int:
    universe = load_universe_symbols()
    graph, _ = load_graph()
    if not universe:
        print("🔕 catalyst-coverage: universe file empty/missing — skipping (no false alarm)")
        return 0

    cov = coverage_against_universe(universe, graph)
    covered: list = cov["covered"]  # type: ignore[assignment]
    dead: list = cov["dead_on_arrival"]  # type: ignore[assignment]
    by_source: dict = cov["by_source"]  # type: ignore[assignment]

    if not dead:
        print(f"✅ catalyst-coverage: all {len(covered)} graph targets are in the "
              f"{len(universe)}-name universe — no dead edges.")
        return 0

    lines = [
        f"⚠️ catalyst-coverage: {len(dead)}/{len(covered) + len(dead)} graph targets "
        f"are DEAD-ON-ARRIVAL (catalyst fires but advisor can't trade them).",
        "",
        f"  universe: {len(universe)} names | covered: {len(covered)} | dead: {len(dead)}",
        "",
        "  Dead-on-arrival targets (packet produced, never consumed):",
    ]
    for sym in dead:
        srcs = ", ".join(by_source.get(sym, []))
        lines.append(f"    • {sym:5} ← {srcs}")
    lines += [
        "",
        "  Per-symbol decision: KEEP (universe transiently missing a liquid name,"
        " e.g. AMD), PRUNE (edge not worth a slot), or ONBOARD (admit strong-catalyst"
        " names — ADR-0073 catalyst-driven onboarding, not yet built).",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
