"""ar91 — instantiate_recipe_risk_gate must coerce risk_gate_config into a RiskConfig.

`instantiate_recipe_risk_gate` did `DefaultRiskGate(**recipe.risk_gate_config)`, but
`DefaultRiskGate.__init__(config: RiskConfig | None = None)` takes a RiskConfig dataclass,
NOT individual RiskConfig field kwargs. So a schema-valid recipe that sets
`risk_gate_config={"max_position_pct": 0.10, "cost_multiple": 3.0}` (the obvious,
field-level form — symmetric with `aggregator_config`, whose ctor DOES take kwargs)
raises `TypeError: __init__() got an unexpected keyword argument 'max_position_pct'` on
the live advise() path. The nested `{"config": {...}}` workaround is worse: it sets
`gate.config` to a raw dict (gate.py `self.config = config or RiskConfig()`), which then
`AttributeError`s on `gate.config.max_position_pct` at the first gate evaluation.

Net effect: recipe-configured risk caps are UNENFORCEABLE — any recipe that tries to
tighten a cap crashes the money path. Builtin recipes use empty `risk_gate_config={}`
(which spreads to no kwargs, masking the bug), and no test covered a non-empty config.

FIX (ar91): coerce the field-level dict into RiskConfig and pass it as `config=`:
`DefaultRiskGate(config=RiskConfig(**recipe.risk_gate_config))`. Empty stays
byte-identical (RiskConfig() == the default). This makes recipe risk caps enforceable.
"""

from __future__ import annotations

import dataclasses

import pytest

from hermes_quant.recipes import get_recipe, instantiate_recipe_risk_gate, recipe_from_mapping
from hermes_quant.risk.gate import RiskConfig


def _mvp_mapping_with_risk_config(risk_gate_config: dict) -> dict:
    """Start from the builtin MVP recipe and override only risk_gate_config."""
    base = get_recipe("btc-usdt-mvp")
    d = dataclasses.asdict(base) if dataclasses.is_dataclass(base) else dict(base.__dict__)
    d["id"] = "mvp-with-risk-config"
    d["risk_gate_config"] = risk_gate_config
    return d


def test_ar91_field_level_risk_config_instantiates_and_enforces():
    """A field-level risk_gate_config must build a working gate whose config carries
    the overridden caps (not crash with TypeError)."""
    mapping = _mvp_mapping_with_risk_config(
        {"max_position_pct": 0.10, "cost_multiple": 3.0}
    )
    recipe = recipe_from_mapping(mapping)
    gate = instantiate_recipe_risk_gate(recipe)  # MUST NOT raise TypeError
    # The gate's config must be a real RiskConfig carrying the overrides.
    assert isinstance(gate.config, RiskConfig)
    assert gate.config.max_position_pct == pytest.approx(0.10)
    assert gate.config.cost_multiple == pytest.approx(3.0)
    # And the gate is usable (has its evaluation method).
    assert hasattr(gate, "gate")


def test_ar91_empty_risk_config_byte_identical_default():
    """Non-vacuity / byte-identity: an empty risk_gate_config (the builtin default)
    yields the moderate RiskConfig default, exactly as before the fix."""
    recipe = get_recipe("btc-usdt-mvp")
    assert recipe.risk_gate_config == {}
    gate = instantiate_recipe_risk_gate(recipe)
    assert isinstance(gate.config, RiskConfig)
    # Default moderate config (unchanged behavior).
    assert gate.config.max_position_pct == RiskConfig().max_position_pct
    assert gate.config.cost_multiple == RiskConfig().cost_multiple


def test_ar91_gate_config_is_never_a_raw_dict():
    """Regression guard for the nested-{config:{...}} AttributeError mode: the gate's
    config must always be a RiskConfig, never a raw dict (which would AttributeError on
    gate.config.max_position_pct at the first evaluation)."""
    mapping = _mvp_mapping_with_risk_config({"max_position_pct": 0.05})
    recipe = recipe_from_mapping(mapping)
    gate = instantiate_recipe_risk_gate(recipe)
    assert not isinstance(gate.config, dict)
    assert isinstance(gate.config, RiskConfig)
    # The cap is reachable as an attribute (would AttributeError pre-fix in mode 2).
    assert gate.config.max_position_pct == pytest.approx(0.05)
