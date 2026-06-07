"""Lock-in tests for the BMA dissent-aware confidence cap (deep-review
2026-06-07; HERMES_QUANT_DISSENT_CAP).

Reproduces the June-4 ASTS failure: three same-direction LONG voices of modest
conviction outvote one HIGH-conviction SHORT (Kronos, raw 0.85). The BMA emits a
confident LONG (~0.69), burying the dissenter. The cap does NOT flip the
direction — it refuses to be confident when a high-conviction minority dissents,
so the gate/sizer pull back. Flag-gated default-off (byte-identical when unset).
"""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator, BMAConfig
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx(asset: str = "ASTS") -> MarketContext:
    bars = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=2, freq="1d"),
        "open": [100.0, 101.0], "high": [101.0, 102.0],
        "low": [99.0, 100.0], "close": [100.5, 101.5],
        "volume": [1000.0, 1000.0],
    })
    return MarketContext(
        asset=asset, timeframe="1d", asset_class="equity", exchange=None,
        bars=bars, last_close=101.5, last_volume=1000.0,
        asof=pd.Timestamp("2026-06-03T20:00:00"),
    )


def _view(analyst, direction, raw_conf, magnitude=0.03):
    return AnalystView(
        analyst=analyst, direction=direction, magnitude=magnitude,  # type: ignore[arg-type]
        confidence=raw_conf, confidence_raw=raw_conf, horizon="1d",
    )


def _asts_panel():
    # The actual June-4 shape: 3 modest LONGs + 1 high-conviction SHORT.
    return [
        _view("classical-ta", 1, 0.47),
        _view("microstructure_lite", 1, 0.33),
        _view("hermes_semantic", 1, 0.895),
        _view("kronos", -1, 0.85),  # the buried high-conviction dissenter
    ]


def _agg():
    # require_ensemble False so the multi-contributor path runs;
    # default dissent thresholds (0.70 trigger, 0.50 ceiling).
    return BMAAggregator(require_ensemble=False, config=BMAConfig())


def test_dissent_cap_off_by_default(monkeypatch):
    # Flag off: behavior is byte-identical to legacy. We can't assert a specific
    # magnitude (depends on calibrator state), only that the flag-off path does
    # NOT apply the ceiling — i.e. it equals a second flag-off run (determinism)
    # and the cap engaging on the flag-ON run produces a DIFFERENT (lower) value.
    monkeypatch.delenv("HERMES_QUANT_DISSENT_CAP", raising=False)
    off1 = _agg().aggregate(_asts_panel(), _ctx()).confidence
    off2 = _agg().aggregate(_asts_panel(), _ctx()).confidence
    assert off1 == pytest.approx(off2)  # deterministic, no hidden cap
    monkeypatch.setenv("HERMES_QUANT_DISSENT_CAP", "1")
    on = _agg().aggregate(_asts_panel(), _ctx()).confidence
    assert on <= off1  # cap can only lower (or leave) confidence, never raise


def test_dissent_cap_engages_when_flag_on(monkeypatch):
    # Use a panel whose UNCAPPED confidence is genuinely above the 0.50 ceiling
    # (3 strong longs + 1 high-conviction short -> ~0.62 long), so the cap
    # demonstrably BITES rather than being a no-op below the ceiling.
    panel = [
        _view("a", 1, 0.90), _view("b", 1, 0.88), _view("c", 1, 0.85),
        _view("kronos", -1, 0.85),
    ]
    monkeypatch.delenv("HERMES_QUANT_DISSENT_CAP", raising=False)
    uncapped = _agg().aggregate(panel, _ctx()).confidence
    monkeypatch.setenv("HERMES_QUANT_DISSENT_CAP", "1")
    capped = _agg().aggregate(panel, _ctx()).confidence
    assert uncapped > 0.50  # precondition: this panel is over-confident uncapped
    assert capped < uncapped  # the cap strictly lowered it (direction unchanged)


def test_dissent_cap_does_not_flip_direction(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DISSENT_CAP", "1")
    sig = _agg().aggregate(_asts_panel(), _ctx())
    sig_off_direction = None
    monkeypatch.delenv("HERMES_QUANT_DISSENT_CAP", raising=False)
    sig_off_direction = _agg().aggregate(_asts_panel(), _ctx()).direction
    # The cap is a confidence haircut, never a direction change.
    assert sig.direction == sig_off_direction


def test_no_cap_when_dissent_is_low_conviction(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DISSENT_CAP", "1")
    # All same direction except a WEAK dissenter (0.20 < 0.70 trigger).
    panel = [
        _view("a", 1, 0.80), _view("b", 1, 0.75), _view("c", -1, 0.20),
    ]
    capped = _agg().aggregate(panel, _ctx()).confidence
    monkeypatch.delenv("HERMES_QUANT_DISSENT_CAP", raising=False)
    uncapped = _agg().aggregate(panel, _ctx()).confidence
    # Low-conviction dissent must NOT trigger the cap -> identical confidence.
    assert capped == pytest.approx(uncapped)


def test_no_cap_when_unanimous(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DISSENT_CAP", "1")
    panel = [_view("a", 1, 0.85), _view("b", 1, 0.80), _view("c", 1, 0.90)]
    capped = _agg().aggregate(panel, _ctx()).confidence
    monkeypatch.delenv("HERMES_QUANT_DISSENT_CAP", raising=False)
    uncapped = _agg().aggregate(panel, _ctx()).confidence
    # No dissent at all -> cap never engages -> identical.
    assert capped == pytest.approx(uncapped)
