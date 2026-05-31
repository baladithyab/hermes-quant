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
import os
from datetime import UTC, datetime
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
from hermes_quant.perception.convergence import (
    CONVERGENCE_MIN_FAMILIES,
    ConvergenceResult,
    validate_convergence,
)
from hermes_quant.semantic import (
    SemanticPacket,
    semantic_packet_from_dict,
    validate_semantic_packet,
)

logger = logging.getLogger(__name__)

_DEFAULT_STORE = Path.home() / ".hermes" / "quant" / "catalyst" / "packets.jsonl"

# "Properly size" the social-arbitrage signal: consumer-trend (brand_self) edges
# passed the D74.7 gate at exactly 0.60 hit-rate on n=5 (TPR/NWL were false
# positives). That is enough to ENTER the ensemble, not enough to carry full
# weight. We multiply the propagation confidence of any packet whose contributions
# are dominated by the consumer-trend relation by this haircut, so it enters BMA as
# a DELIBERATELY WEAK peer view. BMA + require_ensemble already prevent it firing
# alone; this caps its pull on the blend until a larger labeled set earns more.
# Raise toward 1.0 only when a bigger eval clears a higher threshold.
_CONSUMER_TREND_RELATIONS = {"brand_self"}
CONSUMER_TREND_CONFIDENCE_HAIRCUT = 0.5

# PDR-3 (GAP-B): cross-SOURCE require_ensemble. A single-source social-arb signal
# has not been VALIDATED (Camillo: a real trend shows across >=2 independent
# sources). With HERMES_QUANT_CONVERGENCE=1, an un-validated packet is dropped
# (haircut 0.0) so it cannot fire. Set to a partial multiplier (e.g. 0.5) only if a
# larger labeled set (B09) earns a softer policy. DEFAULT-OFF: flag absent => no
# source-count requirement (byte-identical to today). Complementary to BMA's
# cross-ANALYST require_ensemble (bma.py:498-519), never a replacement.
CONVERGENCE_SINGLE_SOURCE_HAIRCUT = 0.0  # 0.0 = drop/abstain (the require_ensemble default)


def _consumer_trend_haircut(result) -> float:
    """Return the confidence multiplier for a propagation result.

    1.0 for established sector edges; CONSUMER_TREND_CONFIDENCE_HAIRCUT when the
    result is driven by consumer-trend (brand_self) edges — the weak-eval social-arb
    class. Keyed on the contribution relations so it survives graph edits.
    """
    contribs = getattr(result, "contributions", None) or []
    if contribs and all(c.get("relation") in _CONSUMER_TREND_RELATIONS for c in contribs):
        return CONSUMER_TREND_CONFIDENCE_HAIRCUT
    return 1.0


