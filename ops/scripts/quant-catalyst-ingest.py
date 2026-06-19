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
from datetime import UTC, datetime
from pathlib import Path

# Re-exec under the hermes venv if needed.
_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

from hermes_quant.catalyst.ingest import ingest_queries  # noqa: E402
from hermes_quant.catalyst.openbb_news import ingest_openbb_news  # noqa: E402
from hermes_quant.catalyst.propagation import load_graph, log_propagations  # noqa: E402
from hermes_quant.catalyst.synthesize import synthesize_packets, write_packets  # noqa: E402

# Query set: per-sector sweeps covering the graph's known entities. Extend as the
# propagation graph grows. when:1d keeps it fresh; the ingester runs frequently.
QUERIES = {
    "space": '(Blue Origin OR "Rocket Lab" OR SpaceX OR "New Glenn" OR "space stocks") when:1d',
    "semis": '(TSMC OR Taiwan OR "chip supply" OR semiconductor) when:1d',
    "aero": '(Boeing OR "737" OR "aircraft grounding" OR "FAA") when:1d',
    "ev": '(Tesla OR "EV recall" OR "electric vehicle" OR Rivian OR Lucid) when:1d',
    "banks": '("bank failure" OR "bank collapse" OR "banking crisis" OR contagion) when:1d',
    # --- consumer-trend / social-arbitrage sweeps (ADR-0074 Phase-1) ---
    # Surface viral-demand / sell-out / craze stories on the curated brands. These
    # feed the consumer-trend graph edges (CELH/CROX/TPR/NWL/DIIBF). News GN-RSS
    # surfaces the social-arb story once it's reported; a dedicated Reddit/Trends
    # producer (hermes_quant.catalyst.social) is wired separately (HERMES_QUANT_SOCIAL_INGEST).
    # Consumer-trend packets are confidence-haircut at synth time, so they enter BMA
    # as a deliberately weak peer view.
    "consumer_celsius": '(Celsius energy drink OR CELH) (viral OR craze OR "sells out" OR surge OR soar) when:1d',
    "consumer_crocs": '(Crocs OR CROX) (viral OR craze OR trend OR surge OR soar) when:1d',
    "consumer_coach": '(Coach handbag OR Tapestry OR TPR) (viral OR trend OR surge OR popularity) when:1d',
    "consumer_brands": '("goes viral" OR "sells out" OR "consumer craze") (brand OR product OR retail) when:1d',
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
# All public, no-auth endpoints (reddit.com Atom .rss, Google trending/rss);
# ingest_social NEVER raises (a dead producer contributes zero items). The social
# CatalystItems carry "reddit/..." and "google_trends/..." source tags that the
# PDR-3 family taxonomy keys on; they flow through the SAME classify->propagate->
# synthesize pipeline as news, so a brand only emits a packet if it is already a
# graph entity (no new authority, evidence-only).
SOCIAL_REDDIT_QUERIES = {
    # NEW.RSS (no-query) pulls on the finance subs that carry FRESH, entity-bearing
    # chatter. The old ticker/brand SEARCH queries used Reddit's search.rss, which is
    # RELEVANCE-ranked and returned posts with a median age of ~62 days (verified live
    # 2026-05-31) — so a reddit packet NEVER co-occurred with fresh news inside PDR-3's
    # 24h cross-source convergence window. new.rss (NO query) returns RECENT posts
    # (most <=72h) and still surfaces graph entities (SpaceX/Tesla/Blue Origin/New Glenn
    # seen live in <=72h posts). A no-query entry is a bare "sub" (NO colon) ->
    # ingest_reddit(sub, query=None) -> new.rss. The recency filter (max_age_days=7,
    # below) trims the tail so every emitted packet is fresh enough to overlap PDR-3's
    # window when the name is active. The provenance labels are cosmetic (PDR-3 keys on
    # the source TAG built in social.py = "reddit/r/<sub> (rss)", not these values).
    "stocks": "social/reddit-r-stocks",
    "wallstreetbets": "social/reddit-r-wsb",
    "StockMarket": "social/reddit-r-stockmarket",
    "investing": "social/reddit-r-investing",
    "options": "social/reddit-r-options",
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


def _openbb_news_on() -> bool:
    """DEFAULT-OFF OpenBB news source. OFF => existing RSS/social path."""
    return os.environ.get("HERMES_QUANT_OPENBB", "0").lower() in {"1", "true", "yes", "on"}


def main() -> int:
    json_mode = "--json" in sys.argv
    graph, aliases = load_graph()

    items = ingest_queries(QUERIES)
    n_social = 0
    if _social_on():
        # Multi-source feed (B08). Silence-by-default: ingest_social never raises;
        # a dead/blocked producer contributes zero items, so the news path is
        # unaffected. These items go through the identical per-item synth loop below.
        from hermes_quant.catalyst.social import ingest_social

        social_items = ingest_social(
            SOCIAL_REDDIT_QUERIES,
            trends_geo="US",
            trends_watch_terms=SOCIAL_TRENDS_WATCH,
            # 7-day recency gate: DROP stale items by their real published_at so a
            # reddit packet can co-occur with fresh news in PDR-3's convergence window
            # (the measured blocker was median-62-day stale Reddit search posts). asof
            # honesty: this only excludes by timestamp, never shifts one.
            max_age_days=7,
        )
        n_social = len(social_items)
        items = items + social_items

    n_openbb = 0
    openbb_error: str | None = None
    if _openbb_news_on():
        try:
            openbb_items, _latency = ingest_openbb_news(
                "openbb_world",
                as_of=datetime.now(tz=UTC),
            )
            n_openbb = len(openbb_items)
            items = items + openbb_items
        except Exception as exc:  # noqa: BLE001 - producer is non-fatal
            openbb_error = f"{type(exc).__name__}: {exc}"[:200]

    # RR2 (PDR-3 fix): synthesize the WHOLE item set in ONE call. The per-item loop
    # this replaced fed synthesize_packets([it]) one item at a time, so the
    # PDR-3 convergence pass (validate_convergence) ALWAYS saw a 1-item set per
    # symbol => n_independent==1 => validated=False => with HERMES_QUANT_CONVERGENCE=1
    # EVERY packet was dropped (the eval batched the full set and passed, but
    # production was blind). A single batch call lets convergence observe each
    # symbol's full MULTI-SOURCE item set, so it can actually fire on a multi-source
    # feed. stamp_log_asof=True preserves the per-propagation asof the profitability/
    # graph-mining loop needs (each log row carries its SOURCE item's publication
    # time, exactly as the per-item log_propagations(asof=...) call did) — so we now
    # log ONCE over the full batch with the per-row asof already in place. With the
    # CONVERGENCE flag OFF this is byte-identical to the old per-item path (packets
    # AND propagation-log rows; golden-verified).
    propagation_log: list[dict] = []
    packets = synthesize_packets(
        items, graph=graph, aliases=aliases,
        propagation_log=propagation_log, stamp_log_asof=True,
    )
    n_logged = log_propagations(propagation_log) if propagation_log else 0
    n_written = write_packets(packets)
    prop_log = [None] * n_logged  # for the summary count only

    if json_mode:
        import json
        payload = {
            "event": "catalyst_ingest",
            "items": len(items), "packets": n_written,
            "social_items": n_social,
            "openbb_items": n_openbb,
            "propagations": len(prop_log),
        }
        if openbb_error:
            payload["openbb_error"] = openbb_error
        print(json.dumps(payload))
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
    src_parts = []
    if n_social:
        src_parts.append(f"{n_social} social")
    if n_openbb:
        src_parts.append(f"{n_openbb} openbb")
    _src = f" ({', '.join(src_parts)})" if src_parts else ""
    print(f"📰 Catalyst Sense: {n_written} packet(s) from {len(items)} items{_src} — {lead}")
    if not os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") == "1":
        print("   (advisor consumption OFF — packets staged, flip HERMES_QUANT_SEMANTIC_ENABLED=1 to use)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
