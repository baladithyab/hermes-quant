"""cut/01f0 (ADR-0097): the DEFAULT-OFF slippage haircut wired into the DECISION
gate (Rule-5 cost gate + Rule-6 sizer) of ``hermes_quant.pdr_core.gate``.

The haircut was previously wired ONLY into the clean_window EVIDENCE series (b61c)
— the LIVE admission gate sized off the RAW expected edge + ordinary costs, so a
thin edge that only clears the cost gate on OPTIMISTIC paper fills passed the gate.
This file proves the gate-side wiring:

  1. THIN-EDGE SILENCED (the money-safety point): an edge that CLEARS the raw cost
     gate (and would EMIT an action) is SILENCED once the slippage penalty is
     subtracted toward silence.
  2. BYTE-IDENTICAL OFF: with the flag off (the default) the gate emits the EXACT
     same GateDecision as a config that never heard of the haircut — the raw-edge
     path is untouched.
  3. NON-FINITE PENALTY does NOT improve edge: a NaN/inf penalty fails toward the
     conservative floor (edge -> 0 = silence), never a free pass.
  4. HAIRCUT-TOWARD-SILENCE invariant on the pure leaf: the haircut can only SHRINK
     |edge| (never amplify), sign preserved.

RED-first: before the ``slippage_gate_enabled`` / ``slippage_penalty_frac`` fields
and the ``_slippage_haircut_edge`` leaf exist, these tests fail (TypeError on the
unknown RiskConfig kwargs / AttributeError on the missing leaf).
"""

from __future__ import annotations

import math

import pandas as pd

from hermes_quant.pdr_core.gate import (
    CoreSignal,
    DefaultRiskGate,
    RiskConfig,
    _slippage_haircut_edge,
)
from hermes_quant.pdr_core.gate_types import (
    CoreMarketState,
    CorePortfolio,
    GateDecision,
)

UTC_NOW = pd.Timestamp("2026-06-17T15:00:00+00:00")


class _Halts:
    def is_halted(self, account_id, asset_class, asset=None) -> bool:
        return False


def _signal(*, direction=1, confidence=0.55, magnitude=0.01, asset="AAPL"):
    # edge = expected_signed_edge(1, 0.55, 0.01) ~= 0.00095 — CLEARS the raw cost
    # gate threshold (~0.0006) but is THIN: a 25-bps live-execution penalty wipes it.
    return CoreSignal(
        asset=asset,
        asset_class="equity",
        asof=UTC_NOW,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
    )


def _market(*, volatility=0.02, commission=0.0001, spread=0.0002, slippage=0.0001):
    return CoreMarketState(
        asset="AAPL",
        asof=UTC_NOW,
        volatility=volatility,
        commission=commission,
        spread=spread,
        slippage_estimate=slippage,
        tz="UTC",
    )


def _portfolio():
    return CorePortfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        asof=UTC_NOW,
        positions={},
        equity_total=100_000.0,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
    )


# ---------------------------------------------------------------------------
# 1. THIN-EDGE SILENCED with the haircut ON.
# ---------------------------------------------------------------------------


def test_thin_edge_clears_raw_cost_gate_then_silenced_by_haircut() -> None:
    """A thin edge that CLEARS the raw cost gate (would emit) is SILENCED once the
    slippage penalty is haircut off BEFORE the cost gate (haircut-toward-silence)."""
    sig, mkt, pf, halts = _signal(), _market(), _portfolio(), _Halts()

    # RAW path (flag off): the thin edge clears the gate and emits a sized action.
    raw_gate = DefaultRiskGate(RiskConfig())
    raw = raw_gate.gate(sig, mkt, pf, halts)
    assert isinstance(raw, GateDecision), "fixture must clear the raw cost gate"
    assert raw.target_position_pct > 0.0
    assert raw_gate.stats()["n_actions"] == 1

    # HAIRCUT path (flag on, equity prior 25 bps): edge 0.00095 - 0.0025 < 0 -> the
    # edge is driven to 0 -> the edge-sign guard silences (cost_gate_edge_sign).
    hc_gate = DefaultRiskGate(
        RiskConfig(slippage_gate_enabled=True, slippage_penalty_frac=0.0025)
    )
    hc = hc_gate.gate(sig, mkt, pf, halts)
    assert hc is None, "the haircut must SILENCE the thin edge"
    assert hc_gate.stats()["n_actions"] == 0
    assert hc_gate.stats()["n_silenced_cost_gate"] == 1


def test_small_penalty_drops_thin_edge_below_threshold() -> None:
    """A penalty smaller than the edge but enough to push |edge| below the cost-gate
    threshold silences via cost_gate_below_threshold (still toward silence)."""
    sig, mkt, pf, halts = _signal(), _market(), _portfolio(), _Halts()
    # edge ~0.00095; penalty 0.0005 -> 0.00045 < threshold 0.0006 -> silenced.
    g = DefaultRiskGate(RiskConfig(slippage_gate_enabled=True, slippage_penalty_frac=0.0005))
    d = g.gate(sig, mkt, pf, halts)
    assert d is None
    assert g.stats()["n_silenced_cost_gate"] == 1


