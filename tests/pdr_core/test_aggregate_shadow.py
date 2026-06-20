"""ADR-0092 Phase-4 (parity proof) — the AGGREGATE-layer runtime SHADOW.

The DECIDE-layer gate has a runtime shadow (run_shadow_gate, proven in
tests/test_pdr_core_adapter_shadow.py). The AGGREGATE layer
(pdr_core.aggregate.core_aggregate) had only a STATIC parity test
(test_aggregate_parity.py) and ZERO live exercise. This file is the safety proof
for the analogous AGGREGATE runtime shadow (run_shadow_aggregate), mirroring the
gate-shadow gold standard EXACTLY:

  * The live BMA's emitted AggregatedSignal continues to drive the decision
    UNCHANGED. run_shadow_aggregate NEVER mutates it.
  * A new flag HERMES_QUANT_PDR_CORE_AGG_SHADOW (default-OFF) ADDITIONALLY runs
    the ported core aggregator in parallel, compares field-by-field, LOGS
    divergence, and best-effort appends a JSONL divergence line.
  * Flag-OFF must be BYTE-IDENTICAL to today (the core aggregator is not even
    constructed; no shadow import is paid).
  * core_aggregate ports ONLY the FLAGS-OFF / COLD-START arm — a fitted
    calibrator OR a set learning flag is NOT-COMPARABLE, never a port-bug
    divergence.
  * If anything inside the shadow raises, it swallows it and returns None.

The cold-start parity driver is lifted from test_aggregate_parity.py: a FRESH
BMAAggregator forced onto ColdStartCalibrator via a guaranteed-nonexistent
calibrator path, with every learning flag unset.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.pdr_core_adapter import (
    _AGG_LEARNING_FLAGS,
    PDR_CORE_AGG_SHADOW_FLAG,
    _compare_signals,
    run_shadow_aggregate,
)
from hermes_quant.protocol import AnalystView as LiveView
from hermes_quant.protocol import MarketContext

ASOF = "2026-06-12T15:00:00+00:00"
BAR = "2026-06-12T14:59:00+00:00"


# ---------------------------------------------------------------------------
# Parity driver — lifted from test_aggregate_parity.py.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    for f in _AGG_LEARNING_FLAGS:
        monkeypatch.delenv(f, raising=False)
    yield


def _live_aggregator(tmp_path, **kwargs) -> BMAAggregator:
    """A FRESH BMAAggregator forced onto ColdStartCalibrator (no update() calls)."""
    nonexistent = tmp_path / "no_such_isotonic.pkl"
    assert not nonexistent.exists()
    agg = BMAAggregator(calibrator_path=nonexistent, **kwargs)
    assert type(agg.calibrator).__name__ == "ColdStartCalibrator"
    return agg


def _live_ctx() -> MarketContext:
    bars = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(BAR)],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1000.0],
        }
    )
    return MarketContext(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=100.5,
        last_volume=1000.0,
        asof=pd.Timestamp(ASOF),
    )


def _live_view(analyst, direction, magnitude, confidence, confidence_raw, horizon):
    return LiveView(
        analyst=analyst,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence_raw,
        horizon=horizon,
    )


# Fixtures lifted from the static parity grid (a representative subset hitting the
# silenced-single-source, unanimous-multi, dissent, and multi-horizon branches).
FIXTURES: dict[str, list[tuple]] = {
    "lone_long_silenced": [("k", 1, 0.5, 0.7, 0.6, "1d")],
    "two_unanimous_long": [
        ("a", 1, 0.4, 0.8, 0.7, "1d"),
        ("b", 1, 0.8, 0.6, 0.5, "1d"),
    ],
    "dissent_long_wins": [
        ("a", 1, 0.6, 0.8, 0.7, "1d"),
        ("b", -1, 0.2, 0.4, 0.3, "1d"),
    ],
    "multi_horizon_all_agree": [
        ("a", 1, 0.4, 0.9, 0.8, "1d"),
        ("b", 1, 0.6, 0.9, 0.8, "1w"),
    ],
    "multi_horizon_mixed": [
        ("a", 1, 0.6, 0.9, 0.8, "1d"),
        ("b", -1, 0.2, 0.3, 0.2, "1w"),
    ],
}


# ===========================================================================
# SECTION 1 — the agreement / not-comparable / fail-closed contract.
# ===========================================================================


def test_run_shadow_aggregate_agreement_fresh_cold_start(tmp_path, caplog):
    """A fresh cold-start BMA + the core port AGREE: comparable, not diverged,
    and (with persist=True + a tmp divergence_path) one JSONL line is written."""
    agg = _live_aggregator(tmp_path)
    rows = FIXTURES["two_unanimous_long"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    log_path = tmp_path / "agg-divergence.jsonl"
    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        report = run_shadow_aggregate(
            views=views,
            ctx=ctx,
            aggregator=agg,
            live_signal=live_sig,
            persist=True,
            divergence_path=log_path,
        )
    assert report is not None
    assert report["comparable"] is True, report
    assert report["diverged"] is False, report
    assert report["fields"] == []
    # No WARNING on agreement.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Exactly one JSONL line persisted.
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    # The live signal is untouched (the shadow never mutates it).
    assert live_sig is report["live"]


@pytest.mark.parametrize("name", list(FIXTURES))
@pytest.mark.parametrize("require_ensemble", [True, False])
def test_run_shadow_aggregate_agrees_across_fixture_grid(name, require_ensemble, tmp_path):
    """Across the lifted fixture grid (unanimous / dissent / multi-horizon /
    silenced-single-source), under both require_ensemble polarities, the live BMA
    and the core port agree — zero divergence, threading the live config."""
    agg = _live_aggregator(tmp_path, require_ensemble=require_ensemble)
    rows = FIXTURES[name]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    report = run_shadow_aggregate(
        views=views,
        ctx=ctx,
        aggregator=agg,
        live_signal=live_sig,
        persist=False,
    )
    assert report is not None
    assert report["comparable"] is True, report
    assert report["diverged"] is False, (
        f"{name}/require_ensemble={require_ensemble} diverged: {report['fields']}"
    )


def test_run_shadow_aggregate_silenced_single_source_agreement(tmp_path):
    """The silenced-single-source path (require_ensemble=True, one analyst) is a
    silence/flat metadata case — the core and live agree on the silence reason."""
    agg = _live_aggregator(tmp_path, require_ensemble=True)
    rows = FIXTURES["lone_long_silenced"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)
    # sanity: the live path actually silenced (direction 0).
    assert live_sig.direction == 0

    report = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
    )
    assert report is not None
    assert report["comparable"] is True
    assert report["diverged"] is False, report


def test_run_shadow_aggregate_not_comparable_when_learning_flag_set(tmp_path, monkeypatch, caplog):
    """A set learning flag (HERMES_QUANT_STACKING) => not-comparable, NO divergence
    flagged (the live path diverges from the cold-start port BY DESIGN)."""
    agg = _live_aggregator(tmp_path)
    rows = FIXTURES["two_unanimous_long"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        report = run_shadow_aggregate(
            views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
        )
    assert report is not None
    assert report["comparable"] is False
    assert report["reason"] == "learning_flag_active"
    assert report["flag"] == "HERMES_QUANT_STACKING"
    assert report["diverged"] is False
    # No WARNING — a skipped comparison is not a divergence.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_run_shadow_aggregate_not_comparable_when_fitted_calibrator_active(tmp_path):
    """A non-cold-start (fitted) calibrator => not-comparable. We stub the
    aggregator's calibrator with a non-ColdStartCalibrator type."""
    agg = _live_aggregator(tmp_path)
    rows = FIXTURES["two_unanimous_long"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    class _FittedCalibrator:  # not named ColdStartCalibrator
        def calibrate(self, raw):
            return raw

    agg.calibrator = _FittedCalibrator()
    report = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
    )
    assert report is not None
    assert report["comparable"] is False
    assert report["reason"] == "fitted_calibrator_active"
    assert report["diverged"] is False


