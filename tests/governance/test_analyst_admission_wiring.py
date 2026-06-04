"""tests/governance/test_analyst_admission_wiring.py — the analyst-admission gate
is WIRED into the committee-build seam (seed 908e), default-OFF.

The gate logic (evaluate_analyst_admission / admit_to_committee) is only valuable if
it actually filters the committee. This pins the wiring at advisor._build_default_analysts:

  * DEFAULT-OFF: with HERMES_QUANT_ANALYST_ADMISSION unset/"0", the committee is
    byte-identical to today (no analyst is dropped) — silence-by-default, no live
    disturbance. The flag lives at the wiring boundary; the gate module reads no env
    (mirrors factors.weight_proposer, whose flag is at the cron boundary only).
  * ON: with the flag "1", the roster is filtered through admit_to_committee using
    persisted admission decisions — an analyst whose decision is not admitted (or
    that has no decision: fail-closed) is dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.governance.analyst_admission import (
    evaluate_analyst_admission,
    load_admission_decisions,
)


# ---------------------------------------------------------------------------
# decisions loader (persisted admission verdicts the wiring reads)
# ---------------------------------------------------------------------------


def test_load_admission_decisions_round_trip(tmp_path: Path):
    """A persisted decisions file round-trips into {analyst_id: AnalystAdmissionDecision}."""
    path = tmp_path / "decisions.json"
    path.write_text(
        json.dumps(
            {
                "good": {"admitted": True, "holdout_dsr": 0.8, "prior_best_dsr": 0.5,
                         "plateau_stable": True, "beats_prior_best": True, "reason": "ok"},
                "bad": {"admitted": False, "holdout_dsr": 0.2, "prior_best_dsr": 0.5,
                        "plateau_stable": True, "beats_prior_best": False, "reason": "held"},
            }
        )
    )
    decisions = load_admission_decisions(path)
    assert decisions["good"].admitted is True
    assert decisions["bad"].admitted is False


def test_load_admission_decisions_missing_file_is_empty(tmp_path: Path):
    """Missing file -> {} (combined with fail-closed admit_to_committee, that means
    NO analyst joins when the flag is on but no decisions exist)."""
    assert load_admission_decisions(tmp_path / "nope.json") == {}


# ---------------------------------------------------------------------------
# the WIRING: default-OFF byte-identical, ON filters
# ---------------------------------------------------------------------------


def test_default_off_committee_is_byte_identical(monkeypatch):
    """Flag unset -> _build_default_analysts returns the full roster unchanged."""
    from hermes_quant import advisor

    monkeypatch.delenv("HERMES_QUANT_ANALYST_ADMISSION", raising=False)
    analysts = advisor._build_default_analysts()
    # Baseline always includes the classical-TA analyst; no admission filtering.
    names = [getattr(a, "name", type(a).__name__) for a in analysts]
    assert any("classical" in n.lower() or "ta" in n.lower() for n in names)
    assert len(analysts) >= 1


def test_flag_on_with_no_decisions_drops_all_fail_closed(monkeypatch, tmp_path: Path):
    """Flag ON + no persisted decisions -> admit_to_committee fail-closed -> empty
    committee (the gate is reachable and fails closed, never silently no-ops)."""
    from hermes_quant import advisor
    from hermes_quant.governance import analyst_admission

    monkeypatch.setenv("HERMES_QUANT_ANALYST_ADMISSION", "1")
    monkeypatch.setattr(
        analyst_admission, "_DEFAULT_DECISIONS_PATH", tmp_path / "absent.json", raising=False
    )
    analysts = advisor._build_default_analysts()
    assert analysts == []


def test_flag_on_admits_only_passing_analysts(monkeypatch, tmp_path: Path):
    """Flag ON + a decisions file that admits the classical-TA analyst -> it joins;
    an analyst with a failing decision would not. We write a decisions file admitting
    every default analyst name so the roster survives, proving the ON path filters
    via the persisted decisions rather than dropping unconditionally."""
    from hermes_quant import advisor
    from hermes_quant.governance import analyst_admission

    monkeypatch.delenv("HERMES_QUANT_ANALYST_ADMISSION", raising=False)
    baseline = advisor._build_default_analysts()
    baseline_names = [getattr(a, "name", type(a).__name__) for a in baseline]

    decisions_file = tmp_path / "decisions.json"
    decisions_file.write_text(
        json.dumps(
            {
                name: {"admitted": True, "holdout_dsr": 0.9, "prior_best_dsr": 0.1,
                       "plateau_stable": True, "beats_prior_best": True, "reason": "ok"}
                for name in baseline_names
            }
        )
    )
    monkeypatch.setenv("HERMES_QUANT_ANALYST_ADMISSION", "1")
    monkeypatch.setattr(
        analyst_admission, "_DEFAULT_DECISIONS_PATH", decisions_file, raising=False
    )
    admitted = advisor._build_default_analysts()
    admitted_names = [getattr(a, "name", type(a).__name__) for a in admitted]
    assert admitted_names == baseline_names  # all admitted -> roster preserved


def test_flag_on_drops_a_failing_analyst(monkeypatch, tmp_path: Path):
    """The discriminating case: admit all default analysts EXCEPT mark the first one
    as not-admitted -> it is dropped from the committee, the rest survive."""
    from hermes_quant import advisor
    from hermes_quant.governance import analyst_admission

    monkeypatch.delenv("HERMES_QUANT_ANALYST_ADMISSION", raising=False)
    baseline_names = [
        getattr(a, "name", type(a).__name__) for a in advisor._build_default_analysts()
    ]
    assert baseline_names, "expected at least one default analyst"
    failing = baseline_names[0]

    decisions = {}
    for name in baseline_names:
        admitted = name != failing
        decisions[name] = {
            "admitted": admitted,
            "holdout_dsr": 0.9 if admitted else 0.05,
            "prior_best_dsr": 0.1,
            "plateau_stable": True,
            "beats_prior_best": admitted,
            "reason": "ok" if admitted else "held",
        }
    decisions_file = tmp_path / "decisions.json"
    decisions_file.write_text(json.dumps(decisions))

    monkeypatch.setenv("HERMES_QUANT_ANALYST_ADMISSION", "1")
    monkeypatch.setattr(
        analyst_admission, "_DEFAULT_DECISIONS_PATH", decisions_file, raising=False
    )
    admitted_names = [
        getattr(a, "name", type(a).__name__) for a in advisor._build_default_analysts()
    ]
    assert failing not in admitted_names
    assert admitted_names == [n for n in baseline_names if n != failing]
