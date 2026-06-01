"""Phase 0 / B09: run the labeled social-arb cases through the REAL catalyst.eval harness.

Builds an in-memory consumer-trend graph extension + alias set + a consumer-trend
lexicon patch, then runs eval_gate (negative-control + precision + sign-consistency)
against the documented consumer-trend cases with REAL forward returns from yfinance.

B09 (Wave-4): the Phase-0 set was 5 cases that cleared the D74.7 >=0.60 directional bar
at a KNIFE-EDGE 3/5=0.60 (TPR/NWL were the documented false positives). This eval now runs
the LARGER 12-case fixture (Phase-0's 5 + 7 added documented consumer-trend social-arb
episodes: ELF/DECK/YETI/MNST/CMG/PTON/WING) so the directional-precision number is
HIGHER-CONFIDENCE, and at a STATED HIGHER threshold (``MIN_HIT_RATE`` below, 0.70 vs the
old 0.60). The point is a more defensible precision MEASUREMENT before B07 (data-gated)
can decide to raise the consumer-trend haircut — this script does NOT touch the haircut.

This is a measurement, not a production wire-up: the graph/lexicon extensions are
passed explicitly to the harness (graph=, aliases=) so nothing live is mutated. The
Phase-0 five (CELH/CROX/DIIBF/TPR/NWL) ARE already promoted to the seed YAML +
propagation.py; the 7 B09 brands live ONLY in this script's in-memory extension (they
are EVAL inputs, not promoted edges) until/unless a future wave proves them out.
"""
from __future__ import annotations

import datetime as dt
import json

from hermes_quant.catalyst import classify as _classify
from hermes_quant.catalyst.eval import EvalCase, SignCase, eval_gate
from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import PropagationEdge

# N13: read the VERSIONED fixture (committed, offline-deterministic), NEVER /tmp.
# Resolved repo-relative from this script (ops/scripts/ -> repo root -> tests/fixtures).
import pathlib as _pathlib
LABELS_PATH = (
    _pathlib.Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "socialarb" / "camillo_labels.json"
)

# ---------------------------------------------------------------------------
# 1. Consumer-trend LEXICON patch — the social-arb catalyst vocabulary that the
#    base lexicon lacks. Without these, a "goes viral / sells out / craze" headline
#    produces NO polarity -> no packet (the silent-miss class). Positive-tier:
#    a viral consumer trend is a positive demand catalyst.
_CONSUMER_POSITIVE = {
    "viral": 0.04, "craze": 0.045, "fad": 0.03, "trending": 0.03, "trend": 0.02,
    "sells out": 0.045, "sold out": 0.045, "sellout": 0.045,
    "stockout": 0.04, "shortage": 0.035,  # demand>supply = bullish for the maker
    "frenzy": 0.045, "buzz": 0.025, "hype": 0.025, "obsession": 0.035,
    "phenomenon": 0.03, "skyrocket": 0.05, "skyrockets": 0.05, "skyrocketing": 0.05,
    "soaring": 0.05, "surging": 0.045, "booming": 0.04, "explodes in popularity": 0.05,
    "tiktok": 0.02,  # platform-surfaced demand (weak alone; corroborates)
}

def _patch_lexicon():
    """Monkey-patch the consumer-trend words into the positive lexicon + clear the
    compiled-regex cache so they take effect. In-memory only; classify.py on disk
    is untouched until the edge proves out."""
    _classify._POSITIVE_SEVERITY.update(_CONSUMER_POSITIVE)
    _classify._compiled.cache_clear()

