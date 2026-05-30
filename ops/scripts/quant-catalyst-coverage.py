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
_BASELINE = pathlib.Path.home() / ".hermes" / "quant" / "catalyst" / "coverage-baseline.json"


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


def _load_baseline() -> set[str]:
    try:
        return set(json.loads(_BASELINE.read_text()).get("dead", []))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_baseline(dead: set[str]) -> None:
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps({"dead": sorted(dead)}))
    except OSError:
        pass


def main() -> int:
    verbose = "--verbose" in sys.argv
    universe = load_universe_symbols()
    graph, _ = load_graph()
    if not universe:
        # silent: empty universe is a universe-scan problem, not a coverage one;
        # no_agent + empty stdout = no Discord message (don't cry wolf).
        return 0

    cov = coverage_against_universe(universe, graph)
    covered: list = cov["covered"]  # type: ignore[assignment]
    dead: list = cov["dead_on_arrival"]  # type: ignore[assignment]
    by_source: dict = cov["by_source"]  # type: ignore[assignment]
    dead_set = set(dead)

    # Change-detection: a standing dead set (the known AMD/LCID/LUNR/SPR) firing
    # every day would train the operator to ignore it. The cron path emits only
    # when the dead set CHANGES vs the persisted baseline (a NEW dead edge, or one
    # cleared). --verbose always shows the full current picture (on-demand pull).
    baseline = _load_baseline()
    newly_dead = sorted(dead_set - baseline)
    newly_clear = sorted(baseline - dead_set)
    changed = bool(newly_dead or newly_clear)
    _save_baseline(dead_set)

    if not dead:
        if verbose:
            print(f"✅ catalyst-coverage: all {len(covered)} graph targets are in the "
                  f"{len(universe)}-name universe — no dead edges.")
        elif newly_clear:  # a previously-dead edge just became tradeable — worth one note
            print(f"✅ catalyst-coverage: {', '.join(newly_clear)} now in universe "
                  f"(no dead edges remain).")
        return 0

    if not verbose and not changed:
        # standing known-dead set, unchanged -> silent (no_agent: no message).
        return 0

    header = "⚠️ catalyst-coverage"
    if changed and not verbose:
        bits = []
        if newly_dead:
            bits.append(f"NEW dead edge(s): {', '.join(newly_dead)}")
        if newly_clear:
            bits.append(f"cleared: {', '.join(newly_clear)}")
        header += f" — {'; '.join(bits)}"
    lines = [
        f"{header}: {len(dead)}/{len(covered) + len(dead)} graph targets "
        f"are DEAD-ON-ARRIVAL (catalyst fires but advisor can't trade them).",
        "",
        f"  universe: {len(universe)} names | covered: {len(covered)} | dead: {len(dead)}",
        "",
        "  Dead-on-arrival targets (packet produced, never consumed):",
    ]
    for sym in dead:
        srcs = ", ".join(by_source.get(sym, []))
        flag = "  ← NEW" if sym in set(newly_dead) else ""
        lines.append(f"    • {sym:5} ← {srcs}{flag}")
    lines += [
        "",
        "  Per-symbol decision: KEEP (universe transiently missing a liquid name,"
        " e.g. AMD), PRUNE (edge not worth a slot), or ONBOARD (admit strong-catalyst"
        " names — ADR-0075 catalyst-driven onboarding, not yet built).",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
