"""PDR-2 TrendVelocity producer math + magnitude band + lookahead cut (plan §6 T1a/b/c).

Pure, deterministic, offline. No flag, no I/O — the producer is gated by its caller.
"""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.perception.velocity import (
    VELOCITY_MAGNITUDE_CEIL,
    VELOCITY_MAGNITUDE_FLOOR,
    compute_trend_velocity,
    counts_per_period,
    velocity_magnitude,
)


def _weekly_timestamps(counts_by_week: list[int], *, start: str = "2021-01-04T00:00:00Z"):
    """Emit observation timestamps so that week k has counts_by_week[k] observations.
    start is a Monday so weekly buckets line up cleanly."""
    base = pd.Timestamp(start)
    out: list[pd.Timestamp] = []
    for wk, n in enumerate(counts_by_week):
        for i in range(n):
            out.append(base + pd.Timedelta(weeks=wk) + pd.Timedelta(hours=i))
    return out


# ---------------------------------------------------------------------------
# T1a — velocity / baseline_z math + abstain + peak_period
# ---------------------------------------------------------------------------
def test_t1a_velocity_math_on_known_series():
    ts = _weekly_timestamps([1, 1, 1, 8])
    asof = pd.Timestamp("2021-01-27T00:00:00Z")  # inside the 4th week
    counts = counts_per_period(ts, asof=asof, freq="W")
    assert list(counts.values) == [1, 1, 1, 8]
    sc = compute_trend_velocity(counts, asof=asof)
    assert sc is not None
    # baseline = [1,1,1] -> mean=1, std=0 -> max(std,1)=1; latest=8
    # velocity = (8 - 1) / max(1,1) = 7.0 ; baseline_z = (8-1)/max(0,1) = 7.0
    assert sc.velocity == pytest.approx(7.0)
    assert sc.baseline_z == pytest.approx(7.0)
    assert sc.n_periods == 3
    assert sc.asof == asof


def test_t1a_flat_series_has_zero_velocity():
    ts = _weekly_timestamps([3, 3, 3, 3])
    asof = pd.Timestamp("2021-01-27T00:00:00Z")
    counts = counts_per_period(ts, asof=asof, freq="W")
    sc = compute_trend_velocity(counts, asof=asof)
    assert sc is not None
    assert sc.velocity == pytest.approx(0.0)
    assert sc.baseline_z == pytest.approx(0.0)


def test_t1a_too_few_periods_abstains():
    # only 3 periods -> < _BASELINE_MIN_PERIODS + 1 (==4) -> None (silence-by-default)
    ts = _weekly_timestamps([1, 1, 5])
    asof = pd.Timestamp("2021-01-20T00:00:00Z")
    counts = counts_per_period(ts, asof=asof, freq="W")
    assert len(counts) == 3
    assert compute_trend_velocity(counts, asof=asof) is None


def test_t1a_peak_period_points_at_max_bucket():
    # max is week index 2 (count 9); peak_period must be that bucket's start.
    ts = _weekly_timestamps([1, 2, 9, 4])
    asof = pd.Timestamp("2021-01-27T00:00:00Z")
    counts = counts_per_period(ts, asof=asof, freq="W")
    sc = compute_trend_velocity(counts, asof=asof)
    assert sc is not None
    assert sc.peak_period is not None
    # the period whose count is 9 is the 3rd weekly bucket (starts 2021-01-18 UTC).
    max_period = counts.index[int(counts.to_numpy().argmax())]
    expected = pd.Timestamp(max_period.start_time, tz="UTC")
    assert sc.peak_period == expected


def test_t1a_empty_series_abstains():
    assert compute_trend_velocity(pd.Series(dtype="int64"), asof=pd.Timestamp("2021-01-01T00:00:00Z")) is None


# ---------------------------------------------------------------------------
# T1b — magnitude band (rail #2): output ALWAYS in [FLOOR, CEIL] for any z
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "z",
    [-1e9, -100.0, -5.0, -0.001, 0.0, 0.001, 0.5, 2.0, 5.0, 50.0, 1e6, 1e9],
)
def test_t1b_magnitude_always_in_band(z):
    m = velocity_magnitude({"baseline_z": z})
    assert m is not None
    assert VELOCITY_MAGNITUDE_FLOOR <= m <= VELOCITY_MAGNITUDE_CEIL, (
        f"z={z} produced out-of-band magnitude {m} (rail #2: a flag flip cannot widen the ladder)"
    )


def test_t1b_negative_or_zero_z_is_floor():
    assert velocity_magnitude({"baseline_z": -5.0}) == VELOCITY_MAGNITUDE_FLOOR
    assert velocity_magnitude({"baseline_z": 0.0}) == VELOCITY_MAGNITUDE_FLOOR


def test_t1b_huge_z_stays_under_ceil():
    # frac = z/(z+2) -> 1 as z->inf, but never reaches CEIL exactly for finite z.
    assert velocity_magnitude({"baseline_z": 1e9}) <= VELOCITY_MAGNITUDE_CEIL


def test_t1b_monotone_in_z():
    vals = [velocity_magnitude({"baseline_z": z}) for z in (0.5, 1.0, 2.0, 4.0, 8.0)]
    assert vals == sorted(vals), "magnitude must be monotone non-decreasing in z"


def test_t1b_none_and_empty_map_return_none():
    assert velocity_magnitude(None) is None
    assert velocity_magnitude({}) is None


# ---------------------------------------------------------------------------
# T1c — lookahead cut in counts_per_period: only observations <= asof
# ---------------------------------------------------------------------------
def test_t1c_counts_per_period_cuts_future():
    asof = pd.Timestamp("2026-01-15T00:00:00Z")
    past = [pd.Timestamp(f"2026-01-{d:02d}T00:00:00Z") for d in (1, 2, 8, 9, 14)]
    future = [pd.Timestamp("2026-01-25T00:00:00Z")] * 50  # LOUD future spike
    counts = counts_per_period(past + future, asof=asof, freq="W")
    # no bucket may start after asof:
    for period in counts.index:
        assert pd.Timestamp(period.start_time, tz="UTC") <= asof, (
            f"future bucket {period} leaked past asof={asof}"
        )
    # the 50-count future spike must NOT appear:
    assert int(counts.sum()) == len(past)


def test_t1c_naive_asof_is_localized_utc():
    asof_naive = pd.Timestamp("2026-01-15T00:00:00")  # no tz
    past = [pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-10T00:00:00Z")]
    counts = counts_per_period(past, asof=asof_naive, freq="W")
    assert int(counts.sum()) == 2
