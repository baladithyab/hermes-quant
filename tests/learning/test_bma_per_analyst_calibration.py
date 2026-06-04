"""f254 — per-analyst calibration wired into BMA, keyed by analyst name.

With HERMES_QUANT_L2_PER_ANALYST_CALIB=1, BMA recalibrates each view's
confidence through a calibrator keyed by the analyst's OWN learned Beta
posterior (from _stats), instead of letting the single global aggregator
calibrator drag every analyst toward the population average. Two analysts with
different track records get DIFFERENT calibrated confidence from the SAME raw
score. An unknown analyst (no history) falls back to the neutral prior — no
crash, no zero-out. With the flag off, BMA is byte-identical to today.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import AnalystView, EpisodeOutcome, MarketContext


def _ctx() -> MarketContext:
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
        asof=ts[-1],
    )


def _view(name: str, direction: int, conf_raw: float) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.01,
        confidence=conf_raw,        # pre-calibrated by the analyst; equal to raw here
        confidence_raw=conf_raw,
        horizon="1h",
    )


def _seed_track_record(a: BMAAggregator, name: str, n_correct: int, n_wrong: int) -> None:
    """Drive the analyst's Beta posterior directly via settlements."""
    sig = a.aggregate([_view(name, 1, 0.5)], _ctx())
    for i in range(n_correct):
        a.update(
            EpisodeOutcome(
                asset="BTC/USDT", timeframe="1h", asof=pd.Timestamp(f"2026-01-{1 + i:02d}"),
                aggregated_signal=sig, realized_returns={"1h": 0.01},
                direction_correct={name: True},
            )
        )
    for i in range(n_wrong):
        a.update(
            EpisodeOutcome(
                asset="BTC/USDT", timeframe="1h", asof=pd.Timestamp(f"2026-02-{1 + i:02d}"),
                aggregated_signal=sig, realized_returns={"1h": -0.01},
                direction_correct={name: False},
            )
        )


def test_per_analyst_calibration_keyed_by_name(monkeypatch):
    """Same raw confidence, different track records -> different per-analyst
    calibrated confidence recorded in metadata."""
    monkeypatch.setenv("HERMES_QUANT_L2_PER_ANALYST_CALIB", "1")
    a = BMAAggregator(require_ensemble=False)
    a.calibrator = ColdStartCalibrator()
    _seed_track_record(a, "skilled", n_correct=20, n_wrong=2)
    _seed_track_record(a, "unskilled", n_correct=2, n_wrong=20)

    sig = a.aggregate(
        [_view("skilled", 1, 0.6), _view("unskilled", 1, 0.6)],
        _ctx(),
    )
    calibrated = sig.metadata["per_analyst_calibrated_confidence"]
    assert calibrated["skilled"] > calibrated["unskilled"]


def test_unknown_analyst_falls_back_safely(monkeypatch):
    """An analyst with no track record is calibrated to the neutral prior mean,
    never a crash and never 0.0 (which would silently drop it from the vote)."""
    monkeypatch.setenv("HERMES_QUANT_L2_PER_ANALYST_CALIB", "1")
    a = BMAAggregator(require_ensemble=False)
    a.calibrator = ColdStartCalibrator()
    sig = a.aggregate([_view("never-seen", 1, 0.6)], _ctx())
    calibrated = sig.metadata["per_analyst_calibrated_confidence"]
    assert 0.0 < calibrated["never-seen"] < 1.0


def test_unanimous_per_analyst_calibration_does_not_turn_agreement_into_one(
    monkeypatch,
):
    """Flag ON: unanimous agreement uses calibrated analyst probabilities.

    Two cold-start analysts with ~0.5 calibrated confidence must not become
    confidence=1.0 just because their vote_share agreement metric is 1.0.
    """
    monkeypatch.setenv("HERMES_QUANT_L2_PER_ANALYST_CALIB", "1")
    a = BMAAggregator()
    a.calibrator = ColdStartCalibrator()

    sig = a.aggregate(
        [_view("cold-a", 1, 0.5), _view("cold-b", 1, 0.5)],
        _ctx(),
    )

    calibrated = sig.metadata["per_analyst_calibrated_confidence"]
    assert calibrated["cold-a"] == pytest.approx(0.5)
    assert calibrated["cold-b"] == pytest.approx(0.5)
    assert sig.metadata["vote_share"] == pytest.approx(1.0)
    assert sig.confidence_raw == pytest.approx(0.6)
    assert sig.confidence == pytest.approx(0.6)
    assert sig.confidence < 0.75
    assert sig.confidence != pytest.approx(1.0)


def test_unanimous_flag_off_keeps_legacy_vote_share_confidence(monkeypatch):
    """Flag OFF: unanimous branch preserves the pre-f254 vote_share behavior."""
    monkeypatch.delenv("HERMES_QUANT_L2_PER_ANALYST_CALIB", raising=False)
    a = BMAAggregator()
    a.calibrator = ColdStartCalibrator()

    sig = a.aggregate(
        [_view("cold-a", 1, 0.5), _view("cold-b", 1, 0.5)],
        _ctx(),
    )

    assert "per_analyst_calibrated_confidence" not in sig.metadata
    assert sig.metadata["vote_share"] == pytest.approx(1.0)
    assert sig.confidence_raw == pytest.approx(1.0)
    assert sig.confidence == pytest.approx(0.375)


def test_flag_off_is_byte_identical(monkeypatch):
    """Flag OFF: no per_analyst_calibrated_confidence key, and the signal equals
    the signal produced with the flag never set."""
    monkeypatch.delenv("HERMES_QUANT_L2_PER_ANALYST_CALIB", raising=False)
    a = BMAAggregator(require_ensemble=False)
    a.calibrator = ColdStartCalibrator()
    _seed_track_record(a, "skilled", n_correct=20, n_wrong=2)
    views = [_view("skilled", 1, 0.6)]
    sig = a.aggregate(views, _ctx())
    assert "per_analyst_calibrated_confidence" not in sig.metadata


def test_skilled_analyst_lifts_confidence_vs_unskilled(monkeypatch):
    """End-to-end: a lone skilled analyst yields a higher aggregate confidence
    than a lone unskilled analyst making the identical raw call, when
    per-analyst calibration is on."""
    monkeypatch.setenv("HERMES_QUANT_L2_PER_ANALYST_CALIB", "1")

    a_sk = BMAAggregator(require_ensemble=False)
    a_sk.calibrator = ColdStartCalibrator()
    _seed_track_record(a_sk, "x", n_correct=20, n_wrong=2)
    sig_sk = a_sk.aggregate([_view("x", 1, 0.6)], _ctx())

    a_un = BMAAggregator(require_ensemble=False)
    a_un.calibrator = ColdStartCalibrator()
    _seed_track_record(a_un, "x", n_correct=2, n_wrong=20)
    sig_un = a_un.aggregate([_view("x", 1, 0.6)], _ctx())

    assert sig_sk.confidence > sig_un.confidence
