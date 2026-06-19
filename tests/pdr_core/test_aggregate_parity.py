"""ADR-0092 Increment-1 step 6 — THE BMA VOTE PARITY GATE.

This is the whole safety argument for the BMA vote port. The numeric vote
arithmetic that fuses AnalystViews into one AggregatedSignal (ADR-0003 — the
input the deterministic gate consumes) was lifted into the host-blind PURE
function ``hermes_quant.pdr_core.aggregate.core_aggregate``. This file proves
that pure core is BEHAVIORALLY IDENTICAL to the live
``hermes_quant.aggregators.bma.BMAAggregator.aggregate`` on the FLAGS-OFF
COLD-START path over a fixture matrix hitting every vote branch.

THE PARITY DRIVER (the single most fragile knob, per the deep-dive port_risks):
the live oracle MUST be forced onto ColdStartCalibrator so its emitted
``confidence`` equals the core's pure ``(confidence_raw + 2) / 8`` arithmetic
byte-for-byte. We do that by:
  (a) constructing a FRESH BMAAggregator (NO update() calls) so per-analyst
      posterior weights stay uniform 0.5 — matching the stateless core; and
  (b) passing ``calibrator_path`` to a guaranteed-NONEXISTENT path so
      ``_load_calibrator`` returns a ColdStartCalibrator (no real isotonic.pkl
      on the dev/CI box can make live != core).
  (c) ensuring every HERMES_QUANT_* learning flag is UNSET in the test env.

THE SAFETY ASSERTION: for every fixture we build the live ``protocol`` inputs AND
the equivalent core ``AnalystView`` inputs, run both aggregators, and assert the
load-bearing scalar surface the deterministic gate reads — direction, magnitude,
confidence (CALIBRATED), confidence_raw, horizon, the metadata audit dict — is
identical. The ``components`` field is compared by mapping between the two
contract shapes on the scalar vote fields (the core uses pdr_core.AnalystView,
live uses protocol.AnalystView — see port_risks CONTRACT-SHAPE MISMATCH).

Any divergence is a PORT BUG.
"""

from __future__ import annotations

import os

import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.pdr_core.aggregate import (
    CoreAggregateContext,
    core_aggregate,
)
from hermes_quant.pdr_core.contracts import AnalystView as CoreView

# --- the LIVE BMA + protocol types (the parity ORACLE) ---------------------
from hermes_quant.protocol import AnalystView as LiveView
from hermes_quant.protocol import MarketContext

# Learning flags that MUST be unset for the flags-off oracle to match the core.
_FLAGS = (
    "HERMES_QUANT_L2_POSTERIOR_DECAY",
    "HERMES_QUANT_L2_PER_ANALYST_CALIB",
    "HERMES_QUANT_L2_LESSON_HAIRCUT",
    "HERMES_QUANT_L2_POSTERIOR_PERSIST",
    "HERMES_QUANT_L2_STACKING",
    "HERMES_QUANT_STACKING",
    "HERMES_QUANT_DISSENT_CAP",
)

ASOF = "2026-06-12T15:00:00+00:00"
BAR = "2026-06-12T14:59:00+00:00"


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    for f in _FLAGS:
        monkeypatch.delenv(f, raising=False)
    yield


def _live_aggregator(tmp_path, **kwargs) -> BMAAggregator:
    """A FRESH BMAAggregator forced onto ColdStartCalibrator (no update() calls)."""
    nonexistent = tmp_path / "no_such_isotonic.pkl"
    assert not nonexistent.exists()
    agg = BMAAggregator(calibrator_path=nonexistent, **kwargs)
    # Sanity: the oracle is on the cold-start path, not a stray real pickle.
    assert type(agg.calibrator).__name__ == "ColdStartCalibrator"
    return agg


def _live_ctx() -> MarketContext:
    import pandas as pd

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


