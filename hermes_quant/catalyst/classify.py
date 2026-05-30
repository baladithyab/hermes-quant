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

from dataclasses import dataclass
from typing import Literal

Polarity = Literal["negative", "positive", "neutral"]

# Severity-tiered catalyst lexicons. Negative (destructive) events de-rate;
# positive events re-rate. Magnitude scales with tier.
_NEGATIVE_SEVERITY: dict[str, float] = {
    # critical (large move)
    "explodes": 0.05, "explosion": 0.05, "blast": 0.05, "fireball": 0.05,
    "destroyed": 0.05, "crash": 0.05, "crashes": 0.05, "bankruptcy": 0.06,
    "fraud": 0.06, "halted": 0.05, "recall": 0.04, "recalls": 0.04,
    "collapse": 0.05, "collapses": 0.05, "grounded": 0.045, "grounding": 0.045,
    "blowout": 0.045, "defect": 0.035, "contagion": 0.04,
    # high
    "anomaly": 0.035, "failure": 0.035, "fails": 0.035, "plunge": 0.04,
    "plunges": 0.04, "tumble": 0.035, "tumbles": 0.035, "probe": 0.03,
    "investigation": 0.03, "lawsuit": 0.03, "downgrade": 0.03, "miss": 0.025,
    "misses": 0.025, "warning": 0.025, "cuts": 0.025, "layoffs": 0.03,
    # medium
    "delay": 0.02, "delays": 0.02, "concern": 0.015, "scrutiny": 0.015,
    "setback": 0.02, "slump": 0.02,
}
_POSITIVE_SEVERITY: dict[str, float] = {
    # critical
    "soars": 0.05, "surge": 0.045, "surges": 0.045, "breakthrough": 0.05,
    "approval": 0.045, "approved": 0.045, "wins": 0.035, "record": 0.035,
    # high
    "beats": 0.03, "upgrade": 0.03, "upgraded": 0.03, "rally": 0.03,
    "jumps": 0.035, "soar": 0.05, "acquires": 0.03, "acquisition": 0.03,
    "contract": 0.025, "deal": 0.02, "launch": 0.02, "expands": 0.02,
    # medium
    "gains": 0.015, "rises": 0.015, "growth": 0.015, "optimism": 0.015,
}


@dataclass(frozen=True)
class Classification:
    polarity: Polarity
    severity: float  # 0..~0.06, maps to packet.magnitude
    matched_terms: tuple[str, ...]
    is_catalyst: bool


def classify_headline(title: str) -> Classification:
    """Deterministic keyword classification of a single headline.

    Polarity is decided by which lexicon accumulates more severity weight;
    severity is the max single-term weight on the winning side (a headline's
    move is driven by its strongest signal, not the sum — avoids double-count
    inflation when synonyms co-occur, e.g. "explodes ... blast").
    """
    t = title.lower()
    neg_hits = [(w, s) for w, s in _NEGATIVE_SEVERITY.items() if w in t]
    pos_hits = [(w, s) for w, s in _POSITIVE_SEVERITY.items() if w in t]
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
