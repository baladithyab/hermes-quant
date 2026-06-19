"""ar124 — recipe risk_gate_config must be finite-guarded before it becomes a money rail.

instantiate_recipe_risk_gate does ``RiskConfig(**recipe.risk_gate_config)`` from
operator-editable recipe YAML (~/.hermes/quant/recipes/*.yaml) with no prior validation,
and RiskConfig was a frozen dataclass with no guard. A non-finite threshold from YAML
(``max_drawdown_pct: 1e400`` overflows to inf; ``.nan`` parses to NaN) silently DISABLES
the rail it bounds — the Rule-1 drawdown breaker (``drawdown_pct > max_drawdown_pct``) and
Rule-2 daily-loss breaker compare ``> inf``/``> nan`` as always-False, so a catastrophic
drawdown never trips/halts (fail-OPEN); ``max_position_pct = inf`` defeats the quarter-Kelly
cap. This is the operator-config seam the ar08-12 finite-guard family had not covered.

The fix adds a fail-CLOSED ``RiskConfig.__post_init__`` finite/range guard (raises on a
non-finite or out-of-range money threshold), protecting EVERY construction path. These
tests go through the real recipe YAML loader.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from hermes_quant.recipes import instantiate_recipe_risk_gate, load_user_recipes
from hermes_quant.risk.gate import RiskConfig


def _write_recipe(root: Path, risk_gate_config_yaml: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "evil.yaml").write_text(
        "id: evil_recipe\n"
        "description: a recipe with a degenerate risk_gate_config (ar124)\n"
        "symbols: [AAPL]\n"
        "asset_class: equity\n"
        "timeframe: 1d\n"
        "risk_gate: default\n"
        "risk_gate_config:\n"
        f"{risk_gate_config_yaml}\n",
        encoding="utf-8",
    )


def _load_and_instantiate(root: Path):
    """Load the user recipe and build its risk gate — the path where
    RiskConfig(**risk_gate_config) is constructed (and the ar124 guard fires)."""
    recipes = load_user_recipes(root=root)
    return instantiate_recipe_risk_gate(recipes["evil_recipe"])


def test_inf_max_drawdown_from_yaml_is_rejected(tmp_path):
    """A recipe YAML with max_drawdown_pct: 1e400 (→ inf) must be REJECTED when the gate
    is instantiated, not silently produce a gate whose drawdown breaker is dead."""
    _write_recipe(tmp_path, "  max_drawdown_pct: 1e400")
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        _load_and_instantiate(tmp_path)


def test_nan_daily_loss_from_yaml_is_rejected(tmp_path):
    _write_recipe(tmp_path, "  max_daily_loss_pct: .nan")
    with pytest.raises(ValueError, match="max_daily_loss_pct"):
        _load_and_instantiate(tmp_path)


def test_inf_max_position_is_rejected(tmp_path):
    _write_recipe(tmp_path, "  max_position_pct: 1e400")
    with pytest.raises(ValueError, match="max_position_pct"):
        _load_and_instantiate(tmp_path)


def test_zero_drawdown_breaker_is_rejected(tmp_path):
    """max_drawdown_pct: 0.0 is not a meaningful breaker threshold; a (0,1] bound
    rejects it as a config error (the breaker must bound a real positive fraction)."""
    _write_recipe(tmp_path, "  max_drawdown_pct: 0.0")
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        _load_and_instantiate(tmp_path)


def test_legit_tighter_recipe_still_loads(tmp_path):
    """A legitimate tighter recipe (the ar91 use case) must still load + instantiate."""
    _write_recipe(tmp_path, "  max_position_pct: 0.10\n  max_drawdown_pct: 0.08")
    recipes = load_user_recipes(root=tmp_path)
    assert "evil_recipe" in recipes
    gate = instantiate_recipe_risk_gate(recipes["evil_recipe"])
    assert gate.config.max_position_pct == pytest.approx(0.10)
    assert gate.config.max_drawdown_pct == pytest.approx(0.08)


def test_breaker_would_be_dead_without_the_guard():
    """RED-pin the actual fail-open: with a non-finite threshold the Rule-1 comparison
    ``drawdown_pct > max_drawdown_pct`` is always False (a 90% real drawdown does NOT
    exceed inf/NaN). The guard now prevents such a RiskConfig from ever being built."""
    # The dangerous comparison, demonstrated directly on the boundary values.
    catastrophic_drawdown = 0.90
    assert (catastrophic_drawdown > float("inf")) is False  # inf threshold → breaker dead
    assert (catastrophic_drawdown > float("nan")) is False  # nan threshold → breaker dead
    # And the guard refuses to construct a RiskConfig carrying those thresholds.
    with pytest.raises(ValueError):
        RiskConfig(max_drawdown_pct=float("inf"))
    with pytest.raises(ValueError):
        RiskConfig(max_daily_loss_pct=float("nan"))


def test_presets_and_default_still_valid():
    """Non-vacuity: the guard does not reject any legitimate built-in profile."""
    for ctor in (RiskConfig, RiskConfig.conservative, RiskConfig.moderate, RiskConfig.aggressive):
        cfg = ctor()
        assert math.isfinite(cfg.max_drawdown_pct) and 0 < cfg.max_drawdown_pct <= 1
        assert math.isfinite(cfg.max_position_pct) and 0 < cfg.max_position_pct <= 1
