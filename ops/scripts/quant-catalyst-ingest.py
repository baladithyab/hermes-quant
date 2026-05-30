#!/usr/bin/env python3
"""quant-catalyst-ingest.py — Catalyst Sense ingester (ADR-0074).

Runs PARALLEL to the universe scan: ingest free news feeds (Google News RSS) ->
classify -> propagate (butterfly graph) -> synthesize SemanticPackets (asof =
publication time) -> append to the packet store the advisor loads at recommend
time.

no_agent cron: prints a tiered human summary (silence-by-default). The advisor
only CONSUMES packets when HERMES_QUANT_SEMANTIC_ENABLED=1; this ingester always
populates the store so packets are ready the moment the flag flips.

Suggested cadence: every 30-60 min in-market (catalysts are intraday). Packets
are timestamped at publication time, so the lookahead gate handles freshness
regardless of when this runs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Re-exec under the hermes venv if needed.
_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

from hermes_quant.catalyst.ingest import ingest_queries
from hermes_quant.catalyst.propagation import load_graph
from hermes_quant.catalyst.synthesize import synthesize_packets, write_packets

# Query set: per-sector sweeps covering the graph's known entities. Extend as the
# propagation graph grows. when:1d keeps it fresh; the ingester runs frequently.
QUERIES = {
    "space": '(Blue Origin OR "Rocket Lab" OR SpaceX OR "New Glenn" OR "space stocks") when:1d',
    "energy": '(OPEC OR "oil prices" OR crude OR "production cut") when:1d',
    "semis": '(TSMC OR Taiwan OR "chip supply" OR semiconductor) when:1d',
    "aero": '(Boeing OR "737" OR "aircraft grounding" OR "FAA") when:1d',
    "ev": '(Tesla OR "EV recall" OR "electric vehicle" OR Rivian OR Lucid) when:1d',
    "banks": '("bank failure" OR "bank collapse" OR "banking crisis" OR contagion) when:1d',
}


def main() -> int:
    json_mode = "--json" in sys.argv
    graph, aliases = load_graph()

    items = ingest_queries(QUERIES)
    prop_log: list[dict] = []
    packets = synthesize_packets(
        items, graph=graph, aliases=aliases, propagation_log=prop_log
    )
    n_written = write_packets(packets)

    if json_mode:
        import json
        print(json.dumps({
            "event": "catalyst_ingest",
            "items": len(items), "packets": n_written,
            "propagations": len(prop_log),
        }))
        return 0

    # Tiered human summary (quant cron standard): loud on fires, silent on nothing.
    if n_written == 0:
        # Nothing actionable surfaced -> stay silent (no_agent + empty stdout).
        return 0

    # Group packets by symbol+stance for a compact headline.
    by_sym: dict[str, str] = {}
    for p in packets:
        by_sym.setdefault(p.asset, p.stance)
    lead = ", ".join(f"{s} {st}" for s, st in sorted(by_sym.items())[:6])
    print(f"📰 Catalyst Sense: {n_written} packet(s) from {len(items)} items — {lead}")
    if not os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") == "1":
        print("   (advisor consumption OFF — packets staged, flip HERMES_QUANT_SEMANTIC_ENABLED=1 to use)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