# ---------------------------------------------------------------------------
# 2. Consumer-trend GRAPH extension — a NEW entity class: brand/product -> ticker.
#    effect_sign semantics: for a NEGATIVE catalyst on the source. A viral trend is
#    a POSITIVE catalyst, and propagate() does symbol_sign = -effect_sign when the
#    catalyst is positive. We want a positive trend -> BULLISH symbol, so:
#        positive catalyst, want +1  =>  -effect_sign = +1  =>  effect_sign = -1
#    i.e. effect_sign=-1 reads as "a NEGATIVE brand event is bearish for the maker"
#    (a brand scandal hurts the stock) AND a positive trend is bullish. Self/brand
#    edges are weight~0.9 (the trend IS the company's product).
_CONSUMER_GRAPH: dict[str, list[PropagationEdge]] = {
    # Phase-0 five (mirror the promoted seed-YAML / propagation.py edges):
    "celsius energy": [PropagationEdge("celsius energy", "CELH", "brand_self", -1, 0.90)],
    "crocs":          [PropagationEdge("crocs", "CROX", "brand_self", -1, 0.90)],
    "dorel bicycle":  [PropagationEdge("dorel bicycle", "DIIBF", "brand_self", -1, 0.85)],
    "coach handbag":  [PropagationEdge("coach handbag", "TPR", "brand_self", -1, 0.88)],
    "elmer glue":     [PropagationEdge("elmer glue", "NWL", "brand_self", -1, 0.85)],
    # B09 seven (EVAL-only in-memory edges; NOT promoted to the live graph):
    "elf beauty":     [PropagationEdge("elf beauty", "ELF", "brand_self", -1, 0.88)],
    "ugg deckers":    [PropagationEdge("ugg deckers", "DECK", "brand_self", -1, 0.85)],
    "yeti drinkware": [PropagationEdge("yeti drinkware", "YETI", "brand_self", -1, 0.85)],
    "monster energy": [PropagationEdge("monster energy", "MNST", "brand_self", -1, 0.85)],
    "chipotle":       [PropagationEdge("chipotle", "CMG", "brand_self", -1, 0.85)],
    "peloton":        [PropagationEdge("peloton", "PTON", "brand_self", -1, 0.85)],
    "wingstop":       [PropagationEdge("wingstop", "WING", "brand_self", -1, 0.85)],
}
_CONSUMER_ALIASES: dict[str, str] = {
    # brand/product surfaces -> canonical entity. ENTITY aliases only (no person
    # names — the person-alias-contamination lesson from the 8-sector expansion).
    # Phase-0 five:
    "celsius": "celsius energy", "celsius energy": "celsius energy",
    "crocs": "crocs",
    "dorel": "dorel bicycle", "dorel bicycle": "dorel bicycle", "bicycle maker dorel": "dorel bicycle",
    "coach": "coach handbag", "coach handbag": "coach handbag", "tapestry": "coach handbag",
    "elmer": "elmer glue", "elmer glue": "elmer glue", "slime": "elmer glue",
    # B09 seven:
    "e.l.f.": "elf beauty", "e.l.f. cosmetics": "elf beauty", "e.l.f. beauty": "elf beauty",
    "elf beauty": "elf beauty", "elf cosmetics": "elf beauty",
    "ugg": "ugg deckers", "ugg boots": "ugg deckers", "deckers": "ugg deckers",
    "yeti": "yeti drinkware", "yeti tumblers": "yeti drinkware",
    "monster": "monster energy", "monster beverage": "monster energy",
    "monster energy": "monster energy", "monster energy drink": "monster energy",
    "chipotle": "chipotle",
    "peloton": "peloton",
    "wingstop": "wingstop",
}

# ---------------------------------------------------------------------------
# 3. Load the labeled cases (real fwd returns) and build EvalCases.
def _item(headline: str, date: str) -> CatalystItem:
    return CatalystItem(
        title=headline,
        published_at=dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC),
        source="phase0-label", link="n/a", query="social-arb-eval",
    )

# B09: the STATED HIGHER directional-precision bar. The Phase-0 set cleared the D74.7
# >=0.60 floor at a knife-edge 3/5=0.60; the larger 12-case fixture is run at 0.70 — a
# materially higher, n=12 higher-confidence bar. This is a MEASUREMENT threshold for the
# eval, NOT the synthesize.py haircut (that is B07, data-gated; untouched here).
MIN_HIT_RATE = 0.70


