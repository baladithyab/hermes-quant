"""Regression test for ADR-0018 §D4 abstain filter (BMA).

This test was added in v0.3.1 after the Phase-7 architecture review
caught that BMA was NOT filtering zero-confidence views, allowing
KronosAnalyst's abstain views to inflate the silence-bias-gate's
`min_analysts_emitted` count.

Without this filter:
- KronosAnalyst on a box without `kronos` package installed emits a
  confidence=0.0 abstain view
- BMA sees 3 views (TA + Microstructure + Kronos-abstain)
- silence-bias gate sees `len(analyst_views) == 3` and PASSES the
  compute-budget dim with min_analysts_emitted=2
- but only 2 voices actually had a signal!
- autonomous mode FIRE based on a vote that included a phantom voice

With this filter, the abstain view is dropped before aggregation;
the silence-bias gate sees only the two real voices; if those agree,
FIRE is correct; if not, silence is preserved.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import ABSTAIN_THRESHOLD, BMAAggregator
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx():
    bars = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-05-13T00:00:00Z"),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "timestamp": pd.Timestamp("2026-05-13T01:00:00Z"),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1000,
            },
        ]
    )
    return MarketContext(
        asset="BTC/USDT",
        asset_class="crypto",
        timeframe="1h",
        exchange="binance",
        asof=bars["timestamp"].iloc[-1],
        bars=bars,
        last_close=101.0,
        last_volume=1000.0,
    )


def _view(name, *, confidence, direction=1, magnitude=0.05):
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence + 0.10,
        horizon="1h",
    )


# ---------------------------------------------------------------------------
# The abstain-filter regression
# ---------------------------------------------------------------------------


def test_abstain_view_dropped_from_components():
    """A confidence=0.0 view (KronosAnalyst-style abstain) MUST NOT appear
    in the aggregated signal's components tuple."""
    agg = BMAAggregator()
    views = [
        _view("classical_ta", confidence=0.7),
        _view("microstructure_lite", confidence=0.6),
        _view("kronos", confidence=0.0),  # abstain
    ]
    sig = agg.aggregate(views, _ctx())
    component_names = [c.analyst for c in sig.components]
    assert "kronos" not in component_names
    assert len(sig.components) == 2


def test_below_threshold_view_dropped():
    """Views with confidence below ABSTAIN_THRESHOLD are dropped, not just
    confidence==0."""
    agg = BMAAggregator()
    views = [
        _view("classical_ta", confidence=0.7),
        _view("microstructure_lite", confidence=0.6),
        _view("flaky", confidence=ABSTAIN_THRESHOLD - 0.01),
    ]
    sig = agg.aggregate(views, _ctx())
    component_names = [c.analyst for c in sig.components]
    assert "flaky" not in component_names


def test_at_threshold_view_admitted():
    """Boundary case: confidence == ABSTAIN_THRESHOLD is admitted (>=)."""
    agg = BMAAggregator()
    views = [
        _view("classical_ta", confidence=0.7),
        _view("microstructure_lite", confidence=0.6),
        _view("borderline", confidence=ABSTAIN_THRESHOLD),
    ]
    sig = agg.aggregate(views, _ctx())
    component_names = [c.analyst for c in sig.components]
    assert "borderline" in component_names


def test_all_views_abstain_yields_flat_signal():
    """If EVERY view abstains, BMA returns the flat signal (silence)."""
    agg = BMAAggregator()
    views = [
        _view("a", confidence=0.0),
        _view("b", confidence=0.05),
        _view("c", confidence=ABSTAIN_THRESHOLD - 0.01),
    ]
    sig = agg.aggregate(views, _ctx())
    assert sig.direction == 0
    assert sig.confidence == 0.0
    assert len(sig.components) == 0


def test_two_real_plus_one_abstain_does_not_inflate_voices():
    """The bug scenario: TA + Microstructure agreeing + Kronos abstaining
    must result in components.count == 2, NOT 3."""
    agg = BMAAggregator()
    views = [
        _view("classical_ta", confidence=0.75, direction=1),
        _view("microstructure_lite", confidence=0.65, direction=1),
        _view("kronos", confidence=0.0, direction=0),
    ]
    sig = agg.aggregate(views, _ctx())
    assert len(sig.components) == 2
    assert sig.direction == 1  # both voted long


def test_disagreement_with_abstainer_handled_correctly():
    """Real voices disagreeing + Kronos abstain: BMA computes a net-flat
    weighted direction sum and returns the flat signal (silence). The
    abstain filter doesn't mask this — which is exactly the desired
    'silence by default' charter behavior."""
    agg = BMAAggregator()
    views = [
        _view("classical_ta", confidence=0.6, direction=1),
        _view("microstructure_lite", confidence=0.6, direction=-1),
        _view("kronos", confidence=0.0, direction=0),
    ]
    sig = agg.aggregate(views, _ctx())
    # 2 voices, exactly disagreeing → BMA returns flat signal.
    # _flat_signal has empty components; that's the silence path.
    assert sig.direction == 0
    assert sig.confidence == 0.0


def test_empty_views_list_handled():
    """Pre-existing behavior: empty views → flat signal."""
    sig = BMAAggregator().aggregate([], _ctx())
    assert sig.direction == 0
    assert len(sig.components) == 0


def test_none_views_handled():
    """Defensive: BMA should treat None views as empty list."""
    sig = BMAAggregator().aggregate(None, _ctx())
    assert sig.direction == 0
    assert len(sig.components) == 0
