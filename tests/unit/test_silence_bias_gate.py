"""Tests for hermes_quant.gates.silence_bias (ADR-0016 §D2).

Covers all four dimensions of the gate, the structured silence reasons,
the gated-by-advisor pass-through, and the dim ordering (cheap dims first).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hermes_quant.gates.silence_bias import (
    GateConfig,
    GateDecision,
    SilenceBiasGate,
    _count_recent_rejections,
    silence_bias_gate,
)

# ---------------------------------------------------------------------------
# Helpers — synthesize advisor results
# ---------------------------------------------------------------------------

def _result(
    *,
    confidence: float = 0.8,
    direction: int = 1,
    magnitude: float = 0.05,
    n_voices: int = 2,
    risk_pass: bool = True,
    atr_relative: float | None = 0.05,
    risk_reason: str = "ok",
):
    """Build a minimal advisor result dict suitable for the gate."""
    views = [
        {"analyst": f"A{i}", "metadata": {"atr_relative": atr_relative}}
        for i in range(n_voices)
    ]
    return {
        "aggregated_signal": {
            "confidence": confidence,
            "direction": direction,
            "magnitude": magnitude,
        },
        "risk_gate": {
            "pass": risk_pass,
            "kelly_fraction": 0.05,
            "gated_reason": None if risk_pass else risk_reason,
            "reason": risk_reason,
        },
        "analyst_views": views,
    }


# ---------------------------------------------------------------------------
# Dim 0: pass-through when advisor's risk gate already vetoed
# ---------------------------------------------------------------------------

def test_silence_when_advisor_risk_gate_failed():
    r = _result(risk_pass=False, risk_reason="cost_gate_veto")
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_GATED_BY_ADVISOR
    assert out.details["gated_reason"] == "cost_gate_veto"
    assert not out.fired


# ---------------------------------------------------------------------------
# Dim 1: confidence threshold
# ---------------------------------------------------------------------------

def test_silence_low_confidence_default():
    r = _result(confidence=0.5)   # below default 0.65
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_LOW_CONFIDENCE
    assert out.details["confidence"] == 0.5
    assert out.details["min_required"] == 0.65


def test_fire_at_confidence_threshold():
    """Edge-case: equal-to threshold should pass (>=)."""
    r = _result(confidence=0.65, magnitude=0.05, atr_relative=0.05)
    out = silence_bias_gate(r)
    # Confidence 0.65 + magnitude 0.05 + signed_edge = 0.05*(1.30-1.0) = 0.015
    # urgency = 0.015 / 0.05 = 0.3, below default 0.5 -> silence by urgency
    # That's expected; we explicitly want the test to verify confidence dim
    # didn't gate (a different reason did).
    assert out.decision == GateDecision.SILENCE_LOW_URGENCY


# ---------------------------------------------------------------------------
# Dim 2: urgency threshold
# ---------------------------------------------------------------------------

def test_silence_low_urgency_when_edge_too_small_vs_vol():
    r = _result(confidence=0.7, magnitude=0.01, atr_relative=0.10)
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_LOW_URGENCY
    assert "urgency" in out.details


def test_silence_when_direction_flat():
    r = _result(direction=0)
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_LOW_URGENCY
    assert "Direction is flat" in out.details["rationale"]


def test_urgency_falls_back_to_default_vol_when_metadata_missing():
    """When no atr_relative and no override, default 1% vol is used."""
    r = _result(confidence=0.8, magnitude=0.05, atr_relative=None)
    out = silence_bias_gate(r)
    # signed_edge = 0.05*(0.6) = 0.03; urgency = 0.03 / 0.01 = 3.0; FIRE
    assert out.decision == GateDecision.FIRE


def test_market_volatility_override():
    r = _result(confidence=0.8, magnitude=0.05, atr_relative=0.001)
    # With atr=0.001, urgency would be massive -> FIRE
    # With override vol=0.1, signed_edge=0.03, urgency=0.3 -> SILENCE
    out = silence_bias_gate(r, market_volatility=0.1)
    assert out.decision == GateDecision.SILENCE_LOW_URGENCY


# ---------------------------------------------------------------------------
# Dim 3: compute budget (number of voices)
# ---------------------------------------------------------------------------

def test_silence_insufficient_voices():
    r = _result(n_voices=1)
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES
    assert out.details["emitted"] == 1
    assert out.details["min_required"] == 2


def test_silence_zero_voices():
    r = _result(n_voices=0)
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES


def test_voices_threshold_configurable():
    r = _result(n_voices=2)
    cfg = GateConfig(min_analysts_emitted=3)
    out = silence_bias_gate(r, config=cfg)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES
    assert out.details["min_required"] == 3


# ---------------------------------------------------------------------------
# Dim 4: salience (recent rejections)
# ---------------------------------------------------------------------------

def test_silence_salience_veto():
    r = _result(confidence=0.9, magnitude=0.10, atr_relative=0.05)
    now = datetime.now(tz=UTC)
    lessons = [
        {"hitl_kind": "reject", "when": (now - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")},
        {"hitl_kind": "reject", "when": (now - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")},
        {"hitl_kind": "reject", "when": (now - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")},
    ]
    out = silence_bias_gate(r, journal_lessons=lessons)
    assert out.decision == GateDecision.SILENCE_SALIENCE_VETO
    assert out.details["recent_rejections"] == 3
    assert out.details["max_allowed"] == 3


def test_salience_below_threshold_doesnt_veto():
    r = _result(confidence=0.9, magnitude=0.10, atr_relative=0.05)
    now = datetime.now(tz=UTC)
    lessons = [
        {"hitl_kind": "reject", "when": (now - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")},
    ]
    out = silence_bias_gate(r, journal_lessons=lessons)
    assert out.decision == GateDecision.FIRE


def test_salience_outside_window_ignored():
    r = _result(confidence=0.9, magnitude=0.10, atr_relative=0.05)
    now = datetime.now(tz=UTC)
    # Three rejections, but all > 7 days old (default window)
    lessons = [
        {"hitl_kind": "reject", "when": (now - timedelta(hours=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")} for _ in range(3)
    ]
    out = silence_bias_gate(r, journal_lessons=lessons)
    assert out.decision == GateDecision.FIRE


def test_salience_only_counts_rejects():
    """Approves should not count as rejections."""
    r = _result(confidence=0.9, magnitude=0.10, atr_relative=0.05)
    now = datetime.now(tz=UTC)
    lessons = [
        {"hitl_kind": "approve", "when": (now - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")} for _ in range(5)
    ]
    out = silence_bias_gate(r, journal_lessons=lessons)
    assert out.decision == GateDecision.FIRE


def test_count_recent_rejections_handles_missing_when_conservatively():
    """A reject with no timestamp should be COUNTED (over-veto safe default)."""
    lessons = [{"hitl_kind": "reject"}, {"hitl_kind": "reject"}]
    n = _count_recent_rejections(lessons, window_hours=168)
    assert n == 2


def test_count_recent_rejections_handles_malformed_when():
    lessons = [{"hitl_kind": "reject", "when": "not-a-date"}]
    n = _count_recent_rejections(lessons, window_hours=168)
    assert n == 1   # conservative


def test_count_recent_rejections_naive_timestamp_treated_as_utc():
    """Naive timestamps should be assumed UTC (don't crash with tz arithmetic)."""
    now = datetime.now(tz=UTC)
    naive = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    lessons = [{"hitl_kind": "reject", "when": naive}]
    n = _count_recent_rejections(lessons, window_hours=168)
    assert n == 1


# ---------------------------------------------------------------------------
# Dim ordering — cheap dims first
# ---------------------------------------------------------------------------

def test_voices_dim_evaluated_first():
    """If voices fail AND confidence fails, we should report voices (cheaper
    + zero-voice means other dims are meaningless)."""
    r = _result(n_voices=1, confidence=0.1)
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES


def test_confidence_dim_evaluated_before_urgency():
    """Confidence is cheaper than urgency math; check it's reported first."""
    r = _result(n_voices=2, confidence=0.4, magnitude=0.001, atr_relative=0.10)
    # both confidence (0.4 < 0.65) and urgency (tiny edge / vol) would fail
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.SILENCE_LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# FIRE happy path
# ---------------------------------------------------------------------------

def test_fire_when_all_dims_pass():
    r = _result(confidence=0.85, magnitude=0.10, atr_relative=0.05, n_voices=2)
    out = silence_bias_gate(r)
    assert out.decision == GateDecision.FIRE
    assert out.fired
    assert "passed_dims" in out.details
    assert set(out.details["passed_dims"]) == {
        "confidence", "urgency", "compute_budget", "salience",
    }


def test_details_dict_includes_diagnostics_on_fire():
    r = _result(confidence=0.85, magnitude=0.10, atr_relative=0.05, n_voices=3)
    out = silence_bias_gate(r)
    assert out.fired
    assert out.details["confidence"] == 0.85
    assert out.details["n_voices"] == 3
    assert out.details["urgency"] > 0


# ---------------------------------------------------------------------------
# Class wrapper
# ---------------------------------------------------------------------------

def test_class_wrapper_evaluates_and_tracks_stats():
    gate = SilenceBiasGate()
    fire_r = _result(confidence=0.85, magnitude=0.10, atr_relative=0.05)
    silence_r = _result(confidence=0.3)

    gate.evaluate(fire_r)
    gate.evaluate(silence_r)
    gate.evaluate(silence_r)

    stats = gate.stats()
    assert stats["n_evaluated"] == 3
    assert stats["n_fires"] == 1
    assert abs(stats["fire_rate"] - 1/3) < 1e-9


def test_class_wrapper_uses_custom_config():
    cfg = GateConfig(min_confidence=0.9, min_analysts_emitted=3)
    gate = SilenceBiasGate(cfg)
    r = _result(confidence=0.85, magnitude=0.10, atr_relative=0.05, n_voices=2)
    out = gate.evaluate(r)
    # would have fired with default config; but min_analysts=3 vetoes
    assert out.decision == GateDecision.SILENCE_INSUFFICIENT_VOICES


# ---------------------------------------------------------------------------
# Defensive — bad input
# ---------------------------------------------------------------------------

def test_handles_empty_advisor_result():
    out = silence_bias_gate({})
    # No risk_gate.pass=true -> SILENCE_GATED_BY_ADVISOR (D0)
    assert out.decision == GateDecision.SILENCE_GATED_BY_ADVISOR


def test_handles_none_advisor_result():
    out = silence_bias_gate(None)
    assert out.decision == GateDecision.SILENCE_GATED_BY_ADVISOR


def test_handles_zero_volatility_as_default():
    r = _result(confidence=0.85, magnitude=0.05, atr_relative=0.0)
    out = silence_bias_gate(r)
    # zero atr_relative -> falls through to next view (none) -> default 0.01
    # So urgency = 0.05*(0.7) / 0.01 = 3.5 -> FIRE
    assert out.decision == GateDecision.FIRE