def synthesize_packets(
    items: list[CatalystItem],
    *,
    horizon: str = "1d",
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
    propagation_log: list[dict] | None = None,
    model: str = "catalyst-sense:v1",
    velocity_by_symbol: dict[str, dict] | None = None,  # PDR-2: frame.trend_velocity, keyed by symbol
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

    from hermes_quant.catalyst.classify import polarity_sign

    # PDR-3 (GAP-B): cross-SOURCE require_ensemble, read at CALL time (default-OFF
    # => byte-identical to today: the two-pass split still classifies/extracts/
    # propagates ONCE per item in the same order, and the emission policy is the
    # identity transform — no haircut, no drop, no metadata["convergence"] key).
    convergence_on = os.environ.get("HERMES_QUANT_CONVERGENCE", "0") == "1"

    # -- Pass 1: classify + extract + propagate ONCE per item (same calls, same
    # order as today so propagation_log is appended exactly once per item); group
    # the items that touch each emitting symbol so convergence can be scored over
    # the symbol's item SET (a per-item loop cannot see the set). --
    prepared: list[tuple[CatalystItem, object, set[str], dict]] = []  # (item, cls, ents, results)
    symbol_items: dict[str, list[CatalystItem]] = {}
    for item in items:
        cls = classify_headline(item.title)
        if not cls.is_catalyst:
            continue
        sign = polarity_sign(cls.polarity)
        ents = extract_entities(item.title, aliases)
        if not ents:
            continue
        results = propagate(ents, sign, graph, log=propagation_log)
        prepared.append((item, cls, ents, results))
        for sym, res in results.items():
            if res.stance == "neutral" or res.confidence <= 0.0:
                continue
            symbol_items.setdefault(sym, []).append(item)

    # -- Pass 1.5: convergence per symbol (only when the flag is ON; OFF => never
    # computed, never consulted, so output is byte-identical to today). --
    convergence_by_symbol: dict[str, ConvergenceResult] = {}
    if convergence_on:
        for sym, its in symbol_items.items():
            convergence_by_symbol[sym] = validate_convergence(
                its, min_families=CONVERGENCE_MIN_FAMILIES
            )

    # -- Pass 2: the existing emission loop (same item + results iteration order),
    # now applying the convergence policy (flag ON only). PDR-2's velocity magnitude
    # logic is preserved VERBATIM below — PDR-3 only gates EMISSION, never sources
    # magnitude (orthogonal). --
    packets: list[SemanticPacket] = []
    for item, cls, ents, results in prepared:
        for sym, res in results.items():
            if res.stance == "neutral" or res.confidence <= 0.0:
                continue
            # "Properly size": haircut consumer-trend (weak-eval social-arb) edges so
            # they enter BMA as a deliberately weak peer view.
            haircut = _consumer_trend_haircut(res)
            sized_confidence = round(min(1.0, res.confidence * haircut), 4)

            # PDR-3 (GAP-B): un-validated single-source signal => haircut/drop. Read
            # at call time; only applied when the flag is ON (else identity). It can
            # only SUBTRACT — complementary to BMA cross-ANALYST require_ensemble.
            conv = convergence_by_symbol.get(sym)
            if convergence_on and conv is not None and not conv.validated:
                sized_confidence = round(
                    sized_confidence * CONVERGENCE_SINGLE_SOURCE_HAIRCUT, 4
                )

            if sized_confidence <= 0.0:
                continue
            # PDR-2 (GAP-A): source magnitude from trend VELOCITY when the flag is
            # ON and a score exists for this symbol; else keep the severity default
            # (byte-identical when flag OFF). Magnitude vs confidence never conflated
            # (D74.3): only magnitude is re-sourced; confidence stays linkage × haircut.
            magnitude = round(float(cls.severity), 4)  # headline severity (default)
            vmag = None
            if (
                velocity_by_symbol is not None
                and os.environ.get("HERMES_QUANT_TREND_VELOCITY", "0") == "1"
            ):
                from hermes_quant.perception.velocity import velocity_magnitude

                vmag = velocity_magnitude(velocity_by_symbol.get(sym))
                if vmag is not None:
                    magnitude = vmag
            metadata = {
                "catalyst_polarity": cls.polarity,
                "matched_terms": list(cls.matched_terms),
                "source_entities": sorted(ents),
                "n_contributions": len(res.contributions),
                "feed_source": item.source,
                "relations": sorted(str(c["relation"]) for c in res.contributions if c.get("relation")),
                "confidence_haircut": haircut,
                "confidence_pre_haircut": round(res.confidence, 4),
            }
            # Provenance ONLY when velocity actually sourced the magnitude — keeps the
            # flag-OFF metadata (and packet hash) byte-identical to today (rail #1).
            if vmag is not None:
                metadata["magnitude_source"] = "velocity"
                metadata["velocity_score"] = velocity_by_symbol.get(sym)
            # PDR-3 provenance: stamp the convergence evidence ONLY when the flag is
            # ON (so the flag-OFF metadata + packet hash stay byte-identical, rail #1).
            if convergence_on and conv is not None:
                metadata["convergence"] = conv.as_evidence()
            packet = semantic_packet_from_dict({
                "schema_version": 1,
                "asset": sym,
                "asof": item.published_at.astimezone(UTC).isoformat(),  # PUB TIME
                "horizon": horizon,
                "stance": res.stance,
                "confidence": sized_confidence,                     # linkage score × size haircut
                "magnitude": magnitude,                             # severity (default) | velocity (flag ON)
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
                "metadata": metadata,
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
    collapse: bool = True,
) -> list[dict]:
    """Load valid (lookahead-honest, fresh) packets for ``symbol`` at ``asof``.

    Returns a list of packet dicts suitable for advisor ``market_extras``::

        market_extras={"semantic_packets": load_packets_for(sym, asof)}

    The returned packets have already passed validate_semantic_packet against
    ``asof`` — so they're guaranteed non-future and within freshness. The
    advisor's analyst re-validates too (defense in depth). Returns [] on any
    error (silence-by-default). This is the ONLY coupling point to the advisor.

    When ``collapse`` is True (default) and many packets describe the same event
    (a common case: dozens of syndicated headlines about one catalyst), the
    result is collapsed to the BEST packet per stance — highest confidence,
    tie-broken by most-recent asof. This keeps the advisor's per-recommend load
    small and principled (vs. feeding 80+ near-duplicate packets and letting the
    analyst's latest-wins pick ignore confidence).
    """
    p = path or _DEFAULT_STORE
    if not p.exists():
        return []
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    valid: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue  # valid JSON but not an object (corrupt/partial append) — skip
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
                valid.append(raw)
    except OSError as e:
        logger.warning("catalyst.synthesize: packet load failed: %s", e)
        return []

    if not collapse or len(valid) <= 1:
        return valid

    # Collapse to best-per-stance: highest confidence, tie-break latest asof.
    best: dict[str, dict] = {}
    for raw in valid:
        stance = raw.get("stance", "neutral")
        cur = best.get(stance)
        if cur is None:
            best[stance] = raw
            continue
        c_new = (float(raw.get("confidence", 0.0)), str(raw.get("asof", "")))
        c_cur = (float(cur.get("confidence", 0.0)), str(cur.get("asof", "")))
        if c_new > c_cur:
            best[stance] = raw
    return list(best.values())
