"""tests/regime/test_per_regime_weights.py — Wave 7 per-regime weight tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.regime.detector import RegimeState
from hermes_quant.regime.per_regime_weights import (
    DEFAULT_REGIME_WEIGHTS,
    RegimeWeightTable,
    _MIN_MULTIPLIER,
    apply_regime_weights,
    load_regime_weights,
    save_regime_weights,
)


# ---------------------------------------------------------------------------
# DEFAULT_REGIME_WEIGHTS
# ---------------------------------------------------------------------------


def test_default_regime_weights_has_all_states():
    for state in RegimeState:
        assert state in DEFAULT_REGIME_WEIGHTS, f"Missing regime {state} in DEFAULT_REGIME_WEIGHTS"


def test_unknown_multipliers_all_one():
    """UNKNOWN row must be all-1.0 so no adjustment is applied."""
    unknown_row = DEFAULT_REGIME_WEIGHTS[RegimeState.UNKNOWN]
    for analyst, mult in unknown_row.items():
        assert mult == 1.0, f"UNKNOWN multiplier for {analyst!r} should be 1.0, got {mult}"


def test_default_bull_sentiment_boosted():
    assert DEFAULT_REGIME_WEIGHTS[RegimeState.BULL]["sentiment"] > 1.0


def test_default_bear_sentiment_suppressed():
    assert DEFAULT_REGIME_WEIGHTS[RegimeState.BEAR]["sentiment"] < 1.0


def test_default_volatile_sentiment_heavily_suppressed():
    assert DEFAULT_REGIME_WEIGHTS[RegimeState.VOLATILE]["sentiment"] < 0.5


def test_default_volatile_ta_boosted():
    assert DEFAULT_REGIME_WEIGHTS[RegimeState.VOLATILE]["classical_ta"] > 1.0


# ---------------------------------------------------------------------------
# apply_regime_weights
# ---------------------------------------------------------------------------


def test_apply_unknown_returns_unchanged():
    """UNKNOWN → all multipliers 1.0 → weights unchanged."""
    base = {"semantic": 0.5, "sentiment": 0.5, "kronos": 0.5}
    result = apply_regime_weights(base, RegimeState.UNKNOWN)
    for analyst, w in base.items():
        assert abs(result[analyst] - w) < 1e-9, (
            f"UNKNOWN should not change weight for {analyst}"
        )


def test_apply_bear_suppresses_sentiment():
    base = {"sentiment": 0.5}
    result = apply_regime_weights(base, RegimeState.BEAR)
    expected = 0.5 * DEFAULT_REGIME_WEIGHTS[RegimeState.BEAR]["sentiment"]
    assert abs(result["sentiment"] - expected) < 1e-9


def test_apply_volatile_suppresses_sentiment_more():
    base = {"sentiment": 0.5}
    bear_result = apply_regime_weights(base, RegimeState.BEAR)
    volatile_result = apply_regime_weights(base, RegimeState.VOLATILE)
    assert volatile_result["sentiment"] < bear_result["sentiment"], (
        "VOLATILE should suppress sentiment more than BEAR"
    )


def test_apply_unknown_analyst_defaults_to_1_0():
    """Analysts not in the regime table get multiplier 1.0."""
    base = {"new_analyst_xyz": 0.7}
    result = apply_regime_weights(base, RegimeState.BULL)
    assert abs(result["new_analyst_xyz"] - 0.7) < 1e-9


def test_apply_never_zeros_weight():
    """Even with extreme suppression, weight must be >= _MIN_MULTIPLIER."""
    base = {"sentiment": 1e-10}
    result = apply_regime_weights(base, RegimeState.VOLATILE)
    assert result["sentiment"] >= _MIN_MULTIPLIER


def test_apply_custom_table():
    custom_table: RegimeWeightTable = {
        RegimeState.BULL: {"semantic": 2.0},
        RegimeState.BEAR: {"semantic": 0.5},
        RegimeState.VOLATILE: {"semantic": 0.1},
        RegimeState.UNKNOWN: {"semantic": 1.0},
    }
    base = {"semantic": 1.0}
    result = apply_regime_weights(base, RegimeState.BULL, table=custom_table)
    assert abs(result["semantic"] - 2.0) < 1e-9


def test_apply_does_not_mutate_base_weights():
    base = {"sentiment": 0.5, "kronos": 0.6}
    _ = apply_regime_weights(base, RegimeState.VOLATILE)
    assert base["sentiment"] == 0.5
    assert base["kronos"] == 0.6


# ---------------------------------------------------------------------------
# Persistence: save / load
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "weights.json"
    save_regime_weights(DEFAULT_REGIME_WEIGHTS, path)
    loaded = load_regime_weights(path)
    for state in RegimeState:
        assert state in loaded
        for analyst, w in DEFAULT_REGIME_WEIGHTS[state].items():
            assert abs(loaded[state][analyst] - w) < 1e-9


def test_load_missing_file_returns_defaults(tmp_path):
    path = tmp_path / "nonexistent.json"
    loaded = load_regime_weights(path)
    for state in RegimeState:
        assert state in loaded


def test_load_corrupt_json_returns_defaults(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("NOT JSON {{{{")
    loaded = load_regime_weights(path)
    # Should fall back to defaults without raising
    for state in RegimeState:
        assert state in loaded


def test_load_partial_table_fills_missing_regimes(tmp_path):
    """If the JSON file only has 'bull', missing rows must be filled in."""
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"bull": {"semantic": 1.5}}))
    loaded = load_regime_weights(path)
    # All four regimes should be present
    for state in RegimeState:
        assert state in loaded


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nested" / "weights.json"
    save_regime_weights(DEFAULT_REGIME_WEIGHTS, path)
    assert path.exists()
