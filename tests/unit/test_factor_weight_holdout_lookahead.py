"""W4 held-out path — REAL + lookahead-honest (H-W4 fix).

These tests pin the corrected W4 eval-gate behaviour:

  - A candidate with a GENUINE held-out edge (a factor that predicts next-bar returns on the
    strictly-later HOLDOUT window) scores a high OOS DSR + a jitter-stable plateau, beats prior-best,
    and is PROPOSED (eval_passed=True, candidates written).
  - A candidate that only wins IN-SAMPLE (a factor that fits the TRAIN window but has no edge on the
    HOLDOUT) scores a near-zero OOS DSR and an unstable plateau, and is REJECTED (eval_passed=False,
    buffered, never promoted).
  - NO LOOKAHEAD: the split is time-ordered (HOLDOUT strictly post-dates TRAIN) and the proposer
    (evaluate_all → propose_weights) is provably never handed the HOLDOUT bars.
  - Flag-OFF is a byte-identical no-op.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hermes_quant.factors.weight_proposer import (
    FactorWeightProposal,
    FactorWeightProposalSet,
    evaluate_against_holdout,
    score_holdout,
)


# ---------------------------------------------------------------------------
# Synthetic-bar helpers (deterministic, no network).
# ---------------------------------------------------------------------------
def _bars_with_signal(signal: np.ndarray, *, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """Build a price path whose next-bar return is driven by ``signal`` (positive IC).

    Returns ``(bars, factor_series)``: the factor at t leads the return over t→t+1 (matching the
    ic_panel.py convention), so a sign-following composite earns real OOS return.
    """
    n = len(signal)
    rng = np.random.RandomState(seed)
    noise = rng.randn(n) * 0.002
    ret = np.empty(n)
    ret[0] = 0.0
    # signal[t] predicts return over t→t+1: ret[t+1] = scale*signal[t] + noise.
    ret[1:] = 0.01 * signal[:-1] + noise[:-1]
    price = 100.0 * np.cumprod(1.0 + ret)
    idx = pd.date_range("2021-01-01", periods=n, freq="D", tz="UTC")
    bars = pd.DataFrame({"close": price}, index=idx)
    return bars, pd.Series(signal, index=idx)


def _pset_one(factor_id: str, proposed: float) -> FactorWeightProposalSet:
    return FactorWeightProposalSet(
        proposals=[
            FactorWeightProposal(
                factor_id=factor_id,
                current_weight=0.0,
                proposed_weight=proposed,
                verdict_tier="premium",
                reason="test",
            )
        ],
        generated_at="2026-05-31T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# A GENUINE held-out winner IS proposed.
# ---------------------------------------------------------------------------
def test_genuine_holdout_winner_is_proposed():
    n = 260
    rng = np.random.RandomState(11)
    signal = rng.randn(n)
    bars, fser = _bars_with_signal(signal, seed=11)
    ps = _pset_one("good", 0.8)

    dsr, sharpe, plateau = score_holdout(ps, bars, lambda fid, b: fser.reindex(b.index))
    assert dsr > 0.9, dsr            # strong OOS edge
    assert plateau is True           # jitter-stable across folds (robustness)

    out = evaluate_against_holdout(
        ps,
        holdout_dsr=dsr,
        holdout_sharpe_delta=sharpe,
        prior_best_dsr=float("-inf"),  # first run
        plateau_stable=plateau,
    )
    assert out.eval_passed is True   # a genuine held-out winner is proposed


# ---------------------------------------------------------------------------
# A factor that ONLY wins in-sample is REJECTED (not promoted).
# ---------------------------------------------------------------------------
def _no_edge_holdout(seed: int, n: int = 140):
    """A strictly-later HOLDOUT window where the factor has NO genuine edge: zero-mean
    (de-drifted) returns INDEPENDENT of the factor. Models an in-sample-only winner — it fit the
    earlier TRAIN window the proposer saw, but on this later window it is noise."""
    rng = np.random.RandomState(seed)
    ret = rng.randn(n) * 0.01
    ret = ret - ret.mean()  # de-drift so a net-long noise signal earns nothing
    ret[0] = 0.0
    price = 100.0 * np.cumprod(1.0 + ret)
    idx = pd.date_range("2022-06-01", periods=n, freq="D", tz="UTC")
    bars = pd.DataFrame({"close": price}, index=idx)
    factor = pd.Series(rng.randn(n), index=idx)  # independent of returns
    return bars, factor


def test_in_sample_only_winner_is_rejected():
    """A factor that fit TRAIN but is NOISE on the strictly-later HOLDOUT is NOT promoted.

    The full eval gate ANDs three conditions — strictly-beat-prior-best, plateau-stable, bounded.
    A genuine-edge factor clears it on EVERY seed; a no-edge (in-sample-only) factor clears it
    only rarely (by chance), because it has no robust held-out plateau and no consistent edge to
    strictly beat a real prior-best. We assert the contrast over many independent holdout windows
    (the AMZN-weight lesson: select on a robust plateau, never the in-sample peak).
    """
    prior_best = 0.999  # a genuine winner's checkpoint (test_genuine_* scores ~1.0)
    seeds = range(40)

    def passes_gate(bars, factor):
        dsr, sharpe, plateau = score_holdout(
            _pset_one("f", 0.8), bars, lambda fid, b: factor.reindex(b.index)
        )
        out = evaluate_against_holdout(
            _pset_one("f", 0.8),
            holdout_dsr=dsr,
            holdout_sharpe_delta=sharpe,
            prior_best_dsr=prior_best,
            plateau_stable=plateau,
        )
        return out.eval_passed

    # Genuine-edge factors: pass the full gate on (essentially) every seed.
    genuine_pass = sum(
        passes_gate(*_bars_with_signal(np.random.RandomState(s).randn(220), seed=s))
        for s in seeds
    )
    # In-sample-only (no held-out edge) factors: pass only rarely, if ever.
    no_edge_pass = sum(passes_gate(*_no_edge_holdout(s)) for s in seeds)

    assert genuine_pass >= 0.9 * len(seeds)        # a real OOS winner is reliably proposed
    assert no_edge_pass <= 0.2 * len(seeds)        # an in-sample-only winner is reliably rejected
    assert no_edge_pass < genuine_pass             # the gate discriminates real from spurious


# ---------------------------------------------------------------------------
# A one-window spike (not a plateau) is REJECTED even with a high single-shot Sharpe.
# ---------------------------------------------------------------------------
def test_one_window_spike_fails_plateau():
    n = 280
    rng = np.random.RandomState(5)
    # The factor predicts returns ONLY in the first quarter; the rest of the window is pure noise
    # in BOTH the factor AND the price path (the price's edge dies with the factor's edge). With a
    # CAUSAL per-bar z-score the early edge is not diluted by the noise tail, so the later folds
    # have no genuine edge and their Sharpe sign flips → fails the majority-sign robustness rail.
    signal = rng.randn(n)
    signal[n // 4:] = 0.0                          # no predictive content after the first quarter
    bars, _fser = _bars_with_signal(signal, seed=5)  # price edge dies after the first quarter
    spiky = pd.Series(rng.randn(n), index=bars.index)  # factor = noise everywhere (mis-fit tail)
    spiky.iloc[: n // 4] = signal[: n // 4]            # except the genuine first-quarter edge

    _dsr, _sharpe, plateau = score_holdout(
        _pset_one("spike", 0.8), bars, lambda fid, b: spiky.reindex(b.index)
    )
    assert plateau is False  # robustness-not-peak: a single-window spike is not a plateau


# ---------------------------------------------------------------------------
# NO LOOKAHEAD (within-holdout): per-bar factor normalization must be CAUSAL.
#
# The composite z-scores each factor before taking np.sign() as the position. If that z-score
# uses the FULL-window mean/std, bar t's z (and hence its sign/position) depends on bars t+1..T —
# a within-holdout lookahead that contaminates the realized OOS Sharpe → DSR → plateau that gate
# eval_passed. A causal (expanding) z-score makes bar t depend only on bars <= t, so perturbing
# ONLY the future tail of the factor series (earlier factor values AND the entire price/return
# series held byte-identical) cannot change the realized score of the earlier bars.
# ---------------------------------------------------------------------------
def _bars_factor_for_causality(n: int, seed: int) -> tuple[pd.DataFrame, pd.Series]:
    """Price path + a leading factor, both fully determined by ``seed`` (deterministic, no net)."""
    rng = np.random.RandomState(seed)
    signal = rng.randn(n)
    noise = rng.randn(n) * 0.002
    ret = np.empty(n)
    ret[0] = 0.0
    ret[1:] = 0.01 * signal[:-1] + noise[:-1]  # signal[t] leads return over t→t+1
    price = 100.0 * np.cumprod(1.0 + ret)
    idx = pd.date_range("2021-01-01", periods=n, freq="D", tz="UTC")
    bars = pd.DataFrame({"close": price}, index=idx)
    return bars, pd.Series(signal, index=idx)


def test_composite_position_prefix_invariant_to_future_tail():
    """The per-bar position for bars 0..t MUST NOT change when ONLY bars > t move.

    This is the precise causality invariant: a CAUSAL/expanding z-score computes bar t from bars
    <= t, so perturbing strictly-LATER factor values cannot alter the realized position of any
    earlier bar. Under the FULL-window leak, bar t's z uses series.mean()/series.std() over the
    ENTIRE window, so a future-tail shift moves the global mean and flips earlier np.sign()
    positions — a within-holdout lookahead.

    RED on current code: bars 0..79's positions change when bars 80..119 are perturbed by +30.0.
    """
    n = 120
    bars, fser = _bars_factor_for_causality(n, seed=0)
    from hermes_quant.factors.weight_proposer import _composite_position

    pos0 = _composite_position(_pset_one("f", 0.8), bars, lambda fid, b: fser.reindex(b.index))

    perturbed = fser.copy()
    perturbed.iloc[80:] = perturbed.iloc[80:] + 30.0  # ONLY the future tail moves
    pos1 = _composite_position(
        _pset_one("f", 0.8), bars, lambda fid, b: perturbed.reindex(b.index)
    )

    # The position for the unperturbed prefix (bars 0..79) must be byte-identical: those bars' z
    # (and sign) depend only on bars <= t < 80, none of which moved.
    pd.testing.assert_series_equal(pos0.iloc[:80], pos1.iloc[:80])


def test_score_holdout_plateau_invariant_to_future_tail_perturbation():
    """score_holdout's plateau verdict must NOT flip when ONLY the future-tail factor values move.

    Same construction as the prefix-invariance test. The plateau (a robustness verdict over the
    whole window) is the gate input that authorizes a promotion, so we pin that it does not flip on
    a future-only perturbation. On current (buggy) full-window code this flips the realized OOS
    Sharpe ≈20.33 → 2.41 and plateau True → False (seed 0); a causal z-score holds the verdict.
    """
    n = 120
    bars, fser = _bars_factor_for_causality(n, seed=0)

    _dsr0, _sharpe0, plateau0 = score_holdout(
        _pset_one("f", 0.8), bars, lambda fid, b: fser.reindex(b.index)
    )

    perturbed = fser.copy()
    perturbed.iloc[80:] = perturbed.iloc[80:] + 30.0  # ONLY the future tail moves
    _dsr1, _sharpe1, plateau1 = score_holdout(
        _pset_one("f", 0.8), bars, lambda fid, b: perturbed.reindex(b.index)
    )

    assert plateau0 == plateau1, (
        f"plateau verdict changed ({plateau0}→{plateau1}) when ONLY future-tail factor values "
        f"moved — within-holdout lookahead in the per-bar normalization"
    )


# ---------------------------------------------------------------------------
# NO LOOKAHEAD: time-ordered split + the proposer provably never sees the holdout bars.
# ---------------------------------------------------------------------------
def _load_cron_module():
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-factor-weight-propose.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_factor_weight_propose_la", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # neutralize the script's execv guard
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod


@pytest.fixture(scope="module")
def cron():
    return _load_cron_module()


def test_split_is_time_ordered_holdout_strictly_later(cron):
    idx = pd.date_range("2020-01-01", periods=100, freq="D", tz="UTC")
    bars = pd.DataFrame({"close": np.arange(100, 200, dtype=float)}, index=idx)
    train, holdout = cron._split_train_holdout(bars)
    assert len(train) + len(holdout) == len(bars)
    assert len(train) > 0 and len(holdout) > 0
    # Every HOLDOUT timestamp strictly post-dates every TRAIN timestamp (no overlap, no shuffle).
    assert train.index.max() < holdout.index.min()


def test_proposer_never_sees_holdout_bars(cron, monkeypatch):
    """main() must hand the proposer (evaluate_all → propose_weights) the TRAIN bars ONLY.

    We capture the exact frame passed to _build_proposal_set and assert its last timestamp is the
    TRAIN cut — i.e. it contains NONE of the strictly-later holdout rows.
    """
    monkeypatch.setenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "1")
    idx = pd.date_range("2020-01-01", periods=200, freq="D", tz="UTC")
    full = pd.DataFrame(
        {"close": 100.0 + np.arange(200, dtype=float)}, index=idx
    )
    monkeypatch.setattr(cron, "_yf_bars", lambda *a, **k: full)
    monkeypatch.setattr(sys, "argv", ["prog"])

    seen = {}

    def _spy_build(bars):
        seen["bars"] = bars.copy()
        return _pset_one("f1", 0.3), object()  # (proposal_set, zoo) contract

    monkeypatch.setattr(cron, "_build_proposal_set", _spy_build)

    holdout_seen = {}

    def _spy_holdout(proposal_set, holdout_bars, zoo):
        holdout_seen["bars"] = holdout_bars.copy()
        return float("-inf"), 0.0, False  # conservative; does not matter for this assertion

    monkeypatch.setattr(cron, "_compute_holdout", _spy_holdout)
    monkeypatch.setattr(cron, "_BASELINE", Path("/nonexistent-baseline-la.json"))

    rc = cron.main()
    assert rc == 0

    train_seen = seen["bars"]
    hold_seen = holdout_seen["bars"]
    # The proposer's frame and the holdout frame are disjoint in time, and the proposer's frame
    # contains none of the holdout's (strictly-later) timestamps.
    assert train_seen.index.max() < hold_seen.index.min()
    assert not set(train_seen.index).intersection(set(hold_seen.index))
    # The proposer saw strictly fewer rows than the full series (it never got the whole window).
    assert len(train_seen) < len(full)


# ---------------------------------------------------------------------------
# Flag-OFF is a byte-identical no-op (no fetch, no split, no proposer call).
# ---------------------------------------------------------------------------
def test_flag_off_is_noop(cron, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", raising=False)
    monkeypatch.setattr(
        cron, "_yf_bars", lambda *a, **k: pytest.fail("flag-off must not fetch bars")
    )
    monkeypatch.setattr(
        cron, "_build_proposal_set", lambda *a, **k: pytest.fail("flag-off must not propose")
    )
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert cron.main() == 0
    assert capsys.readouterr().out == ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
