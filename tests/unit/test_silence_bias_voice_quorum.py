"""Tests for the silence-bias gate's Dim-3 voice-quorum count (ar57).

The bug its own docstring describes (silence_bias.py "structural pruning is
upstream of the gate's min_analysts_emitted count"): the gate computes
`n_emitted = len(advisor_result['analyst_views'])` over the RAW dict the
advisor emits. But the advisor appends EVERY non-None AnalystView to
`analyst_views` (advisor._view_to_dict / advisor.recommend) — including:

  - KronosAnalyst's zero-confidence abstain view (kronos._abstain returns
    confidence=0.0 on weight-load failure; Kronos is in the DEFAULT
    3-analyst committee).
  - grounding-dropped views (advisor annotates `grounding_dropped=True`
    but LEAVES them in analyst_views for the audit trail).

BMA's ABSTAIN_THRESHOLD filter (bma.aggregate: `v.confidence >= 0.10`) and
grounding enforcement both drop these from BMA's LOCAL vote membership —
they NEVER mutate `advisor_result['analyst_views']`, which is the dict the
gate reads directly (autonomous.py calls silence_bias_gate(advisor_result)).

CONCRETE LIVE FAIL-OPEN: operator sets min_analysts_emitted=3 to demand 3
independent voices. Two real analysts agree LONG, Kronos abstains. The raw
analyst_views list has 3 dict entries -> n_emitted=3 >= 3 PASSES Dim-3 and
an autonomous money order can FIRE on 2 real voices against a quorum the
operator set precisely to require 3.

These tests exercise the GATE's n_emitted directly (the existing
test_bma_abstain_filter.py only asserts on BMA sig.components, never the
gate count — which is why the bug is currently live and unguarded).
"""

from __future__ import annotations

from hermes_quant.gates.silence_bias import (
    GateConfig,
    GateDecision,
    silence_bias_gate,
)

# ---------------------------------------------------------------------------
# Helpers — synthesize advisor results with the REAL analyst_views shape
# ---------------------------------------------------------------------------


def _view(
    *,
    analyst: str,
    confidence: float,
    direction: int = 1,
    magnitude: float = 0.10,
    grounding_dropped: bool = False,
    atr_relative: float = 0.05,
) -> dict:
    """A view dict shaped exactly like advisor._view_to_dict() emits."""
    v = {
        "analyst": analyst,
        "direction": int(direction),
        "magnitude": float(magnitude),
        "confidence": float(confidence),
        "confidence_raw": float(confidence),
        "horizon": "1d",
        "rationale": "test",
        "metadata": {"atr_relative": atr_relative},
    }
    if grounding_dropped:
        v["grounding_dropped"] = True
        v["grounding_reason"] = "uncited_claim"
    return v


def _advisor_result(views: list[dict], *, confidence: float = 0.85) -> dict:
    """Build an advisor_result dict whose aggregated_signal would FIRE if the
    voice quorum is (incorrectly) satisfied."""
    return {
        "aggregated_signal": {
            "confidence": confidence,
            "direction": 1,
            "magnitude": 0.10,
        },
        "risk_gate": {
            "pass": True,
            "kelly_fraction": 0.05,
            "gated_reason": None,
            "reason": "ok",
        },
        "analyst_views": views,
    }


# ---------------------------------------------------------------------------
# Abstain path: Kronos default-committee zero-confidence abstain
# ---------------------------------------------------------------------------


def test_abstain_view_does_not_count_toward_quorum():
    """2 real LONG voices + 1 Kronos abstain (confidence=0.0). Operator demands
    3 voices. The abstain must NOT count -> SILENCE_INSUFFICIENT_VOICES.

    This is the headline live fail-open: without the fix n_emitted=3 PASSES."""
    views = [
        _view(analyst="ClassicalTA", confidence=0.7),
        _view(analyst="MicrostructureLite", confidence=0.7),
        _view(analyst="KronosAnalyst", confidence=0.0, direction=0, magnitude=0.0),
    ]
    r = _advisor_result(views)
    cfg = GateConfig(min_analysts_emitted=3)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES, (
        f"abstain view must not satisfy the quorum; got {out.decision} "
        f"with details {out.details}"
    )
    assert out.details["emitted"] == 2
    assert out.details["min_required"] == 3


