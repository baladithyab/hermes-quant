"""tests/regime/test_regime_aware_confidence.py — multiplier semantics tests."""

from __future__ import annotations

import os
from unittest.mock import patch

import pandas as pd
import pytest

from hermes_quant.regime.detector import RegimeState
from hermes_quant.regime.extras_builder import RegimePacket
from hermes_quant.regime.regime_aware_confidence import (
    ENV_FLAG,
    apply_regime_multiplier,
)
from hermes_quant.regime.state_variables import StateVariables


@pytest.fixture
def state_vars():
    return StateVariables(
        realized_vol_60d=0.20,
        realized_vol_percentile=0.5,
        yield_curve_slope=None,
        trend_strength=None,
        as_of=pd.Timestamp("2026-05-27", tz="UTC"),
    )


def _packet(label, tier, sv):
    return RegimePacket(
        label=label,
        volatility_tier=tier,
        posterior=None,
        state_vars=sv,
        asof=sv.as_of,
        classifier_kind="rule_based",
    )


def test_flag_off_returns_unchanged(state_vars, monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    pkt = _packet(RegimeState.BULL, 1, state_vars)
    assert apply_regime_multiplier(0.7, pkt, "classical_ta") == 0.7


def test_flag_on_no_regime_returns_unchanged(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    assert apply_regime_multiplier(0.7, None, "classical_ta") == 0.7


def test_classical_ta_high_vol_dampened(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.VOLATILE, 1, state_vars)
    assert apply_regime_multiplier(0.8, pkt, "classical_ta") == pytest.approx(0.8 * 0.7)


def test_classical_ta_normal_vol_unchanged(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.BULL, 0, state_vars)
    assert apply_regime_multiplier(0.8, pkt, "classical_ta") == 0.8


def test_microstructure_low_vol_boosted(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.BULL, -1, state_vars)
    assert apply_regime_multiplier(0.5, pkt, "microstructure") == pytest.approx(0.5 * 1.15)


def test_microstructure_high_vol_unchanged(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.VOLATILE, 1, state_vars)
    # Only tier == -1 triggers the multiplier
    assert apply_regime_multiplier(0.5, pkt, "microstructure") == 0.5


def test_semantic_high_vol_boosted(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.VOLATILE, 1, state_vars)
    assert apply_regime_multiplier(0.6, pkt, "semantic") == pytest.approx(0.6 * 1.20)


def test_kronos_unknown_dampened_with_floor(state_vars, monkeypatch):
    """Per Claude review H1: Kronos clip is [0.30, 0.85], not [0, 1].

    A pre-multiplier confidence of 0.7 × 0.85 = 0.595 → falls within band → unchanged.
    A pre-multiplier confidence of 0.30 (the calibrator floor) × 0.85 = 0.255 →
    BELOW the ADR-0018 §D8 floor → MUST be clipped UP to 0.30.
    """
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.UNKNOWN, 0, state_vars)
    # In-band case
    assert apply_regime_multiplier(0.7, pkt, "kronos") == pytest.approx(0.7 * 0.85)
    # Floor case — multiplier would push BELOW 0.30, but ADR-0018 floor clips up
    assert apply_regime_multiplier(0.30, pkt, "kronos") == pytest.approx(0.30)
    # Ceiling case — confidence at the cap × 0.85 stays in band
    assert apply_regime_multiplier(0.85, pkt, "kronos") == pytest.approx(0.85 * 0.85)


def test_kronos_known_unchanged(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.BULL, 1, state_vars)
    assert apply_regime_multiplier(0.7, pkt, "kronos") == 0.7


def test_microstructure_clipped_to_unit_interval(state_vars, monkeypatch):
    """Per Claude review H2: × 1.15 must clip to [0, 1] to honor AnalystView invariant."""
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.BULL, -1, state_vars)
    # 0.95 × 1.15 = 1.0925 → clipped to 1.0
    assert apply_regime_multiplier(0.95, pkt, "microstructure") == pytest.approx(1.0)


def test_semantic_clipped_to_unit_interval(state_vars, monkeypatch):
    """Per Claude review H2: × 1.20 must clip to [0, 1]."""
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.VOLATILE, 1, state_vars)
    # 0.9 × 1.20 = 1.08 → clipped to 1.0
    assert apply_regime_multiplier(0.9, pkt, "semantic") == pytest.approx(1.0)


def test_unknown_analyst_kind_returns_unchanged(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.UNKNOWN, 1, state_vars)
    # analyst_kind="other" — no rule applies
    assert apply_regime_multiplier(0.5, pkt, "other_analyst") == 0.5