def main():
    _patch_lexicon()
    labels = json.load(open(LABELS_PATH))
    cases, scored_syms = [], []
    for c in labels:
        if c["fwd_return_pct"] is None:
            continue
        cases.append(EvalCase(
            item=_item(c["headline"], c["date"]),
            symbol=c["ticker"],
            realized_forward_return=float(c["fwd_return_pct"]),
        ))
        scored_syms.append((c["ticker"], c["fwd_return_pct"], c["headline"]))

    # Negative control: benign consumer headlines must produce ZERO packets.
    benign = [
        _item("Celsius reports quarterly results in line with expectations", "2024-01-15"),
        _item("Crocs announces routine board meeting schedule for the year", "2024-02-01"),
        _item("Tapestry to present at investor conference next month", "2024-03-01"),
        _item("Newell updates corporate governance guidelines", "2024-01-20"),
        # B09 brands — same benign-chatter control so the larger graph doesn't cry wolf:
        _item("e.l.f. Beauty schedules its annual shareholder meeting", "2024-01-22"),
        _item("Deckers names a new member to its board of directors", "2024-02-05"),
        _item("YETI to present at a consumer-products investor conference", "2024-03-04"),
        _item("Monster Beverage files routine quarterly report", "2024-01-25"),
        _item("Chipotle updates its corporate governance guidelines", "2024-02-10"),
        _item("Peloton confirms its next earnings call date", "2024-02-15"),
        _item("Wingstop announces a routine franchise-disclosure update", "2024-03-08"),
    ]

    # Sign-consistency: each consumer edge must propagate the DEFENSIBLE stance.
    # A viral/positive trend -> bullish the maker; a brand recall/scandal -> bearish.
    sign_cases = [
        SignCase("Celsius energy drink goes viral, sales soar", "CELH", "positive", "bullish"),
        SignCase("Crocs demand surges in viral craze", "CROX", "positive", "bullish"),
        SignCase("Coach handbag trend drives Tapestry sales surge", "TPR", "positive", "bullish"),
        SignCase("Elmer glue slime craze sends sales soaring", "NWL", "positive", "bullish"),
        SignCase("Dorel bicycle shortage as demand soars", "DIIBF", "positive", "bullish"),
        # B09 brands — each new edge must propagate the DEFENSIBLE positive-trend stance:
        SignCase("e.l.f. Cosmetics goes viral on TikTok, sales soar", "ELF", "positive", "bullish"),
        SignCase("UGG boots surge in viral popularity for Deckers", "DECK", "positive", "bullish"),
        SignCase("YETI tumblers go viral as drinkware demand soars", "YETI", "positive", "bullish"),
        SignCase("Monster energy drink demand surges on social buzz", "MNST", "positive", "bullish"),
        SignCase("Chipotle goes viral on TikTok as orders surge", "CMG", "positive", "bullish"),
        SignCase("Peloton demand soars as at-home fitness goes viral", "PTON", "positive", "bullish"),
        SignCase("Wingstop sales surge on viral social buzz", "WING", "positive", "bullish"),
        # negative-polarity brand event must read bearish:
        SignCase("Crocs recall over safety defect", "CROX", "negative", "bearish"),
        SignCase("e.l.f. Cosmetics recall over safety defect", "ELF", "negative", "bearish"),
    ]

    passed, neg, prec, sign = eval_gate(
        benign, cases,
        min_hit_rate=MIN_HIT_RATE,
        sign_cases=sign_cases,
        graph=_CONSUMER_GRAPH,
        aliases=_CONSUMER_ALIASES,
    )

    print("="*70)
    print("PHASE 0 — SOCIAL-ARB EDGE EVAL (real fwd returns, real harness)")
    print("="*70)
    print(f"\n[1] NEGATIVE CONTROL: {'PASS' if neg.passed else 'FAIL'}")
    print(f"    benign_items={neg.n_benign_items} spurious_packets={neg.n_spurious_packets} {neg.spurious}")
    print(f"\n[2] DIRECTIONAL PRECISION: {'PASS' if prec.passed else 'FAIL'}")
    print(f"    cases={prec.n_cases} scored={prec.n_scored} hits={prec.hits} hit_rate={prec.hit_rate} (min {MIN_HIT_RATE:.2f})")
    for m in prec.misses:
        print(f"    MISS: {m}")
    print(f"\n[3] EDGE-SIGN CONSISTENCY: {'PASS' if sign.passed else 'FAIL'}")
    print(f"    cases={sign.n_cases} correct={sign.n_correct}")
    for m in sign.mismatches:
        print(f"    MISMATCH: {m}")
    print(f"\n{'='*70}\nGATE VERDICT: {'PASS — measurable edge' if passed else 'FAIL — do not ship'}")
    print("="*70)

    # Detail: per-case realized returns (the honest distribution)
    print("\nPer-case realized forward returns (NOT cherry-picked):")
    pos = sum(1 for _,r,_ in scored_syms if r>0)
    for tk,r,h in scored_syms:
        print(f"  {tk:7s} {r:+8.1f}%   {h[:55]}")
    print(f"  -> {pos}/{len(scored_syms)} directionally positive; "
          f"mean {sum(r for _,r,_ in scored_syms)/len(scored_syms):+.1f}%, "
          f"median {sorted(r for _,r,_ in scored_syms)[len(scored_syms)//2]:+.1f}%")

if __name__ == "__main__":
    main()
