"""PDR-4 SaturationScore acceptance tests (ADR-0079 GAP-C / plan §6).

The eval gate as pytest-verifiable acceptance criteria:
  §6.1 Property A — silence-only: post <= pre for EVERY input (adversarial sat dicts).
  §6.2 Property B — view-local: NON-semantic views bit-identical sat-on vs sat-off (D79.4).
  §6.3 flag-OFF byte-identical: flag unset/'0' => the semantic view is bit-identical.
  §6.4 asof / no-lookahead: future anchors ignored => m=1.0; asof stamped == decision asof.
  §6.5 backtest: decay never HURTS social-arb Sharpe on the labeled exit set.
  §6.6 producer unit tests: basis precedence, confirm->floor, empty->1.0, monotone.

Pure, deterministic, offline. The flag is toggled per-case via monkeypatch (read at
call time at BOTH sites). No network, no provider calls.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst
from hermes_quant.analysts.microstructure import MicrostructureLite
from hermes_quant.analysts.semantic import HermesSemanticAnalyst
from hermes_quant.perception.adapter import frame_to_context
from hermes_quant.perception.frame import PerceptionFrame
from hermes_quant.perception.saturation import (
    _FLOOR,
    apply_saturation,
    compute_saturation,
)
from hermes_quant.perception.velocity import (
    compute_trend_velocity,
    counts_per_period,
)
from hermes_quant.semantic import semantic_packet_from_dict

# ---------------------------------------------------------------------------
# Load the backtest driver as a module (it lives under ops/scripts, not a package).
# ---------------------------------------------------------------------------
_BACKTEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "ops" / "scripts" / "quant-pdr4-saturation-backtest.py"
)
_spec = importlib.util.spec_from_file_location("pdr4_backtest", _BACKTEST_PATH)
_backtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backtest)

_EXIT_SET_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "pdr4_saturation" / "exit_set.v1.json"
)


# ===========================================================================
# §6.1 Property Test A — silence-only (post <= pre, for EVERY input)
# ===========================================================================
def test_saturation_never_raises_confidence():
    """post <= pre and post >= 0 over confidence x adversarial saturation dicts
    (including decay_multiplier >1, <=0, NaN, missing key, None, {})."""
    for conf in [0.0, 0.1, 0.37, 0.5, 0.62, 0.9, 1.0]:
        for sat in [
            None, {},
            {"decay_multiplier": 0.5}, {"decay_multiplier": 1.0},
            {"decay_multiplier": 1.5}, {"decay_multiplier": 0.0},
            {"decay_multiplier": -0.2}, {"decay_multiplier": float("nan")},
            {"decay_multiplier": float("inf")}, {"decay_multiplier": "garbage"},
            {"other_key": 7},
        ]:
            post = apply_saturation(conf, sat)
            assert post <= conf + 1e-12, f"AMPLIFIED conf={conf} sat={sat} -> {post}"
            assert post >= 0.0, f"NEGATIVE conf={conf} sat={sat} -> {post}"


def test_saturation_out_of_contract_m_is_noop():
    """Any m outside (0,1] (incl NaN/inf/non-float) is treated as a no-op (== pre)."""
    for bad in [1.5, 0.0, -0.2, float("nan"), float("inf"), "x", None]:
        assert apply_saturation(0.62, {"decay_multiplier": bad}) == 0.62


def test_saturation_in_contract_m_scales_down():
    """A valid m in (0,1] scales the confidence down by exactly m."""
    assert apply_saturation(0.8, {"decay_multiplier": 0.5}) == pytest.approx(0.4)
    assert apply_saturation(0.8, {"decay_multiplier": 1.0}) == pytest.approx(0.8)


# ===========================================================================
# §6.2 Property Test B — view-local (non-semantic views bit-identical sat-on/off)
# ===========================================================================
def _frame_with_packet(*, sat_dict) -> PerceptionFrame:
    """Build a PerceptionFrame carrying a fresh bullish semantic packet and a
    saturation slot. The adapter projects frame.saturation -> ctx.extras['saturation']
    only when non-None (PDR-1 contract), exercising the real produce->adapter->apply path."""
    ts = pd.date_range("2024-01-01", periods=60, freq="1h", tz="UTC")
    bars = pd.DataFrame({
        "timestamp": ts,
        "open": [100 + i * 0.1 for i in range(60)],
        "high": [101 + i * 0.1 for i in range(60)],
        "low": [99 + i * 0.1 for i in range(60)],
        "close": [100 + i * 0.1 for i in range(60)],
        "volume": [1000 + i for i in range(60)],
    })
    pkt = semantic_packet_from_dict({
        "schema_version": 1,
        "asset": "CROX",
        "asof": "2024-01-03T10:00:00Z",
        "horizon": "1h",
        "stance": "bullish",
        "confidence": 0.75,
        "magnitude": 0.012,
        "summary": "viral consumer trend bullish CROX",
        "sources": [{"type": "note", "ref": "unit-test"}],
        "model": "hermes:test-model",
    }).to_dict()
    return PerceptionFrame(
        symbol="CROX",
        asof=pd.Timestamp("2024-01-03T11:00:00Z"),
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        regime=None,
        semantic_packets=(pkt,),
        saturation=sat_dict,
        extras={"decision_asof": "2024-01-03T11:00:00Z"},
    )


def _run_views(monkeypatch, *, sat_flag: str, sat_dict) -> dict:
    """Run the fixed analyst set (classical_ta + microstructure + hermes_semantic)
    against a ctx built from a PerceptionFrame, with HERMES_QUANT_SATURATION set to
    ``sat_flag``. Returns {analyst_name: AnalystView}."""
    monkeypatch.setenv("HERMES_QUANT_SATURATION", sat_flag)
    frame = _frame_with_packet(sat_dict=sat_dict)
    ctx = frame_to_context(frame, timeframe="1h", asset_class="equity")
    analysts = [ClassicalTAAnalyst(), MicrostructureLite(), HermesSemanticAnalyst()]
    out: dict = {}
    for a in analysts:
        v = a.analyze(ctx)
        if v is not None:
            out[v.analyst] = v
    return out


def test_saturation_only_touches_semantic_view(monkeypatch):
    """D79.4 (the most load-bearing rail): with a real decay applied, every
    NON-semantic view is BIT-IDENTICAL sat-on vs sat-off, and only the semantic
    view's confidence differs — and only downward."""
    sat = {"decay_multiplier": 0.3, "score": 0.7, "basis": "confirm_date_passed",
           "asof": "2024-01-03T11:00:00Z"}
    off = _run_views(monkeypatch, sat_flag="0", sat_dict=sat)
    on = _run_views(monkeypatch, sat_flag="1", sat_dict=sat)

    # non-semantic views must be present and BIT-IDENTICAL (frozen dataclass eq).
    nonsem = set(off) - {"hermes_semantic"}
    assert nonsem, "expected at least one non-semantic view to compare"
    for name in nonsem:
        assert name in on
        assert off[name] == on[name], f"non-semantic view '{name}' changed sat-on vs sat-off"

    # the semantic view exists in both, and ON confidence <= OFF confidence (silence-only).
    assert "hermes_semantic" in off and "hermes_semantic" in on
    sem_off, sem_on = off["hermes_semantic"], on["hermes_semantic"]
    assert sem_on.confidence <= sem_off.confidence
    assert sem_on.confidence == pytest.approx(sem_off.confidence * 0.3)
    # everything ELSE about the semantic view is identical (only confidence + provenance move).
    assert sem_on.direction == sem_off.direction
    assert sem_on.magnitude == sem_off.magnitude
    assert sem_on.confidence_raw == sem_off.confidence_raw
    # provenance stamped ON, absent OFF.
    assert sem_on.metadata.get("saturation") == sat
    assert "saturation" not in (sem_off.metadata or {})