def test_fat_edge_survives_haircut_and_sizes_down() -> None:
    """A FAT edge survives a modest haircut but the SIZER consumes the shrunk edge,
    so the haircut can only reduce (never increase) the emitted size."""
    # confidence 0.95, magnitude 0.03 -> edge ~0.0257; vol 0.05 -> variance 0.0025.
    sig = _signal(confidence=0.95, magnitude=0.03)
    mkt = _market(volatility=0.05)
    pf, halts = _portfolio(), _Halts()

    raw = DefaultRiskGate(RiskConfig()).gate(sig, mkt, pf, halts)
    hc = DefaultRiskGate(
        RiskConfig(slippage_gate_enabled=True, slippage_penalty_frac=0.0025)
    ).gate(sig, mkt, pf, halts)
    assert isinstance(raw, GateDecision)
    assert isinstance(hc, GateDecision)
    # The haircut edge is smaller, so the (pre-clip) Kelly size is <= raw. With both
    # at the cap here, the haircut never sizes ABOVE the raw decision.
    assert abs(hc.target_position_pct) <= abs(raw.target_position_pct)


# ---------------------------------------------------------------------------
# 2. BYTE-IDENTICAL when the flag is OFF (the default).
# ---------------------------------------------------------------------------


def test_default_off_is_byte_identical_to_raw_edge_path() -> None:
    """Default config (flag off) emits the EXACT same GateDecision as a config that
    has the slippage fields at their defaults — the raw-edge path is untouched."""
    sig, mkt, pf = _signal(), _market(), _portfolio()

    a = DefaultRiskGate(RiskConfig()).gate(sig, mkt, pf, _Halts())
    # Even with a (large) penalty present, an OFF flag must IGNORE it entirely.
    b = DefaultRiskGate(
        RiskConfig(slippage_gate_enabled=False, slippage_penalty_frac=0.05)
    ).gate(sig, mkt, pf, _Halts())

    assert isinstance(a, GateDecision)
    assert isinstance(b, GateDecision)
    assert a == b, "flag-OFF must be byte-identical regardless of the penalty value"


def test_default_riskconfig_slippage_fields_are_off() -> None:
    cfg = RiskConfig()
    assert cfg.slippage_gate_enabled is False
    assert cfg.slippage_penalty_frac == 0.0


# ---------------------------------------------------------------------------
# 3. NON-FINITE PENALTY never improves edge (fails toward silence).
# ---------------------------------------------------------------------------


def test_nonfinite_penalty_silences_does_not_improve_edge() -> None:
    sig, mkt, pf, halts = _signal(), _market(), _portfolio(), _Halts()
    for bad in (float("nan"), float("inf"), float("-inf")):
        g = DefaultRiskGate(RiskConfig(slippage_gate_enabled=True, slippage_penalty_frac=bad))
        d = g.gate(sig, mkt, pf, halts)
        assert d is None, f"non-finite penalty {bad!r} must silence, never emit"
        assert g.stats()["n_actions"] == 0


# ---------------------------------------------------------------------------
# 4. The pure leaf invariant: haircut only SHRINKS |edge|, sign preserved.
# ---------------------------------------------------------------------------


def test_leaf_only_shrinks_magnitude_and_preserves_sign() -> None:
    for edge in (0.01, -0.01, 0.002, -0.002, 0.0):
        for pen in (0.0, 0.0005, 0.0025, 0.5):
            out = _slippage_haircut_edge(edge, pen)
            assert abs(out) <= abs(edge) + 1e-12, "haircut must NEVER amplify |edge|"
            if out != 0.0:
                assert math.copysign(1.0, out) == math.copysign(1.0, edge)


def test_leaf_zero_penalty_is_identity() -> None:
    for edge in (0.01, -0.01, 0.0):
        assert _slippage_haircut_edge(edge, 0.0) == edge


def test_leaf_nonfinite_inputs_drive_to_zero() -> None:
    assert _slippage_haircut_edge(0.01, float("nan")) == 0.0
    assert _slippage_haircut_edge(0.01, float("inf")) == 0.0
    assert _slippage_haircut_edge(float("nan"), 0.001) == 0.0
    # penalty given as a positive cost; negative penalty is treated by magnitude
    assert _slippage_haircut_edge(0.01, -0.0005) == _slippage_haircut_edge(0.01, 0.0005)


def test_leaf_full_consumption_yields_zero() -> None:
    # penalty >= |edge| -> exactly 0.0 (silence), never a sign flip.
    assert _slippage_haircut_edge(0.001, 0.001) == 0.0
    assert _slippage_haircut_edge(0.001, 0.005) == 0.0
    assert _slippage_haircut_edge(-0.001, 0.005) == 0.0
