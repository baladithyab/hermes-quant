"""Tests for hermes_quant.governance.approvals (ADR-0031 D4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.governance import approvals, audit_log
from hermes_quant.governance.approvals import HumanApprovalToken, NoApprovalError


@pytest.fixture
def gov_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    quant = tmp_path / "quant"
    gov = quant / "governance"
    monkeypatch.setattr(approvals, "TOKEN_STORE_PATH", gov / "approval_tokens.jsonl")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", gov / "audit_log.jsonl")
    return quant


def test_approvals_require_explicit_human_token(gov_paths: Path) -> None:
    with pytest.raises(NoApprovalError):
        approvals.require_human_token("promotion", "live_broker")


def test_grant_then_require_round_trip(gov_paths: Path) -> None:
    t = approvals.grant_token("promotion", "live_broker", granted_by="alice")
    fetched = approvals.require_human_token("promotion", "live_broker")
    assert fetched.token_id == t.token_id


def test_approval_token_expires_after_ttl(gov_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant a 1-minute token; advance time 2 minutes; require fails."""
    t = approvals.grant_token("promotion", "live_broker", granted_by="alice", ttl_minutes=1)

    fake_now = t.granted_at + timedelta(minutes=2)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fake_now if tz is None else fake_now.astimezone(tz)

    monkeypatch.setattr(approvals, "datetime", FakeDatetime)

    with pytest.raises(NoApprovalError):
        approvals.require_human_token("promotion", "live_broker")


def test_consume_token_one_shot(gov_paths: Path) -> None:
    t = approvals.grant_token("amendment", "amend_42", granted_by="alice")
    approvals.consume_token(t.token_id)

    # Second consume → NoApprovalError
    with pytest.raises(NoApprovalError):
        approvals.consume_token(t.token_id)

    # require_human_token also fails (consumed tokens are filtered out)
    with pytest.raises(NoApprovalError):
        approvals.require_human_token("amendment", "amend_42")


def test_grant_token_emits_audit_event(gov_paths: Path) -> None:
    approvals.grant_token("proposal", "prop_x", granted_by="alice")
    rows = list(audit_log.read())
    assert any(
        r.payload.get("row_type") == "approval_granted" and r.payload.get("scope") == "proposal"
        for r in rows
    )


def test_retro_code_change_target_governance_path_rejected(gov_paths: Path) -> None:
    """Defense-in-depth per ADR-0031 D7: even if a token were minted, the
    require_human_token call refuses to look one up under governance/.
    """
    with pytest.raises(NoApprovalError):
        approvals.require_human_token("retro_code_change", "hermes_quant/governance/audit_log.py")


def test_invalid_scope_rejected(gov_paths: Path) -> None:
    with pytest.raises(ValueError):
        approvals.grant_token("not_a_scope", "x", granted_by="alice")  # type: ignore[arg-type]


def test_consume_token_check_then_act_runs_under_cross_process_lock(
    gov_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0031 D3 one-shot single-owner: the consumed-check and the
    `consumed`-row append must occur INSIDE the same cross-process critical
    section. If the check ran outside any lock (as the buggy non-atomic
    check-then-act did), two processes could both pass the guard before either
    appended, double-spending a one-shot kill_switch_clear / promotion token.

    We assert structurally that `_consumed_ids()` (the check) AND `_append_row`
    (the act) are both invoked while the `_flocked()` critical section is held.
    """
    t = approvals.grant_token("kill_switch_clear", "state.json", granted_by="admin")

    lock_state = {"held": False}
    events: list[str] = []

    real_flocked = approvals._flocked
    real_consumed_ids = approvals._consumed_ids
    real_append_row = approvals._append_row

    from contextlib import contextmanager

    @contextmanager
    def tracking_flocked():  # type: ignore[no-untyped-def]
        lock_state["held"] = True
        events.append("lock_acquired")
        try:
            with real_flocked():
                yield
        finally:
            events.append("lock_released")
            lock_state["held"] = False

    def tracking_consumed_ids() -> set[str]:
        events.append(f"check(under_lock={lock_state['held']})")
        return real_consumed_ids()

    def tracking_append_row(row: dict) -> None:  # type: ignore[type-arg]
        events.append(f"append(under_lock={lock_state['held']})")
        return real_append_row(row)

    monkeypatch.setattr(approvals, "_flocked", tracking_flocked)
    monkeypatch.setattr(approvals, "_consumed_ids", tracking_consumed_ids)
    monkeypatch.setattr(approvals, "_append_row", tracking_append_row)

    approvals.consume_token(t.token_id)

    # The consumed-check MUST happen while the cross-process lock is held.
    assert "check(under_lock=True)" in events, events
    assert "check(under_lock=False)" not in events, events
    # The consumed-row append MUST happen while the lock is held.
    assert "append(under_lock=True)" in events, events
    # And the lock must wrap both: acquired before the check, released after.
    assert events[0] == "lock_acquired"
    assert events.index("check(under_lock=True)") < events.index("append(under_lock=True)")
    assert events[-1] == "lock_released"


def _consume_in_subprocess(store_path_str: str, token_id: str, barrier_dir_str: str) -> int:
    """Run in a child process: consume the token, return 0 on success,
    1 on NoApprovalError (already consumed), 2 on any other error."""
    import time as _time
    from pathlib import Path as _Path

    from hermes_quant.governance import approvals as _approvals

    _approvals.TOKEN_STORE_PATH = _Path(store_path_str)

    # Crude rendezvous: spin until a sentinel file appears so both children
    # race the critical section as closely as possible.
    barrier = _Path(barrier_dir_str) / "go"
    for _ in range(2000):
        if barrier.exists():
            break
        _time.sleep(0.001)

    try:
        _approvals.consume_token(token_id)
        return 0
    except _approvals.NoApprovalError:
        return 1
    except Exception:  # noqa: BLE001
        return 2


def test_consume_token_double_spend_two_processes(gov_paths: Path) -> None:
    """Genuine two-process race: spawn two OS processes that both attempt to
    consume the SAME one-shot token at the same instant. Exactly ONE must win.

    Without a cross-process flock both processes read `_consumed_ids()` before
    either appends, both pass the guard, and the token is spent twice
    (ADR-0016 kill-switch rail disarmed in two processes).
    """
    import multiprocessing as mp

    t = approvals.grant_token("kill_switch_clear", "state.json", granted_by="admin")
    store_path = str(approvals.TOKEN_STORE_PATH)
    # Ensure the store/dir exists so children only ever append.
    approvals.TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)

    barrier_dir = approvals.TOKEN_STORE_PATH.parent

    ctx = mp.get_context("fork")
    results: list[int] = []

    n_procs = 8
    with ctx.Pool(n_procs) as pool:
        async_results = [
            pool.apply_async(
                _consume_in_subprocess, (store_path, t.token_id, str(barrier_dir))
            )
            for _ in range(n_procs)
        ]
        # Release all children at once.
        (barrier_dir / "go").write_text("1")
        results = [ar.get(timeout=30) for ar in async_results]

    successes = results.count(0)
    rejections = results.count(1)
    errors = results.count(2)

    assert errors == 0, f"unexpected errors in children: {results}"
    # The one-shot invariant: exactly one process may consume the token.
    assert successes == 1, f"double-spend: {successes} processes consumed one token"
    assert rejections == n_procs - 1

    # And the store itself records exactly one consumed row.
    consumed_rows = [
        row for row in approvals._iter_rows() if row.get("row_type") == "consumed"
    ]
    assert len(consumed_rows) == 1
