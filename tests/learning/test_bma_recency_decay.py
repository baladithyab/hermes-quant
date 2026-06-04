"""c96e — recency-weighted, asof-honest refit wired into BMA._weight_for.

With HERMES_QUANT_L2_POSTERIOR_DECAY=1, the per-analyst weight is computed from
a recency-weighted Beta refit over the analyst's settled sample ring, filtered
so only outcomes observable before the *current* decision asof count. With the
flag off, the weight is the plain posterior accuracy — byte-identical to today.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import AnalystView, EpisodeOutcome, MarketContext


def _ctx(asof: pd.Timestamp | None = None) -> MarketContext:
    ts = pd.date_range("2026-05-13", periods=2, freq="1h")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000.0, 1000.0],
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=101.5,
        last_volume=1000.0,
        asof=asof if asof is not None else ts[-1],
    )


def _view(name: str, direction: int = 1) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.01,
        confidence=0.7,
        confidence_raw=0.85,
        horizon="1d",
    )


def _settle(a: BMAAggregator, name: str, *, asof: str, correct: bool) -> None:
    sig = a.aggregate([_view(name)], _ctx())
    a.update(
        EpisodeOutcome(
            asset="BTC/USDT",
            timeframe="1h",
            asof=pd.Timestamp(asof, tz="UTC"),
            aggregated_signal=sig,
            realized_returns={"1d": 0.01 if correct else -0.01},
            direction_correct={name: correct},
        )
    )


def test_decay_flag_off_uses_plain_posterior(monkeypatch):
    """Flag OFF: _weight_for is the plain posterior accuracy (byte-identical)."""
    monkeypatch.delenv("HERMES_QUANT_L2_POSTERIOR_DECAY", raising=False)
    a = BMAAggregator(n_min_observations=2, prior_alpha=1.0, prior_beta=1.0)
    a.calibrator = ColdStartCalibrator()
    for i in range(4):
        _settle(a, "a", asof=f"2026-01-{1 + i:02d}", correct=True)
    # alpha=5, beta=1 -> posterior accuracy 5/6 ≈ 0.833; no decay applied.
    assert a._weight_for("a") == pytest.approx(5.0 / 6.0)


def test_decay_flag_on_downweights_stale_history(monkeypatch):
    """Flag ON: an analyst whose correct calls are all ANCIENT relative to the
    decision asof gets a weight pulled back toward the prior (the old evidence
    decays), versus the same record evaluated with no decay."""
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_DECAY", "1")
    a = BMAAggregator(n_min_observations=2, prior_alpha=1.0, prior_beta=1.0)
    a.calibrator = ColdStartCalibrator()
    # Four correct calls in Jan 2026, observable ~Jan (horizon 1d).
    for i in range(4):
        _settle(a, "a", asof=f"2026-01-{1 + i:02d}", correct=True)

    # Evaluate the weight as of a decision FAR in the future (Dec 2026): the Jan
    # evidence has decayed heavily, so the weight is well below the undecayed
    # 5/6 and closer to the prior mean (0.5).
    decision = pd.Timestamp("2026-12-01", tz="UTC")
    w = a._weight_for("a", decision_asof=decision)
    assert w < 5.0 / 6.0
    assert w < 0.7  # decayed substantially toward the 0.5 prior mean


def test_decay_is_asof_honest_no_future_samples(monkeypatch):
    """Flag ON: a settled sample whose outcome is observable AFTER the decision
    asof must NOT raise the analyst's weight at that decision. Evaluating the
    weight just before the sample is observable excludes it entirely."""
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_DECAY", "1")
    a = BMAAggregator(n_min_observations=1, prior_alpha=1.0, prior_beta=1.0)
    a.calibrator = ColdStartCalibrator()
    # One correct call decided 2026-06-01, horizon 1d -> observable 2026-06-02.
    _settle(a, "a", asof="2026-06-01", correct=True)

    # Decision BEFORE the sample becomes observable: the sample is excluded, so
    # the weight is exactly the prior mean (1/(1+1) = 0.5), NOT inflated by the
    # not-yet-knowable correct call.
    before = pd.Timestamp("2026-06-01T12:00:00Z")  # < observable 2026-06-02
    assert a._weight_for("a", decision_asof=before) == pytest.approx(0.5)

    # Decision AFTER it becomes observable: now the correct call counts.
    after = pd.Timestamp("2026-06-10", tz="UTC")
    assert a._weight_for("a", decision_asof=after) > 0.5
