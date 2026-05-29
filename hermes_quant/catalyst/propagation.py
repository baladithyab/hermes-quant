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
_BUILTIN_GRAPH: dict[str, list[PropagationEdge]] = {
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
    "opec": [
        PropagationEdge("opec", "XOM", "commodity", 1, 0.70),
        PropagationEdge("opec", "CVX", "commodity", 1, 0.70),
    ],
    "taiwan earthquake": [
        PropagationEdge("taiwan earthquake", "TSM", "supply_chain", -1, 0.80),
        PropagationEdge("taiwan earthquake", "NVDA", "supply_chain", -1, 0.50),
    ],
}

# Entity gazetteer: surface alias -> canonical source key. NER would produce
# these; v1 uses a curated map (extend in the YAML's `aliases` section).
_BUILTIN_ALIASES: dict[str, str] = {
    "blue origin": "blue origin",
    "new glenn": "new glenn",
    "bezos": "blue origin",
    "jeff bezos": "blue origin",
    "opec": "opec",
    "taiwan earthquake": "taiwan earthquake",
}

_SIGN_TO_STANCE = {1: "bullish", -1: "bearish", 0: "neutral"}


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
            slot = acc.setdefault(edge.target_symbol, {"signed": 0.0, "noisy_or": 1.0, "contribs": []})
            slot["signed"] += symbol_sign * edge.weight
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
        confidence = round(1.0 - slot["noisy_or"], 4)  # noisy-OR, in [0,1)
        sign = 1 if signed > 1e-9 else (-1 if signed < -1e-9 else 0)
        results[sym] = PropagationResult(
            symbol=sym,
            stance=_SIGN_TO_STANCE[sign],
            confidence=confidence,
            contributions=slot["contribs"],
        )
    return results