def _core_ctx() -> CoreAggregateContext:
    return CoreAggregateContext(
        asset="AAPL", timeframe="1d", asset_class="equity", asof=ASOF
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


def _core_view(analyst, direction, magnitude, confidence, confidence_raw, horizon):
    return CoreView(
        analyst=analyst,
        asset="AAPL",
        asset_class="equity",
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence_raw,
        horizon=horizon,
        asof_decision=ASOF,
        bar_ts=BAR,
    )


# A (analyst, direction, magnitude, confidence, confidence_raw, horizon) tuple
# fixture matrix hitting EVERY vote branch. Magnitudes stay in [0,1] so the core
# contract accepts them (port_risks CONTRACT-SHAPE MISMATCH).
FIXTURES: dict[str, list[tuple]] = {
    "empty": [],
    "lone_long_silenced": [("k", 1, 0.5, 0.7, 0.6, "1d")],
    "abstain_only": [("k", 1, 0.5, 0.05, 0.6, "1d")],
    "abstain_plus_real": [
        ("a", 1, 0.4, 0.10, 0.4, "1d"),
        ("b", 1, 0.6, 0.70, 0.6, "1d"),
    ],
    # PINS the ABSTAIN_THRESHOLD value (0.10): analyst 'a' carries conf 0.08, which
    # is BELOW 0.10 and dropped -> the input collapses to single-source ('b' only)
    # -> silenced under require_ensemble / lone passthrough under False. If the
    # threshold were any lower (e.g. 0.06) 'a' would SURVIVE, making it a 2-source
    # vote and flipping direction/raw/n_views. (Without this fixture the grid does
    # NOT pin the abstain value — the existing 0.05/0.10 fixtures both stay on the
    # same side of a 0.06 threshold.)
    "abstain_boundary": [
        ("a", 1, 0.3, 0.08, 0.3, "1d"),
        ("b", 1, 0.6, 0.70, 0.6, "1d"),
    ],
    # PINS the NET-FLAT eps (1e-9): two opposing 1d voters whose signed terms very
    # nearly cancel — net = 0.5*(0.5000002 - 0.5) = 1e-7, which is ABOVE 1e-9 (so
    # direction FIRES, +1) but well below any coarser threshold. A wrong eps (e.g.
    # 1e-1) would silence this to direction 0. confidence_raw here is the tiny
    # vote_share (~2e-7), exercising the dissent (no-bonus) branch at the extreme.
    "near_flat_survives": [
        ("a", 1, 0.5, 0.5000002, 0.6, "1d"),
        ("b", -1, 0.5, 0.5000000, 0.6, "1d"),
    ],
    "two_unanimous_long": [
        ("a", 1, 0.4, 0.8, 0.7, "1d"),
        ("b", 1, 0.8, 0.6, 0.5, "1d"),
    ],
    "two_unanimous_short": [
        ("a", -1, 0.4, 0.8, 0.7, "1d"),
        ("b", -1, 0.8, 0.6, 0.5, "1d"),
    ],
    "dissent_long_wins": [
        ("a", 1, 0.6, 0.8, 0.7, "1d"),
        ("b", -1, 0.2, 0.4, 0.3, "1d"),
    ],
    "dissent_short_wins": [
        ("a", -1, 0.6, 0.8, 0.7, "1d"),
        ("b", 1, 0.2, 0.4, 0.3, "1d"),
    ],
    "net_flat_cancel": [
        ("a", 1, 0.5, 0.7, 0.6, "1d"),
        ("b", -1, 0.5, 0.7, 0.6, "1d"),
    ],
    "flat_voter_present": [
        ("a", 1, 0.4, 0.9, 0.8, "1d"),
        ("b", 1, 0.6, 0.3, 0.2, "1d"),
        ("c", 0, 0.5, 0.5, 0.4, "1d"),
    ],
    "multi_horizon_all_agree": [
        ("a", 1, 0.4, 0.9, 0.8, "1d"),
        ("b", 1, 0.6, 0.9, 0.8, "1w"),
    ],
    "multi_horizon_mixed": [
        ("a", 1, 0.6, 0.9, 0.8, "1d"),
        ("b", -1, 0.2, 0.3, 0.2, "1w"),
    ],
    "three_modal_horizon": [
        ("a", 1, 0.4, 0.7, 0.6, "1d"),
        ("b", 1, 0.5, 0.7, 0.6, "1d"),
        ("c", 1, 0.6, 0.7, 0.6, "1w"),
    ],
    "horizon_weight_mix": [
        ("a", 1, 0.2, 0.5, 0.5, "1d"),
        ("b", 1, 0.8, 0.5, 0.5, "1w"),
        ("c", 1, 0.5, 0.5, 0.5, "1M"),
        ("d", 1, 0.5, 0.5, 0.5, "1Q"),
    ],
    "edge_raw_one": [
        ("a", 1, 1.0, 1.0, 1.0, "1d"),
        ("b", 1, 1.0, 1.0, 1.0, "1d"),
    ],
}


@pytest.mark.parametrize("name", list(FIXTURES))
@pytest.mark.parametrize("require_ensemble", [True, False])
def test_bma_vote_parity(name, require_ensemble, tmp_path) -> None:
    rows = FIXTURES[name]

    live_agg = _live_aggregator(tmp_path, require_ensemble=require_ensemble)
    live_views = [_live_view(*r) for r in rows]
    live_sig = live_agg.aggregate(live_views, _live_ctx())

    core_views = [_core_view(*r) for r in rows]
    core_sig = core_aggregate(
        core_views, _core_ctx(), require_ensemble=require_ensemble
    )

    # --- scalar vote surface (what the deterministic gate reads) -----------
    assert core_sig.direction == live_sig.direction, f"{name}: direction"
    assert core_sig.magnitude == pytest.approx(
        live_sig.magnitude, abs=1e-12
    ), f"{name}: magnitude"
    assert core_sig.confidence == pytest.approx(
        live_sig.confidence, abs=1e-12
    ), f"{name}: confidence (CALIBRATED)"
    assert core_sig.confidence_raw == pytest.approx(
        live_sig.confidence_raw, abs=1e-12
    ), f"{name}: confidence_raw"
    assert core_sig.horizon == live_sig.horizon, f"{name}: horizon"
    assert core_sig.aggregator == live_sig.aggregator, f"{name}: aggregator"

    # --- metadata audit dict parity ----------------------------------------
    cm, lm = core_sig.metadata, live_sig.metadata
    if "reason" in lm:
        # flat / silence path: same reason, no other keys.
        assert cm == lm, f"{name}: flat metadata"
    else:
        assert cm["vote_share"] == pytest.approx(
            lm["vote_share"], abs=1e-12
        ), f"{name}: vote_share"
        assert cm["n_contributing"] == lm["n_contributing"], f"{name}: n_contributing"
        assert cm["n_views"] == lm["n_views"], f"{name}: n_views"
        assert cm["horizons_present"] == lm["horizons_present"], f"{name}: horizons_present"
        assert cm["horizon_agreement"] == lm["horizon_agreement"], f"{name}: horizon_agreement"
        assert cm["ic_dedup_excluded_analysts"] == lm["ic_dedup_excluded_analysts"]
        assert cm["regime_state"] == lm["regime_state"]
        assert cm["regime_weight_multipliers"] == lm["regime_weight_multipliers"]
        # weights keyed by analyst name, value-identical
        assert set(cm["weights"]) == set(lm["weights"]), f"{name}: weight keys"
        for analyst, w in cm["weights"].items():
            assert w == pytest.approx(lm["weights"][analyst], abs=1e-12), (
                f"{name}: weight[{analyst}]"
            )

    # --- components scalar-field parity (cross-shape, port_risks) ----------
    assert len(core_sig.components) == len(live_sig.components), f"{name}: n_components"
    for cv, lv in zip(core_sig.components, live_sig.components, strict=True):
        assert cv.analyst == lv.analyst
        assert cv.direction == lv.direction
        assert cv.confidence == pytest.approx(lv.confidence, abs=1e-12)
        assert cv.confidence_raw == pytest.approx(lv.confidence_raw, abs=1e-12)
        assert cv.horizon == lv.horizon


def test_parity_oracle_is_cold_start(tmp_path) -> None:
    """Guard the parity driver: the oracle must be on ColdStartCalibrator, else
    a real isotonic.pkl would silently make every confidence assertion vacuous."""
    agg = _live_aggregator(tmp_path)
    assert type(agg.calibrator).__name__ == "ColdStartCalibrator"
    # And no learning flag leaked into the env.
    for f in _FLAGS:
        assert os.environ.get(f) in (None, "0", "")
