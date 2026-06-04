"""c96e — asof-honest, recency-weighted Beta posterior refit.

Given a per-analyst stream of directional-correctness samples, each tagged with
the timestamp at which its outcome became *observable*, ``refit_beta`` produces
a Beta(alpha, beta) posterior for use as a per-analyst weight (and, for f254, as
a per-analyst calibration prior).

Two properties make this safe for money-software backtests:

1. **No lookahead.** Only samples whose outcome was observable *strictly before*
   the decision asof are admitted. The decision asof is the timestamp of the
   decision being made; a sample observable at or after that instant did not
   exist yet and must not move the posterior.

2. **Recency decay.** Each admitted sample is weighted by ``0.5 ** (age /
   half_life)`` so the posterior tracks the analyst's *current* skill. A sample
   observable exactly one half-life before the decision contributes weight 0.5.

With no admitted samples the posterior is exactly the prior — cold-start safe:
never a crash, never a degenerate zero that silently removes an analyst.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SkillSample:
    """One settled directional-correctness observation for a single analyst.

    Attributes
    ----------
    observable_asof:
        UTC timestamp at which this sample's outcome became *knowable*. For a
        view with horizon H decided at D, this is D + H (the close of the
        horizon), NOT D. This is the field the no-lookahead filter keys on.
    correct:
        Whether the analyst's directional call was right.
    """

    observable_asof: pd.Timestamp
    correct: bool


def _recency_weight(age_seconds: float, half_life_seconds: float) -> float:
    """Exponential decay weight in (0, 1]. Age 0 → 1.0; one half-life → 0.5.

    A non-positive half-life disables decay (every admitted sample weighs 1.0),
    which keeps the function total and defensive against misconfiguration.
    """
    if half_life_seconds <= 0.0:
        return 1.0
    # age is clamped at 0: a sample observable just before the decision asof has
    # a tiny positive age; we never produce a weight > 1.
    age = max(0.0, age_seconds)
    return float(0.5 ** (age / half_life_seconds))


def refit_beta(
    samples: list[SkillSample],
    decision_asof: pd.Timestamp,
    prior_alpha: float,
    prior_beta: float,
    half_life_days: float,
) -> tuple[float, float]:
    """Recency-weighted, asof-honest Beta posterior.

    Parameters
    ----------
    samples:
        Per-analyst correctness samples. Order-independent.
    decision_asof:
        The decision timestamp. Only samples with ``observable_asof <
        decision_asof`` are admitted (strict — equal means the outcome became
        knowable at the decision instant, which we treat as not-yet-available).
    prior_alpha, prior_beta:
        The Beta prior (e.g. the BMA cold-start Beta(5, 5)).
    half_life_days:
        Recency half-life in days. <= 0 disables decay.

    Returns
    -------
    (alpha, beta) with alpha >= prior_alpha and beta >= prior_beta.
    """
    decision_asof = _as_utc(decision_asof)
    half_life_seconds = float(half_life_days) * 86_400.0

    alpha = float(prior_alpha)
    beta = float(prior_beta)

    for sample in samples:
        observable = _as_utc(sample.observable_asof)
        # NO-LOOKAHEAD GUARD: outcome must have been observable strictly before
        # the decision. This is the single most important line in the lane.
        if observable >= decision_asof:
            continue
        age_seconds = (decision_asof - observable).total_seconds()
        weight = _recency_weight(age_seconds, half_life_seconds)
        if sample.correct:
            alpha += weight
        else:
            beta += weight

    return alpha, beta


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalize a timestamp to tz-aware UTC so comparisons never mix naive/aware."""
    ts = pd.Timestamp(ts)
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")
