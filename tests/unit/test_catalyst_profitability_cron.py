"""Tests for the catalyst-profitability change-detecting watchdog (C2-1).

The ops script (quant-catalyst-profitability.py) is now a no_agent watchdog: it
prints ONLY on a state transition (a relation class crossing MIN_SAMPLE for the
first time, or a cleared class flipping verdict). Standing state -> silent. We
test the pure transition logic + main()'s silence/emit behavior with a
monkeypatched measure_profitability (no network) and a tmp_path baseline.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_quant.catalyst.profitability import MIN_SAMPLE, RelationStats


def _load_cron_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-catalyst-profitability.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_catalyst_profitability", path)
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


def _stat(relation: str, *, n_scored: int, hits: int, sum_ret: float) -> RelationStats:
    return RelationStats(
        relation=relation, n_scored=n_scored, hits=hits, sum_signed_return=sum_ret
    )


# ---------------------------------------------------------------------------
# Pure transition logic
# ---------------------------------------------------------------------------


def test_transition_first_clearance(cron):
    """A class going from uncleared -> cleared emits one CLEARED line."""
    cur = {"brand_self": {"cleared": True, "verdict": "PROFITABLE"}}
    baseline = {"brand_self": {"cleared": False, "verdict": "INSUFFICIENT_SAMPLE"}}
    out = cron._transitions(cur, baseline)
    assert len(out) == 1
    assert "brand_self CLEARED MIN_SAMPLE (PROFITABLE)" == out[0]


def test_transition_new_uncleared_is_silent(cron):
    """A brand-new class that hasn't cleared MIN_SAMPLE is silent (untrustworthy)."""
    cur = {"sector_member": {"cleared": False, "verdict": "INSUFFICIENT_SAMPLE"}}
    out = cron._transitions(cur, {})
    assert out == []


def test_transition_standing_state_silent(cron):
    """Cleared + unchanged verdict -> no transition (no_agent silence)."""
    state = {"brand_self": {"cleared": True, "verdict": "PROFITABLE"}}
    out = cron._transitions(state, state)
    assert out == []


def test_transition_verdict_flip(cron):
    """A cleared class flipping verdict emits one flip line."""
    cur = {"brand_self": {"cleared": True, "verdict": "PROFITABLE"}}
    baseline = {"brand_self": {"cleared": True, "verdict": "MARGINAL_HOLD"}}
    out = cron._transitions(cur, baseline)
    assert out == ["brand_self verdict MARGINAL_HOLD -> PROFITABLE"]


# ---------------------------------------------------------------------------
# main() silence / emit behavior (no network — measure_profitability stubbed)
# ---------------------------------------------------------------------------


def test_profitability_silent_when_no_stats(cron, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cron, "measure_profitability", lambda *a, **k: {})
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_profitability_silent_when_unchanged(cron, monkeypatch, tmp_path, capsys):
    # A cleared, PROFITABLE brand_self; baseline already records that.
    stats = {"brand_self": _stat("brand_self", n_scored=MIN_SAMPLE + 5, hits=MIN_SAMPLE, sum_ret=30.0)}
    monkeypatch.setattr(cron, "measure_profitability", lambda *a, **k: stats)
    baseline = tmp_path / "baseline.json"
    verdict = stats["brand_self"].verdict
    baseline.write_text(f'{{"brand_self": {{"cleared": true, "verdict": "{verdict}"}}}}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_profitability_emits_on_min_sample_clearance(cron, monkeypatch, tmp_path, capsys):
    stats = {"brand_self": _stat("brand_self", n_scored=MIN_SAMPLE + 1, hits=MIN_SAMPLE, sum_ret=25.0)}
    monkeypatch.setattr(cron, "measure_profitability", lambda *a, **k: stats)
    # Baseline has brand_self UNCLEARED -> first clearance fires.
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"brand_self": {"cleared": false, "verdict": "INSUFFICIENT_SAMPLE"}}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "CLEARED MIN_SAMPLE" in out
    assert "brand_self" in out


def test_profitability_emits_on_verdict_flip(cron, monkeypatch, tmp_path, capsys):
    # Cleared class with a PROFITABLE verdict; baseline had it MARGINAL_HOLD.
    stats = {"brand_self": _stat("brand_self", n_scored=MIN_SAMPLE + 10, hits=MIN_SAMPLE + 5, sum_ret=40.0)}
    monkeypatch.setattr(cron, "measure_profitability", lambda *a, **k: stats)
    assert stats["brand_self"].verdict == "PROFITABLE"
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"brand_self": {"cleared": true, "verdict": "MARGINAL_HOLD"}}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdict MARGINAL_HOLD -> PROFITABLE" in out


def test_profitability_verbose_always_prints(cron, monkeypatch, tmp_path, capsys):
    stats = {"brand_self": _stat("brand_self", n_scored=MIN_SAMPLE + 5, hits=MIN_SAMPLE, sum_ret=30.0)}
    monkeypatch.setattr(cron, "measure_profitability", lambda *a, **k: stats)
    baseline = tmp_path / "baseline.json"
    # Even with a matching baseline (no transition), --verbose prints the table.
    verdict = stats["brand_self"].verdict
    baseline.write_text(f'{{"brand_self": {{"cleared": true, "verdict": "{verdict}"}}}}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog", "--verbose"])
    rc = cron.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "profitability by relation" in out.lower()
