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
    "semis": '(TSMC OR Taiwan OR "chip supply" OR semiconductor) when:1d',
    "aero": '(Boeing OR "737" OR "aircraft grounding" OR "FAA") when:1d',
    "ev": '(Tesla OR "EV recall" OR "electric vehicle" OR Rivian OR Lucid) when:1d',
    "banks": '("bank failure" OR "bank collapse" OR "banking crisis" OR contagion) when:1d',
    # NOTE: the "energy" query was dropped 2026-05-29 — the OPEC/commodity edge was
    # removed (severity classifier can't extract supply direction, so it mis-signed
    # oil producers). Re-add the query when the energy edge returns with a
    # supply-direction classifier. Querying it now would fetch items that produce
    # zero packets (no graph edge) — wasted fetch.
}

# --- B08: social producers (Reddit + Google Trends), default-OFF -------------
# Flag-gated by HERMES_QUANT_SOCIAL_INGEST (read at call time). OFF => this cron
# is byte-identical to the news-only path it has always run; the packet store
# stays 100% news_rss. ON => the feed becomes genuinely MULTI-SOURCE — which is
# the precondition the perception-layer cross-SOURCE validator (PDR-3,
# HERMES_QUANT_CONVERGENCE) needs before it can be flipped without silencing a
# single-source feed (see docs/operations/2026-05-31-selfevolve-flag-flip-decision.md).
# All public, unauthenticated endpoints (reddit.com .json, Google Trends daily);
# ingest_social NEVER raises (a dead producer contributes zero items). The social
# CatalystItems carry "reddit/..." and "google_trends/..." source tags that the
# PDR-3 family taxonomy keys on; they flow through the SAME classify->propagate->
# synthesize pipeline as news, so a brand only emits a packet if it is already a
# graph entity (no new authority, evidence-only).
SOCIAL_REDDIT_QUERIES = {
    "stocks:Crocs OR Tesla OR Celsius OR Coach OR Tapestry": "social/reddit-r-stocks",
    "wallstreetbets:Crocs OR Tesla OR Celsius OR Boeing": "social/reddit-r-wsb",
    "investing:TSMC OR Boeing OR Tesla OR bank failure": "social/reddit-r-investing",
}
# Trends is filtered to the graph's consumer-brand terms so it stays targeted
# (not a firehose of every trending search). Mirrors the alias brand set.
SOCIAL_TRENDS_WATCH = {
    "crocs", "tesla", "celsius", "coach", "tapestry", "boeing", "tsmc",
    "spacex", "rocket lab", "blue origin",
}


def _social_on() -> bool:
    """DEFAULT-OFF flag, read at call time. OFF => news-only, byte-identical."""
    return os.environ.get("HERMES_QUANT_SOCIAL_INGEST", "0") == "1"


def main() -> int:
    json_mode = "--json" in sys.argv
    graph, aliases = load_graph()

    items = ingest_queries(QUERIES)
    n_social = 0
    if _social_on():
        # Multi-source feed (B08). Silence-by-default: ingest_social never raises;
        # a dead/blocked producer contributes zero items, so the news path is
        # unaffected. These items go through the identical synthesize pipeline.
        from hermes_quant.catalyst.social import ingest_social

        social_items = ingest_social(
            SOCIAL_REDDIT_QUERIES,
            trends_geo="US",
            trends_watch_terms=SOCIAL_TRENDS_WATCH,
        )
        n_social = len(social_items)
        items = items + social_items
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
            "social_items": n_social,
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
    _src = f" ({n_social} social)" if n_social else ""
    print(f"📰 Catalyst Sense: {n_written} packet(s) from {len(items)} items{_src} — {lead}")
    if not os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") == "1":
        print("   (advisor consumption OFF — packets staged, flip HERMES_QUANT_SEMANTIC_ENABLED=1 to use)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
