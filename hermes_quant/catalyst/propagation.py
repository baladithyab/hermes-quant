"""hermes_quant.catalyst.propagation — the butterfly engine (ADR-0074, D74.2).

Stage 3: entity → sector → symbol correlation. A catalyst on an entity
(Blue Origin) propagates to touched symbols (RKLB/LUNR/ASTS) via a curated,
signed, weighted edge graph. Spike 001 validated the mechanic (4/4 directional
hits on the real Blue Origin case, zero price knowledge).

Design constraints (ADR-0074):
  * Edge SIGN is the highest-risk modeling choice. ``effect_sign`` is the sign
    of the propagated effect for a NEGATIVE catalyst on the source. It is
    curated v1 and MUST be surfaced for review; every propagation is logged
    (``propagation_log``) so a learned graph can replace it later.
  * Graph linkage score → packet CONFIDENCE (not magnitude). Severity from the
    classify stage → magnitude. Kept strictly separate (D74.3).
  * v1 graph is operator-editable YAML at the path returned by graph_path().

The graph is intentionally GENERAL (energy/semis edges alongside space) so it is
not a one-event special case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Where the operator-editable curated graph lives (overridable for tests).
_DEFAULT_GRAPH_PATH = Path.home() / ".hermes" / "quant" / "catalyst" / "propagation_graph.yaml"


@dataclass(frozen=True)
class PropagationEdge:
    """One directed edge: a catalyst on ``source`` touches ``target_symbol``.

    effect_sign: sign of the propagated effect for a NEGATIVE catalyst on the
        source. +1 means "negative catalyst on source -> bullish for target"
        (rare; e.g. a competitor's failure helping a rival). -1 means "negative
        catalyst on source -> bearish for target" (sector-contagion; the
        dominant short-horizon reading for catastrophic events). A POSITIVE
        catalyst flips this sign.
    relation: human label (competitor / sector_member / supply_chain / commodity).
    weight: 0..1 linkage strength -> drives CONFIDENCE, not magnitude.
    """

    source: str
    target_symbol: str
    relation: str
    effect_sign: int
    weight: float


@dataclass
class PropagationResult:
    symbol: str
    stance: str  # bullish / bearish / neutral
    confidence: float  # 0..1 — from accumulated linkage weight
    contributions: list[dict] = field(default_factory=list)


# Built-in default graph (general domain knowledge; the seed the operator edits).
# Keyed by canonical source entity. This is what ships if no YAML override exists.
#
# effect_sign = sign of the propagated effect for a NEGATIVE catalyst on the
#   source. A POSITIVE catalyst flips it. THIS IS THE HIGHEST-RISK FIELD — every
#   edge below encodes a defensible short-horizon reading; the eval gate
#   (catalyst.eval) guards against wrong signs, and every propagation is logged
#   for the future learned graph.
_BUILTIN_GRAPH: dict[str, list[PropagationEdge]] = {
    # --- space / launch ---
    "blue origin": [
        PropagationEdge("blue origin", "RKLB", "competitor", -1, 0.85),
        PropagationEdge("blue origin", "LUNR", "competitor", -1, 0.75),
        PropagationEdge("blue origin", "ASTS", "sector_member", -1, 0.70),
        PropagationEdge("blue origin", "RDW", "sector_member", -1, 0.60),
    ],
    "new glenn": [
        PropagationEdge("new glenn", "RKLB", "competitor", -1, 0.80),
        PropagationEdge("new glenn", "LUNR", "competitor", -1, 0.70),
        PropagationEdge("new glenn", "ASTS", "sector_member", -1, 0.65),
    ],
    "rocket lab": [
        PropagationEdge("rocket lab", "RKLB", "self", -1, 0.95),
        PropagationEdge("rocket lab", "LUNR", "sector_member", -1, 0.45),
        PropagationEdge("rocket lab", "ASTS", "sector_member", -1, 0.40),
    ],
    "spacex": [
        PropagationEdge("spacex", "RKLB", "competitor", -1, 0.60),
        PropagationEdge("spacex", "ASTS", "sector_member", -1, 0.55),
    ],
    # --- energy / commodity ---
    # REMOVED 2026-05-29: the OPEC commodity edge was structurally unsignable by
    # this framework. Oil-producer direction depends on SUPPLY direction (output
    # cut -> price up -> XOM up; output surge -> price down -> XOM down), but the
    # severity-keyword classifier only extracts event POLARITY, not supply
    # direction — so every realistic OPEC headline mis-signed XOM/CVX/OXY
    # (verified: "output surge crashes prices" -> bullish XOM, exactly backwards).
    # A commodity-transmission edge needs a supply-direction classifier (LLM tier
    # / dedicated commodity module), not the contagion mechanism. Re-add only with
    # that machinery + sign-consistency coverage. The sign-eval check (D74.7)
    # caught this; it would otherwise have shipped inverted energy signals.
    # --- semis / supply chain ---
    "taiwan earthquake": [
        PropagationEdge("taiwan earthquake", "TSM", "supply_chain", -1, 0.80),
        PropagationEdge("taiwan earthquake", "NVDA", "supply_chain", -1, 0.50),
        PropagationEdge("taiwan earthquake", "AMD", "supply_chain", -1, 0.45),
    ],
    "tsmc": [
        PropagationEdge("tsmc", "TSM", "self", -1, 0.95),
        PropagationEdge("tsmc", "NVDA", "supply_chain", -1, 0.55),
        PropagationEdge("tsmc", "AMD", "supply_chain", -1, 0.45),
    ],
    # --- aerospace / airlines (a crash/grounding is sector + Boeing-supplier shock) ---
    "boeing": [
        PropagationEdge("boeing", "BA", "self", -1, 0.95),
        PropagationEdge("boeing", "SPR", "supply_chain", -1, 0.70),  # Spirit AeroSystems
        PropagationEdge("boeing", "RTX", "sector_member", -1, 0.30),
    ],
    # --- EV / autos (a recall/safety event on a leader hits the EV basket) ---
    "tesla": [
        PropagationEdge("tesla", "TSLA", "self", -1, 0.95),
        PropagationEdge("tesla", "RIVN", "sector_member", -1, 0.50),
        PropagationEdge("tesla", "LCID", "sector_member", -1, 0.45),
    ],
    # --- banks (a failure is contagion across the sector) ---
    "bank failure": [
        PropagationEdge("bank failure", "JPM", "sector_member", -1, 0.40),
        PropagationEdge("bank failure", "BAC", "sector_member", -1, 0.50),
        PropagationEdge("bank failure", "WFC", "sector_member", -1, 0.50),
        PropagationEdge("bank failure", "SCHW", "sector_member", -1, 0.55),
    ],
    # --- consumer-trend entity class (ADR-0074 Phase-1, social-arbitrage) ---
    # A NEW class: brand/product -> the public maker. effect_sign=-1 means a
    # NEGATIVE brand event (recall/scandal) is BEARISH for the maker AND (positive
    # catalyst flips the sign) a VIRAL/positive trend is BULLISH — the social-arb
    # thesis. PHASE-0 EVAL CAVEAT: the D74.7 gate passed at exactly 0.60 hit-rate
    # (3/5; TPR/NWL were false positives). Confidence is further haircut at synth
    # time (CONSUMER_TREND_CONFIDENCE_HAIRCUT) so this enters BMA as a deliberately
    # WEAK peer view until a larger labeled set clears a higher bar. relation tag
    # "brand_self" is what synthesize.py keys the haircut on.
    "celsius energy": [PropagationEdge("celsius energy", "CELH", "brand_self", -1, 0.90)],
    "crocs": [PropagationEdge("crocs", "CROX", "brand_self", -1, 0.90)],
    "dorel bicycle": [PropagationEdge("dorel bicycle", "DIIBF", "brand_self", -1, 0.85)],
    "coach handbag": [PropagationEdge("coach handbag", "TPR", "brand_self", -1, 0.88)],
    "elmer glue": [PropagationEdge("elmer glue", "NWL", "brand_self", -1, 0.85)],
}

# Entity gazetteer: surface alias -> canonical source key. NER would produce
# these; v1 uses a curated map (extend in the YAML's `aliases` section).
_BUILTIN_ALIASES: dict[str, str] = {
    "blue origin": "blue origin",
    "new glenn": "new glenn",
    "bezos": "blue origin",
    "jeff bezos": "blue origin",
    "rocket lab": "rocket lab",
    "spacex": "spacex",
    "taiwan earthquake": "taiwan earthquake",
    "tsmc": "tsmc",
    "taiwan semiconductor": "tsmc",
    "boeing": "boeing",
    "tesla": "tesla",
    "bank failure": "bank failure",
    "bank collapse": "bank failure",
    # --- consumer-trend entity class (ENTITY aliases only — no person names) ---
    "celsius": "celsius energy",
    "celsius energy": "celsius energy",
    "crocs": "crocs",
    "dorel": "dorel bicycle",
    "dorel bicycle": "dorel bicycle",
    "coach": "coach handbag",
    "coach handbag": "coach handbag",
    "tapestry": "coach handbag",
    "elmer": "elmer glue",
    "elmer glue": "elmer glue",
    "slime": "elmer glue",
}

_SIGN_TO_STANCE = {1: "bullish", -1: "bearish", 0: "neutral"}

# Append-only corpus of every propagation, for the future LEARNED graph. Mining
# sign/weight from real news→return co-movement is the moat (spike 001 caveat #3:
# "instrument v1 to log every propagation so the corpus accumulates"). Each row is
# one (entity→symbol) edge fire with its curated sign/weight + the catalyst sign,
# so a later job can join against realized forward returns and learn corrected
# signs/weights without re-deriving the curated graph.
_DEFAULT_LEARNED_LOG = Path.home() / ".hermes" / "quant" / "catalyst" / "propagation-log.jsonl"


def log_propagations(
    entries: list[dict],
    *,
    asof: str | None = None,
    path: Path | None = None,
) -> int:
    """Append propagation-log entries to the learned-graph corpus JSONL.

    ``entries`` is the list produced by ``propagate(..., log=entries)`` — one dict
    per (symbol, source, relation, effect_sign, weight, symbol_sign, catalyst_sign).
    ``asof`` (the headline publication time) is stamped on each row so the corpus is
    join-able against forward returns lookahead-honestly. Returns count written.
    Never raises fatally (silence-by-default; logging must not break the daily run).
    """
    if not entries:
        return 0
    p = path or _DEFAULT_LEARNED_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        import json
        with p.open("a", encoding="utf-8") as f:
            for e in entries:
                row = dict(e)
                if asof is not None:
                    row["asof"] = asof
                f.write(json.dumps(row, default=str) + "\n")
                n += 1
            f.flush()
    except OSError as e:  # noqa: BLE001
        logger.warning("catalyst.propagation: learned-log write failed: %s", e)
    return n


def graph_path() -> Path:
    return _DEFAULT_GRAPH_PATH


def load_graph(path: Path | None = None) -> tuple[dict[str, list[PropagationEdge]], dict[str, str]]:
    """Load the curated graph + aliases. Falls back to the built-in seed.

    YAML shape:
        aliases: {surface: canonical, ...}
        edges:
          <canonical_source>:
            - {target: RKLB, relation: competitor, effect_sign: -1, weight: 0.85}
    Never raises — a malformed/missing file falls back to the built-in graph
    (silence-by-default; the subsystem stays operational).
    """
    p = path or graph_path()
    if not p.exists():
        return dict(_BUILTIN_GRAPH), dict(_BUILTIN_ALIASES)
    try:
        import yaml  # lazy import; PyYAML is already a project dep
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("catalyst.propagation: graph load failed (%s); using built-in", e)
        return dict(_BUILTIN_GRAPH), dict(_BUILTIN_ALIASES)

    graph: dict[str, list[PropagationEdge]] = {}
    for source, edges in (data.get("edges") or {}).items():
        parsed: list[PropagationEdge] = []
        for e in edges or []:
            try:
                parsed.append(PropagationEdge(
                    source=source,
                    target_symbol=str(e["target"]).upper(),
                    relation=str(e.get("relation", "unknown")),
                    effect_sign=int(e["effect_sign"]),
                    weight=float(e["weight"]),
                ))
            except (KeyError, ValueError, TypeError) as ex:
                logger.warning("catalyst.propagation: bad edge under %r: %s", source, ex)
        if parsed:
            graph[source] = parsed
    aliases = {str(k).lower(): str(v).lower() for k, v in (data.get("aliases") or {}).items()}
    if not graph:
        return dict(_BUILTIN_GRAPH), dict(_BUILTIN_ALIASES)
    return graph, (aliases or dict(_BUILTIN_ALIASES))


def graph_target_symbols(graph: dict[str, list[PropagationEdge]]) -> set[str]:
    """All distinct target symbols the graph can propagate to."""
    return {e.target_symbol for edges in graph.values() for e in edges}


def coverage_against_universe(
    universe_symbols: set[str],
    graph: dict[str, list[PropagationEdge]] | None = None,
) -> dict[str, object]:
    """Which graph targets are tradeable vs dead-on-arrival.

    A catalyst on a symbol the advisor's universe doesn't contain produces a
    packet that is never consumed (the advisor only recommends in-universe
    names) — a silent dead edge. This surfaces the gap so graph edits can't add
    dead edges unnoticed, and so the operator can decide per-symbol: keep (the
    universe screen is transiently missing a liquid name), prune (the edge isn't
    worth a universe slot), or onboard (admit strong-catalyst names to the
    tradeable set — the ADR-0073 catalyst-driven onboarding, not yet built).

    Returns {"covered": [...], "dead_on_arrival": [...], "by_source": {sym: [src,...]}}.
    """
    g = graph if graph is not None else _BUILTIN_GRAPH
    targets = graph_target_symbols(g)
    covered = sorted(t for t in targets if t in universe_symbols)
    dead = sorted(t for t in targets if t not in universe_symbols)
    by_source: dict[str, list[str]] = {}
    for src, edges in g.items():
        for e in edges:
            if e.target_symbol in dead:
                by_source.setdefault(e.target_symbol, []).append(src)
    return {"covered": covered, "dead_on_arrival": dead, "by_source": by_source}


def extract_entities(title: str, aliases: dict[str, str]) -> set[str]:
    """Map headline text to canonical source entities via the alias gazetteer.

    v1 stand-in for NER: substring match on alias surfaces. Longest-alias-first
    so "jeff bezos" wins over "bezos" when both would match.
    """
    t = title.lower()
    found: set[str] = set()
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in t:
            found.add(aliases[alias])
    return found


def propagate(
    entities: set[str],
    catalyst_sign: int,
    graph: dict[str, list[PropagationEdge]],
    *,
    log: list[dict] | None = None,
) -> dict[str, PropagationResult]:
    """Propagate a signed catalyst on ``entities`` to touched symbols.

    For each matched entity's edges, the symbol-effect sign is::

        symbol_sign = catalyst_sign * edge.effect_sign

    i.e. a NEGATIVE catalyst (catalyst_sign=-1) over an edge whose effect_sign=-1
    (bearish-contagion) yields symbol_sign = +1*... wait: (-1)*(-1)=+1 would be
    bullish — that's wrong. effect_sign is DEFINED for a negative catalyst, so we
    apply it directly when the catalyst is negative and flip it when positive::

        symbol_sign = edge.effect_sign           if catalyst_sign < 0
        symbol_sign = -edge.effect_sign          if catalyst_sign > 0
        (neutral catalyst -> no propagation)

    Confidence accumulates as 1 - prod(1 - weight) across contributing edges
    (noisy-OR: multiple corroborating edges raise confidence, capped at 1).
    Every propagation is appended to ``log`` (for the future learned graph).
    """
    results: dict[str, PropagationResult] = {}
    if catalyst_sign == 0:
        return results

    # accumulate signed weight and noisy-OR confidence per symbol
    acc: dict[str, dict[str, Any]] = {}
    for ent in entities:
        for edge in graph.get(ent, []):
            if catalyst_sign < 0:
                symbol_sign = edge.effect_sign
            else:
                symbol_sign = -edge.effect_sign
            slot = acc.setdefault(edge.target_symbol, {"signed": 0.0, "abs": 0.0, "noisy_or": 1.0, "contribs": []})
            slot["signed"] += symbol_sign * edge.weight
            slot["abs"] += abs(edge.weight)
            slot["noisy_or"] *= (1.0 - edge.weight)
            contrib = {
                "source": ent, "relation": edge.relation,
                "effect_sign": edge.effect_sign, "weight": edge.weight,
                "symbol_sign": symbol_sign,
            }
            slot["contribs"].append(contrib)
            if log is not None:
                log.append({"symbol": edge.target_symbol, **contrib, "catalyst_sign": catalyst_sign})

    for sym, slot in acc.items():
        signed = slot["signed"]
        abs_total = slot["abs"] or 1.0
        # Directional agreement in [0,1]: 1.0 when every edge agrees on direction,
        # → 0 when opposing edges cancel. Confidence is the noisy-OR linkage scaled
        # by agreement, so a near-cancelling coin-flip net (e.g. -0.8 +0.75 = -0.05)
        # does NOT emit a high-confidence packet just because the linkage is strong.
        agreement = abs(signed) / abs_total
        linkage = 1.0 - slot["noisy_or"]  # noisy-OR, in [0,1)
        confidence = round(linkage * agreement, 4)
        sign = 1 if signed > 1e-9 else (-1 if signed < -1e-9 else 0)
        results[sym] = PropagationResult(
            symbol=sym,
            stance=_SIGN_TO_STANCE[sign],
            confidence=confidence,
            contributions=slot["contribs"],
        )
    return results
