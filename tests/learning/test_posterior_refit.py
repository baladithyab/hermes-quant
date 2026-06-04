"""c96e — asof-honest, recency-weighted Beta posterior refit.

The refit takes a list of per-analyst correctness samples (each carrying the
timestamp at which its outcome became *observable*) plus a decision asof, and
produces a Beta(alpha, beta) posterior in which:

  - samples whose outcome was NOT yet observable at the decision asof are
    excluded (NO LOOKAHEAD — the #1 invariant for this lane);
  - older observable samples are down-weighted by an exponential recency decay
    so the posterior tracks the analyst's *current* skill, not ancient history;
  - with an empty sample set the posterior is exactly the prior (cold-start
    safe — never a crash, never a degenerate 0).

These tests are pure-Python and offline: no sklearn, no torch, no network.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.learning.posterior_refit import SkillSample, refit_beta


PRIOR_ALPHA = 5.0
PRIOR_BETA = 5.0
# A half-life expressed in days: a sample observable exactly one half-life
# before the decision contributes half the weight of a brand-new sample.
HALF_LIFE_DAYS = 30.0


def _sample(observable_at: str, correct: bool) -> SkillSample:
    return SkillSample(observable_asof=pd.Timestamp(observable_at, tz="UTC"), correct=correct)


def test_empty_samples_returns_prior_exactly():
    """Cold-start: no samples → posterior IS the prior, no crash, no zero."""
    alpha, beta = refit_beta(
        samples=[],
        decision_asof=pd.Timestamp("2026-06-01", tz="UTC"),
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    assert alpha == PRIOR_ALPHA
    assert beta == PRIOR_BETA


def test_future_observable_samples_are_excluded_no_lookahead():
    """A sample whose outcome becomes observable AT OR AFTER the decision asof
    must NOT influence the posterior — using it would be lookahead."""
    decision = pd.Timestamp("2026-06-01", tz="UTC")
    samples = [
        _sample("2026-05-15", correct=True),   # observable before decision → counts
        _sample("2026-06-01", correct=True),   # observable exactly at decision → excluded
        _sample("2026-06-10", correct=True),   # observable after decision → excluded
    ]
    alpha, beta = refit_beta(
        samples=samples,
        decision_asof=decision,
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    # Only the single pre-decision correct sample (weight ~1.0 at/near asof) is
    # admitted. It adds to alpha; beta is untouched. Crucially the two
    # future-observable correct samples did NOT inflate alpha.
    alpha_only_past, _ = refit_beta(
        samples=[_sample("2026-05-15", correct=True)],
        decision_asof=decision,
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    assert alpha == alpha_only_past
    assert beta == PRIOR_BETA


def test_recency_decay_downweights_old_samples():
    """Two correct samples — one recent, one old — must not contribute equally.
    The older one is down-weighted, so it raises alpha LESS than the recent one."""
    decision = pd.Timestamp("2026-06-01", tz="UTC")

    alpha_recent, _ = refit_beta(
        samples=[_sample("2026-05-31", correct=True)],  # 1 day old → weight ~1.0
        decision_asof=decision,
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    alpha_old, _ = refit_beta(
        samples=[_sample("2026-01-01", correct=True)],  # ~5 months old → small weight
        decision_asof=decision,
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    # Both lift alpha above the prior, but the recent sample lifts it more.
    assert alpha_recent > alpha_old > PRIOR_ALPHA


def test_half_life_weight_is_one_half():
    """A correct sample observable exactly one half-life before the decision
    contributes weight 0.5 — alpha increases by exactly 0.5."""
    decision = pd.Timestamp("2026-06-01", tz="UTC")
    one_half_life_ago = decision - pd.Timedelta(days=HALF_LIFE_DAYS)
    alpha, beta = refit_beta(
        samples=[SkillSample(observable_asof=one_half_life_ago, correct=True)],
        decision_asof=decision,
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    assert alpha == PRIOR_ALPHA + 0.5
    assert beta == PRIOR_BETA


def test_incorrect_samples_raise_beta_not_alpha():
    """A recency-weighted incorrect sample lifts beta, leaving alpha at prior."""
    decision = pd.Timestamp("2026-06-01", tz="UTC")
    alpha, beta = refit_beta(
        samples=[_sample("2026-05-31", correct=False)],
        decision_asof=decision,
        prior_alpha=PRIOR_ALPHA,
        prior_beta=PRIOR_BETA,
        half_life_days=HALF_LIFE_DAYS,
    )
    assert alpha == PRIOR_ALPHA
    assert beta > PRIOR_BETA


def test_refit_is_deterministic():
    """Same samples + same asof → identical posterior on repeat calls."""
    decision = pd.Timestamp("2026-06-01", tz="UTC")
    samples = [
        _sample("2026-05-01", correct=True),
        _sample("2026-05-20", correct=False),
        _sample("2026-05-30", correct=True),
    ]
    first = refit_beta(samples, decision, PRIOR_ALPHA, PRIOR_BETA, HALF_LIFE_DAYS)
    second = refit_beta(samples, decision, PRIOR_ALPHA, PRIOR_BETA, HALF_LIFE_DAYS)
    assert first == second
