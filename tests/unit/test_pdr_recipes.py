"""Tests for PDR recipe runtime contract (ADR-0021)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from hermes_quant.advisor import recommend
from hermes_quant.recipes import (
    DEFAULT_RECIPE,
    DELIBERATIVE_RECIPE,
    PDRRecipe,
    get_recipe,
    instantiate_recipe_aggregator,
    instantiate_recipe_analysts,
    instantiate_recipe_risk_gate,
    list_recipes,
)
from hermes_quant.tools import quant_recipes


class _Provider:
    def fetch_bars(self, symbol, timeframe, *args, **kwargs):
        ts = pd.date_range("2024-01-01", periods=120, freq="1h", tz="UTC")
        close = [100 + i * 0.01 for i in range(120)]
        return pd.DataFrame({
            "timestamp": ts,
            "open": close,
            "high": [c + 0.1 for c in close],
            "low": [c - 0.1 for c in close],
            "close": close,
            "volume": 1000.0,
        })


def test_default_recipe_validates_and_hash_stable():
    DEFAULT_RECIPE.validate()
    assert DEFAULT_RECIPE.id == "btc-usdt-mvp"
    assert len(DEFAULT_RECIPE.config_hash) == 16
    assert DEFAULT_RECIPE.config_hash == get_recipe("btc-usdt-mvp").config_hash


def test_recipe_hash_changes_with_composition():
    a = DEFAULT_RECIPE
    b = PDRRecipe(
        **{**a.to_dict(), "analysts": ("classical_ta",)}
    )
    assert a.config_hash != b.config_hash


def test_invalid_recipe_rejected():
    with pytest.raises(ValueError):
        PDRRecipe(
            id="bad id with spaces",
            description="bad",
            symbols=("BTC/USDT",),
            asset_class="crypto",
            timeframe="1h",
        ).validate()


def test_live_autonomous_recipe_rejected_until_gates():
    with pytest.raises(ValueError):
        PDRRecipe(
            id="live-bot",
            description="bad",
            symbols=("BTC/USDT",),
            asset_class="crypto",
            timeframe="1h",
            supported_modes=("autonomous",),
            live_allowed=True,
        ).validate()


def test_list_recipes_surfaces_builtins():
    recipes = list_recipes()
    assert [r.id for r in recipes] == ["btc-usdt-deliberative", "btc-usdt-mvp"]


def test_deliberative_recipe_validates_and_hashes():
    DELIBERATIVE_RECIPE.validate()
    assert DELIBERATIVE_RECIPE.aggregator == "deliberative_committee"
    assert "hermes_semantic" in DELIBERATIVE_RECIPE.analysts
    assert get_recipe("btc-usdt-deliberative").config_hash == DELIBERATIVE_RECIPE.config_hash


def test_builtin_recipe_components_instantiate():
    recipe = get_recipe("btc-usdt-mvp")
    analysts = instantiate_recipe_analysts(recipe)
    assert [getattr(a, "name", type(a).__name__) for a in analysts]
    agg = instantiate_recipe_aggregator(recipe)
    assert hasattr(agg, "aggregate")
    gate = instantiate_recipe_risk_gate(recipe)
    assert hasattr(gate, "gate")


def test_quant_recipes_tool_lists_hashes():
    out = json.loads(quant_recipes({}))
    assert out["success"] is True
    assert out["count"] == 2
    ids = [r["id"] for r in out["recipes"]]
    assert ids == ["btc-usdt-deliberative", "btc-usdt-mvp"]
    assert all(len(r["config_hash"]) == 16 for r in out["recipes"])


def test_advisor_accepts_recipe_id_and_records_metadata():
    result = recommend(
        "BTC/USDT",
        provider=_Provider(),
        include_lessons=False,
        recipe_id="btc-usdt-mvp",
    )
    assert result["recipe"]["id"] == "btc-usdt-mvp"
    assert len(result["recipe"]["config_hash"]) == 16
    assert result["asset_class"] == "crypto"
    assert result["timeframe"] == "1h"


def test_advisor_deliberative_recipe_accepts_semantic_packet():
    packet = {
        "schema_version": 1,
        "asset": "BTC/USDT",
        "asof": "2024-01-05T22:00:00Z",
        "horizon": "1h",
        "stance": "bullish",
        "confidence": 0.8,
        "magnitude": 0.01,
        "summary": "Hermes semantic packet sees constructive regime.",
        "sources": [{"type": "note", "ref": "test"}],
        "model": "hermes:test",
    }
    from hermes_quant.semantic import semantic_packet_from_dict
    packet = semantic_packet_from_dict(packet).to_dict()
    result = recommend(
        "BTC/USDT",
        provider=_Provider(),
        include_lessons=False,
        recipe_id="btc-usdt-deliberative",
        market_extras={"semantic_packets": [packet]},
    )
    assert result["recipe"]["id"] == "btc-usdt-deliberative"
    assert any(v["analyst"] == "hermes_semantic" for v in result["analyst_views"])
    assert result["aggregated_signal"]["aggregator"] == "deliberative_committee"
    assert result["aggregated_signal"]["metadata"]["committee"]["safety"]["risk_gate_still_required"] is True
