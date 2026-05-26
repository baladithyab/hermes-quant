"""Live LLM smoke test for the LLM-backed committee (ADR-0037).

Skipped unless ``HERMES_QUANT_LIVE_LLM=1`` is set — exercises a real
OpenRouter call and is therefore $$, slow, and non-deterministic. Use to
verify wiring against a production-like endpoint when the OpenRouter API
key is available.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from hermes_quant.aggregators.deliberative import DeliberativeConfig
from hermes_quant.aggregators.llm_committee import run_llm_committee
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

pytestmark = pytest.mark.skipif(
    "HERMES_QUANT_LIVE_LLM" not in os.environ,
    reason="set HERMES_QUANT_LIVE_LLM=1 to run live LLM smoke (costs money)",
)


def _ctx() -> MarketContext:
    ts = pd.date_range("2026-01-15", periods=3, freq="1d", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [1_000_000, 1_100_000, 900_000],
        }
    )
    return MarketContext(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=102.5,
        last_volume=900_000.0,
        asof=ts[-1],
    )


def _views() -> list[AnalystView]:
    return [
        AnalystView(
            analyst="classical_ta",
            direction=1,
            magnitude=0.012,
            confidence=0.7,
            confidence_raw=0.7,
            horizon="1d",
            rationale="Bullish breakout above 50d MA with rising volume",
        )
    ]


def _baseline() -> AggregatedSignal:
    return AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=pd.Timestamp("2026-01-17", tz="UTC"),
        direction=1,
        magnitude=0.012,
        confidence=0.6,
        confidence_raw=0.6,
        horizon="1d",
        components=tuple(_views()),
        aggregator="bma",
    )


def test_live_llm_committee_returns_structured_turns() -> None:
    cfg = DeliberativeConfig(enable_llm_turns=True)
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=_views(),
        baseline_signal=_baseline(),
        config=cfg,
    )
    # We do not assert specific content (it is non-deterministic), only
    # that at least one turn was emitted and the prompt-hash audit trail
    # is present.
    assert len(out) >= 1
    for t in out:
        assert isinstance(t.metadata, dict)
        assert "prompt_hash" in t.metadata
        assert len(t.metadata["prompt_hash"]) == 64
