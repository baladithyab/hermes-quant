"""57f6 — lesson haircut wired into BMA's default (non-LLM) decision path.

With HERMES_QUANT_L2_LESSON_HAIRCUT=1 and an injected loss-lesson provider, the
BMA aggregate confidence is reduced when a recent same-symbol same-direction
loss exists. This closes the reflection->decision loop on the DETERMINISTIC path
(not the optional LLM-committee prompt). With the flag off OR no provider OR no
matching lesson, BMA is byte-identical to today.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.learning.lesson_haircut import LossLesson
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx(asof: str = "2026-06-01") -> MarketContext:
    ts = pd.date_range("2026-05-31", periods=2, freq="1h")
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
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange="nasdaq",
        bars=bars,
        last_close=101.5,
        last_volume=1000.0,
        asof=pd.Timestamp(asof, tz="UTC"),
    )


def _views(direction: int = 1) -> list[AnalystView]:
    return [
        AnalystView(analyst="classical-ta", direction=direction, magnitude=0.01,
                    confidence=0.7, confidence_raw=0.85, horizon="1d"),
        AnalystView(analyst="kronos", direction=direction, magnitude=0.012,
                    confidence=0.8, confidence_raw=0.9, horizon="1d"),
    ]


class _FakeProvider:
    """In-memory loss-lesson provider for offline tests."""

    def __init__(self, lessons: list[LossLesson]):
        self._lessons = lessons
        self.calls: list[tuple[str, pd.Timestamp]] = []

    def recent_loss_lessons(self, ticker: str, asof: pd.Timestamp) -> list[LossLesson]:
        self.calls.append((ticker, asof))
        return list(self._lessons)


def _loss(lesson_id: str, ticker: str, direction: int, observable: str) -> LossLesson:
    return LossLesson(
        lesson_id=lesson_id, ticker=ticker, direction=direction,
        tau_observable=pd.Timestamp(observable, tz="UTC"), alpha_return=-0.05,
    )


def _baseline_confidence() -> float:
    """Confidence with no haircut path at all (flag off, no provider)."""
    a = BMAAggregator()
    a.calibrator = ColdStartCalibrator()
    return a.aggregate(_views(1), _ctx()).confidence


def test_matching_loss_lessons_haircut_default_path(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_L2_LESSON_HAIRCUT", "1")
    provider = _FakeProvider([_loss("l1", "AAPL", 1, "2026-05-20")])
    a = BMAAggregator(loss_lesson_provider=provider)
    a.calibrator = ColdStartCalibrator()

    sig = a.aggregate(_views(1), _ctx())
    assert sig.confidence < _baseline_confidence()
    # Audit metadata records that a haircut was applied and how many lessons.
    assert sig.metadata["lesson_haircut_applied"] is True
    assert sig.metadata["lesson_haircut_n_lessons"] == 1
    # The provider was queried with the decision's ticker and asof (asof-honest).
    assert provider.calls and provider.calls[0][0] == "AAPL"


def test_no_matching_lesson_is_noop(monkeypatch):
    """Flag on + provider present, but the lesson is a different direction:
    confidence equals the no-haircut baseline and no haircut is recorded."""
    monkeypatch.setenv("HERMES_QUANT_L2_LESSON_HAIRCUT", "1")
    provider = _FakeProvider([_loss("l1", "AAPL", -1, "2026-05-20")])  # short loss
    a = BMAAggregator(loss_lesson_provider=provider)
    a.calibrator = ColdStartCalibrator()

    sig = a.aggregate(_views(1), _ctx())  # we are going LONG
    assert sig.confidence == _baseline_confidence()
    assert sig.metadata["lesson_haircut_applied"] is False


def test_future_lesson_does_not_haircut(monkeypatch):
    """asof-honesty at the BMA seam: a loss observable after the decision asof
    must not reduce confidence."""
    monkeypatch.setenv("HERMES_QUANT_L2_LESSON_HAIRCUT", "1")
    provider = _FakeProvider([_loss("l1", "AAPL", 1, "2026-07-01")])  # future
    a = BMAAggregator(loss_lesson_provider=provider)
    a.calibrator = ColdStartCalibrator()

    sig = a.aggregate(_views(1), _ctx("2026-06-01"))
    assert sig.confidence == _baseline_confidence()
    assert sig.metadata["lesson_haircut_applied"] is False


def test_flag_off_is_byte_identical(monkeypatch):
    """Flag OFF even with a provider present: no haircut, no metadata key."""
    monkeypatch.delenv("HERMES_QUANT_L2_LESSON_HAIRCUT", raising=False)
    provider = _FakeProvider([_loss("l1", "AAPL", 1, "2026-05-20")])
    a = BMAAggregator(loss_lesson_provider=provider)
    a.calibrator = ColdStartCalibrator()

    sig = a.aggregate(_views(1), _ctx())
    assert sig.confidence == _baseline_confidence()
    assert "lesson_haircut_applied" not in sig.metadata
    assert provider.calls == []  # provider never even consulted while flag off


def test_no_provider_is_noop(monkeypatch):
    """Flag on but no provider injected: graceful no-op, no crash."""
    monkeypatch.setenv("HERMES_QUANT_L2_LESSON_HAIRCUT", "1")
    a = BMAAggregator()  # no provider
    a.calibrator = ColdStartCalibrator()
    sig = a.aggregate(_views(1), _ctx())
    assert sig.confidence == _baseline_confidence()


def test_provider_exception_is_swallowed(monkeypatch):
    """A provider that raises must not break the decision path (silence-by-
    default): the haircut is skipped, confidence is the baseline."""
    monkeypatch.setenv("HERMES_QUANT_L2_LESSON_HAIRCUT", "1")

    class _Boom:
        def recent_loss_lessons(self, ticker, asof):
            raise RuntimeError("reflections.jsonl unreadable")

    a = BMAAggregator(loss_lesson_provider=_Boom())
    a.calibrator = ColdStartCalibrator()
    sig = a.aggregate(_views(1), _ctx())
    assert sig.confidence == _baseline_confidence()
