"""ProposalStore.reject must not clobber a concurrently-approved+fired row.

This is the SYMMETRIC sibling of the ar16/expire_one CAS fix. ``expire_one`` was
hardened with a guarded compare-and-set (``_cas_expire``: ``UPDATE ... WHERE
state='pending'``) precisely so a stale cross-process sweeper read could never
overwrite a concurrently-approved (FIRED) row back to ``expired`` and drop the
realized fill. ``reject`` (and the legacy ``approve``) were NOT hardened: they do a
non-atomic check-then-act —

    current = self._get_or_raise(...)        # read state == 'pending'
    self._require_state(current, 'pending')  # passes on the stale snapshot
    ...
    self._persist(updated, event='reject')   # UNCONDITIONAL ON CONFLICT upsert

The in-process ``self._lock`` (RLock) gives NO cross-process protection (the very
gap the expire_one docstring documents). ``reject`` is reachable from
``quant_reject`` (an MCP/CLI process); ``claim_for_approval`` + ``record_execution``
are reachable from ``quant_approve`` (a SEPARATE operator / playbook / daemon
process). Two operators — or operator-reject racing an autonomous/playbook approve —
interleave like this:

  1. P-reject reads the row as ``pending`` and passes ``_require_state``.
  2. P-approve CAS-claims the row -> ``approved``, FIRES the reactor (a fill lands
     on the executions bus), and attaches the fill via ``record_execution``.
  3. P-reject resumes and runs its UNCONDITIONAL upsert, clobbering the row to
     ``state='rejected'`` with ``execution=None``.

Result: a proposal that MOVED CAPITAL (fill on the bus) is durably recorded as
``rejected`` with no execution — a fired position misrepresented as rejected on
both the SQLite index AND (since the 'reject' JSONL line is appended last and
``_reconcile_index`` is latest-event-per-id) the audit source-of-truth. This is the
exact fail-class the ``expire_one`` CAS docstring warns about, on the un-swept
``reject``/``approve`` paths (fail-open: a capital-moved position misrepresented).

The deterministic reproduction hooks ``_persist`` so the concurrent approve+fire
commits AFTER ``reject`` has read+checked but BEFORE it writes. The fix is a guarded
CAS (``UPDATE ... WHERE state='pending'``) for ``reject``/``approve`` mirroring
``_cas_expire``: a reject/approve of a no-longer-pending row must LOSE the race and
raise ``ProposalStateError`` rather than clobber the approved+fired row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.proposals import (
    Proposal,
    ProposalStateError,
    ProposalStore,
    _proposal_to_dict,
)


def _build_store(tmp_path: Path) -> ProposalStore:
    return ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )


def test_reject_must_not_clobber_concurrent_approved_fill(tmp_path: Path) -> None:
    """reject() interleaved with a concurrent claim+fire MUST NOT drop the fill.

    Deterministically reproduces the cross-process TOCTOU: a concurrent approve
    (claim_for_approval) + fire (record_execution) commits between reject's
    read+check and its write. The realized fill MUST survive; the proposal MUST
    remain 'approved', and reject MUST lose (raise), never overwrite it to
    'rejected' with execution=None.
    """
    store = _build_store(tmp_path)
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result={"risk_gate": {"kelly_fraction": 0.05}},
    )
    pid = proposal.proposal_id

    # The fill the concurrent approve will attach (proof capital moved).
    fill_record = {
        "proposal_id": pid,
        "asset": "AAPL",
        "fill_size_pct": 0.05,
        "fill_price": 190.0,
        "reactor_name": "paper",
    }

    # Hook the read-then-write GAP: _require_state runs on the stale 'pending'
    # snapshot just BEFORE the write (in both the buggy unconditional-_persist
    # path and the fixed guarded-CAS path). Injecting the concurrent claim+fire
    # right after it returns reproduces the cross-process preemption: the row is
    # 'approved' WITH a fill on the bus by the time reject's write lands.
    real_require_state = store._require_state
    injected = {"done": False}

    def _require_state_then_interleave(p: Proposal, want: str) -> None:
        real_require_state(p, want)  # original gate on the stale snapshot
        if not injected["done"]:
            injected["done"] = True
            store.claim_for_approval(pid, approver_user_id="op-A")
            store.record_execution(pid, execution=fill_record)

    store._require_state = _require_state_then_interleave  # type: ignore[method-assign]

    # With the bug reject's UNCONDITIONAL upsert clobbers the approved+fired row
    # to rejected/execution=None. With the fix the guarded CAS finds state !=
    # 'pending' and reject loses (raises ProposalStateError).
    try:
        store.reject(pid, reason="operator changed mind")
    except ProposalStateError:
        pass  # fix path: reject correctly loses the race

    # Restore so the assertions read the real store.
    store._require_state = real_require_state  # type: ignore[method-assign]

    final = store.get(pid)
    assert final is not None
    assert final.state == "approved", (
        f"proposal {pid} is {final.state!r}; a concurrent reject CLOBBERED the "
        "approved+fired row — a position that moved capital is misrepresented as "
        "rejected (reject lost-update; symmetric to the expire_one CAS fix)"
    )
    assert final.execution is not None, (
        "the realized fill was DROPPED by the concurrent reject — execution=None "
        "on a proposal that fired (fill is on the bus); the audit trail lost the "
        "capital-moved record"
    )
    assert final.execution.get("fill_size_pct") == 0.05


def test_approve_must_not_clobber_concurrent_approved_fill(tmp_path: Path) -> None:
    """The legacy approve() has the identical hole: a concurrent claim+fire that
    commits between approve's read+check and its unconditional write would be
    clobbered to an approved row with execution=None (losing the realized fill).
    approve() of a no-longer-pending row MUST lose the race (raise), not overwrite.
    """
    store = _build_store(tmp_path)
    proposal = store.propose(
        symbol="NVDA",
        asset_class="equity",
        timeframe="1d",
        advisor_result={},
    )
    pid = proposal.proposal_id

    fill_record = {"proposal_id": pid, "asset": "NVDA", "fill_size_pct": 0.04}

    # Same read-then-write gap hook as the reject test: inject the concurrent
    # claim+fire right after approve()'s _require_state gate passes on the stale
    # snapshot, before approve's write lands.
    real_require_state = store._require_state
    injected = {"done": False}

    def _require_state_then_interleave(p: Proposal, want: str) -> None:
        real_require_state(p, want)
        if not injected["done"]:
            injected["done"] = True
            store.claim_for_approval(pid, approver_user_id="op-A")
            store.record_execution(pid, execution=fill_record)

    store._require_state = _require_state_then_interleave  # type: ignore[method-assign]
    try:
        store.approve(pid, approver_user_id="op-B")
    except ProposalStateError:
        pass  # fix path: approve correctly loses the race
    store._require_state = real_require_state  # type: ignore[method-assign]

    final = store.get(pid)
    assert final is not None
    assert final.execution is not None, (
        "the realized fill was DROPPED by a concurrent legacy approve() — "
        "execution=None on a fired proposal (approve lost-update)"
    )
    assert final.execution.get("fill_size_pct") == 0.04


def test_reject_pending_byte_identical_no_concurrency(tmp_path: Path) -> None:
    """Non-vacuity / happy-path: a plain reject of a genuinely pending proposal
    still advances pending -> rejected exactly as before (the CAS does not break
    the uncontended path)."""
    store = _build_store(tmp_path)
    proposal = store.propose(
        symbol="MSFT",
        asset_class="equity",
        timeframe="1d",
        advisor_result={},
    )
    rejected = store.reject(proposal.proposal_id, reason="too risky")
    assert rejected.state == "rejected"
    assert rejected.rejection_reason == "too risky"
    assert rejected.execution is None
    # And it is durable on a fresh read.
    again = store.get(proposal.proposal_id)
    assert again is not None
    assert again.state == "rejected"


def test_approve_pending_byte_identical_no_concurrency(tmp_path: Path) -> None:
    """Non-vacuity: legacy approve() of a genuinely pending proposal still
    advances pending -> approved uncontended."""
    store = _build_store(tmp_path)
    proposal = store.propose(
        symbol="AMD",
        asset_class="equity",
        timeframe="1d",
        advisor_result={},
    )
    approved = store.approve(proposal.proposal_id, approver_user_id="op")
    assert approved.state == "approved"
    again = store.get(proposal.proposal_id)
    assert again is not None
    assert again.state == "approved"


def test_reject_already_approved_loses(tmp_path: Path) -> None:
    """A reject of an ALREADY-approved (claimed) proposal must raise
    ProposalStateError and leave the approved row untouched — never demote a
    claimed/approved proposal to rejected.

    This is the non-interleaved expression of the same invariant and proves the
    state-machine forbids the approved -> rejected transition.
    """
    store = _build_store(tmp_path)
    proposal = store.propose(
        symbol="TSLA",
        asset_class="equity",
        timeframe="1d",
        advisor_result={},
    )
    pid = proposal.proposal_id
    store.claim_for_approval(pid, approver_user_id="op-A")
    store.record_execution(pid, execution={"proposal_id": pid, "fill_size_pct": 0.03})

    with pytest.raises(ProposalStateError):
        store.reject(pid, reason="too late")

    final = store.get(pid)
    assert final is not None
    assert final.state == "approved"
    assert final.execution is not None
    assert _proposal_to_dict(final)["execution"]["fill_size_pct"] == 0.03
