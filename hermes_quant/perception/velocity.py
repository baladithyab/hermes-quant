"""hermes_quant.perception.velocity — TrendVelocity DETECT primitive (ADR-0079 PDR-2, GAP-A).

Week-over-week ACCELERATION of an interest series vs a trailing baseline. The Camillo
DETECT edge is in the SLOPE, not the severity (design pdr-unified-architecture.md §3.1).
This module is PURE: it scores a pre-built series and stamps asof. It reads NO flag and
does NO I/O — the HERMES_QUANT_TREND_VELOCITY gate lives at the call site (builder.py),
exactly as build_regime_extras is pure and the advisor gates it.

Lookahead-honest by construction: callers MUST pass a series already truncated to
observations with timestamp <= asof; the score stamps that same asof (ADR-0079 D-4).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

# Magnitude floor/ceiling mirror the severity scale the packet carries today
# (classify.py weights live in ~0.0..0.06; synthesize rounds to 4dp). Keeping the
# velocity-sourced magnitude inside the SAME band means a flag flip cannot widen the
# discrete ladder or hand BMA an out-of-distribution magnitude.
VELOCITY_MAGNITUDE_FLOOR = 0.0
VELOCITY_MAGNITUDE_CEIL = 0.06          # == max lexicon weight (classify.py "bankruptcy"=0.06)
_BASELINE_MIN_PERIODS = 3               # need >= this many trailing periods or we abstain (return None)


@dataclass(frozen=True)
class VelocityScore:
    velocity: float           # week-over-week acceleration: (last - prev) normalized by baseline mean
    baseline_z: float         # z-score of the latest period vs the trailing baseline
    asof: pd.Timestamp        # the series cutoff = the score's lookahead anchor (UTC)
    n_periods: int            # how many trailing periods fed the baseline (provenance)
    peak_period: pd.Timestamp | None  # period of the series max (PDR-4 "past the velocity peak" reads this)

    def to_mapping(self) -> dict:
        return {
            "velocity": round(float(self.velocity), 6),
            "baseline_z": round(float(self.baseline_z), 6),
            "asof": self.asof.isoformat(),
            "n_periods": int(self.n_periods),
            "peak_period": self.peak_period.isoformat() if self.peak_period is not None else None,
        }


def counts_per_period(
    timestamps: Sequence[datetime | pd.Timestamp],
    *,
    asof: datetime | pd.Timestamp,
    freq: str = "W",
) -> pd.Series:
    """Bucket observation timestamps (CatalystItem.published_at) into a per-period count
    series, truncated to <= asof (NO future buckets). Empty -> empty Series. Pure."""
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    ts = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    ts = ts[ts <= asof_ts]                       # LOOKAHEAD CUT — only past observations
    if ts.empty:
        return pd.Series(dtype="int64")
    # Drop tz to naive-UTC BEFORE to_period: the cut above already ran on tz-aware
    # data, and to_period buckets on the UTC wall-clock either way — doing the tz
    # drop explicitly yields identical buckets while silencing pandas' UserWarning
    # ("Converting to PeriodArray will drop timezone information"). Behavior-identical.
    return ts.dt.tz_localize(None).dt.to_period(freq).value_counts().sort_index()


def compute_trend_velocity(
    counts: pd.Series,            # index = period, value = item count (from counts_per_period)
    *,
    asof: datetime | pd.Timestamp,
) -> VelocityScore | None:
    """Score week-over-week acceleration vs a trailing baseline.

    velocity   = (latest_count - prev_count) / max(baseline_mean, 1.0)
    baseline_z = (latest_count - baseline_mean) / max(baseline_std, 1.0)
    baseline   = all periods BEFORE the latest (the trailing window).

    Returns None (abstain -> magnitude falls back to severity) when there are fewer than
    _BASELINE_MIN_PERIODS+1 periods — silence-by-default; never fabricate a slope from noise.
    """
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    if counts is None or len(counts) < _BASELINE_MIN_PERIODS + 1:
        return None
    vals = counts.to_numpy(dtype=float)
    latest, prev = vals[-1], vals[-2]
    baseline = vals[:-1]
    baseline_mean = float(baseline.mean())
    baseline_std = float(baseline.std(ddof=0))
    velocity = (latest - prev) / max(baseline_mean, 1.0)
    baseline_z = (latest - baseline_mean) / max(baseline_std, 1.0)
    peak_idx = int(vals.argmax())
    peak_period = pd.Timestamp(counts.index[peak_idx].start_time, tz="UTC")
    return VelocityScore(
        velocity=velocity, baseline_z=baseline_z, asof=asof_ts,
        n_periods=len(baseline), peak_period=peak_period,
    )


def velocity_magnitude(score_mapping: Mapping[str, object] | None) -> float | None:
    """Map a velocity score Mapping (frame.trend_velocity[sym]) to a packet magnitude in
    the SAME band as severity ([0, VELOCITY_MAGNITUDE_CEIL]). Returns None when there is
    no score (caller falls back to severity). A higher baseline_z -> larger magnitude.

    Bounded by construction: a flag flip can NEVER hand BMA a magnitude outside the band
    the discrete ladder was calibrated against (rail #2, design §3.1)."""
    if not score_mapping:
        return None
    z = float(score_mapping.get("baseline_z", 0.0))
    # squash z>=0 into [floor, ceil]; negative/decelerating z -> floor (no demand spike).
    if z <= 0.0:
        return VELOCITY_MAGNITUDE_FLOOR
    frac = z / (z + 2.0)                          # smooth, monotone, in (0,1); z=2 -> 0.5
    return round(VELOCITY_MAGNITUDE_FLOOR + frac * (VELOCITY_MAGNITUDE_CEIL - VELOCITY_MAGNITUDE_FLOOR), 4)


__all__ = ["VelocityScore", "counts_per_period", "compute_trend_velocity", "velocity_magnitude"]
