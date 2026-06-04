"""tests/unit/test_catalyst_onboarding_audit.py — pre-flip onboarding audit (seed 2b63).

Seed ba90 says: BEFORE the operator flips HERMES_QUANT_CATALYST_ONBOARDING=1, the
ADR-0075 admission-precision axis must be GREEN — admitted out-of-universe names
must beat a forward-return bar. Today run_admission_precision EXISTS but nothing
wires it into a pre-flip audit the operator can run.

This module adds a READ-ONLY audit:
  * loads admission episodes (from the versioned fixture or any episodes file),
  * runs run_admission_precision,
  * returns a structured OnboardingPreflipAudit report (pass/fail + the flag name
    + an explicit operator-action note),
  * NEVER reads, writes, sets, or flips HERMES_QUANT_CATALYST_ONBOARDING or any
    env/flag — it only MEASURES whether the bar is cleared.

The audit FAILS when admitted names don't beat the bar, so the operator can SEE
the gate would not be defensible before flipping it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_quant.catalyst.onboarding_audit import (
    OnboardingPreflipAudit,
    audit_onboarding_preflip,
    load_admission_episodes,
)

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "catalyst_onboarding"
FIXTURE_FILE = FIXT / "admission_episodes.v1.json"
MIN_HIT_RATE = 0.6


# ---------------------------------------------------------------------------
# episode loading
# ---------------------------------------------------------------------------


def test_load_admission_episodes_from_fixture():
    eps = load_admission_episodes(FIXTURE_FILE)
    assert len(eps) == 10
    assert any(e.symbol == "LUNR" for e in eps)


# ---------------------------------------------------------------------------
# the GATE: the committed fixture clears the bar -> audit PASSES
# ---------------------------------------------------------------------------


def test_audit_passes_on_committed_fixture():
    """The versioned fixture is GREEN at the 0.60 bar (5 admitted, 4 hits => 0.80),
    so the pre-flip audit reports passed=True. This is the operator's evidence the
    flag COULD be flipped (the flip itself stays an operator action)."""
    audit = audit_onboarding_preflip(FIXTURE_FILE, min_hit_rate=MIN_HIT_RATE)
    assert isinstance(audit, OnboardingPreflipAudit)
    assert audit.passed is True
    assert audit.result.n_admitted == 5
    assert audit.result.hit_rate == 0.80
    # The report names the flag it gates and states the flip is an operator action.
    assert audit.flag == "HERMES_QUANT_CATALYST_ONBOARDING"
    assert "operator" in audit.operator_note.lower()


# ---------------------------------------------------------------------------
# the GATE FAILS when admitted names don't beat the bar (the required acceptance)
# ---------------------------------------------------------------------------


def test_audit_fails_when_admitted_names_miss_the_forward_return_bar(tmp_path: Path):
    """Acceptance (2b63): the pre-flip audit returns a FAIL when admitted out-of-
    universe names don't beat the forward-return bar. Build an episodes file whose
    admitted names are mostly directionally WRONG -> hit_rate below the bar."""
    episodes = {
        "min_hit_rate": MIN_HIT_RATE,
        "tau_conf": 0.6,
        "tau_mag": 0.04,
        "episodes": [
            # 3 admitted, only 1 directionally correct -> hit_rate 0.33 < 0.60.
            {"symbol": "AAA", "stance": "bullish", "confidence": 0.80, "magnitude": 0.06,
             "in_universe": False, "tradeable": True, "realized_forward_return": -5.0},
            {"symbol": "BBB", "stance": "bullish", "confidence": 0.80, "magnitude": 0.06,
             "in_universe": False, "tradeable": True, "realized_forward_return": -3.0},
            {"symbol": "CCC", "stance": "bullish", "confidence": 0.80, "magnitude": 0.06,
             "in_universe": False, "tradeable": True, "realized_forward_return": +4.0},
        ],
    }
    f = tmp_path / "failing_episodes.json"
    f.write_text(json.dumps(episodes))
    audit = audit_onboarding_preflip(f, min_hit_rate=MIN_HIT_RATE)
    assert audit.passed is False
    assert audit.result.n_scored == 3
    assert audit.result.hit_rate < MIN_HIT_RATE


def test_audit_fails_on_empty_admission_set_no_vacuous_pass(tmp_path: Path):
    """A flag must never be flippable on zero evidence: if nothing is admissible,
    the audit FAILS (run_admission_precision's scored==0 => passed=False)."""
    episodes = {
        "episodes": [
            # in-universe (screen artifact) -> not admitted; nothing scored.
            {"symbol": "AMD", "stance": "bullish", "confidence": 0.9, "magnitude": 0.08,
             "in_universe": True, "tradeable": True, "realized_forward_return": 3.0},
        ],
    }
    f = tmp_path / "empty.json"
    f.write_text(json.dumps(episodes))
    audit = audit_onboarding_preflip(f, min_hit_rate=MIN_HIT_RATE)
    assert audit.passed is False
    assert audit.result.n_admitted == 0


# ---------------------------------------------------------------------------
# RAIL: the audit is READ-ONLY — it NEVER touches the flag or any env.
# ---------------------------------------------------------------------------


def test_audit_never_sets_the_onboarding_flag(monkeypatch):
    """The audit must not write HERMES_QUANT_CATALYST_ONBOARDING (or any env). We
    snapshot the env before/after and assert byte-identical."""
    monkeypatch.delenv("HERMES_QUANT_CATALYST_ONBOARDING", raising=False)
    before = dict(os.environ)
    audit_onboarding_preflip(FIXTURE_FILE, min_hit_rate=MIN_HIT_RATE)
    after = dict(os.environ)
    assert before == after
    assert "HERMES_QUANT_CATALYST_ONBOARDING" not in os.environ


def test_audit_does_not_read_the_flag_to_decide(monkeypatch):
    """The audit result is independent of the flag's value: it MEASURES the bar, it
    does not gate on whether onboarding is currently enabled. Same fixture, both
    flag states -> identical pass/fail and hit_rate."""
    monkeypatch.setenv("HERMES_QUANT_CATALYST_ONBOARDING", "0")
    off = audit_onboarding_preflip(FIXTURE_FILE, min_hit_rate=MIN_HIT_RATE)
    monkeypatch.setenv("HERMES_QUANT_CATALYST_ONBOARDING", "1")
    on = audit_onboarding_preflip(FIXTURE_FILE, min_hit_rate=MIN_HIT_RATE)
    assert off.passed == on.passed
    assert off.result.hit_rate == on.result.hit_rate


# ---------------------------------------------------------------------------
# report shape (for the CLI / operator)
# ---------------------------------------------------------------------------


def test_audit_report_to_dict_is_serializable_and_complete():
    audit = audit_onboarding_preflip(FIXTURE_FILE, min_hit_rate=MIN_HIT_RATE)
    d = audit.to_dict()
    # JSON round-trip (the CLI prints this with --json)
    round_tripped = json.loads(json.dumps(d, default=str))
    assert round_tripped["passed"] is True
    assert round_tripped["flag"] == "HERMES_QUANT_CATALYST_ONBOARDING"
    assert round_tripped["min_hit_rate"] == MIN_HIT_RATE
    assert round_tripped["n_admitted"] == 5
    assert round_tripped["hit_rate"] == 0.80
    assert "rejected" in round_tripped  # the excluded negative-controls are surfaced


def test_audit_missing_file_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        audit_onboarding_preflip(tmp_path / "nope.json", min_hit_rate=MIN_HIT_RATE)
