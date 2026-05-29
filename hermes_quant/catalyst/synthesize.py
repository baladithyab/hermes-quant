"""hermes_quant.catalyst.synthesize — catalyst items -> SemanticPackets (ADR-0074).

Stages 4+5: take ingested+classified+correlated catalysts and emit
SemanticPackets keyed by (symbol, asof). Two hard rules from the spike caveats:

  * packet.asof = the headline's PUBLICATION time (item.published_at), NEVER
    wall-clock-now (D74.4). This keeps backtests honest; the lookahead gate does
    the rest.
  * confidence <- propagation linkage score; magnitude <- classify severity.
    Never conflated (D74.3).

A packet's stance comes from the propagation result (which already folds in the
catalyst polarity). Packets are persisted append-only to a JSONL store and can be
loaded at advisor recommend-time via load_packets_for().
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from hermes_quant.catalyst.classify import classify_headline
from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import (
    PropagationEdge,
    extract_entities,
    load_graph,
    propagate,
)
from hermes_quant.semantic import (
    SemanticPacket,
    SemanticSource,
    semantic_packet_from_dict,
    validate_semantic_packet,
)

logger = logging.getLogger(__name__)

_DEFAULT_STORE = Path.home() / ".hermes" / "quant" / "catalyst" / "packets.jsonl"


def synthesize_packets(
    items: list[CatalystItem],
    *,
    horizon: str = "1d",
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
    propagation_log: list[dict] | None = None,
    model: str = "catalyst-sense:v1",
) -> list[SemanticPacket]:
    """Turn catalyst items into SemanticPackets via classify + propagate.

    For each item: classify polarity+severity, extract entities, propagate to
    symbols, emit one packet per touched symbol with asof = item.published_at.
    Neutral / non-catalyst items and neutral propagations produce no packet.
    """
    if graph is None or aliases is None:
        g, a = load_graph()
        graph = graph or g
        aliases = aliases or a

    packets: list[SemanticPacket] = []
    for item in items:
        cls = classify_headline(item.title)
        if not cls.is_catalyst:
            continue
        from hermes_quant.catalyst.classify import polarity_sign
        sign = polarity_sign(cls.polarity)
        ents = extract_entities(item.title, aliases)
        if not ents:
            continue
        results = propagate(ents, sign, graph, log=propagation_log)
        for sym, res in results.items():
            if res.stance == "neutral" or res.confidence <= 0.0:
                continue
            packet = semantic_packet_from_dict({
                "schema_version": 1,
                "asset": sym,
                "asof": item.published_at.astimezone(timezone.utc).isoformat(),  # PUB TIME
                "horizon": horizon,
                "stance": res.stance,
                "confidence": round(min(1.0, res.confidence), 4),  # linkage score
                "magnitude": round(float(cls.severity), 4),         # headline severity
                "summary": (
                    f"{item.title[:180]} — propagated {res.stance} to {sym} "
                    f"via {', '.join(c['relation'] for c in res.contributions[:3])}."
                ),
                "sources": [{
                    "type": "google_news_rss",
                    "ref": item.link or "n/a",
                    "title": item.title[:200],
                }],
                "model": model,
                "metadata": {
                    "catalyst_polarity": cls.polarity,
                    "matched_terms": list(cls.matched_terms),
                    "source_entities": sorted(ents),
                    "n_contributions": len(res.contributions),
                    "feed_source": item.source,
                },
            })
            packets.append(packet)
    return packets


def write_packets(packets: list[SemanticPacket], *, path: Path | None = None) -> int:
    """Append packets to the JSONL store. Returns count written. Never raises fatally."""
    p = path or _DEFAULT_STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        with p.open("a", encoding="utf-8") as f:
            for pkt in packets:
                f.write(json.dumps(pkt.to_dict(include_hash=True), default=str) + "\n")
                n += 1
            f.flush()
    except OSError as e:
        logger.warning("catalyst.synthesize: packet write failed: %s", e)
    return n


def load_packets_for(
    symbol: str,
    asof: datetime | pd.Timestamp | str,
    *,
    horizon: str = "1d",
    max_age_minutes: float = 24 * 60,
    path: Path | None = None,
) -> list[dict]:
    """Load valid (lookahead-honest, fresh) packets for ``symbol`` at ``asof``.

    Returns a list of packet dicts suitable for advisor ``market_extras``::

        market_extras={"semantic_packets": load_packets_for(sym, asof)}

    The returned packets have already passed validate_semantic_packet against
    ``asof`` — so they're guaranteed non-future and within freshness. The
    advisor's analyst re-validates too (defense in depth). Returns [] on any
    error (silence-by-default). This is the ONLY coupling point to the advisor.
    """
    p = path or _DEFAULT_STORE
    if not p.exists():
        return []
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("asset") != symbol:
                continue
            try:
                packet = semantic_packet_from_dict(raw, attach_hash=False)
            except Exception:  # noqa: BLE001
                continue
            ok, _reason = validate_semantic_packet(
                packet, asset=symbol, asof=asof_ts, horizon=horizon,
                max_age_minutes=max_age_minutes, verify_hash=False,
            )
            if ok:
                out.append(raw)
    except OSError as e:
        logger.warning("catalyst.synthesize: packet load failed: %s", e)
        return []
    return out
