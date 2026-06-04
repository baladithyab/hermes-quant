"""tests/unit/test_cli_admission_precision.py — `hermes quant admission-precision`
CLI verb (seed 2b63).

Exposes run_admission_precision (and the onboarding pre-flip audit) as an operator
CLI verb that reports pass/fail with a gate-style exit code:
  * 0 — gate clears (audit.passed)
  * 1 — gate fails (admitted names miss the forward-return bar)
  * 2 — error (missing/unparseable episodes file)

The verb is READ-ONLY (ADR-0007) — it never flips HERMES_QUANT_CATALYST_ONBOARDING.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_quant.cli.admission_precision import cmd_admission_precision

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "catalyst_onboarding"
FIXTURE_FILE = FIXT / "admission_episodes.v1.json"


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        episodes_file=str(FIXTURE_FILE),
        min_hit_rate=0.6,
        tau_conf=None,
        tau_mag=None,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_passes_on_green_fixture_exit_0(capsys):
    """The committed fixture clears the 0.60 bar -> exit code 0 and a PASS line."""
    rc = cmd_admission_precision(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "0.80" in out or "80" in out  # hit_rate surfaced


def test_cli_fails_above_achieved_bar_exit_1(capsys):
    """At a 0.85 bar the 0.80 fixture FAILS -> exit code 1, FAIL line."""
    rc = cmd_admission_precision(_ns(min_hit_rate=0.85))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_cli_fails_when_admitted_names_miss_bar_exit_1(tmp_path: Path, capsys):
    """Acceptance (2b63): the verb reports a FAIL (exit 1) when admitted out-of-
    universe names don't beat the forward-return bar."""
    episodes = {
        "min_hit_rate": 0.6,
        "episodes": [
            {"symbol": "AAA", "stance": "bullish", "confidence": 0.80, "magnitude": 0.06,
             "in_universe": False, "tradeable": True, "realized_forward_return": -5.0},
            {"symbol": "BBB", "stance": "bullish", "confidence": 0.80, "magnitude": 0.06,
             "in_universe": False, "tradeable": True, "realized_forward_return": -3.0},
            {"symbol": "CCC", "stance": "bullish", "confidence": 0.80, "magnitude": 0.06,
             "in_universe": False, "tradeable": True, "realized_forward_return": +4.0},
        ],
    }
    f = tmp_path / "failing.json"
    f.write_text(json.dumps(episodes))
    rc = cmd_admission_precision(_ns(episodes_file=str(f)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_cli_missing_file_exit_2(tmp_path: Path, capsys):
    rc = cmd_admission_precision(_ns(episodes_file=str(tmp_path / "nope.json")))
    assert rc == 2


def test_cli_json_output_is_machine_readable(capsys):
    rc = cmd_admission_precision(_ns(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["passed"] is True
    assert payload["flag"] == "HERMES_QUANT_CATALYST_ONBOARDING"
    assert payload["hit_rate"] == 0.80


def test_cli_registered_in_setup_argparse():
    """The verb is wired into the canonical CLI surface (setup_argparse) and routed
    by dispatch — not just a loose handler."""
    from hermes_quant import cli

    parser = argparse.ArgumentParser()
    cli.setup_argparse(parser)
    # The subparser must accept the verb + required --episodes-file.
    args = parser.parse_args(
        ["admission-precision", "--episodes-file", str(FIXTURE_FILE)]
    )
    assert args.quant_cmd == "admission-precision"
    assert args.episodes_file == str(FIXTURE_FILE)


def test_dispatch_routes_admission_precision(capsys):
    """dispatch() returns the handler's exit code for the verb."""
    from hermes_quant import cli

    args = argparse.Namespace(
        quant_cmd="admission-precision",
        episodes_file=str(FIXTURE_FILE),
        min_hit_rate=0.6,
        tau_conf=None,
        tau_mag=None,
        json=False,
    )
    rc = cli.dispatch(args)
    assert rc == 0