def test_saturation_no_extra_means_no_op(monkeypatch):
    """Flag ON but frame.saturation is None => adapter writes no extra =>
    apply_saturation is a no-op => semantic view identical to flag-OFF."""
    off = _run_views(monkeypatch, sat_flag="0", sat_dict=None)
    on = _run_views(monkeypatch, sat_flag="1", sat_dict=None)
    assert off["hermes_semantic"] == on["hermes_semantic"]


# ===========================================================================
# §6.3 flag-OFF byte-identical (semantic view bit-identical to flag-absent)
# ===========================================================================
def _semantic_view(sat_flag_setter, sat_dict):
    frame = _frame_with_packet(sat_dict=sat_dict)
    ctx = frame_to_context(frame, timeframe="1h", asset_class="equity")
    return HermesSemanticAnalyst().analyze(ctx)


def test_flag_off_byte_identical(monkeypatch):
    """Flag unset/'0' => the semantic view is bit-identical regardless of whether a
    saturation dict is present in the frame (the analyst gate never consults it)."""
    sat = {"decay_multiplier": 0.1, "basis": "confirm_date_passed"}
    monkeypatch.delenv("HERMES_QUANT_SATURATION", raising=False)
    v_absent = _semantic_view(monkeypatch, sat)
    monkeypatch.setenv("HERMES_QUANT_SATURATION", "0")
    v_zero = _semantic_view(monkeypatch, sat)
    # and the baseline with NO saturation slot at all:
    monkeypatch.delenv("HERMES_QUANT_SATURATION", raising=False)
    v_baseline = _semantic_view(monkeypatch, None)
    assert v_absent == v_zero == v_baseline
    assert "saturation" not in (v_absent.metadata or {})


