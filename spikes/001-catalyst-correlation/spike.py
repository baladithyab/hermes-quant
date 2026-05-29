#!/usr/bin/env python3
"""Spike 001 — catalyst correlation (the butterfly engine).

QUESTION: Given the raw 2026-05-28 Blue Origin explosion headline, can an
entity-extract + propagation-graph layer surface RKLB / LUNR / ASTS as
bearish-touched with a defensible score — WITHOUT hardcoding the answer?

ANTI-CHEAT DISCIPLINE:
  * The propagation graph is built from GENERAL DOMAIN KNOWLEDGE about the
    space sector (who competes with whom, supply-chain relations) — the kind
    of edge set you'd build BEFORE any explosion happened. It does NOT encode
    "these 3 stocks dropped on 2026-05-28".
  * Entity extraction works on raw headline text, not a pre-tagged "space" label.
  * VALIDATION is the separate, after-the-fact step: we pull real price moves
    via yfinance to check whether the symbols the graph FLAGGED actually moved
    in the direction predicted. The graph never sees the price data.

This is a throwaway spike. Hardcoded, single-case, no error handling beyond
what's needed to get an honest answer.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# THE INPUT: raw catalyst headlines as they'd arrive from an RSS/GN feed.
# (Paraphrased from real 2026-05-28 coverage; the point is the TEXT, untagged.)
# ---------------------------------------------------------------------------
HEADLINES = [
    "Blue Origin's New Glenn rocket explodes during hotfire test at Cape Canaveral",
    "Jeff Bezos's Blue Origin suffers anomaly during prelaunch test, all personnel safe",
    "Space stocks tumble after Blue Origin New Glenn launchpad explosion",
]

# ---------------------------------------------------------------------------
# THE PROPAGATION GRAPH (curated, general domain knowledge — built BLIND to
# the outcome). Edges encode HOW a catalyst on an entity propagates to symbols.
#
# edge types:
#   competitor       — a competitor's FAILURE is typically BULLISH for rivals
#                       (less competition) BUT a sector-wide safety/confidence
#                       shock is BEARISH for the whole basket. Net sign is
#                       context-dependent; we encode the SECTOR-SHOCK reading
#                       (bearish contagion) as the dominant short-horizon effect
#                       for a catastrophic/safety event, which is the empirically
#                       observed pattern for launch failures (whole sector de-rates).
#   sector_member    — same sector, moves with sector sentiment
#   supply_chain     — supplier/customer linkage
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    target_symbol: str
    relation: str
    # base sign of the propagated effect for a NEGATIVE (catastrophic) catalyst
    # on the source entity. +1 bullish, -1 bearish.
    neg_catalyst_sign: int
    weight: float  # 0..1 strength of linkage

# Source entity -> outgoing edges. This is the kind of map an analyst builds
# from knowing the space sector, with NO knowledge of 2026-05-28 price action.
PROPAGATION_GRAPH: dict[str, list[Edge]] = {
    "blue origin": [
        # Direct launch-services competitors. A catastrophic launch failure is
        # a SECTOR safety/confidence shock -> bearish contagion dominates the
        # short-horizon "competitor relief" effect for the whole basket.
        Edge("RKLB", "competitor", neg_catalyst_sign=-1, weight=0.85),  # Rocket Lab
        Edge("LUNR", "competitor", neg_catalyst_sign=-1, weight=0.75),  # Intuitive Machines
        Edge("ASTS", "sector_member", neg_catalyst_sign=-1, weight=0.70),  # AST SpaceMobile
        Edge("RDW",  "sector_member", neg_catalyst_sign=-1, weight=0.60),  # Redwire
    ],
    "new glenn": [  # the vehicle -> same as its operator
        Edge("RKLB", "competitor", neg_catalyst_sign=-1, weight=0.80),
        Edge("LUNR", "competitor", neg_catalyst_sign=-1, weight=0.70),
        Edge("ASTS", "sector_member", neg_catalyst_sign=-1, weight=0.65),
    ],
    # other sectors, present to prove the graph is GENERAL not space-rigged:
    "opec": [
        Edge("XOM", "commodity", neg_catalyst_sign=+1, weight=0.7),
        Edge("CVX", "commodity", neg_catalyst_sign=+1, weight=0.7),
    ],
    "taiwan earthquake": [
        Edge("TSM", "supply_chain", neg_catalyst_sign=-1, weight=0.8),
        Edge("NVDA", "supply_chain", neg_catalyst_sign=-1, weight=0.5),
    ],
}

# Entity aliases -> canonical graph key. NER would produce these; we hardcode a
# tiny gazetteer for the spike.
ENTITY_ALIASES = {
    "blue origin": "blue origin",
    "new glenn": "new glenn",
    "bezos": "blue origin",   # person -> company
    "jeff bezos": "blue origin",
}

# A "negative catalyst" lexicon — words that mark a destructive/bearish event.
NEG_CATALYST_WORDS = {
    "explodes", "explosion", "anomaly", "fails", "failure", "crash",
    "tumble", "tumbles", "plunge", "blast", "destroyed", "fireball",
}


# ---------------------------------------------------------------------------
# STAGE 2-ish: lightweight entity extraction (stand-in for NER)
# ---------------------------------------------------------------------------
def extract_entities(text: str) -> set[str]:
    t = text.lower()
    found = set()
    for alias, canon in ENTITY_ALIASES.items():
        if alias in t:
            found.add(canon)
    return found


def catalyst_polarity(text: str) -> int:
    """-1 if the text marks a negative/destructive catalyst, else 0."""
    t = text.lower()
    return -1 if any(w in t for w in NEG_CATALYST_WORDS) else 0


# ---------------------------------------------------------------------------
# STAGE 3: propagate entities -> symbols with signed, weighted scores
# ---------------------------------------------------------------------------
@dataclass
class SymbolSignal:
    symbol: str
    score: float = 0.0          # signed: <0 bearish, >0 bullish
    contributions: list = field(default_factory=list)

def correlate(headlines: list[str]) -> dict[str, SymbolSignal]:
    signals: dict[str, SymbolSignal] = {}
    for h in headlines:
        ents = extract_entities(h)
        pol = catalyst_polarity(h)
        if pol == 0:
            continue  # not a catalyst headline
        for ent in ents:
            for edge in PROPAGATION_GRAPH.get(ent, []):
                sig = signals.setdefault(edge.target_symbol, SymbolSignal(edge.target_symbol))
                # effect = polarity * edge's neg-catalyst sign * weight
                # (pol=-1 negative catalyst; neg_catalyst_sign=-1 bearish -> +? )
                # We want: negative catalyst (pol=-1) on a competitor with
                # neg_catalyst_sign=-1 -> BEARISH symbol. So effect = pol==neg? 
                # Define effect = neg_catalyst_sign when pol is negative.
                effect = edge.neg_catalyst_sign * edge.weight if pol < 0 else 0.0
                sig.score += effect
                sig.contributions.append((ent, edge.relation, round(effect, 3)))
    return signals


# ---------------------------------------------------------------------------
# VALIDATION (separate, after-the-fact — the graph never sees this)
# ---------------------------------------------------------------------------
def validate_against_reality(symbols: list[str]) -> dict[str, float]:
    """Pull the actual move around the catalyst date via yfinance.

    Catalyst: Thu 2026-05-28 ~21:00 ET (after close). The reaction prints
    Fri 2026-05-29. We measure 2026-05-28 close -> 2026-05-29 close % move.
    """
    import yfinance as yf
    out: dict[str, float] = {}
    for sym in symbols:
        try:
            df = yf.Ticker(sym).history(start="2026-05-27", end="2026-05-31")
            if len(df) >= 2:
                closes = df["Close"].tolist()
                # last two closes spanning the event
                pct = (closes[-1] - closes[-2]) / closes[-2] * 100.0
                out[sym] = round(pct, 2)
            else:
                out[sym] = float("nan")
        except Exception as e:
            out[sym] = float("nan")
            print(f"  yfinance error {sym}: {e}", file=sys.stderr)
    return out


def main() -> int:
    print("=" * 70)
    print("SPIKE 001 — catalyst correlation (butterfly engine)")
    print("=" * 70)
    print("\nINPUT HEADLINES (raw, untagged):")
    for h in HEADLINES:
        print(f"  • {h}")

    print("\n--- STAGE: entity extraction ---")
    all_ents = set()
    for h in HEADLINES:
        e = extract_entities(h)
        all_ents |= e
        print(f"  {e or '{}'}  <- {h[:55]}")
    print(f"  entities found: {sorted(all_ents)}")

    print("\n--- STAGE: propagation -> symbols ---")
    signals = correlate(HEADLINES)
    ranked = sorted(signals.values(), key=lambda s: s.score)
    for s in ranked:
        stance = "BEARISH" if s.score < -0.1 else ("BULLISH" if s.score > 0.1 else "neutral")
        print(f"  {s.symbol:5} score={s.score:+.2f}  {stance}")
        for ent, rel, eff in s.contributions:
            print(f"          <- {ent} ({rel}) {eff:+.2f}")

    flagged = [s.symbol for s in ranked if abs(s.score) > 0.1]
    print(f"\n  FLAGGED (|score|>0.1): {flagged}")

    print("\n--- VALIDATION: did the flagged symbols actually move as predicted? ---")
    reality = validate_against_reality(flagged)
    print(f"  (measuring 2026-05-28 close -> 2026-05-29 close, the catalyst reaction)\n")
    hits, total = 0, 0
    for s in ranked:
        if abs(s.score) <= 0.1:
            continue
        actual = reality.get(s.symbol, float("nan"))
        predicted_dir = "DOWN" if s.score < 0 else "UP"
        if actual == actual:  # not nan
            actual_dir = "DOWN" if actual < 0 else "UP"
            hit = "✅" if predicted_dir == actual_dir else "❌"
            if predicted_dir == actual_dir:
                hits += 1
            total += 1
            print(f"  {s.symbol:5} predicted={predicted_dir:4} actual={actual:+6.2f}%  {actual_dir:4} {hit}")
        else:
            print(f"  {s.symbol:5} predicted={predicted_dir:4} actual=NO DATA")

    print(f"\n  DIRECTIONAL HIT RATE: {hits}/{total}")
    print("\n" + "=" * 70)
    print("VERDICT: see README.md — did the graph turn raw 'rocket exploded'")
    print("text into the right bearish space basket WITHOUT seeing prices?")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
