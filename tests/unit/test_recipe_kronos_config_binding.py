"""Regression: kronos analyst_config must bind to KronosConfig, not raw kwargs.

Config-precedence / default-divergence family (sibling of the ar91
risk_gate_config bug). ``KronosAnalyst.__init__`` takes a ``config: KronosConfig``
object as its first positional argument, NOT flat KronosConfig kwargs. But
``instantiate_recipe_analysts`` spreads ``recipe.analyst_config["kronos"]`` as
``KronosAnalyst(**kwargs)``. A user who configures kronos via the documented
``KronosConfig`` knobs (``max_context``, ``model``, ``pred_len`` …) in a recipe's
``analyst_config`` would crash the entire advisor run with a ``TypeError`` at
recipe-instantiation time — the configured Kronos knobs are unreachable.

The happy path (empty / no kronos config) must remain byte-identical:
``KronosAnalyst()`` <=> ``KronosAnalyst(config=KronosConfig())``.
"""

from __future__ import annotations

from hermes_quant.analysts.kronos import KronosAnalyst, KronosConfig
from hermes_quant.recipes import PDRRecipe, instantiate_recipe_analysts


def _kronos_recipe(analyst_config: dict) -> PDRRecipe:
    return PDRRecipe(
        id="kronos-cfg-binding-test",
        description="kronos config-binding regression recipe",
        symbols=("BTC/USDT",),
        asset_class="crypto",
        timeframe="1h",
        analysts=("kronos",),
        analyst_config=analyst_config,
    )


def test_kronos_analyst_config_binds_to_kronos_config():
    """A recipe configuring kronos with KronosConfig fields must instantiate.

    RED before fix: instantiate_recipe_analysts raised
    `TypeError: KronosAnalyst.__init__() got an unexpected keyword argument
    'max_context'`.
    """
    recipe = _kronos_recipe(
        {"kronos": {"max_context": 256, "model": "small", "pred_len": 8}}
    )
    analysts = instantiate_recipe_analysts(recipe)
    assert len(analysts) == 1
    kronos = analysts[0]
    assert isinstance(kronos, KronosAnalyst)
    # The configured knobs must actually land on the analyst's config (non-vacuity:
    # prove the values flow through, not merely that instantiation didn't crash).
    assert kronos.config.max_context == 256
    assert kronos.config.model == "small"
    assert kronos.config.pred_len == 8
    # Untouched fields keep their KronosConfig defaults.
    assert kronos.config.sample_count == KronosConfig().sample_count


def test_kronos_no_config_is_byte_identical_default():
    """No kronos config => default KronosConfig (happy path unchanged)."""
    recipe = _kronos_recipe({})
    analysts = instantiate_recipe_analysts(recipe)
    assert len(analysts) == 1
    kronos = analysts[0]
    assert isinstance(kronos, KronosAnalyst)
    assert kronos.config == KronosConfig()
