"""Regression — a NaT (unknown) observability timestamp must be EXCLUDED.

Adversarial review found that `NaT >= decision_asof` is always False, so a guard
written as `if observable >= decision_asof: continue` SILENTLY ADMITS a sample
whose outcome time is unknown — and the recency weight degenerates to 1.0 (max).
A signal-bus row with a missing/None/empty 'asof' can produce such a NaT through
settlement. The conservative, asof-honest reading is: unknown observability =
not yet observable = exclude. These tests pin that across all three guards.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.learning.lesson_haircut import LossLesson, apply_lesson_haircut
from hermes_quant.learning.posterior_refit import SkillSample, refit_beta
from hermes_quant.protocol import AnalystView, EpisodeOutcome, MarketContext


DECISION = pd.Timestamp("2020-01-01", tz="UTC")  # far before any plausible sample


def test_refit_excludes_nat_observable_sample():
    """A NaT-observable correct sample must NOT raise alpha — it is unknown, not
    'observable now'."""
    alpha, beta = refit_beta([SkillSample(pd.NaT, True)], DECISION, 5.0, 5.0, 30.0)
    assert (alpha, beta) == (5.0, 5.0)  # prior, untouched


def test_haircut_excludes_nat_lesson():
    lessons = [LossLesson("l1", "AAPL", 1, pd.NaT, -0.05)]
    out = apply_lesson_haircut(0.8, "AAPL", 1, DECISION, lessons, 0.15, 0.5)
    assert out == 0.8  # no-op


def test_count_excludes_nat_lesson():
    a = BMAAggregator(loss_lesson_provider=None)
    lessons = [LossLesson("l1", "AAPL", 1, pd.NaT, -0.05)]
    assert a._count_matching_lessons(lessons, "AAPL", 1, DECISION) == 0


def test_nat_decision_asof_haircut_is_noop():
    """A NaT *decision* asof (poisoned context) must not haircut either — we
    cannot know any lesson is in the past relative to an unknown decision time."""
    lessons = [LossLesson("l1", "AAPL", 1, pd.Timestamp("2019-01-01", tz="UTC"), -0.05)]
    out = apply_lesson_haircut(0.8, "AAPL", 1, pd.NaT, lessons, 0.15, 0.5)
    assert out == 0.8


def _ctx(asof) -> MarketContext:
    ts = pd.date_range("2026-05-13", periods=2, freq="1h")
    bars = pd.DataFrame(
        {"timestamp": ts, "open": [100.0, 101.0], "high": [101.0, 102.0],
         "low": [99.0, 100.0], "close": [100.5, 101.5], "volume": [1000.0, 1000.0]}
    )
    return MarketContext(
        asset="BTC/USDT", timeframe="1h", asset_class="crypto", exchange="binance",
        bars=bars, last_close=101.5, last_volume=1000.0, asof=asof,
    )


def test_bma_update_with_nat_episode_asof_does_not_leak(monkeypatch):
    """End-to-end: settling an episode whose asof is NaT must not let that
    sample lift the analyst's decayed weight at a far-past decision."""
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_DECAY", "1")
    a = BMAAggregator(n_min_observations=1, prior_alpha=1.0, prior_beta=1.0)
    a.calibrator = ColdStartCalibrator()
    sig = a.aggregate(
        [AnalystView(analyst="a", direction=1, magnitude=0.01, confidence=0.7,
                     confidence_raw=0.85, horizon="1d")],
        _ctx(pd.Timestamp("2026-05-13")),
    )
    # Poisoned settlement: NaT asof.
    a.update(
        EpisodeOutcome(
            asset="BTC/USDT", timeframe="1h", asof=pd.NaT,
            aggregated_signal=sig, realized_returns={"1d": 0.01},
            direction_correct={"a": True},
        )
    )
    # At a far-past decision, the NaT-stamped correct sample must NOT raise the
    # weight above the 0.5 prior mean.
    w = a._weight_for("a", decision_asof=pd.Timestamp("2020-01-01", tz="UTC"))
    assert w == pytest.approx(0.5)
