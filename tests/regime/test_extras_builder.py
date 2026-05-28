"""tests/regime/test_extras_builder.py — v0.6.0 R1 regime-in-extras tests.

Per ADR-0063 §"Test Plan" and design v0.6.0-regime-in-state.md §6.7.

The contract under test:
  - build_regime_extras(symbol, bars, asof=None, min_bars=60) -> dict
  - Returns dict with keys: "regime", "regime_failure", "regime_classifier_kind"
  - Never raises (silence-by-default per ADR-0036)
  - regime is RegimePacket | None
  - volatility_tier is the stable channel (-1/0/+1) — branches on label are forbidden
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from hermes_quant.regime.extras_builder import (
    RegimePacket,
    build_regime_extras,
)
from hermes_quant.regime.detector import RegimeState
from hermes_quant.regime.state_variables import StateVariables


# ---------------------------------------------------------------------------
# Helpers — synthetic bar generators
# ---------------------------------------------------------------------------


def _make_bars(
    n: int = 300,
    *,
    seed: int = 42,
    trend: float = 0.0,
    vol: float = 0.015,
) -> pd.DataFrame:
    """OHLCV bars with optional drift and configurable per-bar log-return vol."""
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(loc=trend / 252, scale=vol, size=n)
    prices = 100.0 * np.exp(np.cumsum(log_returns))
    timestamps = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": prices * 0.999,
            "high": prices * 1.002,
            "low": prices * 0.998,
            "close": prices,
            "volume": rng.integers(1_000, 100_000, size=n).astype(float),
        }
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_regime_packet():
    """A clean 252-bar uptrend returns a RegimePacket with valid fields."""
    bars = _make_bars(n=252, trend=0.30, seed=1)

    extras = build_regime_extras("AAPL", bars)

    # Always-set keys
    assert "regime" in extras
    assert "regime_failure" in extras
    assert "regime_classifier_kind" in extras

    packet = extras["regime"]
    assert packet is not None
    assert isinstance(packet, RegimePacket)

    # label is a RegimeState enum member
    assert isinstance(packet.label, RegimeState)
    # volatility_tier is the stable monotone tier
    assert packet.volatility_tier in (-1, 0, 1)
    # state_vars passes through
    assert isinstance(packet.state_vars, StateVariables)
    # no failure reason on happy path
    assert extras["regime_failure"] is None
    # classifier_kind is a known string
    assert packet.classifier_kind in ("rule_based", "hmm", "hmm_synthetic")
    assert extras["regime_classifier_kind"] == packet.classifier_kind


# ---------------------------------------------------------------------------
# Test 2 — classifier missing returns failure reason
# ---------------------------------------------------------------------------


def test_classifier_missing_returns_failure_reason():
    """If the underlying classifier import/instantiation fails, regime is None
    with a failure reason set; no exception escapes."""
    bars = _make_bars(n=252)

    # Patch the internal classifier-construction helper to raise ImportError.
    with patch(
        "hermes_quant.regime.extras_builder._build_classifier",
        side_effect=ImportError("HMMClassifier not available"),
    ):
        extras = build_regime_extras("AAPL", bars)

    assert extras["regime"] is None
    assert extras["regime_failure"] is not None
    assert extras["regime_failure"].startswith("classifier_unavailable")
    assert extras["regime_classifier_kind"] == "unavailable"


# ---------------------------------------------------------------------------
# Test 3 — insufficient bars
# ---------------------------------------------------------------------------


def test_insufficient_bars_returns_failure_reason():
    """Fewer bars than `min_bars` returns a None packet with `insufficient_bars`."""
    bars = _make_bars(n=30)  # < 60

    extras = build_regime_extras("AAPL", bars, min_bars=60)

    assert extras["regime"] is None
    assert extras["regime_failure"] is not None
    assert extras["regime_failure"].startswith("insufficient_bars")
    assert extras["regime_classifier_kind"] == "unavailable"


# ---------------------------------------------------------------------------
# Test 4 — classify exception caught
# ---------------------------------------------------------------------------


def test_classify_exception_returns_failure_reason(caplog):
    """A classifier whose classify() raises is caught silently."""
    bars = _make_bars(n=252)

    class _BoomDetector:
        def classify(self, _state_vars):
            raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        extras = build_regime_extras("AAPL", bars, detector=_BoomDetector())

    assert extras["regime"] is None
    assert extras["regime_failure"] is not None
    assert extras["regime_failure"].startswith("classify_error")
    assert "boom" in extras["regime_failure"]


# ---------------------------------------------------------------------------
# Test 5 — label-stability invariant
# ---------------------------------------------------------------------------


def test_label_stability_invariant_across_seeds():
    """volatility_tier (the stable channel) is identical regardless of which
    HMM seed produced the label string. This is the regression for ADR-0058
    label-mapping caveat: downstream MUST NOT branch on .label strings.

    Our test setup: classify the same bars with two distinct fake classifiers
    that return the SAME RegimeState but with different reason strings — the
    derived volatility_tier (which comes from realized_vol_percentile, not the
    label) must be identical.
    """
    bars = _make_bars(n=252, vol=0.030, seed=7)  # high-vol regime

    class _LabelA:
        def classify(self, _sv):
            # Pretends to be index-0 mapped to BULL
            return RegimeState.BULL, "label_A:state=0->bull"

    class _LabelB:
        def classify(self, _sv):
            # Pretends to be index-2 mapped to BULL after retrain
            return RegimeState.BULL, "label_B:state=2->bull"

    extras_a = build_regime_extras("AAPL", bars, detector=_LabelA())
    extras_b = build_regime_extras("AAPL", bars, detector=_LabelB())

    assert extras_a["regime"] is not None
    assert extras_b["regime"] is not None
    # the stable channel matches across "retrains"
    assert extras_a["regime"].volatility_tier == extras_b["regime"].volatility_tier


# ---------------------------------------------------------------------------
# Test 6 — UNKNOWN is not a failure
# ---------------------------------------------------------------------------


def test_unknown_is_not_failure():
    """When the classifier *successfully* returns UNKNOWN it is a valid regime,
    not a failure — populate a packet with label=UNKNOWN and `regime_failure`
    stays None."""
    bars = _make_bars(n=252)

    class _UnknownDetector:
        def classify(self, _sv):
            return RegimeState.UNKNOWN, "indeterminate"

    extras = build_regime_extras("AAPL", bars, detector=_UnknownDetector())

    assert extras["regime"] is not None
    assert isinstance(extras["regime"], RegimePacket)
    assert extras["regime"].label == RegimeState.UNKNOWN
    assert extras["regime_failure"] is None


# ---------------------------------------------------------------------------
# Test 7 — caller cannot shadow regime key
# ---------------------------------------------------------------------------


def test_caller_cannot_shadow_regime_key():
    """The advisor merges build_regime_extras() OVER caller-provided
    market_extras — a caller passing extras={'regime': 'evil'} cannot override
    the canonical regime calculation. We test this here at the helper-merge
    level: the helper itself returns a dict with `regime` populated, and the
    advisor must be wired to merge canonical-on-top.

    For the helper unit-level: confirm the returned dict ALWAYS has the
    canonical "regime" key — never delegating to a caller-supplied one.
    """
    bars = _make_bars(n=252)

    extras = build_regime_extras("AAPL", bars)

    # The returned dict must always own these three keys.
    assert "regime" in extras
    # The value is canonical: a RegimePacket | None, not a string.
    assert extras["regime"] is None or isinstance(extras["regime"], RegimePacket)


# ---------------------------------------------------------------------------
# Test 8 — integration: advisor merges regime into MarketContext.extras
# ---------------------------------------------------------------------------


def test_e2e_advisor_with_regime_in_extras():
    """End-to-end: a fake analyst captures ctx.extras and we verify the
    canonical regime key is present and is a RegimePacket | None."""
    from hermes_quant.protocol import AnalystView, MarketContext
    from hermes_quant.advisor import recommend_multi_horizon

    captured: list[MarketContext] = []

    class _CapturingAnalyst:
        name = "capture"

        def analyze(self, ctx: MarketContext) -> AnalystView | None:
            captured.append(ctx)
            return None

    bars = _make_bars(n=252)

    class _FakeProvider:
        def fetch_bars(self, symbol, timeframe, start, end, *, as_of=None, **kwargs):
            return bars

    views = recommend_multi_horizon(
        "AAPL",
        horizons=("1d",),
        asset_class="equity",
        provider=_FakeProvider(),
        analysts=[_CapturingAnalyst()],
        market_extras={"regime": "evil_caller_attempt"},
    )

    # Advisor returned (no views from fake analyst is fine)
    assert views == []

    # Analyst was invoked and saw ctx.extras with canonical regime key
    assert len(captured) == 1
    ctx = captured[0]
    assert "regime" in ctx.extras
    # Caller's "evil" string MUST have been overridden by the canonical packet (or None)
    regime_val = ctx.extras["regime"]
    assert regime_val is None or isinstance(regime_val, RegimePacket)
    assert "regime_classifier_kind" in ctx.extras
