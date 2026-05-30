"""hermes_quant.catalyst.classify — catalyst severity + entity classification.

Stage 2 of Catalyst Sense (ADR-0074, D74.6). A cheap-to-expensive cascade; v1
ships the DETERMINISTIC keyword tier so the subsystem runs dependency-free and
replayably. The LLM tier is an optional refinement (not required for v1) and is
intentionally left as a documented seam.

The deterministic tier answers two questions per headline:
  1. Is this a catalyst at all, and what POLARITY (negative/positive/neutral)?
  2. What SEVERITY (drives packet.magnitude downstream)?
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

Polarity = Literal["negative", "positive", "neutral"]

# Severity-tiered catalyst lexicons. Negative (destructive) events de-rate;
# positive events re-rate. Magnitude scales with tier.
#
# Each key is a surface form matched with WORD BOUNDARIES (see _compiled()).
# Inflections are enumerated EXPLICITLY rather than inferred, because the two
# naive shortcuts both fail:
#   * substring match ("recall" in title) catches "recalling"/"recalled" but
#     ALSO catches "miss" in "mission"/"emissions" — wrong-polarity false fires.
#   * prefix-wildcard ("\bmiss\w*") still matches "mission".
# So we list the real inflected forms of each catalyst verb. A missing
# inflection is a silent miss (recall vs recalling was found live 2026-05-29),
# so the inflected forms below are deliberately exhaustive for the high-severity
# verbs (recall/plunge/tumble/downgrade/miss/soar/crash etc.).
_NEGATIVE_SEVERITY: dict[str, float] = {
    # critical (large move)
    "explodes": 0.05, "explode": 0.05, "exploded": 0.05, "explosion": 0.05,
    "blast": 0.05, "fireball": 0.05, "mishap": 0.05, "destroyed": 0.05,
    "crash": 0.05, "crashes": 0.05, "crashed": 0.05,
    "bankruptcy": 0.06, "bankrupt": 0.06, "fraud": 0.06,
    "halt": 0.05, "halts": 0.05, "halted": 0.05,
    "recall": 0.04, "recalls": 0.04, "recalled": 0.04, "recalling": 0.04,
    "collapse": 0.05, "collapses": 0.05, "collapsed": 0.05,
    "grounded": 0.045, "grounding": 0.045, "blowout": 0.045,
    "defect": 0.035, "defects": 0.035, "contagion": 0.04,
    # high
    "anomaly": 0.035, "failure": 0.035, "fails": 0.035, "failed": 0.035,
    "plunge": 0.04, "plunges": 0.04, "plunged": 0.04,
    "tumble": 0.035, "tumbles": 0.035, "tumbled": 0.035,
    "probe": 0.03, "investigation": 0.03, "lawsuit": 0.03,
    "downgrade": 0.03, "downgrades": 0.03, "downgraded": 0.03,
    "miss": 0.025, "misses": 0.025, "missed": 0.025,
    "warning": 0.025, "cuts": 0.025, "layoffs": 0.03, "sinks": 0.035, "sank": 0.035,
    # medium
    "delay": 0.02, "delays": 0.02, "delayed": 0.02, "concern": 0.015,
    "concerns": 0.015, "scrutiny": 0.015, "setback": 0.02, "slump": 0.02, "slumps": 0.02,
}
_POSITIVE_SEVERITY: dict[str, float] = {
    # critical
    "soar": 0.05, "soars": 0.05, "soared": 0.05, "surge": 0.045,
    "surges": 0.045, "surged": 0.045, "breakthrough": 0.05,
    "approval": 0.045, "approved": 0.045, "approves": 0.045,
    "wins": 0.035, "win": 0.035, "won": 0.035, "record": 0.035,
    # high
    "beats": 0.03, "beat": 0.03, "upgrade": 0.03, "upgrades": 0.03,
    "upgraded": 0.03, "rally": 0.03, "rallies": 0.03, "rallied": 0.03,
    "jumps": 0.035, "jumped": 0.035, "acquires": 0.03, "acquired": 0.03,
    "acquisition": 0.03, "contract": 0.025, "deal": 0.02, "launch": 0.02,
    "launches": 0.02, "expands": 0.02, "expanded": 0.02,
    # medium
    "gains": 0.015, "gained": 0.015, "rises": 0.015, "rose": 0.015,
    "growth": 0.015, "optimism": 0.015,
    # --- consumer-trend / social-arbitrage vocabulary (ADR-0074 Phase-1) ---
    # A viral consumer trend is a positive DEMAND catalyst on the brand's maker.
    # Without these the base lexicon only fires on incidental price-verbs
    # ("surge"/"soar"), missing the social signal itself ("goes viral", "craze",
    # "sells out"). Severity is the headline's strongest single term (MAX, not sum).
    "viral": 0.04, "craze": 0.045, "fad": 0.03, "trending": 0.03, "trend": 0.02,
    "sells out": 0.045, "sold out": 0.045, "sellout": 0.045, "stockout": 0.04,
    "shortage": 0.035, "frenzy": 0.045, "buzz": 0.025, "hype": 0.025,
    "obsession": 0.035, "phenomenon": 0.03,
    "skyrocket": 0.05, "skyrockets": 0.05, "skyrocketing": 0.05,
    "soaring": 0.05, "surging": 0.045, "booming": 0.04,
}


@dataclass(frozen=True)
class Classification:
    polarity: Polarity
    severity: float  # 0..~0.06, maps to packet.magnitude
    matched_terms: tuple[str, ...]
    is_catalyst: bool


@lru_cache(maxsize=2)
def _compiled(lexicon_id: int) -> dict[str, re.Pattern]:
    """Compile each catalyst term into a word-boundary regex.

    Substring matching (``term in title``) produced false positives where a
    short catalyst word is a substring of a benign word: "miss" in "mission" /
    "dismisses" / "emissions", "deal" in "ideal" / "dealer", "gains" in
    "bargains" / "against". A wrong-polarity catalyst on a graph entity
    propagates a wrong-direction packet into the BMA — exactly the
    false-correlation class the eval gate exists to prevent. Word boundaries
    (``\\b``) eliminate the substring traps while still matching the term as a
    standalone token (and its space-separated multi-word forms).
    """
    lex = _NEGATIVE_SEVERITY if lexicon_id == 0 else _POSITIVE_SEVERITY
    return {w: re.compile(rf"\b{re.escape(w)}\b") for w in lex}


def classify_headline(title: str) -> Classification:
    """Deterministic keyword classification of a single headline.

    Polarity is decided by which lexicon accumulates more severity weight;
    severity is the max single-term weight on the winning side (a headline's
    move is driven by its strongest signal, not the sum — avoids double-count
    inflation when synonyms co-occur, e.g. "explodes ... blast").

    Matching is WORD-BOUNDARY (not substring): "mission" does not trigger
    "miss", "ideal" does not trigger "deal". See _compiled() for why.
    """
    t = title.lower()
    neg_pat = _compiled(0)
    pos_pat = _compiled(1)
    neg_hits = [(w, s) for w, s in _NEGATIVE_SEVERITY.items() if neg_pat[w].search(t)]
    pos_hits = [(w, s) for w, s in _POSITIVE_SEVERITY.items() if pos_pat[w].search(t)]
    neg_weight = max((s for _, s in neg_hits), default=0.0)
    pos_weight = max((s for _, s in pos_hits), default=0.0)

    if neg_weight == 0.0 and pos_weight == 0.0:
        return Classification("neutral", 0.0, (), is_catalyst=False)
    if neg_weight >= pos_weight:
        return Classification(
            "negative", neg_weight, tuple(w for w, _ in neg_hits), is_catalyst=True
        )
    return Classification(
        "positive", pos_weight, tuple(w for w, _ in pos_hits), is_catalyst=True
    )


def polarity_sign(polarity: Polarity) -> int:
    """Map polarity to a catalyst sign: -1 negative, +1 positive, 0 neutral."""
    return {"negative": -1, "positive": 1, "neutral": 0}[polarity]
