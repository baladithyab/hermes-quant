"""PDR recipe registry.

A recipe is the Hermes-visible unit of a trading system: it declares the
Perceive, Decide, React, and evaluation components that form one replayable
runtime. Components are still discovered/implemented as Python classes; recipes
compose them into named strategies that Hermes can inspect, backtest, schedule,
and eventually run in HITL/autonomous modes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Literal


PDRMode = Literal["advise", "hitl", "autonomous", "backtest"]


@dataclass(frozen=True)
class PDRRecipe:
    """Named composition of a PDR trading system."""

    id: str
    description: str
    symbols: tuple[str, ...]
    asset_class: str
    timeframe: str

    # Perceive
    data_provider: str = "yfinance"
    data_provider_config: dict[str, Any] = field(default_factory=dict)
    analysts: tuple[str, ...] = ("classical_ta", "microstructure_lite")
    analyst_config: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Decide
    aggregator: str = "bma"
    aggregator_config: dict[str, Any] = field(default_factory=dict)
    risk_gate: str = "default"
    risk_gate_config: dict[str, Any] = field(default_factory=dict)

    # React / operator policy
    reactor: str = "paper"
    supported_modes: tuple[PDRMode, ...] = ("advise", "hitl", "backtest")
    live_allowed: bool = False

    # Evaluation policy
    min_decisions_for_charter_gate: int = 30
    min_settlements_for_charter_gate: int = 30
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def validate(self) -> None:
        if not self.id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.id):
            raise ValueError(f"invalid recipe id: {self.id!r}")
        if not self.symbols:
            raise ValueError(f"recipe {self.id}: at least one symbol is required")
        if not self.analysts:
            raise ValueError(f"recipe {self.id}: at least one analyst is required")
        if self.live_allowed and "autonomous" in self.supported_modes:
            raise ValueError(
                f"recipe {self.id}: live autonomous recipes are forbidden until live-reactor ADR gates pass"
            )
        if self.min_decisions_for_charter_gate < 0 or self.min_settlements_for_charter_gate < 0:
            raise ValueError(f"recipe {self.id}: minimum gate counts must be non-negative")


DEFAULT_RECIPE = PDRRecipe(
    id="btc-usdt-mvp",
    description=(
        "Charter MVP committee for BTC/USDT: ClassicalTA + MicrostructureLite "
        "+ Kronos (abstains if optional dependency unavailable), BMA aggregator, "
        "default risk gate, paper reactor."
    ),
    symbols=("BTC/USDT",),
    asset_class="crypto",
    timeframe="1h",
    data_provider="ccxt:kraken",
    analysts=("classical_ta", "microstructure_lite", "kronos"),
    aggregator="bma",
    risk_gate="default",
    reactor="paper",
    supported_modes=("advise", "hitl", "autonomous", "backtest"),
    live_allowed=False,
    min_decisions_for_charter_gate=30,
    min_settlements_for_charter_gate=30,
)

_BUILTIN_RECIPES: dict[str, PDRRecipe] = {
    DEFAULT_RECIPE.id: DEFAULT_RECIPE,
}


def list_recipes() -> list[PDRRecipe]:
    """Return built-in recipes in deterministic order."""
    return [_BUILTIN_RECIPES[k] for k in sorted(_BUILTIN_RECIPES)]


def get_recipe(recipe_id: str | None = None) -> PDRRecipe:
    rid = recipe_id or DEFAULT_RECIPE.id
    recipe = _BUILTIN_RECIPES.get(rid)
    if recipe is None:
        raise KeyError(f"unknown PDR recipe {rid!r}; available: {sorted(_BUILTIN_RECIPES)}")
    recipe.validate()
    return recipe


def instantiate_recipe_analysts(recipe: PDRRecipe):
    """Instantiate analyst objects for a recipe.

    Built-ins are loaded directly for reliability; third-party analysts can be
    added through `daemon.discovery` entry-points later without changing the
    recipe schema.
    """
    out = []
    for name in recipe.analysts:
        kwargs = recipe.analyst_config.get(name, {})
        if name == "classical_ta":
            from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst
            out.append(ClassicalTAAnalyst(**kwargs))
        elif name == "microstructure_lite":
            from hermes_quant.analysts.microstructure import MicrostructureLite
            out.append(MicrostructureLite(**kwargs))
        elif name == "kronos":
            from hermes_quant.analysts.kronos import KronosAnalyst
            out.append(KronosAnalyst(**kwargs))
        else:
            from hermes_quant.daemon.discovery import instantiate_analysts
            found = instantiate_analysts([name], overrides={name: kwargs})
            if not found:
                raise ValueError(f"recipe {recipe.id}: analyst {name!r} is not available")
            out.extend(found)
    return out


def instantiate_recipe_aggregator(recipe: PDRRecipe):
    if recipe.aggregator == "bma":
        from hermes_quant.aggregators.bma import BMAAggregator
        return BMAAggregator(**recipe.aggregator_config)
    from hermes_quant.daemon.discovery import instantiate_aggregator
    agg = instantiate_aggregator(recipe.aggregator, **recipe.aggregator_config)
    if agg is None:
        raise ValueError(f"recipe {recipe.id}: aggregator {recipe.aggregator!r} is not available")
    return agg


def instantiate_recipe_risk_gate(recipe: PDRRecipe):
    if recipe.risk_gate == "default":
        from hermes_quant.risk.gate import DefaultRiskGate
        return DefaultRiskGate(**recipe.risk_gate_config)
    raise ValueError(f"recipe {recipe.id}: risk gate {recipe.risk_gate!r} is not available")