def test_two_real_plus_abstain_at_default_quorum_fires():
    """Sanity: at default min=2, two REAL voices + an abstain still FIREs
    (the two real voices satisfy the 2-quorum). Verifies the filter doesn't
    over-silence the genuinely-sufficient case."""
    views = [
        _view(analyst="ClassicalTA", confidence=0.7),
        _view(analyst="MicrostructureLite", confidence=0.7),
        _view(analyst="KronosAnalyst", confidence=0.0, direction=0, magnitude=0.0),
    ]
    r = _advisor_result(views)
    cfg = GateConfig(min_analysts_emitted=2)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.FIRE
    assert out.details["n_voices"] == 2


# ---------------------------------------------------------------------------
# Grounding-dropped path: phantom voter at the DEFAULT quorum (min=2)
# ---------------------------------------------------------------------------


def test_grounding_dropped_view_does_not_count_toward_quorum():
    """1 real voice + 1 grounding-dropped phantom (confidence>=0.10 but
    grounding_dropped=True). At default min=2 the phantom must NOT count ->
    SILENCE_INSUFFICIENT_VOICES (the clean default-quorum fail-open)."""
    views = [
        _view(analyst="ClassicalTA", confidence=0.7),
        _view(analyst="SemanticAnalyst", confidence=0.6, grounding_dropped=True),
    ]
    r = _advisor_result(views)
    cfg = GateConfig(min_analysts_emitted=2)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES, (
        f"grounding-dropped view must not satisfy the quorum; got {out.decision} "
        f"with details {out.details}"
    )
    assert out.details["emitted"] == 1


def test_two_real_plus_grounding_dropped_at_quorum_three_silences():
    """2 real + 1 grounding-dropped phantom, operator demands 3 -> SILENCE
    (only 2 real voices)."""
    views = [
        _view(analyst="ClassicalTA", confidence=0.7),
        _view(analyst="MicrostructureLite", confidence=0.7),
        _view(analyst="SemanticAnalyst", confidence=0.6, grounding_dropped=True),
    ]
    r = _advisor_result(views)
    cfg = GateConfig(min_analysts_emitted=3)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES
    assert out.details["emitted"] == 2


# ---------------------------------------------------------------------------
# Non-finite confidence must also fail-CLOSED (NaN abstain)
# ---------------------------------------------------------------------------


def test_nan_confidence_view_does_not_count_toward_quorum():
    """A NaN-confidence view must not count as a real voter (NaN >= 0.10 is
    False; fail-CLOSED toward silence)."""
    views = [
        _view(analyst="ClassicalTA", confidence=0.7),
        _view(analyst="Broken", confidence=float("nan")),
    ]
    r = _advisor_result(views)
    cfg = GateConfig(min_analysts_emitted=2)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES
    assert out.details["emitted"] == 1


# ---------------------------------------------------------------------------
# Three genuine voices still FIRE at min=3 (non-vacuity of the FIRE path)
# ---------------------------------------------------------------------------


def test_three_real_voices_fires_at_quorum_three():
    """3 genuine voices (all confidence>=0.10, none grounding-dropped) must
    FIRE at min=3 — the filter must not silence the legitimately-quorate case."""
    views = [
        _view(analyst="ClassicalTA", confidence=0.7),
        _view(analyst="MicrostructureLite", confidence=0.7),
        _view(analyst="KronosAnalyst", confidence=0.6),
    ]
    r = _advisor_result(views)
    cfg = GateConfig(min_analysts_emitted=3)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.FIRE
    assert out.details["n_voices"] == 3
