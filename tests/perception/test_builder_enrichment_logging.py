"""RR13 — perception ENRICHMENT failures for an ENABLED feature are VISIBLE.

The four flag-gated enrichment blocks in ``build_perception_frame`` (Step 5
semantic, Step 5b velocity, Step 5c convergence, Step 6b saturation) each catch
their own failure so the frame is never blocked (silence-by-default rail). RR13:
those except handlers previously logged at ``debug`` only, so an ENABLED feature
that fails on EVERY tick stayed invisible. They now log at ``warning`` (the block
is only reached when its flag is ON => the feature is enabled).

Behaviour-preserving: the frame is still built (failure is swallowed); only the
log LEVEL of the failure changed. The HAPPY path emits nothing (no exception ->
no log), so the flag-OFF / success path is byte-identical.

Offline-deterministic: synthetic bars via an inert provider; the enrichment
failure is forced by monkeypatching the loader the enabled block calls. No
network, no real packet store.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import pandas as pd

import hermes_quant.perception.builder as builder_mod
from hermes_quant.perception.builder import build_perception_frame


def _make_bars(n: int = 120, *, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed=seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = 100.0 + np.arange(n) * 0.5 + rng.normal(0, 0.5, n)
    opens = closes - rng.uniform(0, 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, 0.4, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _InertProvider:
    name = "inert"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache: bool = True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _build(provider_bars: pd.DataFrame, *, decision: datetime):
    return build_perception_frame(
        "AAPL",
        timeframe="1d",
        asset_class="equity",
        provider=_InertProvider(provider_bars),
        asof_ts=pd.Timestamp(provider_bars["timestamp"].iloc[-1]),
        lookback_bars=200,
        decision_asof=decision,
    )


def test_enabled_semantic_load_failure_logs_warning(monkeypatch, caplog):
    """HERMES_QUANT_SEMANTIC_ENABLED=1 + a loader that always raises => the Step-5
    enrichment failure is logged at WARNING (visible), and the frame is STILL built
    (failure swallowed — the silence-by-default rail is preserved)."""
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")

    def _boom_load(*_a, **_k):
        raise RuntimeError("forced semantic load failure")

    # Step 5 does `from hermes_quant.catalyst.synthesize import load_packets_for`
    # inside the try — patch the source symbol so the local import resolves to it.
    monkeypatch.setattr(
        "hermes_quant.catalyst.synthesize.load_packets_for", _boom_load
    )

    bars = _make_bars()
    with caplog.at_level(logging.WARNING, logger=builder_mod.__name__):
        frame = _build(bars, decision=datetime(2026, 5, 30, 12, 0, tzinfo=UTC))

    # Frame still produced (degrade gracefully), no packets absorbed.
    assert frame is not None
    assert frame.semantic_packets == ()
    # The enabled-feature failure is VISIBLE at WARNING (RR13), not silent debug.
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "semantic load failed (feature ENABLED)" in m for m in msgs
    ), f"expected a semantic-load WARNING, got: {msgs}"


def test_disabled_feature_logs_nothing(monkeypatch, caplog):
    """Flag OFF (default) => the Step-5 block is never entered, so a broken loader
    is never called and NO warning is emitted (byte-identical to today)."""
    monkeypatch.delenv("HERMES_QUANT_SEMANTIC_ENABLED", raising=False)

    def _boom_load(*_a, **_k):  # would raise IF called — but it must not be
        raise RuntimeError("must not be reached when the flag is OFF")

    monkeypatch.setattr(
        "hermes_quant.catalyst.synthesize.load_packets_for", _boom_load
    )

    bars = _make_bars()
    with caplog.at_level(logging.WARNING, logger=builder_mod.__name__):
        frame = _build(bars, decision=datetime(2026, 5, 30, 12, 0, tzinfo=UTC))

    assert frame is not None
    assert not any(
        "feature ENABLED" in r.message for r in caplog.records
    ), "flag-OFF path must emit no enrichment warning (byte-identical)"


def test_enabled_velocity_build_failure_logs_warning(monkeypatch, caplog):
    """HERMES_QUANT_TREND_VELOCITY=1 + a velocity source that raises => the Step-5b
    enrichment failure is logged at WARNING; the frame is still built."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")

    def _boom_ts(*_a, **_k):
        raise RuntimeError("forced velocity source failure")

    monkeypatch.setattr(
        "hermes_quant.perception.velocity_source.interest_timestamps_by_symbol",
        _boom_ts,
    )

    bars = _make_bars()
    with caplog.at_level(logging.WARNING, logger=builder_mod.__name__):
        frame = _build(bars, decision=datetime(2026, 5, 30, 12, 0, tzinfo=UTC))

    assert frame is not None
    assert frame.trend_velocity is None  # failed -> stays None (no decay basis)
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "velocity build failed (feature ENABLED)" in m for m in msgs
    ), f"expected a velocity-build WARNING, got: {msgs}"
