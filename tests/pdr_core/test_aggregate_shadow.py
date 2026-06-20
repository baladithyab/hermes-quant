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


def test_run_shadow_aggregate_not_comparable_when_hierarchical_pooling_set(
    tmp_path, monkeypatch, caplog
):
    """REGRESSION (review P1 false-divergence): HERMES_QUANT_HIERARCHICAL_POOLING is
    a vote-branching flag the live BMA reads (bma.py:296) — when set on a SETTLED
    aggregator it replaces per-analyst weights with pooled skill, so the live signal
    diverges from the cold-start core port BY DESIGN. The first cut OMITTED this flag
    from _AGG_LEARNING_FLAGS, so a pooling-on tick was wrongly COMPARED and logged a
    FALSE magnitude/weights divergence. After the fix the flag is in the set => the
    tick is not-comparable, NO divergence flagged.

    RED-PROOF: this fixture exercises the exact hole — with the flag missing from the
    set, run_shadow_aggregate would return comparable=True (and, on a settled
    aggregator, diverged=True). Pinning reason==learning_flag_active + flag-name
    proves the flag is now in the gate."""
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")
    assert "HERMES_QUANT_HIERARCHICAL_POOLING" in _AGG_LEARNING_FLAGS, (
        "pooling flag must be in the comparability gate (review P1)"
    )
    agg = _live_aggregator(tmp_path)
    rows = FIXTURES["two_unanimous_long"]
    views = [_live_view(*r) for r in rows]
    ctx = _live_ctx()
    live_sig = agg.aggregate(views, ctx)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        report = run_shadow_aggregate(
            views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
        )
    assert report is not None
    assert report["comparable"] is False
    assert report["reason"] == "learning_flag_active"
    assert report["flag"] == "HERMES_QUANT_HIERARCHICAL_POOLING"
    assert report["diverged"] is False
    # No WARNING — a by-design skip is not a divergence.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_l2_stacking_phantom_flag_not_in_gate():
    """REGRESSION (review nit): the live BMA reads HERMES_QUANT_STACKING, NOT
    HERMES_QUANT_L2_STACKING. The phantom L2_STACKING (carried by the static test's
    set) would over-conservatively skip an otherwise-comparable tick — it must NOT
    be in the runtime gate."""
    assert "HERMES_QUANT_STACKING" in _AGG_LEARNING_FLAGS
    assert "HERMES_QUANT_L2_STACKING" not in _AGG_LEARNING_FLAGS


def test_run_shadow_aggregate_out_of_core_bounds_view_is_not_comparable(tmp_path):
    """REGRESSION (review P3 observability): a live AnalystView with magnitude > 1.0
    is accepted by protocol.AnalystView but REJECTED by the core CoreView
    (__post_init__ bounds). The projection raises; instead of the outer try/except
    swallowing it to a SILENT None (a tick dropped from the parity sample with no
    record), the shadow records a not-comparable report with reason
    view_out_of_core_bounds so the operator's coverage stays honest."""
    agg = _live_aggregator(tmp_path)
    ctx = _live_ctx()
    # magnitude 1.5 > 1.0: live accepts, core CoreView rejects.
    bad_view = _live_view("k", 1, 1.5, 0.7, 0.6, "1d")
    # the live aggregator tolerates it (its own bounds are looser); build a live sig
    live_sig = agg.aggregate([_live_view("a", 1, 0.5, 0.7, 0.6, "1d")], ctx)

    report = run_shadow_aggregate(
        views=[bad_view], ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
    )
    assert report is not None, "must record a not-comparable report, not silently drop"
    assert report["comparable"] is False
    assert report["reason"] == "view_out_of_core_bounds"
    assert report["diverged"] is False
    assert "detail" in report  # the ValueError text is surfaced for the operator


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


