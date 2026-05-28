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


def test_kronos_unknown_dampened(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.UNKNOWN, 0, state_vars)
    assert apply_regime_multiplier(0.7, pkt, "kronos") == pytest.approx(0.7 * 0.85)


def test_kronos_known_unchanged(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.BULL, 1, state_vars)
    assert apply_regime_multiplier(0.7, pkt, "kronos") == 0.7


def test_unknown_analyst_kind_returns_unchanged(state_vars, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    pkt = _packet(RegimeState.UNKNOWN, 1, state_vars)
    # analyst_kind="other" — no rule applies
    assert apply_regime_multiplier(0.5, pkt, "other_analyst") == 0.5
