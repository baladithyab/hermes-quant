"""d97e — BMA persists the ag03 hierarchical pooler state across restarts.

The ag03 pooler accumulates per-(analyst, regime, epoch) correctness cells whose
effective-n drives the headline warm-up band. Today those cells live ONLY in the
in-memory ``BMAAggregator._pooler``, so a cron-mode restart silently resets the
whole warm-up state to cold. This wires durable persistence behind the EXISTING
default-OFF HERMES_QUANT_HIERARCHICAL_POOLING flag: ``update()`` saves the pooler
state when the flag is on and a NEW aggregator loads it at construction.

The hard invariant: with the flag OFF, BMA is byte-identical to today — no load,
no save, no new file. Pure-Python, offline, deterministic; every assertion RED-proven.
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


def test_flag_off_does_not_persist_pooler(tmp_path, monkeypatch):
    """Default-OFF: update() writes no pooler file and a fresh aggregator is cold."""
    path = tmp_path / "pooler.json"
    monkeypatch.delenv("HERMES_QUANT_HIERARCHICAL_POOLING", raising=False)

    a = BMAAggregator(pooler_store_path=path)
    a.calibrator = ColdStartCalibrator()
    sig = a.aggregate([_view("a", 1), _view("b", 1)], _ctx())
    a.update(_episode(sig, asof="2026-05-13", correct={"a": True, "b": True}))

    assert not path.exists()  # nothing persisted while flag is off


def test_flag_on_persists_pooler_and_survives_restart(tmp_path, monkeypatch):
    """With the flag on, update() saves the pooler; a NEW aggregator loads the
    effective-n / warm-up state instead of resetting to cold. The d97e fix."""
    path = tmp_path / "pooler.json"
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")

    a1 = BMAAggregator(pooler_store_path=path)
    a1.calibrator = ColdStartCalibrator()
    sig = a1.aggregate([_view("a", 1), _view("b", 1)], _ctx())
    a1.update(_episode(sig, asof="2026-05-13", correct={"a": True, "b": False}))
    a1.update(_episode(sig, asof="2026-05-14", correct={"a": True, "b": True}))
    a1.update(_episode(sig, asof="2026-05-15", correct={"a": False, "b": True}))

    assert path.exists()
    # analyst 'a' has 3 settled samples in the 'unknown' regime cell.
    diag_before = a1._pooler.cell_diagnostics("a", "unknown", epoch="")
    assert diag_before["effective_n"] == pytest.approx(3.0)

    # A brand-new aggregator (simulating a cron-mode process restart) must START
    # from the persisted pooler cells, not an empty cold pooler.
    a2 = BMAAggregator(pooler_store_path=path)
    assert ("a", "unknown", "") in a2._pooler._cells
    diag_after = a2._pooler.cell_diagnostics("a", "unknown", epoch="")
    assert diag_after["effective_n"] == pytest.approx(3.0)
    # The headline warm-up band survives the restart (3 < warmup_n=30).
    assert a2.status()["hierarchical_pooling"]["headline_in_warmup"] is True
    assert (
        a2.status()["hierarchical_pooling"]["cells"]
        == a1.status()["hierarchical_pooling"]["cells"]
    )


def test_flag_on_cold_start_when_no_pooler_file(tmp_path, monkeypatch):
    """Flag on but no persisted file yet → empty pooler, no crash (cold-start)."""
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")
    a = BMAAggregator(pooler_store_path=tmp_path / "absent.json")
    assert a._pooler._cells == {}
