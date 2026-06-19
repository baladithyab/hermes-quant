"""expire_one TOCTOU clobber regression fence.

``expire_one`` (the TTL sweep) does a NON-ATOMIC check-then-act:

    current = self._get_or_raise(proposal_id)   # read state on connection C1
    if current.state != "pending": return None  # check
    ... self._persist(updated, event="expire")  # UNCONDITIONAL upsert on C2

``_persist`` runs an UNCONDITIONAL
``INSERT ... ON CONFLICT(proposal_id) DO UPDATE SET state='expired', ...`` —
there is NO ``WHERE state='pending'`` compare-and-set guard. The in-process
``self._lock`` (RLock) gives ZERO cross-process protection; the docstring claims
cross-process safety is the JSONL flock + WAL, but this upsert is unconditional.

``expire_one`` is driven cross-process by callers in a DIFFERENT process from the
HITL approve flow: ``sweep_expired`` (cron / MCP), ``list_pending`` ->
``sweep_expired`` (quant_list_proposals), and lazy-expire inside ``get``
(quant_get_proposal).

RACE around the TTL boundary:
  Process A (sweeper): reads proposal P as pending, passes the
  ``state != 'pending'`` gate, then is preempted before ``_persist`` commits.
  Process B (approve): the proposal's expires_at has not yet elapsed on B's
  separately-sampled clock, so B's approve does NOT see it expired; B advances
  P to 'approved' and FIRES the reactor (capital moves) + attaches the fill.
  Process A: resumes from its STALE state=pending read and the UNCONDITIONAL
  upsert overwrites the row back to state='expired', execution=None.

RED-verifiable: a proposal that fired a real fill ends FINAL state='expired'
with execution=None. The JSONL audit does NOT heal it — ``_persist`` appends the
'expire' event LAST (A commits last) and ``_reconcile_index`` is
last-event-per-id, so it reconstructs state='expired' too — corrupting the audit
source of truth, not just the SQLite index.

These tests use two ProposalStore handles on the SAME db_path (the cross-process
shape: separate connections, independent in-process locks) and reproduce the
stale-read window deterministically (no real threads).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_quant.proposals import Proposal, ProposalStore, _proposal_from_json


def _advisor_result() -> dict[str, Any]:
    return {
        "as_of": "2026-06-04T00:00:00Z",
        "decision_price": 200.0,
        "signal_id": "sig-expire-toctou",
        "risk_gate": {"pass": True, "kelly_fraction": 0.05},
    }


def _two_handles(tmp_path: Path) -> tuple[ProposalStore, ProposalStore]:
    """Two independent ProposalStore handles on the same on-disk db/jsonl —
    the cross-process shape (separate connections; independent in-process
    RLocks, so self._lock provides NO mutual exclusion between them)."""
    bus = tmp_path / "proposals.jsonl"
    db = tmp_path / "proposals.db"
    return (
        ProposalStore(bus_path=bus, db_path=db),
        ProposalStore(bus_path=bus, db_path=db),
    )


def _bus_events(path: Path) -> list[tuple[str, bool]]:
    """(state, has-execution) of every reconstructable JSONL event line."""
    out: list[tuple[str, bool]] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        p = _proposal_from_json(line)
        out.append((p.state, p.execution is not None))
    return out


# ---------------------------------------------------------------------------
# RED: a stale-pending expire must NOT clobber an approved+fired proposal
# ---------------------------------------------------------------------------


def test_expire_one_stale_read_must_not_clobber_approved_fired(
    tmp_path: Path,
) -> None:
    """Handle A is mid-``expire_one`` (read P as pending) when handle B
    approves + attaches a fill. A's unconditional upsert (unfixed) overwrites
    the approved/fired row back to expired/execution=None.

    We drive the exact preemption window by wrapping handle A's ``_persist``:
    after A has read+checked P as pending but BEFORE A commits the expire, we
    let B win the approve + record the execution (B's clock has not yet crossed
    the TTL, so B does not see P expired). Then A's expire write lands.

    On the FIXED code A's expire is a compare-and-set (``WHERE state='pending'``):
    the row is no longer pending, so A updates 0 rows, returns None, and writes
    NO 'expire' audit line — the approved+fired proposal is preserved.
    """
    store_a, store_b = _two_handles(tmp_path)
    proposal = store_a.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id

    fired_execution = {"asof_execution": "2026-06-04T00:00:01Z", "filled_qty": 10}

    # Inject B's concurrent approve+fill into A's check->act gap, hooked at the
    # POINT OF READ so it is robust to either code version's write path: the
    # moment A's expire_one has read P as 'pending' (the stale snapshot), let B
    # win the approve + attach the fill. A then proceeds to its write from the
    # stale snapshot. On the unfixed code A's write is an UNCONDITIONAL upsert
    # (clobbers approved -> expired); on the fixed code A's write is a guarded
    # compare-and-set (``WHERE state='pending'`` -> 0 rows -> no clobber).
    real_read = store_a._get_or_raise
    injected = {"done": False}

    def _read_with_injected_race(proposal_id_arg: str) -> Proposal:
        result = real_read(proposal_id_arg)
        if (
            not injected["done"]
            and proposal_id_arg == pid
            and result.state == "pending"
        ):
            injected["done"] = True
            # Process B: approve + attach the fired execution (capital moved).
            store_b.approve(pid, approver_user_id="operator", execution=fired_execution)
        return result

    store_a._get_or_raise = _read_with_injected_race  # type: ignore[method-assign]

    # Process A: TTL sweep enters expire_one, reads P as pending (injecting B's
    # approve+fire), then A's expire write lands from the stale snapshot.
    store_a.expire_one(pid)
    # Restore so the post-checks below read the real DB state.
    store_a._get_or_raise = real_read  # type: ignore[method-assign]

    # The approved+fired proposal must survive: never clobbered to expired.
    final = store_a.get(pid)
    assert final is not None
    assert final.state == "approved", (
        f"proposal that fired a real fill ended state={final.state!r} — a "
        "stale-pending expire clobbered an approved+fired proposal "
        "(capital-moved position misrepresented as expired)"
    )
    assert final.execution is not None, (
        "the fired execution record was dropped by the expire clobber "
        f"(execution={final.execution!r})"
    )

    # JSONL audit (the source of truth) must not carry a terminal 'expired'
    # event for this fired proposal — _reconcile_index is last-event-per-id, so
    # a trailing 'expire' event would corrupt the rebuilt index too.
    events = _bus_events(tmp_path / "proposals.jsonl")
    assert ("expired", False) not in events, (
        "JSONL audit corrupted: an 'expired' event with no execution was "
        f"appended for a fired proposal — events={events}"
    )

    # And the SQLite index, after a full reconcile from the JSONL truth, agrees.
    store_a._reconcile_index()
    reconciled = store_a.get(pid)
    assert reconciled is not None
    assert reconciled.state == "approved"
    assert reconciled.execution is not None


# ---------------------------------------------------------------------------
# GREEN guards: normal single-handle lifecycle is unchanged
# ---------------------------------------------------------------------------


def test_expire_one_still_expires_a_genuinely_pending_proposal(
    tmp_path: Path,
) -> None:
    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id

    expired = store.expire_one(pid)
    assert expired is not None
    assert expired.state == "expired"
    assert expired.expired_at is not None
    assert store.get(pid).state == "expired"


def test_expire_one_idempotent_on_already_expired(tmp_path: Path) -> None:
    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id
    assert store.expire_one(pid) is not None
    # A second expire is a no-op (already non-pending).
    assert store.expire_one(pid) is None


def test_expire_one_refuses_to_expire_an_approved_proposal(tmp_path: Path) -> None:
    """Even from a single handle, expire_one must never advance a non-pending
    (already approved) proposal to expired."""
    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id
    store.approve(pid, approver_user_id="operator", execution={"filled_qty": 5})
    assert store.expire_one(pid) is None
    assert store.get(pid).state == "approved"


def test_sweep_expired_still_counts_genuine_expiries(tmp_path: Path) -> None:
    """sweep_expired must still expire genuinely-past-TTL pending proposals."""
    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    # ttl_minutes=0 -> expires_at == created_at, immediately past TTL.
    store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
        ttl_minutes=0,
    )
    store.propose(
        symbol="MSFT",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
        ttl_minutes=0,
    )
    n = store.sweep_expired()
    assert n == 2
    # Idempotent: a second sweep expires nothing more.
    assert store.sweep_expired() == 0
