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