def test_run_shadow_aggregate_swallows_exceptions_and_returns_none(caplog):
    """If anything inside the shadow raises, run_shadow_aggregate swallows it and
    returns None — never re-raises. aggregator=None makes the calibrator probe and
    downstream core call fail; ctx with no attributes makes the build raise."""
    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        # aggregator=None: getattr(None, 'calibrator') is None ->
        # type(None).__name__ != 'ColdStartCalibrator' -> not-comparable, NO raise.
        # To force a genuine raise we feed a ctx object that explodes on .asset
        # AND an aggregator whose calibrator IS cold-start so we pass the gate.
        class ColdStartCalibrator:  # must match the gate's __name__ check
            pass

        class _Agg:
            calibrator = ColdStartCalibrator()
            require_ensemble = True
            agreement_bonus = 0.10
            horizon_weights = {"1d": 1.0}

        class _ExplodingCtx:
            @property
            def asset(self):
                raise RuntimeError("boom")

        report = run_shadow_aggregate(
            views=[],
            ctx=_ExplodingCtx(),
            aggregator=_Agg(),
            live_signal=None,
            persist=False,
        )
    assert report is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===========================================================================
# SECTION 2 — RED-PROOF: the comparator actually catches divergence.
# ===========================================================================


def test_compare_signals_catches_flipped_direction(tmp_path):
    """RED-PROOF (comparator): two signals that differ on direction MUST be
    flagged. Proves _compare_signals is not vacuously passing."""
    agg = _live_aggregator(tmp_path)
    rows = FIXTURES["two_unanimous_long"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    # A deliberately divergent shadow signal: flip the direction.
    from dataclasses import replace

    bogus = replace(live_sig, direction=-1)
    diverged = _compare_signals(live_sig, bogus)
    assert "direction" in diverged, diverged


def test_run_shadow_aggregate_red_proof_monkeypatched_core(tmp_path, monkeypatch, caplog):
    """RED-PROOF (end-to-end): monkeypatch core_aggregate so the shadow signal has
    a flipped direction; the SAME faithful input now flips to diverged=True with
    'direction' named, and a WARNING fires. Proves the agreement path is a real
    signal, not vacuous."""
    import hermes_quant.pdr_core.aggregate as core_agg_mod

    agg = _live_aggregator(tmp_path)
    rows = FIXTURES["two_unanimous_long"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    # Sanity: the faithful run agrees (the agreement path is reachable).
    faithful = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
    )
    assert faithful is not None and faithful["diverged"] is False, faithful

    real_core = core_agg_mod.core_aggregate

    def _flipping_core(core_views, core_ctx, **kwargs):
        from dataclasses import replace

        sig = real_core(core_views, core_ctx, **kwargs)
        flipped = -1 if sig.direction >= 0 else 1
        return replace(sig, direction=flipped)

    # The adapter imports core_aggregate lazily from the module, so patch the
    # module attribute.
    monkeypatch.setattr(core_agg_mod, "core_aggregate", _flipping_core)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        broken = run_shadow_aggregate(
            views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
        )
    assert broken is not None
    assert broken["comparable"] is True
    assert broken["diverged"] is True, broken
    assert "direction" in broken["fields"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===========================================================================
# SECTION 3 — the live recommend() flag-OFF no-op seam.
# ===========================================================================


def _make_bars(n: int = 120, *, base: float = 100.0, trend: float = 0.5, seed: int = 7):
    import numpy as np

    rng = np.random.default_rng(seed=seed)
    timestamps = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = base + np.arange(n) * trend + rng.normal(0, 0.5, n)
    opens = closes - rng.uniform(0, 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, 0.4, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _CannedProvider:
    name = "canned"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache: bool = True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of
            if cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _recommend_kwargs(provider):
    return dict(
        symbol="TEST",
        asset_class="equity",
        as_of="2026-03-15T00:00:00Z",
        provider=provider,
        include_lessons=False,
    )


def test_recommend_flag_off_does_not_run_aggregate_shadow(monkeypatch):
    """Flag-OFF NON-VACUITY: the aggregate shadow runner is NEVER called flag-off.

    Install an OBSERVABLE spy on adapter.run_shadow_aggregate that appends to an
    external list AND raises (the swallow-all seam would hide a plain raise). With
    the flag UNSET and "0", the spy list stays EMPTY; with "1" (control, same
    test) it fires — proving the spy WOULD have caught the seam had it run."""
    import hermes_quant.pdr_core_adapter as adapter
    from hermes_quant.advisor import recommend

    bars = _make_bars()
    called: list[object] = []

    def _spy(*args, **kwargs):
        called.append(("aggregate_shadow_ran", kwargs))
        raise RuntimeError("aggregate shadow poisoned — must be swallowed flag-on")

    monkeypatch.setattr(adapter, "run_shadow_aggregate", _spy)

    # Flag UNSET: dark branch, spy untouched, recommend() succeeds.
    monkeypatch.delenv(PDR_CORE_AGG_SHADOW_FLAG, raising=False)
    out = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out["risk_gate"] is not None
    assert called == [], "aggregate shadow ran while the flag was UNSET"

    # Flag "0": still dark.
    monkeypatch.setenv(PDR_CORE_AGG_SHADOW_FLAG, "0")
    out_zero = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out_zero["risk_gate"] is not None
    assert called == [], "aggregate shadow ran while the flag was '0'"

    # Control — flag "1": the spy DOES fire (its raise is swallowed, live
    # unaffected). Proves the spy is live, so the empty list above is genuine.
    monkeypatch.setenv(PDR_CORE_AGG_SHADOW_FLAG, "1")
    out_on = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out_on["risk_gate"] is not None  # poison swallowed
    assert called, "control: flag-on must reach + call the aggregate shadow"


def test_recommend_flag_off_byte_identical_aggregated_signal(monkeypatch):
    """Flag-OFF: result.aggregated_signal is identical whether the flag is UNSET
    or '0' — the shadow seam is a pure no-op on the live decision."""
    from hermes_quant.advisor import recommend

    bars = _make_bars()

    monkeypatch.delenv(PDR_CORE_AGG_SHADOW_FLAG, raising=False)
    unset = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    monkeypatch.setenv(PDR_CORE_AGG_SHADOW_FLAG, "0")
    explicit_zero = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    assert unset["aggregated_signal"] == explicit_zero["aggregated_signal"]
    assert unset["risk_gate"] == explicit_zero["risk_gate"]


def test_recommend_flag_on_does_not_change_aggregated_signal(monkeypatch):
    """Flag-ON with the REAL cold-start aggregator: result.aggregated_signal is
    identical to the flag-OFF result. The shadow is a pure observer."""
    from hermes_quant.advisor import recommend

    bars = _make_bars()

    monkeypatch.delenv(PDR_CORE_AGG_SHADOW_FLAG, raising=False)
    off = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    monkeypatch.setenv(PDR_CORE_AGG_SHADOW_FLAG, "1")
    on = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    assert off["aggregated_signal"] == on["aggregated_signal"]
    assert off["risk_gate"] == on["risk_gate"]
