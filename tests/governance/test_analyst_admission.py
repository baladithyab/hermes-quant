"""tests/governance/test_analyst_admission.py — DSR/walk-forward OOS admission gate
for analysts joining the committee (seed 908e, anti-overfit lane L3).

Factors already get an eval-gate before any live weight: factors.weight_proposer.
evaluate_against_holdout admits a weight-set iff (held_out_dsr > prior_best_dsr) AND
plateau_stable — STRICT, external-truth (floats/bool only), robustness-not-peak
(ADR-0080 §D80.3). ANALYSTS, by contrast, joined the BMA committee with NO overfit
check (advisor._build_default_analysts appends them unconditionally).

Seed 908e mirrors the factor contract for analysts (it does NOT invent a second
mechanism): the admission decision reuses the SAME DSR instrument
(evaluation.dsr.deflated_sharpe) and the SAME decision contract
(dsr-beats-prior-best AND plateau-stable). The analyst's OOS evidence is the
per-fold Sharpe series the walk_forward_replay instrument already produces.

Contract (HARD INVARIANTS, lane L3):
  * STRICTER never looser — an analyst failing the bar does NOT join; passing joins.
  * FAIL-CLOSED — a tie (dsr == prior_best) reverts (strict >); insufficient
    observations / <2 folds => plateau_stable False => not admitted; a candidate
    with no admission decision does NOT join.
  * EXTERNAL-TRUTH STRUCTURAL — evaluate_analyst_admission takes only floats/bool;
    no analyst object can feed back into the number that grades it (no lookahead).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from hermes_quant.governance.analyst_admission import (
    AnalystAdmissionDecision,
    admit_to_committee,
    evaluate_analyst_admission,
    load_prior_best_dsr,
    save_prior_best_dsr,
    score_analyst_oos,
)


# ---------------------------------------------------------------------------
# the GATE contract — mirrors factors.weight_proposer.evaluate_against_holdout
# ---------------------------------------------------------------------------


def test_admitted_when_beats_prior_best_and_plateau_stable():
    d = evaluate_analyst_admission(
        "newbie",
        holdout_dsr=0.80,
        prior_best_dsr=0.50,
        plateau_stable=True,
    )
    assert isinstance(d, AnalystAdmissionDecision)
    assert d.admitted is True
    assert d.beats_prior_best is True


def test_not_admitted_when_does_not_beat_prior_best():
    d = evaluate_analyst_admission(
        "newbie", holdout_dsr=0.40, prior_best_dsr=0.50, plateau_stable=True
    )
    assert d.admitted is False
    assert d.beats_prior_best is False


def test_tie_reverts_strict_inequality():
    """A tie (dsr == prior_best) must NOT admit — strict >, mirroring the factor
    checkpoint-fallback (a sideways move reverts)."""
    d = evaluate_analyst_admission(
        "newbie", holdout_dsr=0.50, prior_best_dsr=0.50, plateau_stable=True
    )
    assert d.admitted is False


def test_not_admitted_when_not_plateau_stable_even_if_beats_prior():
    """Robustness-not-peak: a high single-window DSR that is NOT plateau-stable
    does not admit (the AMZN-weight lesson, mirrored from score_holdout)."""
    d = evaluate_analyst_admission(
        "spikey", holdout_dsr=0.99, prior_best_dsr=0.10, plateau_stable=False
    )
    assert d.admitted is False
    assert d.plateau_stable is False


def test_first_run_prior_best_neg_inf_admits_any_stable_positive():
    """First-ever candidate: prior_best defaults to -inf, so any plateau-stable
    finite DSR strictly beats it (mirrors load_prior_best_dsr missing -> -inf)."""
    d = evaluate_analyst_admission(
        "first", holdout_dsr=0.55, prior_best_dsr=float("-inf"), plateau_stable=True
    )
    assert d.admitted is True


# ---------------------------------------------------------------------------
# scoring OOS evidence — reuses the DSR instrument (deflated_sharpe)
# ---------------------------------------------------------------------------


def test_score_analyst_oos_returns_dsr_and_plateau():
    """Consistent positive per-fold Sharpes => finite DSR in [0,1] and plateau_stable
    True (low cross-fold dispersion, majority same sign)."""
    fold_sharpes = [1.1, 1.0, 1.2, 0.9]  # 4 OOS folds, all positive, low dispersion
    dsr, plateau = score_analyst_oos(fold_sharpes, n_observations=120)
    assert 0.0 <= dsr <= 1.0
    assert plateau is True


def test_score_analyst_oos_unstable_when_folds_flip_sign():
    """A spike that does not reproduce (folds flip sign / high dispersion) is NOT
    plateau-stable — robustness-not-peak."""
    fold_sharpes = [3.0, -1.0, -0.5, 2.5]  # high dispersion, sign flips
    _dsr, plateau = score_analyst_oos(fold_sharpes, n_observations=120)
    assert plateau is False


def test_score_analyst_oos_insufficient_observations_is_conservative():
    """DSR is meaningless < 30 obs (dsr.py raises). The scorer must fail conservative
    (-inf DSR, plateau False) rather than raise — so a thin-sample analyst is held."""
    dsr, plateau = score_analyst_oos([1.0, 1.1], n_observations=10)
    assert dsr == float("-inf")
    assert plateau is False


def test_score_analyst_oos_single_fold_cannot_establish_plateau():
    """< 2 folds cannot establish robustness -> plateau_stable False (conservative)."""
    _dsr, plateau = score_analyst_oos([1.2], n_observations=120)
    assert plateau is False


# ---------------------------------------------------------------------------
# the JOIN seam — admitted analysts join the committee, failing ones do not
# ---------------------------------------------------------------------------


class _StubAnalyst:
    def __init__(self, name: str):
        self.name = name


def test_failing_analyst_does_not_join_committee_passing_one_does():
    """Seed 908e acceptance: an analyst failing the DSR/OOS admission bar does NOT
    join the committee; a passing one does."""
    passing = _StubAnalyst("passing")
    failing = _StubAnalyst("failing")
    decisions = {
        "passing": evaluate_analyst_admission(
            "passing", holdout_dsr=0.80, prior_best_dsr=0.50, plateau_stable=True
        ),
        "failing": evaluate_analyst_admission(
            "failing", holdout_dsr=0.30, prior_best_dsr=0.50, plateau_stable=True
        ),
    }
    committee = admit_to_committee([passing, failing], decisions=decisions)
    names = [a.name for a in committee]
    assert "passing" in names
    assert "failing" not in names


def test_candidate_without_a_decision_does_not_join_fail_closed():
    """A candidate with NO admission decision must be EXCLUDED (fail-closed — never
    join an analyst that was never gated)."""
    ungated = _StubAnalyst("ungated")
    committee = admit_to_committee([ungated], decisions={})
    assert committee == []


def test_admit_to_committee_preserves_order_of_admitted():
    a, b, c = _StubAnalyst("a"), _StubAnalyst("b"), _StubAnalyst("c")
    decisions = {
        n: evaluate_analyst_admission(n, holdout_dsr=0.9, prior_best_dsr=0.1, plateau_stable=True)
        for n in ("a", "b", "c")
    }
    # b fails
    decisions["b"] = evaluate_analyst_admission(
        "b", holdout_dsr=0.05, prior_best_dsr=0.1, plateau_stable=True
    )
    committee = admit_to_committee([a, b, c], decisions=decisions)
    assert [x.name for x in committee] == ["a", "c"]


# ---------------------------------------------------------------------------
# prior-best checkpoint (mirror of weight_proposer load/save_prior_best_dsr)
# ---------------------------------------------------------------------------


def test_prior_best_checkpoint_round_trip(tmp_path: Path):
    path = tmp_path / "analyst-prior-best.json"
    assert load_prior_best_dsr("anlst", path=path) == float("-inf")  # missing -> -inf
    save_prior_best_dsr("anlst", 0.62, path=path)
    assert load_prior_best_dsr("anlst", path=path) == pytest.approx(0.62)
    # a different analyst id is independent (its own checkpoint, still -inf)
    assert load_prior_best_dsr("other", path=path) == float("-inf")


# ---------------------------------------------------------------------------
# EXTERNAL-TRUTH STRUCTURAL — no analyst object can feed back into the grade
# ---------------------------------------------------------------------------


def test_admission_decision_is_pure_function_of_floats():
    """The decision depends ONLY on the (holdout_dsr, prior_best_dsr, plateau_stable)
    scalars — there is no analyst-object parameter that could let an analyst author
    the number that grades it (the structural no-lookahead guarantee, D80.3)."""
    import inspect

    sig = inspect.signature(evaluate_analyst_admission)
    # analyst_id is a label only; the grading inputs are all float/bool.
    params = sig.parameters
    assert "holdout_dsr" in params
    assert "prior_best_dsr" in params
    assert "plateau_stable" in params
    # Same scalars -> same verdict, regardless of any external state.
    d1 = evaluate_analyst_admission("x", holdout_dsr=0.7, prior_best_dsr=0.6, plateau_stable=True)
    d2 = evaluate_analyst_admission("x", holdout_dsr=0.7, prior_best_dsr=0.6, plateau_stable=True)
    assert d1.admitted == d2.admitted is True


# ---------------------------------------------------------------------------
# NO-LOOKAHEAD: OOS evidence comes from the REAL walk_forward_replay folds
# ---------------------------------------------------------------------------


def _gbm_bars(n: int = 900, *, seed: int = 11, drift: float = 0.05, vol: float = 0.4):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    ts = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    closes = 100 + np.cumsum(rng.normal(drift, vol, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": 1000.0,
        }
    )


def _long_advisor(**kwargs):
    return {
        "as_of": kwargs["as_of"].isoformat(),
        "aggregated_signal": {
            "asset": kwargs.get("symbol", "TEST"),
            "asset_class": kwargs.get("asset_class", "equity"),
            "timeframe": kwargs.get("timeframe", "1h"),
            "direction": 1,
            "magnitude": 0.5,
            "confidence": 0.7,
            "confidence_raw": 0.7,
            "horizon": "1h",
            "aggregator": "bma",
        },
        "risk_gate": {"pass": True, "kelly_fraction": 0.10},
        "analyst_views": [
            {"analyst": "wf_voice", "direction": 1, "magnitude": 0.5,
             "confidence": 0.7, "confidence_raw": 0.7, "horizon": "1h"}
        ],
    }


def test_oos_evidence_from_walk_forward_replay_folds_feeds_admission():
    """End-to-end: the analyst's OOS per-fold Sharpes come from the REAL
    walk_forward_replay (strictly out-of-sample test slices). Those Sharpes feed
    score_analyst_oos -> evaluate_analyst_admission. No future data can reach the
    admission decision because the fold Sharpes are computed on as_of-clamped slices
    and the gate consumes only the resulting scalars."""
    from hermes_quant.backtest import walk_forward_replay

    wf = walk_forward_replay(
        _gbm_bars(900),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=4,
        warmup_bars=20,
        advisor_recommend=_long_advisor,
    )
    # every fold is a strictly out-of-sample slice (the leak-free structure):
    for fold in wf.folds:
        assert fold.split.train_end < fold.split.val_start
        assert fold.split.val_end <= fold.split.test_start

    fold_sharpes = [f.result.sharpe for f in wf.folds]
    n_obs = wf.total_settlements
    dsr, plateau = score_analyst_oos(fold_sharpes, n_observations=max(n_obs, 30))
    decision = evaluate_analyst_admission(
        "wf_voice", holdout_dsr=dsr, prior_best_dsr=float("-inf"), plateau_stable=plateau
    )
    # The decision is a deterministic function of the OOS scalars (admit iff the
    # genuine OOS DSR is finite, beats -inf, and the folds form a stable plateau).
    assert decision.admitted == (math.isfinite(dsr) and dsr > float("-inf") and plateau)
