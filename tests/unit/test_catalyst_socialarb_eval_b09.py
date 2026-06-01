"""B09 (Wave-4) — the LARGER, higher-threshold social-arb directional-precision eval.

The Phase-0 social-arb set was 5 cases (CELH/CROX/DIIBF/TPR/NWL) that cleared the D74.7
``>=0.60`` directional bar at a KNIFE-EDGE 3/5=0.60. B09 expands the committed fixture to
12 documented consumer-trend social-arb episodes (+ELF/DECK/YETI/MNST/CMG/PTON/WING) so the
precision MEASUREMENT is higher-confidence, and runs ``run_precision`` over it at a STATED
HIGHER ``min_hit_rate`` (0.70 vs 0.60).

Fully OFFLINE/deterministic off the versioned fixture (N13: committed under tests/fixtures,
NEVER /tmp, NEVER live yfinance in-test — the real forward returns were captured ONCE by
ops/scripts/quant-catalyst-socialarb-labels.py and committed). The graph extension here
MIRRORS ops/scripts/quant-catalyst-socialarb-eval.py: the Phase-0 five are also promoted
to the live seed YAML, the 7 B09 brands are EVAL-only in-memory edges (eval inputs, not
promoted edges). External truth (committed returns), never self-graded.

This DOES NOT change the synthesize.py consumer-trend haircut (that is B07, data-gated);
it only raises the eval MEASUREMENT bar so B07 has a higher-confidence number to act on.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from hermes_quant.catalyst.eval import EvalCase, eval_gate, run_precision
from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import PropagationEdge, load_graph

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "socialarb"

# The STATED HIGHER bar (matches MIN_HIT_RATE in the ops eval script). Above the old 0.60
# floor, and a materially higher-confidence number on n=12 than the n=5 knife-edge.
MIN_HIT_RATE = 0.70

# The 7 B09 brand_self edges + aliases — EVAL-only in-memory (NOT promoted to the live
# graph). Mirrors ops/scripts/quant-catalyst-socialarb-eval.py so the test measures the
# SAME extended graph the ops script reports on.
_B09_GRAPH: dict[str, list[PropagationEdge]] = {
    "elf beauty":     [PropagationEdge("elf beauty", "ELF", "brand_self", -1, 0.88)],
    "ugg deckers":    [PropagationEdge("ugg deckers", "DECK", "brand_self", -1, 0.85)],
    "yeti drinkware": [PropagationEdge("yeti drinkware", "YETI", "brand_self", -1, 0.85)],
    "monster energy": [PropagationEdge("monster energy", "MNST", "brand_self", -1, 0.85)],
    "chipotle":       [PropagationEdge("chipotle", "CMG", "brand_self", -1, 0.85)],
    "peloton":        [PropagationEdge("peloton", "PTON", "brand_self", -1, 0.85)],
    "wingstop":       [PropagationEdge("wingstop", "WING", "brand_self", -1, 0.85)],
}
_B09_ALIASES: dict[str, str] = {
    "e.l.f.": "elf beauty", "e.l.f. cosmetics": "elf beauty", "e.l.f. beauty": "elf beauty",
    "elf beauty": "elf beauty", "elf cosmetics": "elf beauty",
    "ugg": "ugg deckers", "ugg boots": "ugg deckers", "deckers": "ugg deckers",
    "yeti": "yeti drinkware", "yeti tumblers": "yeti drinkware",
    "monster": "monster energy", "monster beverage": "monster energy",
    "monster energy": "monster energy", "monster energy drink": "monster energy",
    "chipotle": "chipotle", "peloton": "peloton", "wingstop": "wingstop",
}


def _extended_graph() -> tuple[dict, dict]:
    g, a = load_graph()
    g = dict(g)
    g.update(_B09_GRAPH)
    a = dict(a)
    a.update(_B09_ALIASES)
    return g, a


def _item(headline: str, date: str) -> CatalystItem:
    return CatalystItem(
        title=headline,
        published_at=dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC),
        source="b09-label",
        link="n/a",
        query="social-arb-eval",
    )


def _load_cases() -> list[EvalCase]:
    labels = json.loads((FIXT / "camillo_labels.json").read_text())  # versioned, NOT /tmp
    cases: list[EvalCase] = []
    for c in labels:
        if c["fwd_return_pct"] is None:
            continue
        cases.append(
            EvalCase(
                item=_item(c["headline"], c["date"]),
                symbol=c["ticker"],
                realized_forward_return=float(c["fwd_return_pct"]),
            )
        )
    return cases


_BENIGN = [
    _item("Celsius reports quarterly results in line with expectations", "2024-01-15"),
    _item("Crocs announces routine board meeting schedule for the year", "2024-02-01"),
    _item("Tapestry to present at investor conference next month", "2024-03-01"),
    _item("Newell updates corporate governance guidelines", "2024-01-20"),
    _item("e.l.f. Beauty schedules its annual shareholder meeting", "2024-01-22"),
    _item("Deckers names a new member to its board of directors", "2024-02-05"),
    _item("YETI to present at a consumer-products investor conference", "2024-03-04"),
    _item("Monster Beverage files routine quarterly report", "2024-01-25"),
    _item("Chipotle updates its corporate governance guidelines", "2024-02-10"),
    _item("Peloton confirms its next earnings call date", "2024-02-15"),
    _item("Wingstop announces a routine franchise-disclosure update", "2024-03-08"),
]


def test_fixture_is_larger_than_phase0_and_versioned():
    """The B09 set is committed (N13) and STRICTLY LARGER than the Phase-0 five."""
    labels = json.loads((FIXT / "camillo_labels.json").read_text())
    assert (FIXT / "camillo_labels.json").exists()
    assert len(labels) > 5, "B09 must expand the eval set beyond the Phase-0 five"
    assert len(labels) == 12
    # every case carries provenance (the defensibility record) and a captured return:
    assert all(c.get("provenance") for c in labels), "each case needs documented provenance"
    assert all(c["fwd_return_pct"] is not None for c in labels), "no uncaptured returns"


def test_b09_larger_set_clears_higher_precision_bar():
    """Directional precision over the LARGER 12-case set clears the STATED HIGHER bar
    (0.70), vs the Phase-0 knife-edge 0.60. External truth (committed yfinance returns)."""
    cases = _load_cases()
    g, a = _extended_graph()
    res = run_precision(cases, min_hit_rate=MIN_HIT_RATE, graph=g, aliases=a)
    assert res.passed, (
        f"B09 FAIL: hit_rate={res.hit_rate} scored={res.n_scored} misses={res.misses}"
    )
    # all 12 cases produce a packet and a non-zero return => all scored.
    assert res.n_scored == 12
    assert res.hits == 9  # CELH/CROX/DIIBF/ELF/DECK/YETI/MNST/PTON/WING
    assert res.hit_rate == 0.75
    assert res.hit_rate > 0.60, "must be strictly above the old Phase-0 knife-edge"


def test_b09_misses_are_the_documented_false_positives():
    """The 3 misses are the DOCUMENTED false positives (TPR/NWL/CMG), not surprises —
    proof the set is honest (not cherry-picked to inflate the rate)."""
    cases = _load_cases()
    g, a = _extended_graph()
    res = run_precision(cases, min_hit_rate=MIN_HIT_RATE, graph=g, aliases=a)
    missed_syms = {m.split(":")[0] for m in res.misses}
    assert missed_syms == {"TPR", "NWL", "CMG"}


def test_b09_negative_control_zero_packets():
    """Benign chatter on every brand (Phase-0 + B09) produces ZERO packets — the larger
    graph does not cry wolf (the cry-wolf guard scales with the set)."""
    cases = _load_cases()
    g, a = _extended_graph()
    passed, neg, prec, sign = eval_gate(
        _BENIGN, cases, min_hit_rate=MIN_HIT_RATE, graph=g, aliases=a
    )
    assert neg.passed, f"negative control fired packets: {neg.spurious}"
    assert neg.n_spurious_packets == 0
    assert prec.passed  # the gate's precision axis also clears the higher bar
    assert passed