# ===========================================================================
# §6.4 asof / no-lookahead
# ===========================================================================
def test_saturation_is_lookahead_honest():
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    # future peak / future confirm / future packet -> ALL ignored -> no decay (m == 1.0)
    fut = compute_saturation(
        packet_asof="2024-04-01T00:00:00Z", asof=asof,
        trend_velocity={"peak_asof": "2024-04-01T00:00:00Z"},
        confirm_date="2024-04-01T00:00:00Z",
    )
    assert fut["decay_multiplier"] == 1.0 and fut["basis"] == "no_basis"
    assert pd.Timestamp(fut["asof"]) == asof          # stamps the DECISION asof, not wall-clock
    # past peak -> decays
    past = compute_saturation(
        packet_asof="2024-02-01T00:00:00Z", asof=asof,
        trend_velocity={"peak_asof": "2024-02-01T00:00:00Z"},
    )
    assert past["decay_multiplier"] < 1.0
    assert past["basis"] == "velocity_peak"


def test_saturation_stamps_naive_asof_as_utc():
    """A naive (tz-less) asof is localized to UTC and stamped that way."""
    out = compute_saturation(packet_asof=None, asof=pd.Timestamp("2024-03-01T00:00:00"))
    assert pd.Timestamp(out["asof"]).tzinfo is not None
    assert pd.Timestamp(out["asof"]) == pd.Timestamp("2024-03-01T00:00:00Z")


# ===========================================================================
# §6.5 Backtest — decay never HURTS social-arb Sharpe on the labeled exit set
# ===========================================================================
def test_decay_improves_exit_set_sharpe():
    report = _backtest.run(_EXIT_SET_PATH)
    assert report["sharpe_on"] >= report["sharpe_off"], (
        f"decay HURT Sharpe: on={report['sharpe_on']} off={report['sharpe_off']}"
    )


