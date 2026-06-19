"""Tests for `hermes quant govern` — additive CLI front-door to the
governance kill-switch-clear token path (inc/gov8188).

The verb only CALLS the existing `governance.approvals.grant_token` /
`kill_switch.clear`; it does NOT change the human-authorizes contract.
These tests prove:
  1. `grant-clear --granted-by op1` mints a kill_switch_clear token + prints
     the token_id (RED->GREEN mint+print).
  2. Missing --granted-by errors (argparse exit 2) — human-authorizes preserved.
  3. A token minted by the verb is ACCEPTED by the REAL clear path
     (load-bearing target_ref='state.json').
  4. `clear-halt --confirm` mints + clears in one step; without --confirm it
     refuses (exit 2) and the halt stays set.
  5. --ttl-min controls the persisted token's lifetime.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

import hermes_quant.cli as cli
from hermes_quant.governance import approvals, audit_log, kill_switch

TOKEN_ID_RE = re.compile(r"ap_[0-9a-f]{16}")


@pytest.fixture
def gov_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all governance paths into a per-test tmpdir.

    Mirrors tests/governance/test_kill_switch.py:14-22 — the verb writes to
    the module-constant approvals.TOKEN_STORE_PATH, so monkeypatching it here
    isolates the store the same way the canonical kill-switch tests do.
    """
    quant = tmp_path / "quant"
    gov = quant / "governance"
    monkeypatch.setattr(kill_switch, "STATE_JSON_PATH", quant / "state.json")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", gov / "audit_log.jsonl")
    monkeypatch.setattr(approvals, "TOKEN_STORE_PATH", gov / "approval_tokens.jsonl")
    return quant


def _run(argv: list[str]) -> int:
    """Drive the CLI in-process: build the parser, parse argv, dispatch."""
    parser = argparse.ArgumentParser(prog="hermes quant")
    cli.setup_argparse(parser)
    args = parser.parse_args(argv)
    return cli.dispatch(args)


def _grant_rows() -> list[dict]:
    path = approvals.TOKEN_STORE_PATH
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("row_type") == "grant":
            rows.append(obj)
    return rows


def test_grant_clear_mints_and_prints_token_id(
    gov_paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(["govern", "grant-clear", "--granted-by", "op1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert TOKEN_ID_RE.search(out), f"expected an ap_ token_id in stdout, got: {out!r}"

    rows = _grant_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["scope"] == "kill_switch_clear"
    assert row["target_ref"] == "state.json"
    assert row["granted_by"] == "op1"
    # the printed token_id is the persisted one
    assert TOKEN_ID_RE.search(out).group(0) == row["token_id"]


def test_grant_clear_requires_granted_by(gov_paths: Path) -> None:
    """Human-authorizes preserved: no operator id => argparse exit 2, no mint."""
    parser = argparse.ArgumentParser(prog="hermes quant")
    cli.setup_argparse(parser)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["govern", "grant-clear"])
    assert exc.value.code == 2
    assert _grant_rows() == []


def test_minted_token_accepted_by_real_clear_path(
    gov_paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: a fired halt is cleared by a token the verb minted."""
    kill_switch.fire("t", "test")
    assert kill_switch.is_halted() is True

    rc = _run(["govern", "grant-clear", "--granted-by", "op1"])
    assert rc == 0
    capsys.readouterr()  # drain

    tok = approvals.require_human_token("kill_switch_clear", "state.json")
    kill_switch.clear(tok)
    assert kill_switch.is_halted() is False


def test_clear_halt_mints_and_clears_in_one_step(
    gov_paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kill_switch.fire("t", "test")
    assert kill_switch.is_halted() is True

    rc = _run(["govern", "clear-halt", "--granted-by", "op1", "--confirm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert TOKEN_ID_RE.search(out)
    assert kill_switch.is_halted() is False


def test_clear_halt_without_confirm_refuses(
    gov_paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kill_switch.fire("t", "test")
    assert kill_switch.is_halted() is True

    rc = _run(["govern", "clear-halt", "--granted-by", "op1"])
    assert rc == 2
    capsys.readouterr()
    # halt stays set; no token consumed against the live path
    assert kill_switch.is_halted() is True


def test_clear_halt_requires_granted_by(gov_paths: Path) -> None:
    parser = argparse.ArgumentParser(prog="hermes quant")
    cli.setup_argparse(parser)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["govern", "clear-halt", "--confirm"])
    assert exc.value.code == 2


def test_grant_clear_ttl_min_controls_expiry(
    gov_paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(["govern", "grant-clear", "--granted-by", "op1", "--ttl-min", "10"])
    assert rc == 0
    capsys.readouterr()
    row = _grant_rows()[0]
    granted = approvals._parse_dt(row["granted_at"])
    expires = approvals._parse_dt(row["expires_at"])
    delta_min = (expires - granted).total_seconds() / 60.0
    assert 9.5 <= delta_min <= 10.5
