"""Tests for hermes_quant.governance.kill_switch (ADR-0031 D3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.governance import approvals, audit_log, kill_switch
from hermes_quant.governance.approvals import NoApprovalError


@pytest.fixture
def gov_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all governance paths into a per-test tmpdir."""
    quant = tmp_path / "quant"
    gov = quant / "governance"
    monkeypatch.setattr(kill_switch, "STATE_JSON_PATH", quant / "state.json")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", gov / "audit_log.jsonl")
    monkeypatch.setattr(approvals, "TOKEN_STORE_PATH", gov / "approval_tokens.jsonl")
    return quant


def test_kill_switch_fire_writes_state(gov_paths: Path) -> None:
    kill_switch.fire("test_reason", "test_source")
    state = json.loads((gov_paths / "state.json").read_text())
    assert state["halt"] is True
    assert state["halt_reason"] == "test_reason"
    assert state["halt_source"] == "test_source"


def test_kill_switch_is_halted_reads_state_json(gov_paths: Path) -> None:
    assert kill_switch.is_halted() is False
    kill_switch.fire("r", "s")
    assert kill_switch.is_halted() is True


def test_kill_switch_fire_is_idempotent(gov_paths: Path) -> None:
    """Two consecutive fire() calls produce exactly one audit-log entry."""
    kill_switch.fire("first_reason", "first_source")
    kill_switch.fire("second_reason", "second_source")

    rows = list(audit_log.read(kinds=["kill_switch_fired"]))
    assert len(rows) == 1
    assert rows[0].payload["reason"] == "first_reason"
    assert rows[0].source == "first_source"


def test_kill_switch_fire_drains_state_json_atomically(
    gov_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject crash between tmp-write and rename. state.json must never
    appear with the new content because rename was the atomic step."""
    state_path = gov_paths / "state.json"
    tmp_path_target = state_path.with_suffix(".json.tmp")

    # Pre-existing state file (unhalted)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"halt": False, "marker": "original"}))

    class CrashAfterTmp(Exception):
        pass

    real_replace = Path.replace

    def crashing_replace(self: Path, target):  # type: ignore[no-untyped-def]
        # The rename is the atomic boundary. We crash *before* it executes.
        raise CrashAfterTmp("simulated SIGKILL between tmp-write and rename")

    monkeypatch.setattr(Path, "replace", crashing_replace)

    with pytest.raises(CrashAfterTmp):
        kill_switch.fire("crash_test", "test")

    # state.json is still the pre-crash file (no torn write)
    assert json.loads(state_path.read_text())["marker"] == "original"
    assert kill_switch.is_halted() is False
    # The tmp file may or may not exist; doesn't matter — it's not visible
    # under the canonical path.
    Path.replace = real_replace  # type: ignore[method-assign]


def test_kill_switch_clear_requires_token(gov_paths: Path) -> None:
    """clear() without a token raises NoApprovalError (no auto-approve)."""
    kill_switch.fire("r", "s")
    with pytest.raises(NoApprovalError):
        kill_switch.clear()


def test_kill_switch_clear_with_valid_token(gov_paths: Path) -> None:
    kill_switch.fire("r", "s")
    token = approvals.grant_token("kill_switch_clear", "state.json", granted_by="admin")
    kill_switch.clear(token)
    assert kill_switch.is_halted() is False


def test_kill_switch_clear_rejects_wrong_scope_token(gov_paths: Path) -> None:
    kill_switch.fire("r", "s")
    bad_token = approvals.grant_token("promotion", "anything", granted_by="admin")
    with pytest.raises(NoApprovalError):
        kill_switch.clear(bad_token)


def test_ar36_clear_rejects_expired_token_and_does_not_spend_valid_grant(gov_paths: Path) -> None:
    """ar36: clear() must BIND the PASSED token. An EXPIRED token object must NOT clear the
    rail just because SOME other valid grant exists for the same (scope, target_ref); and
    that genuinely-valid grant must survive UNSPENT (the one-shot single-owner invariant)."""
    kill_switch.fire("r", "s")
    # An already-expired token for the right scope/target...
    expired = approvals.grant_token(
        "kill_switch_clear", "state.json", granted_by="admin", ttl_minutes=-1
    )
    # ...plus an unrelated genuinely-valid grant for the SAME scope/target.
    valid = approvals.grant_token("kill_switch_clear", "state.json", granted_by="admin")

    with pytest.raises(NoApprovalError):
        kill_switch.clear(expired)
    # The rail must STILL be halted (the expired token did not clear it).
    assert kill_switch.is_halted() is True
    # And the genuinely-valid grant must remain UNSPENT (re-usable for a real clear).
    still_valid = approvals.require_human_token("kill_switch_clear", "state.json")
    assert still_valid.token_id == valid.token_id
    # A real clear with the valid token then works (and consumes it).
    kill_switch.clear(valid)
    assert kill_switch.is_halted() is False


def test_ar37_forged_token_id_does_not_disarm_rail_on_disk(gov_paths: Path) -> None:
    """ar37: consume_token must run BEFORE the cleared state is written. A forged token_id
    (passes the scope check but is not a granted id) must leave the rail HALTED on disk —
    not flip halt=False and only then raise."""
    import dataclasses

    kill_switch.fire("r", "s")
    valid = approvals.grant_token("kill_switch_clear", "state.json", granted_by="admin")
    # A token object that is the valid grant EXCEPT its token_id is forged.
    forged = valid.model_copy(update={"token_id": "ap_FORGED_NEVER_GRANTED"}) \
        if hasattr(valid, "model_copy") else dataclasses.replace(valid, token_id="ap_FORGED_NEVER_GRANTED")

    with pytest.raises(NoApprovalError):
        kill_switch.clear(forged)
    # The rail MUST still be halted on disk (the forged clear did not silently disarm it).
    assert kill_switch.is_halted() is True
    assert json.loads(kill_switch.STATE_JSON_PATH.read_text())["halt"] is True
