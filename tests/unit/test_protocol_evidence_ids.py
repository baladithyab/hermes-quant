"""Backward-compat tests for ADR-0033 D4: evidence_ids on AnalystView/AggregatedSignal.

Phase 1: field is optional, defaults to empty tuple, no CI gate yet.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from hermes_quant.protocol import AggregatedSignal, AnalystView


def _make_view(**overrides) -> AnalystView:
    base = dict(
        analyst="test",
        direction=1,
        magnitude=0.01,
        confidence=0.6,
        confidence_raw=0.7,
        horizon="1h",
    )
    base.update(overrides)
    return AnalystView(**base)


def _make_signal(**overrides) -> AggregatedSignal:
    base = dict(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp("2026-01-01", tz="UTC"),
        direction=1,
        magnitude=0.01,
        confidence=0.6,
        confidence_raw=0.7,
        horizon="1h",
        components=(),
        aggregator="bma",
    )
    base.update(overrides)
    return AggregatedSignal(**base)


def test_analystview_evidence_ids_default_empty_tuple():
    view = _make_view()
    assert view.evidence_ids == ()
    assert isinstance(view.evidence_ids, tuple)


def test_analystview_accepts_evidence_id_tuple():
    ids = ("uuid-1", "uuid-2")
    view = _make_view(evidence_ids=ids)
    assert view.evidence_ids == ids


def test_analystview_evidence_ids_is_immutable():
    view = _make_view(evidence_ids=("uuid-1",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.evidence_ids = ("uuid-2",)  # type: ignore[misc]


def test_aggregatedsignal_evidence_ids_default_empty_tuple():
    sig = _make_signal()
    assert sig.evidence_ids == ()
    assert isinstance(sig.evidence_ids, tuple)


def test_aggregatedsignal_accepts_evidence_id_tuple():
    ids = ("uuid-a", "uuid-b", "uuid-c")
    sig = _make_signal(evidence_ids=ids)
    assert sig.evidence_ids == ids


def test_existing_analystview_construction_still_works():
    """Pre-amendment construction (no evidence_ids kwarg) must continue to work."""
    view = AnalystView(
        analyst="legacy",
        direction=-1,
        magnitude=0.005,
        confidence=0.55,
        confidence_raw=0.65,
        horizon="5m",
        rationale="legacy rationale",
        metadata={"k": "v"},
    )
    assert view.analyst == "legacy"
    assert view.evidence_ids == ()
    assert view.rationale == "legacy rationale"
    assert view.metadata == {"k": "v"}


def test_aggregatedsignal_evidence_ids_can_aggregate_from_components():
    v1 = _make_view(evidence_ids=("uuid-1", "uuid-2"))
    v2 = _make_view(evidence_ids=("uuid-2", "uuid-3"))
    flattened = tuple(sorted(set(v1.evidence_ids + v2.evidence_ids)))
    sig = _make_signal(components=(v1, v2), evidence_ids=flattened)
    assert sig.evidence_ids == ("uuid-1", "uuid-2", "uuid-3")
    assert len(sig.evidence_ids) == 3
