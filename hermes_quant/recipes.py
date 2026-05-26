"""PDR recipe registry.

A recipe is the Hermes-visible unit of a trading system: it declares the
Perceive, Decide, React, and evaluation components that form one replayable
runtime. Components are still discovered/implemented as Python classes; recipes
compose them into named strategies that Hermes can inspect, backtest, schedule,
and eventually run in HITL/autonomous modes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

PDRMode = Literal["advise", "hitl", "autonomous", "backtest"]
QUANT_HOME = Path.home() / ".hermes" / "quant"
USER_RECIPE_DIR = QUANT_HOME / "recipes"


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

DELIBERATIVE_RECIPE = PDRRecipe(
    id="btc-usdt-deliberative",
    description=(
        "Hermes-native deliberative BTC/USDT recipe: quantitative analysts + "
        "Hermes semantic packets feed a TradingAgents-style deterministic "
        "committee aggregator."
    ),
    symbols=("BTC/USDT",),
    asset_class="crypto",
    timeframe="1h",
    data_provider="ccxt:kraken",
    analysts=("classical_ta", "microstructure_lite", "kronos", "hermes_semantic"),
    analyst_config={
        "hermes_semantic": {
            "max_age_minutes": 24 * 60,
            "require_horizon_match": False,
        }
    },
    aggregator="deliberative_committee",
    risk_gate="default",
    reactor="paper",
    supported_modes=("advise", "hitl", "backtest"),
    live_allowed=False,
    min_decisions_for_charter_gate=30,
    min_settlements_for_charter_gate=30,
    notes=(
        "Model-backed debate turns are intentionally external artifacts for now; "
        "the aggregator's hot path remains deterministic and replayable."
    ),
)

_BUILTIN_RECIPES: dict[str, PDRRecipe] = {
    DEFAULT_RECIPE.id: DEFAULT_RECIPE,
    DELIBERATIVE_RECIPE.id: DELIBERATIVE_RECIPE,
}


def recipe_from_mapping(data: dict[str, Any]) -> PDRRecipe:
    """Create a PDRRecipe from YAML/JSON mapping data.

    Lists are normalized to tuples for hash stability. Unknown keys are rejected
    by the dataclass constructor so typos fail early.
    """
    normalized = dict(data)
    for key in ("symbols", "analysts", "supported_modes"):
        if key in normalized and isinstance(normalized[key], list):
            normalized[key] = tuple(normalized[key])
    recipe = PDRRecipe(**normalized)
    recipe.validate()
    return recipe


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a project dep
        raise RuntimeError("pyyaml is required to load recipe YAML") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"recipe file {path} must contain a mapping")
    return data


def load_user_recipes(*, root: Path | None = None) -> dict[str, PDRRecipe]:
    """Load user-editable recipes from ~/.hermes/quant/recipes/*.yaml."""
    base = root or USER_RECIPE_DIR
    if not base.exists():
        return {}
    out: dict[str, PDRRecipe] = {}
    for path in sorted([*base.glob("*.yaml"), *base.glob("*.yml")]):
        recipe = recipe_from_mapping(_load_yaml(path))
        if recipe.id in _BUILTIN_RECIPES:
            raise ValueError(
                f"user recipe {path} uses built-in id {recipe.id!r}; choose a custom id"
            )
        if recipe.id in out:
            raise ValueError(f"duplicate user recipe id {recipe.id!r} under {base}")
        out[recipe.id] = recipe
    return out


def list_recipes(*, include_user: bool = True, user_root: Path | None = None) -> list[PDRRecipe]:
    """Return built-in plus user recipes in deterministic order."""
    recipes = {**_BUILTIN_RECIPES}
    if include_user:
        recipes.update(load_user_recipes(root=user_root))
    return [recipes[k] for k in sorted(recipes)]


def get_recipe(
    recipe_id: str | None = None,
    *,
    include_user: bool = True,
    user_root: Path | None = None,
) -> PDRRecipe:
    rid = recipe_id or DEFAULT_RECIPE.id
    recipes = {**_BUILTIN_RECIPES}
    if include_user:
        recipes.update(load_user_recipes(root=user_root))
    recipe = recipes.get(rid)
    if recipe is None:
        raise KeyError(f"unknown PDR recipe {rid!r}; available: {sorted(recipes)}")
    recipe.validate()
    return recipe


def example_user_recipe() -> dict[str, Any]:
    """Minimal YAML-serializable custom recipe template."""
    return {
        "id": "my-btc-usdt-recipe",
        "description": "Custom BTC/USDT recipe derived from the deliberative template.",
        "symbols": ["BTC/USDT"],
        "asset_class": "crypto",
        "timeframe": "1h",
        "data_provider": "ccxt:kraken",
        "analysts": ["classical_ta", "microstructure_lite", "hermes_semantic"],
        "analyst_config": {
            "hermes_semantic": {"max_age_minutes": 1440, "require_horizon_match": False}
        },
        "aggregator": "deliberative_committee",
        "risk_gate": "default",
        "reactor": "paper",
        "supported_modes": ["advise", "hitl", "backtest"],
        "live_allowed": False,
        "min_decisions_for_charter_gate": 30,
        "min_settlements_for_charter_gate": 30,
    }


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
        elif name == "hermes_semantic":
            from hermes_quant.analysts.semantic import HermesSemanticAnalyst

            out.append(HermesSemanticAnalyst(**kwargs))
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
    if recipe.aggregator == "deliberative_committee":
        from hermes_quant.aggregators.deliberative import DeliberativeCommitteeAggregator

        return DeliberativeCommitteeAggregator(**recipe.aggregator_config)
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
