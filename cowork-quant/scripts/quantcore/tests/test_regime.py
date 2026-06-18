"""Regime classifier semantics vs hermes_quant.regime.detector @ 7eb148a.

Covers the two latest hermes fixes ported into quantcore.regime:
  1b4ee61 — 0.60-0.70 dead-zone fill (bull vol ceiling 0.70, weak-lean +
            neutral zones)
  7eb148a — NaN-fail-open guard (any non-finite intermediate -> unknown)

Synthetic series are built so the intended zone is structural, not seed luck:
the noise sigma decays (current 20d vol ranks lowest -> vol_pct ~ 0) or rises
(ranks highest -> vol_pct ~ 1), and trend comes from the drift ramp, which is
slope-scale-invariant under the (SMA20 - SMA50) / stdev50 normalization.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from random import Random

import pytest

from quantcore.regime import (
    BULL_TREND_MIN,
    MIN_CLOSES,
    VOLATILE_VOL_MIN,
    WEAK_TREND_MIN,
    RegimeRead,
    classify_regime,
    map_state,
)

UTC = timezone.utc
ASOF = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)


def make_walk(
    n: int, drift: float, sigma_start: float, sigma_end: float, seed: int
) -> list[float]:
    """Geometric walk: constant log-drift, noise sigma linearly interpolated."""
    rng = Random(seed)
    closes = [100.0]
    for i in range(n - 1):
        sigma = sigma_start + (sigma_end - sigma_start) * i / (n - 2)
        closes.append(closes[-1] * math.exp(drift + rng.gauss(0.0, sigma)))
    return closes


def make_flat_choppy(n: int = 300) -> list[float]:
    """Flat around 100: sine chop (period 10 divides both SMA windows, so both
    SMAs sit at ~100 -> trend ~ 0) plus seeded noise whose sigma decays (so the
    current 20d vol ranks low -> vol_pct well under the volatile ceiling)."""
    rng = Random(42)
    closes = []
    for i in range(n):
        sigma = 0.08 - 0.04 * i / (n - 1)
        closes.append(100.0 + 0.5 * math.sin(2.0 * math.pi * i / 10.0) + rng.gauss(0.0, sigma))
    return closes


# ---------------------------------------------------------------------------
# Headline labels from synthetic price paths
# ---------------------------------------------------------------------------


def test_risk_on_steady_uptrend_low_vol():
    closes = make_walk(300, drift=0.002, sigma_start=0.012, sigma_end=0.001, seed=7)
    out = classify_regime(closes, ASOF)
    assert out.regime == "risk_on"
    assert out.evidence.endswith("zone=bull")  # strong trend, not just weak lean
    assert out.trend is not None and out.trend >= BULL_TREND_MIN
    assert out.vol_pct is not None and out.vol_pct <= VOLATILE_VOL_MIN


def test_risk_off_downtrend_moderate_vol_is_bear_zone():
    closes = make_walk(300, drift=-0.004, sigma_start=0.012, sigma_end=0.008, seed=11)
    out = classify_regime(closes, ASOF)
    assert out.regime == "risk_off"
    assert out.evidence.endswith("zone=bear")
    assert out.trend is not None and out.trend <= -BULL_TREND_MIN


def test_risk_off_downtrend_spiking_vol_is_volatile_zone():
    closes = make_walk(300, drift=-0.003, sigma_start=0.002, sigma_end=0.030, seed=13)
    out = classify_regime(closes, ASOF)
    assert out.regime == "risk_off"
    assert out.evidence.endswith("zone=volatile")
    assert out.vol_pct is not None and out.vol_pct > VOLATILE_VOL_MIN


def test_choppy_flat_with_noise():
    out = classify_regime(make_flat_choppy(), ASOF)
    assert out.regime == "choppy"
    assert out.evidence.endswith("zone=neutral")
    assert out.trend is not None and abs(out.trend) < WEAK_TREND_MIN
    assert out.vol_pct is not None and out.vol_pct <= VOLATILE_VOL_MIN


# ---------------------------------------------------------------------------
# NaN-fail-open (7eb148a): bad data never earns a confident label
# ---------------------------------------------------------------------------


def test_nan_close_yields_unknown():
    closes = make_walk(300, drift=0.002, sigma_start=0.012, sigma_end=0.001, seed=7)
    closes[150] = float("nan")
    out = classify_regime(closes, ASOF)
    assert out.regime == "unknown"
    assert out.trend is None and out.vol_pct is None


def test_inf_close_yields_unknown():
    closes = make_walk(300, drift=0.002, sigma_start=0.012, sigma_end=0.001, seed=7)
    closes[-1] = float("inf")
    assert classify_regime(closes, ASOF).regime == "unknown"


def test_non_positive_close_yields_unknown():
    closes = make_flat_choppy()
    closes[100] = 0.0  # log-return undefined; must fail open, not raise
    assert classify_regime(closes, ASOF).regime == "unknown"


def test_map_state_non_finite_inputs_are_unknown():
    # NaN comparisons are all False: without the 7eb148a guard these would
    # fall through to the flat/neutral zone (= a confident "choppy").
    assert map_state(float("nan"), 0.5)[0] == "unknown"
    assert map_state(float("inf"), 0.5)[0] == "unknown"
    assert map_state(0.3, float("nan"))[0] == "unknown"
    assert map_state(None, 0.5)[0] == "unknown"
    assert map_state(0.3, None)[0] == "unknown"


def test_genuinely_flat_valid_data_is_still_choppy_not_unknown():
    # 7eb148a explicitly preserved: trend exactly 0.0 from VALID data is the
    # honest no-edge state, not insufficient data.
    regime, evidence = map_state(0.0, 0.5)
    assert regime == "choppy"
    assert evidence.endswith("zone=neutral")


# ---------------------------------------------------------------------------
# Insufficient history
# ---------------------------------------------------------------------------


def test_short_history_yields_unknown():
    closes = make_walk(MIN_CLOSES - 1, drift=0.002, sigma_start=0.01, sigma_end=0.01, seed=3)
    out = classify_regime(closes, ASOF)
    assert out.regime == "unknown"
    assert out.evidence == "insufficient_history"
    assert out.trend is None and out.vol_pct is None


def test_exactly_min_closes_classifies():
    closes = make_walk(MIN_CLOSES, drift=0.002, sigma_start=0.01, sigma_end=0.01, seed=3)
    out = classify_regime(closes, ASOF)
    assert out.regime != "unknown"
    assert out.trend is not None and out.vol_pct is not None


# ---------------------------------------------------------------------------
# Dead-zone fill + weak-lean/neutral boundaries (1b4ee61, exact thresholds)
# ---------------------------------------------------------------------------


def test_dead_zone_fill_strong_trend_at_elevated_vol_is_bull():
    # The 1b4ee61 motivating example (2023-01-26: trend +1.61, vol_pct 0.67):
    # vol in (0.60, 0.70] with strong trend fell to UNKNOWN under v0.1 rules;
    # the widened BULL_VOL_MAX (0.60 -> 0.70) classifies it bull -> risk_on.
    regime, evidence = map_state(1.61, 0.67)
    assert regime == "risk_on"
    assert evidence.endswith("zone=bull")


def test_weak_lean_boundary_exact_labels():
    # trend == WEAK_TREND_MIN is INCLUSIVE for the weak lean (>= 0.15)
    regime, evidence = map_state(WEAK_TREND_MIN, 0.65)
    assert regime == "risk_on"
    assert evidence.endswith("zone=bull_weak")
    regime, evidence = map_state(-WEAK_TREND_MIN, 0.65)
    assert regime == "risk_off"
    assert evidence.endswith("zone=bear_weak")
    # just below BULL_TREND_MIN stays a weak lean, at it becomes full bull
    assert map_state(0.49, 0.65)[1].endswith("zone=bull_weak")
    assert map_state(BULL_TREND_MIN, 0.65)[1].endswith("zone=bull")


def test_neutral_zone_inside_weak_floor():
    regime, evidence = map_state(0.149, 0.65)
    assert regime == "choppy"
    assert evidence.endswith("zone=neutral")
    regime, evidence = map_state(-0.149, 0.65)
    assert regime == "choppy"


def test_volatile_boundary_is_strict():
    # vol_pct == 0.70 is still the trend zones (<=); strictly above is volatile
    assert map_state(0.9, VOLATILE_VOL_MIN)[1].endswith("zone=bull")
    regime, evidence = map_state(0.9, VOLATILE_VOL_MIN + 1e-9)
    assert regime == "risk_off"
    assert evidence.endswith("zone=volatile")


# ---------------------------------------------------------------------------
# Purity / determinism / model hygiene
# ---------------------------------------------------------------------------


def test_determinism_same_input_identical_output():
    closes = make_flat_choppy()
    a = classify_regime(closes, ASOF)
    b = classify_regime(list(closes), ASOF)
    assert a == b
    assert a.model_dump() == b.model_dump()


def test_input_not_mutated():
    closes = make_flat_choppy()
    snapshot = list(closes)
    classify_regime(closes, ASOF)
    assert closes == snapshot


def test_asof_must_be_tz_aware():
    closes = make_flat_choppy()
    with pytest.raises(Exception):  # pydantic ValidationError
        classify_regime(closes, datetime(2026, 6, 10, 14, 0))


def test_regime_read_roundtrip():
    out = classify_regime(make_flat_choppy(), ASOF)
    assert RegimeRead.model_validate_json(out.model_dump_json()) == out
    assert out.asof == ASOF