class _LightAnalyst:
    """A minimal deterministic analyst (no torch / no HF / no network).

    codex-review-2026-06-20 finding A (CI-hang): the live recommend() seam tests
    MUST pass an explicit lightweight loadout. Calling recommend() with no
    ``analysts=`` builds the DEFAULT roster, which includes KronosAnalyst ->
    a live HuggingFace fetch / torch inference that HANGS the full sweep on an
    [all,dev] box (the aegis-ci-hang family; tests/pdr_core/ has no HF-offline
    conftest guard). Two of these form a genuine ensemble so BMA's
    require_ensemble guard is satisfied without any heavy analyst.
    """

    def __init__(self, name: str, direction: int, *, conf: float = 0.6) -> None:
        self.name = name
        self.timeframes = ["1d"]
        self.asset_classes = ["equity"]
        self.enabled = True
        self._direction = direction
        self._conf = conf

    def analyze(self, ctx: MarketContext) -> LiveView | None:
        if len(ctx.bars["close"]) < 5:
            return None
        return LiveView(
            analyst=self.name,
            direction=self._direction,
            magnitude=0.02,
            confidence=self._conf,
            confidence_raw=self._conf,
            horizon="1d",
        )

    def health(self) -> dict:
        return {"n_views_emitted": 0, "last_view_at": None, "error_count": 0}


def _recommend_kwargs(provider):
    return dict(
        symbol="TEST",
        asset_class="equity",
        as_of="2026-03-15T00:00:00Z",
        provider=provider,
        include_lessons=False,
        # finding A: explicit lightweight loadout — never build the default roster
        # (KronosAnalyst -> HF/torch hang). A 2-analyst ensemble clears
        # require_ensemble so the live BMA emits a non-silenced signal.
        analysts=[_LightAnalyst("a", 1), _LightAnalyst("b", 1)],
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


# ===========================================================================
# SECTION 4 — codex-review-2026-06-20 regressions (comparability + comparison
# completeness). Each pins a confirmed review finding.
# ===========================================================================
import json  # noqa: E402 — local to the review-regression section


def test_not_comparable_learned_posteriors_no_false_divergence(tmp_path):
    """Finding D: a REUSED aggregator with accumulated settled outcomes (a learned
    posterior past n_min_observations) returns a learned weight != core's fixed 0.5
    -> the shadow must mark it NOT-comparable, not log a false divergence."""
    agg = _live_aggregator(tmp_path)
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["two_unanimous_long"]]
    live_sig = agg.aggregate(views, ctx)

    # Force a settled posterior on one analyst past the n_min threshold.
    stats = agg._get_or_create_stats("a")
    stats.n_observations = agg.n_min_observations + 5

    report = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
    )
    assert report is not None
    assert report["comparable"] is False
    assert report["reason"] == "learned_posteriors_active"
    assert report["diverged"] is False


def test_not_comparable_injected_collaborators(tmp_path):
    """Finding E: an injected ic_dedup_gate / regime_detector adjusts the live vote
    even with its env flag unset -> not-comparable (no false divergence).

    The comparability gate fires on PRESENCE alone, BEFORE the live signal is read,
    so we build the live signal on a clean aggregator and then attach the collaborator
    (a sentinel object suffices — the gate checks `is not None`, not the collaborator's
    behavior)."""
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["two_unanimous_long"]]

    # ic_dedup_gate present.
    agg1 = _live_aggregator(tmp_path)
    live_sig = agg1.aggregate(views, ctx)  # built BEFORE attaching the collaborator
    agg1.ic_dedup_gate = object()
    r1 = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg1, live_signal=live_sig, persist=False
    )
    assert r1 is not None and r1["comparable"] is False
    assert r1["reason"] == "ic_dedup_gate_injected"

    # regime_detector present.
    agg2 = _live_aggregator(tmp_path)
    live_sig2 = agg2.aggregate(views, ctx)
    agg2.regime_detector = object()
    r2 = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg2, live_signal=live_sig2, persist=False
    )
    assert r2 is not None and r2["comparable"] is False
    assert r2["reason"] == "regime_detector_injected"


def test_custom_horizon_config_threaded_no_false_divergence(tmp_path):
    """Finding B: a BMAAggregator built with non-default horizon multipliers must NOT
    log a false divergence — the shadow threads config.horizon_agreement_bonus /
    horizon_disagreement_penalty into core_aggregate.

    RED-PROOF: with the config NOT threaded (the pre-fix behavior), the multi-horizon
    fixture's confidence diverges (live uses the custom multiplier, core the default)."""
    from hermes_quant.aggregators.bma import BMAConfig

    cfg = BMAConfig(horizon_agreement_bonus=1.25, horizon_disagreement_penalty=0.70)
    agg = _live_aggregator(tmp_path, config=cfg, require_ensemble=False)
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["multi_horizon_all_agree"]]
    live_sig = agg.aggregate(views, ctx)

    report = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig, persist=False
    )
    assert report is not None
    assert report["comparable"] is True
    assert report["diverged"] is False, f"custom horizon config not threaded: {report['fields']}"