def test_backtest_per_case_decay_is_silence_only():
    """Every case's sat-on PnL is no further from zero on the LOSING side than sat-off
    (decay shrinks position): for an adverse return, |pnl_on| <= |pnl_off|."""
    report = _backtest.run(_EXIT_SET_PATH)
    for c in report["per_case"]:
        # decay multiplier is always in (0,1]
        assert 0.0 < c["decay_multiplier"] <= 1.0
        # position-scaled PnL magnitude never grows under decay (m<=1)
        assert abs(c["pnl_on"]) <= abs(c["pnl_off"]) + 1e-9


# ===========================================================================
# §6.6 Producer unit tests — basis precedence, confirm->floor, empty->1.0, monotone
# ===========================================================================
def test_basis_precedence_confirm_beats_peak_and_age():
    """confirm_date passed dominates even when a peak and packet age are also present."""
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    out = compute_saturation(
        packet_asof="2024-01-01T00:00:00Z", asof=asof,
        trend_velocity={"peak_asof": "2024-01-15T00:00:00Z"},
        confirm_date="2024-02-01T00:00:00Z",
    )
    assert out["basis"] == "confirm_date_passed"
    assert out["decay_multiplier"] == pytest.approx(_FLOOR)


def test_basis_precedence_peak_beats_packet_age():
    """With no confirm_date, a passed velocity peak is preferred over packet age."""
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    out = compute_saturation(
        packet_asof="2024-01-01T00:00:00Z", asof=asof,
        trend_velocity={"peak_asof": "2024-02-20T00:00:00Z"},
    )
    assert out["basis"] == "velocity_peak"


def test_basis_packet_age_fallback_when_no_peak():
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    out = compute_saturation(packet_asof="2024-02-15T00:00:00Z", asof=asof)
    assert out["basis"] == "packet_age"
    assert 0.0 < out["decay_multiplier"] < 1.0


def test_confirm_date_passed_goes_to_floor():
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    out = compute_saturation(
        packet_asof="2024-02-01T00:00:00Z", asof=asof,
        confirm_date="2024-02-15T00:00:00Z",
    )
    assert out["decay_multiplier"] == pytest.approx(_FLOOR)
    assert out["score"] == pytest.approx(round(1.0 - _FLOOR, 6))


def test_empty_inputs_return_no_decay():
    """Nothing usable (no packet, no peak, no confirm) -> m == 1.0 (silence-only safety:
    do NOT silence a position you cannot prove is stale)."""
    out = compute_saturation(packet_asof=None, asof=pd.Timestamp("2024-03-01T00:00:00Z"))
    assert out["decay_multiplier"] == 1.0
    assert out["basis"] == "no_basis"
    assert out["score"] == 0.0


def test_unparseable_inputs_return_no_decay():
    """Garbage anchors never raise; they parse to None -> m == 1.0."""
    out = compute_saturation(
        packet_asof="not-a-date", asof=pd.Timestamp("2024-03-01T00:00:00Z"),
        trend_velocity={"peak_asof": "also-bad"}, confirm_date="nope",
    )
    assert out["decay_multiplier"] == 1.0
    assert out["basis"] == "no_basis"


def test_decay_is_monotone_in_age():
    """Older age (within the packet_age basis) -> smaller m (more saturated)."""
    pub = "2024-01-01T00:00:00Z"
    ms = [
        compute_saturation(packet_asof=pub, asof=pd.Timestamp(a))["decay_multiplier"]
        for a in ("2024-01-02T00:00:00Z", "2024-01-15T00:00:00Z",
                  "2024-02-01T00:00:00Z", "2024-04-01T00:00:00Z")
    ]
    assert ms == sorted(ms, reverse=True), f"decay not monotone in age: {ms}"
    assert all(0.0 < m <= 1.0 for m in ms)


def test_age_zero_is_no_decay():
    """At age 0 (packet/peak == asof) the decay multiplier is exactly 1.0."""
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    out = compute_saturation(packet_asof="2024-03-01T00:00:00Z", asof=asof)
    assert out["decay_multiplier"] == pytest.approx(1.0)


