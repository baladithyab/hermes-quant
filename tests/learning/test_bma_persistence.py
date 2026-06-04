"""c96e — BMA persists per-analyst Beta posteriors across recommend() lifecycles.

Today ``advisor.recommend()`` builds a fresh ``BMAAggregator()`` each call, so
all learned per-analyst skill (the ``_stats`` Beta posteriors) resets to the
prior every time. This wires durable persistence: behind a default-OFF flag,
``update()`` saves posteriors atomically and a NEW aggregator loads them at
construction, so skill survives the recommend() lifecycle.

The hard invariant: with the flag OFF, BMA is byte-identical to today (no load,
no save, no new file). Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.learning import posterior_store
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


def _view(name: str, direction: int, conf: float = 0.7) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.01,
        confidence=conf,
        confidence_raw=0.85,
        horizon="1h",
    )


def _episode(sig, *, asof: str, correct: dict[str, bool]) -> EpisodeOutcome:
    return EpisodeOutcome(
        asset="BTC/USDT",
        timeframe="1h",
        asof=pd.Timestamp(asof),
        aggregated_signal=sig,
        realized_returns={"1h": 0.01},
        direction_correct=correct,
    )


def test_flag_off_does_not_persist(tmp_path, monkeypatch):
    """Default-OFF: update() writes no file and a fresh aggregator starts cold."""
    path = tmp_path / "p.json"
    # Flag unset (the autouse flag-isolation fixture guarantees a clean slate).
    monkeypatch.delenv("HERMES_QUANT_L2_POSTERIOR_PERSIST", raising=False)

    a = BMAAggregator(posterior_store_path=path)
    a.calibrator = ColdStartCalibrator()
    sig = a.aggregate([_view("a", 1)], _ctx())
    a.update(_episode(sig, asof="2026-05-13", correct={"a": True}))

    assert not path.exists()  # nothing persisted while flag is off


def test_flag_on_persists_and_reloads_across_aggregators(tmp_path, monkeypatch):
    """With the flag on, update() saves; a NEW aggregator loads the evolved
    posterior instead of resetting to the prior. This is the c96e fix."""
    path = tmp_path / "p.json"
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_PERSIST", "1")

    a1 = BMAAggregator(posterior_store_path=path)
    a1.calibrator = ColdStartCalibrator()
    sig = a1.aggregate([_view("a", 1)], _ctx())
    # Two correct settlements for analyst 'a'.
    a1.update(_episode(sig, asof="2026-05-13", correct={"a": True}))
    a1.update(_episode(sig, asof="2026-05-14", correct={"a": True}))

    assert path.exists()
    evolved_alpha = a1._stats["a"].alpha
    assert evolved_alpha == pytest.approx(a1.prior_alpha + 2.0)

    # A brand-new aggregator (simulating the next recommend()) must START from
    # the persisted posterior, not the cold prior.
    a2 = BMAAggregator(posterior_store_path=path)
    assert "a" in a2._stats
    assert a2._stats["a"].alpha == pytest.approx(evolved_alpha)
    assert a2._stats["a"].beta == pytest.approx(a1._stats["a"].beta)
    assert a2._stats["a"].n_observations == 2


def test_flag_on_cold_start_when_no_file(tmp_path, monkeypatch):
    """Flag on but no persisted file yet → empty _stats, no crash (cold-start)."""
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_PERSIST", "1")
    a = BMAAggregator(posterior_store_path=tmp_path / "absent.json")
    assert a._stats == {}


def test_persisted_posterior_feeds_weight(tmp_path, monkeypatch):
    """The reloaded posterior actually drives _weight_for — proving persistence
    closes the loop into the decision, not just the cache."""
    path = tmp_path / "p.json"
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_PERSIST", "1")

    # Seed a strong track record (well past n_min_observations) for analyst 'a'.
    a1 = BMAAggregator(posterior_store_path=path, n_min_observations=5)
    a1.calibrator = ColdStartCalibrator()
    sig = a1.aggregate([_view("a", 1)], _ctx())
    for i in range(8):
        a1.update(_episode(sig, asof=f"2026-05-{13 + i:02d}", correct={"a": True}))

    a2 = BMAAggregator(posterior_store_path=path, n_min_observations=5)
    w = a2._weight_for("a")
    # 8 correct + prior -> posterior accuracy clearly above the 0.5 uniform proxy.
    assert w > 0.6
