"""ar126 — close the ar122 RESIDUAL vacuity: 3 of 5 shadow rules still recorded zero
decisions in prod even after ar122 fixed the int-direction parse.

The wave-21 replication review (RED-proven) found that ar122 only fixed the 2 rules that
read the flat advisor `direction`. The other rules gate on analyst NAMES / per-analyst
directions that never matched the real producer:
  - SemanticOnlyRule matched ("semantic","semantic_analyst","semanticanalyst") by EXACT
    equality, but the real analyst name is "hermes_semantic" (analysts/semantic.py:57)
    → never matched → vacuous.
  - TrendFollowingRule needed classical-TA's OWN direction, read only from
    signal_provenance.classical_ta_direction / payload.analyst_votes — NEITHER emitted by
    the gate → always None → vacuous. (And the analyst name is "classical-ta", hyphen.)
  - SentimentOnlyRule matched a sentiment analyst that does NOT exist in the codebase →
    correctly inert (a hypothesis-pending rule, NOT a bug).

Fix: (1) a separator-insensitive substring _analyst_voted matcher (rules.py); (2) the gate
now emits signal_provenance.per_analyst_directions {analyst: buy/sell/flat} (additive), and
_classical_ta_direction reads it keyed on the real "classical-ta" name.

These tests build the REAL post-fix gate-emitter provenance shape.
"""
from __future__ import annotations

from hermes_quant.shadow.rules import (
    SemanticOnlyRule,
    SentimentOnlyRule,
    TrendFollowingRule,
    default_rules,
)


def _event(direction=1, analysts=None, per_analyst=None, vote_share=0.7):
    """A gate_approval shaped like the REAL emitter (risk/gate.py _audit_approval +
    _build_signal_provenance): flat int direction, contributing_analysts with REAL names,
    per_analyst_directions map (ar126)."""
    sp = {
        "contributing_analysts": analysts
        if analysts is not None
        else ["hermes_semantic", "classical-ta", "microstructure-lite"],
        "vote_share": vote_share,
    }
    if per_analyst is not None:
        sp["per_analyst_directions"] = per_analyst
    return {
        "kind": "gate_approval",
        "asof": "2026-06-15T10:00:00Z",
        "source": "risk.gate",
        "payload": {
            "asset": "AAPL",
            "direction": direction,
            "confidence": 0.8,
            "target_position_pct": 0.10,
            "signal_provenance": sp,
        },
    }


def test_semantic_only_fires_on_real_hermes_semantic_name():
    """RED before ar126: SemanticOnlyRule never matched 'hermes_semantic'."""
    ev = _event(analysts=["hermes_semantic", "classical-ta"])
    d = SemanticOnlyRule().evaluate(ev)
    assert d is not None, (
        "ar126: SemanticOnlyRule must fire when the REAL 'hermes_semantic' analyst voted "
        "(it matched only the bare 'semantic' token before)"
    )
    assert d.action == "buy"


def test_semantic_only_silent_when_semantic_absent():
    """Non-vacuity guard: no semantic analyst → no fire (the rule still discriminates)."""
    ev = _event(analysts=["classical-ta", "microstructure-lite"])
    assert SemanticOnlyRule().evaluate(ev) is None


def test_trend_following_fires_on_real_per_analyst_direction():
    """RED before ar126: TrendFollowingRule's _classical_ta_direction always returned None
    (the gate emitted neither classical_ta_direction nor analyst_votes). It now reads the
    real per_analyst_directions map keyed on 'classical-ta'."""
    ev = _event(
        direction=1,
        per_analyst={"hermes_semantic": "buy", "classical-ta": "buy"},
        vote_share=0.7,
    )
    d = TrendFollowingRule().evaluate(ev)
    assert d is not None, (
        "ar126: TrendFollowingRule must fire when classical-TA direction (from "
        "per_analyst_directions) matches the advisor direction and vote_share > 0.6"
    )


def test_trend_following_silent_when_ta_disagrees():
    """Non-vacuity: TA direction opposite the advisor → no confluence → no fire."""
    ev = _event(
        direction=1,
        per_analyst={"hermes_semantic": "buy", "classical-ta": "sell"},
        vote_share=0.7,
    )
    assert TrendFollowingRule().evaluate(ev) is None


def test_trend_following_silent_below_vote_share():
    ev = _event(
        direction=1,
        per_analyst={"classical-ta": "buy"},
        vote_share=0.55,  # <= 0.6 threshold
    )
    assert TrendFollowingRule().evaluate(ev) is None


def test_sentiment_only_is_inert_no_sentiment_analyst():
    """SentimentOnlyRule stays correctly inert — no sentiment analyst exists in the
    codebase. This is a hypothesis-pending rule, NOT a bug (documented)."""
    ev = _event(analysts=["hermes_semantic", "classical-ta"])
    assert SentimentOnlyRule().evaluate(ev) is None


def test_majority_of_default_rules_fire_on_a_real_confluent_event():
    """The ADR-0049 point: a real high-conviction confluent gate_approval must exercise
    MULTIPLE rules (pre-ar126 only 2/5 could fire; SentimentOnly stays inert by design)."""
    ev = _event(
        direction=1,
        analysts=["hermes_semantic", "classical-ta"],
        per_analyst={"hermes_semantic": "buy", "classical-ta": "buy"},
        vote_share=0.7,
    )
    firing = {r.name for r in default_rules() if r.evaluate(ev) is not None}
    # always_follow + inverse_consensus (ar122) + semantic_only + trend_following (ar126).
    assert {"semantic_only", "trend_following"} <= firing, (
        f"ar126: semantic_only + trend_following must fire on a real confluent event; "
        f"firing={firing}"
    )
    assert len(firing) >= 4