def test_half_life_halves_at_one_half_life():
    """At exactly one half-life of age, decay == 0.5 so m == _FLOOR + (1-_FLOOR)*0.5."""
    asof = pd.Timestamp("2024-01-15T00:00:00Z")  # 14 days after pub
    out = compute_saturation(
        packet_asof="2024-01-01T00:00:00Z", asof=asof, half_life_days=14.0,
    )
    expected = _FLOOR + (1.0 - _FLOOR) * 0.5
    assert out["decay_multiplier"] == pytest.approx(round(expected, 6))


# ===========================================================================
# §6.6b RR6 regression — a REAL VelocityScore.to_mapping() engages velocity_peak
# ===========================================================================
def test_real_velocity_mapping_engages_velocity_peak_basis():
    """RR6: feeding the ONLY real producer's output (VelocityScore.to_mapping(), which
    emits "peak_period", NOT "peak_asof") to compute_saturation MUST hit the
    "velocity_peak" basis. Previously the key mismatch made this DEAD (silently fell
    through to packet_age). Builds a real series whose interest peaks well before asof,
    so the peak anchor is asof-honest (peak <= asof) and dominates packet age."""
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    # Weekly interest observations: a clear early peak (mid-Jan), then it cools off.
    # All timestamps <= asof, so counts_per_period keeps every bucket.
    weeks = [
        ("2024-01-01", 2), ("2024-01-08", 5), ("2024-01-15", 9),  # <- peak week
        ("2024-01-22", 4), ("2024-01-29", 3), ("2024-02-05", 2),
    ]
    timestamps: list[pd.Timestamp] = []
    for day, n in weeks:
        timestamps.extend([pd.Timestamp(day + "T12:00:00Z")] * n)

    counts = counts_per_period(timestamps, asof=asof, freq="W")
    score = compute_trend_velocity(counts, asof=asof)
    assert score is not None, "expected a real VelocityScore from a multi-week series"
    assert score.peak_period is not None

    mapping = score.to_mapping()
    # Guard the contract this test exists to protect: the real producer emits peak_period,
    # and compute_saturation must read it (it does NOT emit peak_asof).
    assert "peak_period" in mapping and mapping["peak_period"] is not None
    assert "peak_asof" not in mapping
    # asof honesty: the peak the producer found is in the past relative to the decision asof.
    assert pd.Timestamp(mapping["peak_period"]) <= asof

    out = compute_saturation(
        packet_asof="2024-02-25T00:00:00Z",  # packet age would ALSO be a valid (weaker) basis
        asof=asof,
        trend_velocity=mapping,
    )
    assert out["basis"] == "velocity_peak", (
        f"real VelocityScore mapping did not engage velocity_peak: {out}"
    )
    assert 0.0 < out["decay_multiplier"] < 1.0  # a passed peak decays, but not to floor
    assert pd.Timestamp(out["asof"]) == asof    # stamps the decision asof, not wall-clock


def test_back_compat_peak_asof_still_engages_velocity_peak():
    """Older synthetic mappings using the legacy "peak_asof" key still engage the
    velocity_peak basis (back-compat preserved by the dual-key read)."""
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    out = compute_saturation(
        packet_asof="2024-01-01T00:00:00Z", asof=asof,
        trend_velocity={"peak_asof": "2024-02-20T00:00:00Z"},
    )
    assert out["basis"] == "velocity_peak"


def test_peak_asof_wins_when_both_keys_present():
    """When both keys are supplied, peak_asof takes precedence (documented contract)."""
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    # peak_asof is in the past (engages); peak_period is in the FUTURE (would NOT engage).
    out = compute_saturation(
        packet_asof="2024-01-01T00:00:00Z", asof=asof,
        trend_velocity={"peak_asof": "2024-02-20T00:00:00Z",
                        "peak_period": "2024-04-01T00:00:00Z"},
    )
    assert out["basis"] == "velocity_peak"
    assert 0.0 < out["decay_multiplier"] < 1.0
