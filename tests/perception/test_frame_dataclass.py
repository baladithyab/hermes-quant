"""T1 — PerceptionFrame is frozen / add-only / defaults-safe (ADR-0079 PDR-1).

Pins the carrier's shape: the 11 fields of ADR-0079 §D79.2 in order, frozen,
with all future-score fields defaulting to None/empty so the adapter omits them.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from hermes_quant.perception.frame import PerceptionFrame


def _bars(n: int = 3) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000.0] * n,
        }
    )


def test_frame_is_frozen():
    f = PerceptionFrame(
        symbol="AAPL",
        asof=pd.Timestamp("2026-01-03", tz="UTC"),
        bars=_bars(),
        last_close=100.5,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.symbol = "MSFT"  # type: ignore[misc]


def test_frame_field_order_matches_adr_0079():
    """The 11 fields in ADR-0079 §D79.2 order."""
    names = [fld.name for fld in dataclasses.fields(PerceptionFrame)]
    assert names == [
        "symbol",
        "asof",
        "bars",
        "last_close",
        "regime",
        "semantic_packets",
        "trend_velocity",
        "convergence",
        "saturation",
        "provenance",
        "extras",
    ]


def test_future_score_fields_default_safe():
    """PDR-1 leaves the three future scores None + provenance/semantic empty."""
    f = PerceptionFrame(
        symbol="AAPL",
        asof=pd.Timestamp("2026-01-03", tz="UTC"),
        bars=_bars(),
        last_close=100.5,
    )
    assert f.regime is None
    assert f.semantic_packets == ()
    assert f.trend_velocity is None
    assert f.convergence is None
    assert f.saturation is None
    assert f.provenance == ()
    assert dict(f.extras) == {}


def test_extras_default_is_independent():
    """Mutable-default trap check: each instance gets its own extras dict."""
    a = PerceptionFrame(
        symbol="A", asof=pd.Timestamp("2026-01-03", tz="UTC"), bars=_bars(), last_close=1.0
    )
    b = PerceptionFrame(
        symbol="B", asof=pd.Timestamp("2026-01-03", tz="UTC"), bars=_bars(), last_close=2.0
    )
    assert a.extras is not b.extras
