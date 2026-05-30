"""tests/unit/test_proposals_reconcile.py — JSONL index reconciliation.

Per the module contract in hermes_quant/proposals.py: the proposals.jsonl
append-only log is the source of truth; the SQLite index is derived and can
be rebuilt via ProposalStore._reconcile_index().

Money-software discipline: reconciliation must be idempotent, tolerant of a
partial/corrupt trailing line (log + skip, never crash the run), and must
reduce the append-only event log to the latest record per proposal_id.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from hermes_quant.proposals import (
    Proposal,
    ProposalStore,
    _iso,
    _proposal_to_dict,
    _utc_now,
)


def _build_store(tmp_path: Path) -> ProposalStore:
    return ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )


def _seed_jsonl(store: ProposalStore) -> dict[str, str]:
    """Write proposal events to the JSONL by hand (NOT via _persist, so the
    SQLite index starts empty/stale). Returns expected live states keyed by id.

    Events written (file order):
      1. p_pending   create  -> pending   (stays pending, far-future TTL)
      2. p_super     create  -> pending
      3. p_super     approve -> approved  (supersedes the create above)
      4. p_other     create  -> pending
      <malformed trailing line>            -> must be skipped
    """
    now = _utc_now()
    future = _iso(now + timedelta(hours=1))

    def line(*, pid: str, state: str, symbol: str, event: str, **extra) -> str:
        rec = _proposal_to_dict(
            _mk(pid=pid, state=state, symbol=symbol, expires_at=future, **extra)
        )
        rec["_event"] = event
        rec["_event_at"] = _iso(now)
        return json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n"

    text = ""
    text += line(pid="p_pending", state="pending", symbol="AAPL", event="create")
    text += line(pid="p_super", state="pending", symbol="MSFT", event="create")
    text += line(
        pid="p_super",
        state="approved",
        symbol="MSFT",
        event="approve",
        approved_at=_iso(now),
        approver_user_id="op1",
    )
    text += line(pid="p_other", state="pending", symbol="NVDA", event="create")
    # Malformed trailing line: a partial write (no terminating newline,
    # truncated JSON) — the common benign crash-mid-write case.
    text += '{"proposal_id":"p_trunc","state":"pend'

    store.bus_path.write_text(text, encoding="utf-8")

    return {"p_pending": "pending", "p_super": "approved", "p_other": "pending"}


def _mk(*, pid: str, state: str, symbol: str, expires_at: str, **extra):
    base = dict(
        proposal_id=pid,
        state=state,
        symbol=symbol,
        asset_class="equity",
        timeframe="1d",
        created_at=_iso(_utc_now()),
        expires_at=expires_at,
        advisor_result={"symbol": symbol},
    )
    base.update(extra)
    return Proposal(**base)


def test_reconcile_rebuilds_index_from_jsonl(tmp_path):
    store = _build_store(tmp_path)
    expected = _seed_jsonl(store)

    # Index is stale/empty before reconcile.
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0

    n = store._reconcile_index()

    # 3 distinct proposals (p_super collapsed to its latest state),
    # malformed trailing line ignored.
    assert n == 3

    rebuilt = {pid: store.get(pid) for pid in expected}
    for pid, want_state in expected.items():
        assert rebuilt[pid] is not None, f"{pid} missing after reconcile"
        assert rebuilt[pid].state == want_state

    # Superseded proposal carries the approve-transition fields, not the
    # earlier pending create.
    assert rebuilt["p_super"].approved_at is not None
    assert rebuilt["p_super"].approver_user_id == "op1"

    # Malformed line did NOT leak a row.
    assert store.get("p_trunc") is None

    # Only the live set ends up listed pending.
    pending_ids = {p.proposal_id for p in store.list_pending()}
    assert pending_ids == {"p_pending", "p_other"}


def test_reconcile_is_idempotent(tmp_path):
    store = _build_store(tmp_path)
    _seed_jsonl(store)

    first = store._reconcile_index()
    second = store._reconcile_index()
    assert first == second == 3

    with store._conn() as conn:
        rows = conn.execute(
            "SELECT proposal_id, state FROM proposals ORDER BY proposal_id"
        ).fetchall()
    assert {(r["proposal_id"], r["state"]) for r in rows} == {
        ("p_other", "pending"),
        ("p_pending", "pending"),
        ("p_super", "approved"),
    }


def test_reconcile_empty_bus_yields_empty_index(tmp_path):
    store = _build_store(tmp_path)  # _ensure_dirs touches an empty jsonl
    assert store._reconcile_index() == 0
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_reconcile_drops_stale_rows_not_in_jsonl(tmp_path):
    """A row present only in SQLite (e.g. orphaned by a deleted/rotated bus)
    is dropped — JSONL is authoritative."""
    store = _build_store(tmp_path)
    expected = _seed_jsonl(store)
    store._reconcile_index()

    def _ghost_in_index() -> bool:
        with store._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM proposals WHERE proposal_id = 'ghost'"
            ).fetchone()
        return row is not None

    # Inject a ghost row that has no JSONL backing (a valid full record so it
    # could be read back, but it exists only in the index).
    ghost = _mk(
        pid="ghost",
        state="pending",
        symbol="GHOST",
        expires_at=_iso(_utc_now() + timedelta(hours=1)),
    )
    with store._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO proposals (proposal_id, state, symbol, asset_class, "
            "timeframe, created_at, expires_at, record_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                ghost.proposal_id,
                ghost.state,
                ghost.symbol,
                ghost.asset_class,
                ghost.timeframe,
                ghost.created_at,
                ghost.expires_at,
                json.dumps(_proposal_to_dict(ghost)),
            ),
        )
        conn.execute("COMMIT")

    assert _ghost_in_index()  # present before reconcile

    n = store._reconcile_index()
    assert n == len(expected)
    assert not _ghost_in_index()  # dropped: not in the authoritative log
