"""quantcore.regime — heuristic market-regime classifier (pure, stdlib + pydantic).

Lean port of hermes_quant.regime.detector (rule state machine) and
hermes_quant.regime.state_variables (trend / vol-percentile inputs) as of
hermes-quant commit 7eb148a (2026-06-09). Two recent hermes fixes are ported
faithfully:

  1b4ee61  "fill the 0.60-0.70 dead zone with weak-lean + neutral zones"
           - BULL vol ceiling widened 0.60 -> 0.70 (symmetric with BEAR), so a
             strong uptrend at moderately-elevated vol no longer falls into a
             classification hole (14.1% of 6y SPY days under the old rules).
           - Weak-lean band: 0.15 <= |trend| < 0.5 at non-volatile vol leans
             bull_weak / bear_weak (moderate conviction, not silence).
           - Genuinely flat trend (|trend| < 0.15) is an honest "no edge"
             neutral state, NOT unknown.
           - "unknown" is reserved STRICTLY for insufficient/missing data.
  7eb148a  "guard non-finite vol_pct/trend -> UNKNOWN (NaN-fail-open)"
           - ANY non-finite intermediate maps to "unknown". All NaN comparisons
             are False, so without the guard a NaN trend would fail every
             threshold check and fall through to the flat zone — labeling bad
             data as a valid regime. Bad data must never earn a confident label.

Hermes' seven internal zones map onto the four-label cowork-quant taxonomy
(the hermes zone name is preserved at the end of `evidence`):

  hermes zone   rule (trend t, vol percentile v)       cowork label
  -----------   -----------------------------------    ------------
  volatile      v > 0.70                               risk_off
  bear          t <= -0.5  and v <= 0.70               risk_off
  bull          t >= +0.5  and v <= 0.70               risk_on
  bull_weak     +0.15 <= t < +0.5 and v <= 0.70        risk_on
  bear_weak     -0.5 < t <= -0.15 and v <= 0.70        risk_off
  neutral       |t| < 0.15 and v <= 0.70               choppy
  unknown       insufficient / non-finite data         unknown

(volatile -> risk_off: an extreme realized-vol percentile is a de-risk signal
regardless of trend direction, mirroring hermes' "high volatility overrides
trend signal" check, which runs FIRST.)

State variables (stdlib re-implementation; no numpy/pandas):

  trend   = (SMA20 - SMA50) / stdev(last 50 closes)     sample stdev, ddof=1;
            a 20-vs-50 SMA spread normalized by 50-bar dispersion (hermes uses
            (close - SMA50) / stdev50; B-03 specifies the 20-vs-50 relation)
  vol_pct = empirical-CDF rank (strictly-below fraction, hermes convention) of
            the current 20-day realized vol — stdev(log returns) * sqrt(252) —
            within the rolling 20d vols over the trailing 252 returns or the
            available history (hermes uses a 60d vol window; B-03 specifies 20d)

classify_regime is a pure function: no I/O, no globals, deterministic.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

UTC = timezone.utc

Regime = Literal["risk_on", "risk_off", "choppy", "unknown"]

# ---------------------------------------------------------------------------
# Thresholds — verbatim from hermes_quant/regime/detector.py @ 7eb148a
# ---------------------------------------------------------------------------

#: BULL: trend >= BULL_TREND_MIN AND vol_pct <= BULL_VOL_MAX
BULL_TREND_MIN: float = 0.5
#: 1b4ee61: widened 0.60 -> 0.70, symmetric with BEAR_VOL_MAX. The old 0.60
#: ceiling left a 0.60-0.70 hole where strong uptrends fell to unknown.
BULL_VOL_MAX: float = 0.7

#: BEAR: trend <= BEAR_TREND_MAX AND vol_pct <= BEAR_VOL_MAX
BEAR_TREND_MAX: float = -0.5
BEAR_VOL_MAX: float = 0.7

#: VOLATILE: vol_pct > VOLATILE_VOL_MIN (checked first; overrides trend)
VOLATILE_VOL_MIN: float = 0.7

#: 1b4ee61 weak-lean band: |trend| in [WEAK_TREND_MIN, BULL_TREND_MIN) at
#: non-volatile vol leans bull_weak / bear_weak. Below it: genuinely flat.
WEAK_TREND_MIN: float = 0.15

# ---------------------------------------------------------------------------
# State-variable windows
# ---------------------------------------------------------------------------

MIN_CLOSES: int = 60  # need SMA50 plus a usable vol-ranking tail
SMA_FAST: int = 20
SMA_SLOW: int = 50
VOL_WINDOW: int = 20  # 20d realized vol per B-03 (hermes uses 60d)
VOL_LOOKBACK: int = 252  # rank within trailing ~1y of returns
TRADING_DAYS: float = 252.0


class RegimeRead(BaseModel):
    """One regime classification. trend/vol_pct are None when unavailable."""

    regime: Regime
    trend: float | None = Field(default=None, description="(SMA20-SMA50)/stdev50")
    vol_pct: float | None = Field(
        default=None, ge=0.0, le=1.0, description="20d realized-vol percentile in [0,1]"
    )
    evidence: str = Field(min_length=1, description="rule that fired, incl. hermes zone name")
    asof: datetime

    @field_validator("asof")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("asof must be tz-aware (UTC)")
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# State-variable computation (stdlib)
# ---------------------------------------------------------------------------


def _sma(closes: list[float], n: int) -> float:
    return math.fsum(closes[-n:]) / n


def _trend_strength(closes: list[float]) -> float | None:
    """(SMA20 - SMA50) / sample-stdev(last 50 closes). None if < 50 bars."""
    if len(closes) < SMA_SLOW:
        return None
    sd = statistics.stdev(closes[-SMA_SLOW:])
    if sd < 1e-12:
        # Perfectly flat window: genuinely no trend (hermes convention),
        # NOT a divide-by-zero NaN.
        return 0.0
    return (_sma(closes, SMA_FAST) - _sma(closes, SMA_SLOW)) / sd


def _vol_percentile(closes: list[float]) -> float | None:
    """Strictly-below empirical-CDF rank of current 20d vol vs trailing window.

    Hermes convention (state_variables._compute_vol_percentile): the current
    window is part of the distribution and the rank is the fraction of rolling
    vols STRICTLY below the current one, clamped to [0, 1].
    """
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    rets = rets[-VOL_LOOKBACK:]
    if len(rets) < VOL_WINDOW + 1:
        return None
    vols: list[float] = []
    for i in range(VOL_WINDOW, len(rets) + 1):
        vols.append(statistics.stdev(rets[i - VOL_WINDOW : i]) * math.sqrt(TRADING_DAYS))
    if not all(math.isfinite(v) for v in vols):
        return float("nan")  # propagate -> NaN-fail-open guard in map_state
    current = vols[-1]
    below = sum(1 for v in vols if v < current)
    return min(1.0, max(0.0, below / len(vols)))


# ---------------------------------------------------------------------------
# Rule state machine — port of RegimeDetector.classify @ 7eb148a
# ---------------------------------------------------------------------------


def map_state(trend: float | None, vol_pct: float | None) -> tuple[Regime, str]:
    """Map (trend, vol_pct) to a cowork regime label. Check order is hermes':

    vol guards -> volatile -> trend guards -> bear -> bull -> weak leans -> neutral
    """
    # --- insufficient / bad data -> unknown (never a confident label) ---
    if vol_pct is None:
        return "unknown", "vol_pct is None -> zone=unknown"
    if not math.isfinite(vol_pct):
        # 7eb148a NaN-fail-open guard: a non-finite vol_pct must NOT reach any
        # threshold comparison (all NaN comparisons are False).
        return "unknown", f"vol_pct is non-finite ({vol_pct!r}) -> zone=unknown"

    # --- VOLATILE: high vol percentile overrides the trend signal ---
    if vol_pct > VOLATILE_VOL_MIN:
        return (
            "risk_off",
            f"vol_pct={vol_pct:.3f} > {VOLATILE_VOL_MIN} -> zone=volatile",
        )

    if trend is None:
        return "unknown", f"trend is None; vol_pct={vol_pct:.3f} -> zone=unknown"
    if not math.isfinite(trend):
        # 7eb148a: without this, NaN trend falls through to the flat zone,
        # mislabeling bad data as a valid neutral regime.
        return (
            "unknown",
            f"trend is non-finite ({trend!r}); vol_pct={vol_pct:.3f} -> zone=unknown",
        )

    # --- BEAR ---
    if trend <= BEAR_TREND_MAX:
        return (
            "risk_off",
            f"trend={trend:.3f} <= {BEAR_TREND_MAX} AND "
            f"vol_pct={vol_pct:.3f} <= {BEAR_VOL_MAX} -> zone=bear",
        )

    # --- BULL (1b4ee61: vol ceiling 0.70 — the former 0.60-0.70 dead zone) ---
    if trend >= BULL_TREND_MIN:
        return (
            "risk_on",
            f"trend={trend:.3f} >= {BULL_TREND_MIN} AND "
            f"vol_pct={vol_pct:.3f} <= {BULL_VOL_MAX} -> zone=bull",
        )

    # --- 1b4ee61 weak-lean zones (moderate trend, non-volatile vol) ---
    if trend >= WEAK_TREND_MIN:
        return (
            "risk_on",
            f"trend={trend:.3f} in [{WEAK_TREND_MIN}, {BULL_TREND_MIN}) AND "
            f"vol_pct={vol_pct:.3f} <= {BULL_VOL_MAX} -> zone=bull_weak",
        )
    if trend <= -WEAK_TREND_MIN:
        return (
            "risk_off",
            f"trend={trend:.3f} in ({BEAR_TREND_MAX}, -{WEAK_TREND_MIN}] AND "
            f"vol_pct={vol_pct:.3f} <= {BEAR_VOL_MAX} -> zone=bear_weak",
        )

    # --- NEUTRAL: genuinely flat trend at moderate vol (honest "no edge") ---
    return (
        "choppy",
        f"trend={trend:.3f} within +/-{WEAK_TREND_MIN}, "
        f"vol_pct={vol_pct:.3f}: flat -> zone=neutral",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_regime(closes: list[float], asof: datetime) -> RegimeRead:
    """Classify the market regime from >= 60 chronological daily closes.

    Pure and deterministic. Any non-finite or non-positive close, and any
    non-finite intermediate, yields "unknown" (NaN-fail-open, 7eb148a):
    bad data never earns a confident label.
    """
    if len(closes) < MIN_CLOSES:
        return RegimeRead(
            regime="unknown", trend=None, vol_pct=None,
            evidence="insufficient_history", asof=asof,
        )
    if any(
        not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0.0
        for c in closes
    ):
        return RegimeRead(
            regime="unknown", trend=None, vol_pct=None,
            evidence="non_finite_or_non_positive_close -> zone=unknown",
            asof=asof,
        )

    trend = _trend_strength(closes)
    vol_pct = _vol_percentile(closes)
    regime, evidence = map_state(trend, vol_pct)

    def _finite_or_none(x: float | None) -> float | None:
        return x if x is not None and math.isfinite(x) else None

    return RegimeRead(
        regime=regime,
        trend=_finite_or_none(trend),
        vol_pct=_finite_or_none(vol_pct),
        evidence=evidence,
        asof=asof,
    )
