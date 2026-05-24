"""Tests for the AI-Trader-style MessageKind discriminator on protocol dataclasses.

Steals AI-Trader's signal-type discriminator pattern:
- AnalystView and AggregatedSignal default to 'discussion' (analytical context).
- Proposal is fixed at 'operation' (execution-channel message).

Together these enforce channel separation at the type level so downstream
consumers don't have to string-grep to find the actionable record.
"""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    MessageKind,
    Proposal,
    _MESSAGE_KIND_VALUES,
)


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


def _make_proposal(**overrides) -> Proposal:
    base = dict(
        proposal_id="prop_2026-01-01T00:00:00_BTCUSDT_abc123",
        asset="BTC/USDT",
        asof=pd.Timestamp("2026-01-01", tz="UTC"),
        direction=1,
        target_size_pct_nav=0.10,
        horizon="1h",
    )
    base.update(overrides)
    return Proposal(**base)


# ---------------------------------------------------------------------------
# AnalystView
# ---------------------------------------------------------------------------

def test_analystview_message_kind_default_discussion() -> None:
    """Default AnalystView is on the 'discussion' channel — pure commentary."""
    view = _make_view()
    assert view.message_kind == "discussion"


def test_analystview_can_be_strategy() -> None:
    """An analyst can explicitly emit a strategy-channel view."""
    view = _make_view(message_kind="strategy")
    assert view.message_kind == "strategy"


def test_analystview_rejects_unknown_kind() -> None:
    """Runtime check guards against typos — Literal isn't enforced at runtime."""
    with pytest.raises(ValueError, match="message_kind"):
        _make_view(message_kind="broker_call")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AggregatedSignal
# ---------------------------------------------------------------------------

def test_aggregatedsignal_message_kind_default_discussion() -> None:
    """Default AggregatedSignal is also 'discussion' — only the risk gate
    promotes it to an actionable channel by minting a Proposal."""
    sig = _make_signal()
    assert sig.message_kind == "discussion"


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

def test_proposal_default_message_kind_is_operation() -> None:
    """Proposals are pinned to the 'operation' channel by default."""
    prop = _make_proposal()
    assert prop.message_kind == "operation"


def test_proposal_rejects_non_operation_message_kind() -> None:
    """Trying to construct a Proposal as anything other than 'operation' is
    a programming error — Proposals are the execution-channel type."""
    with pytest.raises(TypeError, match="must be 'operation'"):
        _make_proposal(message_kind="discussion")  # type: ignore[arg-type]


def test_proposal_required_fields_enforced() -> None:
    """Constructing a Proposal without required fields raises (dataclass
    enforcement). Belt-and-braces against accidental empty proposals."""
    with pytest.raises(TypeError):
        Proposal()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------

def test_message_kind_literal_set_matches_dataclass_runtime_check() -> None:
    """If MessageKind grows a new value but _MESSAGE_KIND_VALUES doesn't (or
    vice versa), the runtime check would silently drift away from the type
    annotation. This test catches that."""
    assert set(MessageKind.__args__) == _MESSAGE_KIND_VALUES
    assert _MESSAGE_KIND_VALUES == {"operation", "strategy", "discussion", "forbidden"}
