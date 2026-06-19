"""W4 factor-weight proposer cron — flag-OFF no-op + change-detecting watchdog (AC-11..AC-13).

The ops script (quant-factor-weight-propose.py) is a DEFAULT-OFF no_agent watchdog: with the flag
unset it is a byte-identical no-op (writes nothing); with the flag on it is silent unless a factor
crosses a tier boundary or the eval verdict flips. We drive the pure watchdog logic + main()'s
gate with monkeypatched internals (no network) and a tmp baseline.

Also includes the O6-still-upstream-blocked regression: W4 must NOT have lifted the
settlement_loop slippage_only skip that blocks aggregator.update() (BMA Beta-posterior learning).
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
)


def _load_cron_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-factor-weight-propose.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_factor_weight_propose", path)
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


def _pset(tiers: dict[str, str]) -> FactorWeightProposalSet:
    """Build a proposal set from {factor_id: tier} without running the oracle."""
    props = [
        FactorWeightProposal(
            factor_id=fid,
            current_weight=0.2,
            proposed_weight=0.3,
            verdict_tier=tier,
            reason="test",
        )
        for fid, tier in tiers.items()
    ]
    return FactorWeightProposalSet(proposals=props, generated_at="2026-05-30T00:00:00+00:00")


# ---------------------------------------------------------------------------
# AC-11 — cron is a no-op when the flag is OFF (byte-identical off-state, D80.8)
# ---------------------------------------------------------------------------
def test_cron_is_noop_when_flag_off(cron, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", raising=False)
    cand = tmp_path / "weight-candidates.json"
    # If main() did ANY work it would touch these; assert it doesn't.
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    # If the flag were honored as ON, _yf_bars would be called — make it explode if so.
    monkeypatch.setattr(cron, "_yf_bars", lambda *a, **k: pytest.fail("flag-off must not fetch"))
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert not cand.exists()
    assert not (tmp_path / "baseline.json").exists()
    # Also explicitly "0".
    monkeypatch.setenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "0")
    assert cron._flag_on() is False
    assert cron.main() == 0


def test_flag_on_recognized(cron, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "1")
    assert cron._flag_on() is True


# ---------------------------------------------------------------------------
# AC-12 — silent unless a transition (no_agent watchdog)
# ---------------------------------------------------------------------------
def test_cron_silent_unless_transition(cron, monkeypatch, tmp_path, capsys):
    ps = _pset({"f1": "standard"})
    monkeypatch.setattr(cron, "_build_proposal_set", lambda bars: ps)
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    # Seed prior-best so the eval fails deterministically (eval_passed=False) and is stable.
    factors_dir = tmp_path / "factors"
    factors_dir.mkdir()
    import hermes_quant.factors.weight_proposer as wp

    monkeypatch.setattr(wp, "_DEFAULT_DIR", factors_dir)
    # First run establishes the baseline (factor f1 newly seen -> not itself a transition).
    rc1 = cron.run_once(
        bars=None, holdout_dsr=0.1, holdout_sharpe_delta=0.0, plateau_stable=False
    )
    assert rc1 == 0
    capsys.readouterr()  # drain
    # Second run, identical tiers + identical eval verdict -> silent.
    rc2 = cron.run_once(
        bars=None, holdout_dsr=0.1, holdout_sharpe_delta=0.0, plateau_stable=False
    )
    assert rc2 == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# AC-13 — emits on a tier transition
# ---------------------------------------------------------------------------
def test_cron_emits_on_tier_transition(cron, monkeypatch, tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    factors_dir = tmp_path / "factors"
    factors_dir.mkdir()
    import hermes_quant.factors.weight_proposer as wp

    monkeypatch.setattr(wp, "_DEFAULT_DIR", factors_dir)

    # Run 1: f1 standard. Establishes baseline.
    monkeypatch.setattr(cron, "_build_proposal_set", lambda bars: _pset({"f1": "standard"}))
    cron.run_once(bars=None, holdout_dsr=0.1, holdout_sharpe_delta=0.0, plateau_stable=False)
    capsys.readouterr()  # drain

    # Run 2: f1 crosses standard -> premium. One transition line + the table.
    monkeypatch.setattr(cron, "_build_proposal_set", lambda bars: _pset({"f1": "premium"}))
    rc = cron.run_once(bars=None, holdout_dsr=0.1, holdout_sharpe_delta=0.0, plateau_stable=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "f1 tier standard -> premium" in out
    assert "f1" in out


def test_cron_pure_transition_logic(cron):
    cur = {"f1": {"tier": "premium"}, "__eval__": {"eval_passed": True}}
    base = {"f1": {"tier": "standard"}, "__eval__": {"eval_passed": False}}
    out = cron._transitions(cur, base)
    assert "f1 tier standard -> premium" in out
    assert "eval verdict False -> True" in out
    # standing state -> silent
    assert cron._transitions(cur, cur) == []
    # a newly-seen factor is not itself a transition
    assert cron._transitions({"f2": {"tier": "rejected"}, "__eval__": {"eval_passed": False}}, {}) == []


def test_cron_passing_eval_writes_candidates(cron, monkeypatch, tmp_path, capsys):
    """A passing held-out eval writes weight-candidates.json; a failing one does not."""
    factors_dir = tmp_path / "factors"
    factors_dir.mkdir()
    import hermes_quant.factors.weight_proposer as wp

    monkeypatch.setattr(wp, "_DEFAULT_DIR", factors_dir)
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(cron, "_build_proposal_set", lambda bars: _pset({"f1": "premium"}))
    # prior-best is missing -> -inf, so DSR 0.8 strictly beats; plateau_stable=True -> pass.
    cron.run_once(bars=None, holdout_dsr=0.8, holdout_sharpe_delta=0.5, plateau_stable=True)
    assert (factors_dir / "weight-candidates.json").exists()
    assert (factors_dir / "weight-prior-best.json").exists()


def test_cron_failing_eval_buffers_not_candidates(cron, monkeypatch, tmp_path):
    factors_dir = tmp_path / "factors"
    factors_dir.mkdir()
    import hermes_quant.factors.weight_proposer as wp

    monkeypatch.setattr(wp, "_DEFAULT_DIR", factors_dir)
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(cron, "_build_proposal_set", lambda bars: _pset({"f1": "premium"}))
    # plateau_stable=False -> fail regardless of DSR (robustness-not-peak)
    cron.run_once(bars=None, holdout_dsr=0.99, holdout_sharpe_delta=0.5, plateau_stable=False)
    assert not (factors_dir / "weight-candidates.json").exists()
    assert (factors_dir / "weight-rejected-buffer.jsonl").exists()


# ---------------------------------------------------------------------------
# ar23 / 2f01 — IC-dedup-at-ingest gate must be FUNCTIONAL in the production
# call path, not inert. The script's _build_proposal_set is the production
# register entry point: it must compute the per-factor returns FIRST and pass
# them to register_starter_set so the gate can run when the operator flips
# HERMES_QUANT_IC_DEDUP_AT_INGEST=1. Before the fix the script registered with
# no returns, so run_ic_gate was always False and the flag was a no-op.
# ---------------------------------------------------------------------------


def _ohlcv_bars(n: int = 120, seed: int = 0) -> pd.DataFrame:
    """Deterministic OHLCV bars (no network) for the factor expressions."""
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(size=n))
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": rng.integers(1_000_000, 2_000_000, n).astype(float),
        },
        index=idx,
    )


def _patch_two_near_duplicate_starters(monkeypatch, tmp_path):
    """Replace the starter set with two factors that produce a near-perfect
    (corr >= 0.99) pair on any bars — so the IC-dedup gate MUST reject the
    second one IF (and only if) it actually runs at ingest.

    Also pin the AlphaZoo storage dir to *tmp_path*. ``alpha_zoo._DEFAULT_DIR``
    is frozen at import time, so a bare ``AlphaZoo()`` (what the production
    ``_build_proposal_set`` constructs) would otherwise write to the real
    ``~/.hermes/quant/factors`` registry; patching the module global keeps the
    test hermetic.
    """
    import hermes_quant.factors.alpha_zoo as az
    import hermes_quant.factors.factor_oracle as fo
    import hermes_quant.factors.starter_set as ss

    dup_set = [
        {
            "name": "dup_a",
            "description": "close minus open",
            "source_code": 'bars["close"] - bars["open"]',
            "tags": [],
        },
        {
            "name": "dup_b",
            "description": "near-identical to dup_a",
            "source_code": 'bars["close"] - bars["open"] + 1e-12',
            "tags": [],
        },
    ]
    monkeypatch.setattr(ss, "_STARTER_FACTORS", dup_set)
    monkeypatch.setattr(az, "_DEFAULT_DIR", tmp_path)
    monkeypatch.setattr(fo, "_DEFAULT_DIR", tmp_path)


def test_build_proposal_set_gates_near_duplicate_when_flag_on(
    cron, monkeypatch, tmp_path
):
    """RED→GREEN (ar23): with the flag ON, the PRODUCTION register path
    (_build_proposal_set → register_starter_set) must reject a near-duplicate
    factor at ingest. Pre-fix this admitted both (gate never ran)."""
    from hermes_quant.factors.alpha_zoo import RedundantFactorError

    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    _patch_two_near_duplicate_starters(monkeypatch, tmp_path)

    with pytest.raises(RedundantFactorError) as exc:
        cron._build_proposal_set(_ohlcv_bars())

    # The gate ran on the production-computed factor returns and rejected dup_b.
    assert exc.value.result.max_corr >= 0.99


def test_build_proposal_set_flag_off_admits_duplicate_unchanged(
    cron, monkeypatch, tmp_path
):
    """Flag-OFF is byte-identical: even with two near-duplicate starters, the
    gate never runs and both register (no behavior change when the flag unset)."""
    from hermes_quant.factors.alpha_zoo import RedundantFactorError

    monkeypatch.delenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", raising=False)
    _patch_two_near_duplicate_starters(monkeypatch, tmp_path)

    try:
        _, zoo = cron._build_proposal_set(_ohlcv_bars())
    except RedundantFactorError:  # pragma: no cover - would be a regression
        pytest.fail("flag-OFF must not run the IC-dedup gate")
    # Both near-duplicate factors admitted -> flag-off path unchanged.
    assert len(zoo.list_all()) == 2


# ---------------------------------------------------------------------------
# O6-still-upstream-blocked regression: W4 must NOT lift the slippage_only skip
# ---------------------------------------------------------------------------
def test_settlement_slippage_only_skip_still_in_place():
    """dispatch_settlement must STILL skip slippage_only-tagged outcomes (O6 stays
    upstream-blocked on v0.1.2 fill-joining — W4 does not touch settlement_loop)."""
    from hermes_quant.daemon.settlement_loop import (
        CALIBRATION_QUALITY_SLIPPAGE_ONLY,
        dispatch_settlement,
    )

    class _View:
        def __init__(self):
            self.analyst = "kronos-small"
            self.metadata = {"_calibration_quality": CALIBRATION_QUALITY_SLIPPAGE_ONLY}

    class _Outcome:
        def __init__(self):
            self.view = _View()

    class _Sig:
        metadata = {"_calibration_quality": CALIBRATION_QUALITY_SLIPPAGE_ONLY}

    class _Episode:
        aggregated_signal = _Sig()

    class _Agg:
        def __init__(self):
            self.called = 0

        def update(self, episode):
            self.called += 1

    agg = _Agg()
    stats = dispatch_settlement(
        [_Outcome()],
        [("sig-1", _Episode())],
        analysts_by_name={},
        aggregator=agg,
    )
    assert stats["n_skipped_slippage_only"] >= 2
    assert stats["n_aggregator_updates"] == 0
    assert agg.called == 0  # BMA Beta-posterior learning stays gated upstream


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