def test_compare_signals_catches_component_divergence(tmp_path):
    """Finding C (RED-PROOF): a port bug that changes the components tuple while
    leaving the scalar surface equal MUST be flagged. Proves components are compared."""
    from dataclasses import replace

    agg = _live_aggregator(tmp_path)
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["two_unanimous_long"]]
    live_sig = agg.aggregate(views, ctx)

    # Drop one component from the shadow — scalars unchanged, components differ.
    bogus = replace(live_sig, components=live_sig.components[:-1])
    diverged = _compare_signals(live_sig, bogus)
    assert "components" in diverged, diverged


def test_compare_signals_catches_identity_divergence(tmp_path):
    """Finding C (RED-PROOF): a divergence in an identity field (asset) MUST be
    flagged (downstream halt/event-risk/replay read identity)."""
    from dataclasses import replace

    agg = _live_aggregator(tmp_path)
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["two_unanimous_long"]]
    live_sig = agg.aggregate(views, ctx)

    bogus = replace(live_sig, asset="WRONG")
    diverged = _compare_signals(live_sig, bogus)
    assert "asset" in diverged, diverged


def test_compare_signals_presence_asymmetry_and_reason_mismatch():
    """Facet-4 LOW: pin the presence-asymmetry + silence-reason RED cases."""
    # Build two real silence-path signals with different reasons.
    from hermes_quant.pdr_core.aggregate import CoreAggregateContext, _flat_signal

    ctx = CoreAggregateContext(asset="AAPL", timeframe="1d", asset_class="equity", asof=ASOF)
    s1 = _flat_signal(ctx, reason="flat_or_no_views")
    s2 = _flat_signal(ctx, reason="silenced_single_source")
    diverged = _compare_signals(s1, s2)
    assert "metadata.reason" in diverged, diverged
    # presence asymmetry: a real signal vs None.
    assert _compare_signals(s1, None) == ["presence"]


def test_real_divergence_is_persisted_end_to_end(tmp_path, monkeypatch):
    """Finding G: a REAL divergent report is logged AND PERSISTED (the prior tests
    persisted only an agreement line; a serialization bug in a divergent record
    could slip). Monkeypatch core to flip direction, persist=True to a tmp path."""
    import hermes_quant.pdr_core.aggregate as core_agg_mod
    from dataclasses import replace

    agg = _live_aggregator(tmp_path)
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["two_unanimous_long"]]
    live_sig = agg.aggregate(views, ctx)

    real_core = core_agg_mod.core_aggregate

    def _flip(core_views, core_ctx, **kwargs):
        sig = real_core(core_views, core_ctx, **kwargs)
        return replace(sig, direction=(-1 if sig.direction >= 0 else 1))

    monkeypatch.setattr(core_agg_mod, "core_aggregate", _flip)

    log_path = tmp_path / "agg-div.jsonl"
    report = run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig,
        persist=True, divergence_path=log_path,
    )
    assert report is not None and report["diverged"] is True
    assert log_path.exists(), "divergent report must be persisted"
    line = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert line["comparable"] is True
    assert line["diverged"] is True
    assert "direction" in line["fields"]
    # the reduced primitives serialized without error (no full-repr leak)
    assert line["live"] is not None and line["shadow"] is not None
    assert "components" in line["live"]  # the new compared field is in the record


def test_not_comparable_record_persists_flag(tmp_path):
    """Finding H: a not-comparable record persists WHICH flag blocked it."""
    agg = _live_aggregator(tmp_path)
    ctx = _live_ctx()
    views = [_live_view(*r) for r in FIXTURES["two_unanimous_long"]]
    live_sig = agg.aggregate(views, ctx)  # before attaching the collaborator
    agg.regime_detector = object()
    log_path = tmp_path / "nc.jsonl"
    run_shadow_aggregate(
        views=views, ctx=ctx, aggregator=agg, live_signal=live_sig,
        persist=True, divergence_path=log_path,
    )
    line = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert line["comparable"] is False
    assert line["reason"] == "regime_detector_injected"
