"""Unit tests for the B01 multi-leg PRODUCER seam (ADR-0029).

Deterministic, offline, no network / no Alpaca. Covers the full produce->consume loop:

  * a CC built from a DETERMINISTIC replay chain -> options_gate -> minted via
    from_gate_result -> persisted as proposal_kind=='multi_leg';
  * store.get() returns something select_reactor routes to MultiLegPaperReactor;
  * with HERMES_QUANT_MULTILEG_REACTOR=1 the reactor FILLS it on paper;
  * the equity quant_propose path is byte-identical (no proposal_kind / multi_leg keys);
  * an ungated / gate-rejected structure does NOT persist a passing proposal;
  * the SQLite migration is additive + backward-compatible;
  * the #38 lock holds across persistence: a reconstructed multi_leg proposal's
    risk_gate_pass is the gate's verdict re-minted via from_gate_result, never hand-set.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import ChainSnapshotReader
from hermes_quant.options.recipes import (
    RecipeBuildError,
    build_and_persist_multi_leg,
    build_multi_leg_proposal,
)
from hermes_quant.proposals import (
    Proposal,
    ProposalStore,
    StoredMultiLegProposal,
    _multi_leg_from_dict,
    _multi_leg_to_dict,
)
from hermes_quant.react.dispatch import is_multi_leg_proposal, select_reactor
from hermes_quant.react.multileg import MultiLegPaperReactor


# --------------------------------------------------------------------------- #
# Fixtures / chain builders (mirror tests/unit/test_options_data.py)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_alpaca_creds(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")


ASOF = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)


def _write_chain(reader: ChainSnapshotReader, rows: list[dict]) -> None:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = reader._path_for("NVDA", ASOF.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), path)


def _row(symbol: str, *, right: str = "C", delta: float = 0.30) -> dict:
    return {
        "contract_symbol": symbol,
        "asof": ASOF,
        "fetched_at": ASOF,
        "underlying_spot": 150.0,
        "risk_free_rate": 0.05,
        "bid": 2.40,
        "ask": 2.60,
        "last": 2.50,
        "volume": 100,
        "open_interest": 500,
        # theta>0 (theta-collecting short) so the CC passes O4; sign convention in
        # aggregate_net_greeks flips for a short leg.
        "delta": delta,
        "gamma": 0.001,
        "theta": 0.05,
        "vega": 0.05,
        "rho": 0.01,
        "iv": 0.45,
        "iv_source": "provider",
    }


def _call_chain(reader: ChainSnapshotReader) -> None:
    # ~25 DTE calls at three strikes; the 0.30-delta strike is the selected short.
    _write_chain(
        reader,
        [
            _row("NVDA260626C00155000", delta=0.40),
            _row("NVDA260626C00160000", delta=0.30),
            _row("NVDA260626C00165000", delta=0.20),
        ],
    )


def _put_chain(reader: ChainSnapshotReader) -> None:
    _write_chain(
        reader,
        [
            _row("NVDA260626P00145000", right="P", delta=-0.40),
            _row("NVDA260626P00140000", right="P", delta=-0.30),
            _row("NVDA260626P00135000", right="P", delta=-0.20),
        ],
    )


def _store(tmp_path) -> ProposalStore:
    return ProposalStore(bus_path=tmp_path / "p.jsonl", db_path=tmp_path / "p.db")


# --------------------------------------------------------------------------- #
# Build -> gate -> mint
# --------------------------------------------------------------------------- #
def test_cc_builds_and_gates_and_mints(gate_on, tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    res = build_multi_leg_proposal(
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        reader=reader,
        nav=1_000_000.0,
        held_shares=1000,
        options_buying_power=500_000.0,
    )
    assert res.admitted is True
    assert res.bucket.value == "covered_call"
    assert res.proposal is not None
    # risk_gate_pass came from the gate via from_gate_result (the #38 lock).
    assert res.proposal.risk_gate_pass is True
    # the 0.30-delta strike (160) was selected deterministically.
    assert res.proposal.all_symbols == ("NVDA260626C00160000",)
    # a short call is a CREDIT => negative signed net.
    assert res.proposal.net_debit_credit < 0
    assert res.proposal.stock_leg is not None
    assert res.proposal.stock_leg.qty == 100 * res.contracts


def test_gate_disabled_makes_build_raise(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_GATE", raising=False)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    from hermes_quant.risk.options_gate import OptionsGateDisabled

    with pytest.raises(OptionsGateDisabled):
        build_multi_leg_proposal(
            symbol="NVDA",
            asof=ASOF,
            strategy_kind="covered_call",
            reader=reader,
            nav=1_000_000.0,
            held_shares=1000,
            options_buying_power=500_000.0,
        )


def test_csp_builds_and_gates(gate_on, tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _put_chain(reader)
    res = build_multi_leg_proposal(
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="cash_secured_put",
        reader=reader,
        nav=1_000_000.0,
        held_shares=0,
        options_buying_power=5_000_000.0,  # cover strike*100*c
    )
    assert res.admitted is True
    assert res.bucket.value == "cash_secured_put"
    assert res.proposal.stock_leg is None  # CSP has no equity leg


# --------------------------------------------------------------------------- #
# Persist -> store.get -> route -> fill
# --------------------------------------------------------------------------- #
def test_persist_get_routes_to_multileg_reactor(gate_on, tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    store = _store(tmp_path)
    res, rec = build_and_persist_multi_leg(
        store=store,
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        nav=1_000_000.0,
        options_buying_power=500_000.0,
        held_shares=1000,
        reader=reader,
    )
    assert rec is not None
    assert rec.proposal_kind == "multi_leg"
    assert rec.asset_class == "multi_leg"

    got = store.get(rec.proposal_id)
    assert isinstance(got, StoredMultiLegProposal)
    assert got.state == "pending"
    assert got.proposal_kind == "multi_leg"
    # the re-minted verdict is the gate's, via from_gate_result.
    assert got.risk_gate_pass is True
    assert is_multi_leg_proposal(got) is True
    assert isinstance(select_reactor(got), MultiLegPaperReactor)


def test_reactor_fires_on_paper_when_flag_on(gate_on, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    store = _store(tmp_path)
    res, rec = build_and_persist_multi_leg(
        store=store,
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        nav=1_000_000.0,
        options_buying_power=500_000.0,
        held_shares=1000,
        reader=reader,
    )
    got = store.get(rec.proposal_id)
    reactor = MultiLegPaperReactor(executions_path=tmp_path / "executions.jsonl")
    parent = reactor.execute(got, fill_size_pct=0.05, approver_user_id="op1")
    assert parent.asset == "NVDA"
    assert parent.asset_class == "multi_leg"
    assert parent.reactor_metadata.get("parent_status") == "filled"
    assert (tmp_path / "executions.jsonl").exists()


def test_reactor_disabled_refuses_not_silent_equity(gate_on, monkeypatch, tmp_path) -> None:
    from hermes_quant.react.multileg import MultiLegReactorDisabled

    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    store = _store(tmp_path)
    _, rec = build_and_persist_multi_leg(
        store=store,
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        nav=1_000_000.0,
        options_buying_power=500_000.0,
        held_shares=1000,
        reader=reader,
    )
    got = store.get(rec.proposal_id)
    reactor = select_reactor(got)
    with pytest.raises(MultiLegReactorDisabled):
        reactor.execute(got, fill_size_pct=0.05)


# --------------------------------------------------------------------------- #
# Rail: an ungated / gate-rejected structure does NOT persist a passing proposal
# --------------------------------------------------------------------------- #
def test_gate_rejected_does_not_persist(gate_on, tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    store = _store(tmp_path)
    # held_shares=0 -> the short call is NAKED -> gate rejects.
    res, rec = build_and_persist_multi_leg(
        store=store,
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        nav=1_000_000.0,
        options_buying_power=500_000.0,
        held_shares=0,
        reader=reader,
    )
    assert res.admitted is False
    assert res.proposal is None
    assert rec is None
    # nothing pending persisted.
    assert store.list_pending() == []


def test_reconstructed_rejected_proposal_is_not_passing() -> None:
    """A persisted REJECTED gate result re-mints risk_gate_pass=False (the verdict is
    rebuilt via from_gate_result; a stored reject can never become a pass)."""
    from hermes_quant.options.data import (
        NetGreeks,
        OptionGreeksSnapshot,
        OptionLeg,
    )
    from hermes_quant.options.multileg import MultiLegProposal
    from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket

    gr = OptionsGateResult(
        admitted=False,
        bucket=StructureBucket.NAKED,
        reason="naked_short_call",
        net_greeks=NetGreeks(),
        bpr_estimate=0.0,
        max_loss=None,
        contracts=0,
        warnings=(),
    )
    leg = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=OptionGreeksSnapshot(
            delta=0.3, gamma=0.01, theta=0.05, vega=0.05, rho=0.01, iv=0.4
        ),
    )
    p = MultiLegProposal.from_gate_result(
        gate_result=gr,
        proposal_id="prop_x",
        asof=ASOF,
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(leg,),
        stock_leg=None,
        outer_qty=1,
        net_debit_credit=Decimal("-2.50"),
        max_gain=None,
        breakeven_underlying=(Decimal("160"),),
        rationale="r",
        source_recipe_id="s",
    )
    payload = _multi_leg_to_dict(p, gr)
    payload["proposal_id"] = "prop_x"
    inner = _multi_leg_from_dict(payload)
    assert inner.risk_gate_pass is False
    assert inner.risk_gate_reason == "naked_short_call"


def test_no_eligible_contract_raises(gate_on, tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    # only PUTS in the chain -> a covered_call (calls) has no eligible leg.
    _put_chain(reader)
    with pytest.raises(RecipeBuildError):
        build_multi_leg_proposal(
            symbol="NVDA",
            asof=ASOF,
            strategy_kind="covered_call",
            reader=reader,
            nav=1_000_000.0,
            held_shares=1000,
            options_buying_power=500_000.0,
        )


# --------------------------------------------------------------------------- #
# Persistence round-trip + the #38 lock at the store boundary
# --------------------------------------------------------------------------- #
def test_multi_leg_payload_roundtrip(gate_on, tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _call_chain(reader)
    res = build_multi_leg_proposal(
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        reader=reader,
        nav=1_000_000.0,
        held_shares=1000,
        options_buying_power=500_000.0,
    )
    from hermes_quant.options.recipes import _result_to_gate

    payload = _multi_leg_to_dict(res.proposal, _result_to_gate(res))
    payload["proposal_id"] = res.proposal.proposal_id
    rebuilt = _multi_leg_from_dict(payload)
    assert rebuilt.proposal_id == res.proposal.proposal_id
    assert rebuilt.strategy_kind == res.proposal.strategy_kind
    assert rebuilt.all_symbols == res.proposal.all_symbols
    assert rebuilt.net_debit_credit == res.proposal.net_debit_credit
    assert rebuilt.risk_gate_pass == res.proposal.risk_gate_pass
    assert rebuilt.risk_gate_bucket == res.proposal.risk_gate_bucket
    assert isinstance(rebuilt.net_debit_credit, Decimal)


# --------------------------------------------------------------------------- #
# Equity byte-identity + additive migration
# --------------------------------------------------------------------------- #
def test_equity_serialization_byte_identical(tmp_path) -> None:
    store = _store(tmp_path)
    prop = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result={"risk_gate": {"pass": True, "kelly_fraction": 0.05}},
    )
    # No B01 keys in the in-memory dict, the JSONL record, or the reconstructed object.
    from hermes_quant.proposals import _proposal_to_dict

    d = _proposal_to_dict(prop)
    assert "proposal_kind" not in d
    assert "multi_leg" not in d

    line = (tmp_path / "p.jsonl").read_text().strip().splitlines()[0]
    rec = json.loads(line)
    assert "proposal_kind" not in rec
    assert "multi_leg" not in rec

    got = store.get(prop.proposal_id)
    assert isinstance(got, Proposal)
    assert not isinstance(got, StoredMultiLegProposal)
    assert got.proposal_kind == "equity"


def test_additive_migration_on_pre_b01_db(tmp_path) -> None:
    db = tmp_path / "p.db"
    bus = tmp_path / "p.jsonl"
    # Build a PRE-B01 schema (no proposal_kind column) + an old equity row.
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """CREATE TABLE proposals (
            proposal_id TEXT PRIMARY KEY NOT NULL, state TEXT NOT NULL,
            symbol TEXT NOT NULL, asset_class TEXT NOT NULL, timeframe TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, approved_at TEXT,
            rejected_at TEXT, expired_at TEXT, record_json TEXT NOT NULL
        ) WITHOUT ROWID;"""
    )
    old = {
        "proposal_id": "prop_old",
        "state": "pending",
        "symbol": "AAPL",
        "asset_class": "equity",
        "timeframe": "1d",
        "created_at": "2026-05-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "approved_at": None,
        "approver_user_id": None,
        "size_override_pct": None,
        "rejected_at": None,
        "rejection_reason": None,
        "expired_at": None,
        "advisor_result": {},
        "execution": None,
    }
    conn.execute(
        "INSERT INTO proposals (proposal_id,state,symbol,asset_class,timeframe,"
        "created_at,expires_at,approved_at,rejected_at,expired_at,record_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "prop_old",
            "pending",
            "AAPL",
            "equity",
            "1d",
            "2026-05-01T00:00:00Z",
            "2099-01-01T00:00:00Z",
            None,
            None,
            None,
            json.dumps(old, sort_keys=True, separators=(",", ":")),
        ),
    )
    conn.commit()
    conn.close()
    bus.write_text(
        json.dumps(old, sort_keys=True, separators=(",", ":")) + "\n"
    )

    # Opening the store runs the additive migration.
    store = ProposalStore(bus_path=bus, db_path=db)
    cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(proposals)")}
    assert "proposal_kind" in cols

    got = store.get("prop_old")
    assert isinstance(got, Proposal)
    assert got.symbol == "AAPL"
    assert got.proposal_kind == "equity"  # backfilled default

    # _reconcile_index also writes the new column.
    assert store._reconcile_index() == 1
    assert store.get("prop_old").proposal_kind == "equity"


# --------------------------------------------------------------------------- #
# tools.quant_propose branch (the operator seam)
# --------------------------------------------------------------------------- #
def _hitl_home(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg_dir = tmp_path / ".hermes"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("quant:\n  pdr:\n    mode: hitl\n")


def _patch_default_store(monkeypatch, store: ProposalStore) -> None:
    import hermes_quant.proposals as proposals_module

    monkeypatch.setattr(proposals_module, "_default_store", store)


def _patch_multileg_executions_path(monkeypatch, path) -> None:
    import hermes_quant.daemon.signal_bus as bus_module
    import hermes_quant.react.multileg as multileg_module

    monkeypatch.setattr(multileg_module, "EXECUTION_BUS_PATH", path)
    monkeypatch.setattr(bus_module, "EXECUTION_BUS_PATH", path)


def _execution_records(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_quant_propose_equity_branch_returns_none() -> None:
    """The multi-leg helper returns None for an equity (or absent) strategy_kind, so
    the equity quant_propose path runs unchanged (byte-identical)."""
    from hermes_quant.tools import _maybe_propose_multi_leg

    assert _maybe_propose_multi_leg("AAPL", {}) is None
    assert _maybe_propose_multi_leg("AAPL", {"strategy_kind": "equity"}) is None
    assert _maybe_propose_multi_leg("AAPL", {"strategy_kind": "long"}) is None


def test_quant_propose_multileg_requires_gate_flag(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_GATE", raising=False)
    _hitl_home(monkeypatch, tmp_path)
    from hermes_quant.tools import quant_propose

    out = json.loads(
        quant_propose({"symbol": "NVDA", "strategy_kind": "covered_call"})
    )
    assert out["success"] is False
    assert out["error"] == "options_gate_disabled"


def test_quant_propose_multileg_happy_path(gate_on, monkeypatch, tmp_path) -> None:
    _hitl_home(monkeypatch, tmp_path)
    # Lay down a deterministic replay chain under a dedicated chains_dir we pass via args.
    chains_dir = tmp_path / "chains"
    reader = ChainSnapshotReader(chains_dir=chains_dir)
    _call_chain(reader)

    # Point get_default_store at an isolated store.
    store = ProposalStore(bus_path=tmp_path / "p.jsonl", db_path=tmp_path / "p.db")
    _patch_default_store(monkeypatch, store)

    from hermes_quant.tools import quant_propose

    out = json.loads(
        quant_propose(
            {
                "symbol": "NVDA",
                "strategy_kind": "covered_call",
                "asof": "2026-06-01T16:00:00Z",
                "nav": 1_000_000.0,
                "options_buying_power": 500_000.0,
                "held_shares": 1000,
                "chains_dir": str(chains_dir),
            }
        )
    )
    assert out["success"] is True, out
    assert out["proposal_kind"] == "multi_leg"
    assert out["bucket"] == "covered_call"
    # the proposal is retrievable + routable.
    got = store.get(out["proposal_id"])
    assert isinstance(got, StoredMultiLegProposal)
    assert isinstance(select_reactor(got), MultiLegPaperReactor)


def test_quant_propose_multileg_gate_reject_does_not_persist(
    gate_on, monkeypatch, tmp_path
) -> None:
    _hitl_home(monkeypatch, tmp_path)
    chains_dir = tmp_path / "chains"
    reader = ChainSnapshotReader(chains_dir=chains_dir)
    _call_chain(reader)

    store = ProposalStore(bus_path=tmp_path / "p.jsonl", db_path=tmp_path / "p.db")
    _patch_default_store(monkeypatch, store)

    from hermes_quant.tools import quant_propose

    out = json.loads(
        quant_propose(
            {
                "symbol": "NVDA",
                "strategy_kind": "covered_call",
                "asof": "2026-06-01T16:00:00Z",
                "nav": 1_000_000.0,
                "options_buying_power": 500_000.0,
                "held_shares": 0,  # naked -> gate reject
                "chains_dir": str(chains_dir),
            }
        )
    )
    assert out["success"] is False
    assert out["error"] == "gate_rejected"
    assert store.list_pending() == []


def test_quant_approve_multileg_over_cap_override_rejected_before_bus_write(
    gate_on,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    _hitl_home(monkeypatch, tmp_path)
    exec_path = tmp_path / "executions.jsonl"
    _patch_multileg_executions_path(monkeypatch, exec_path)
    reader = ChainSnapshotReader(chains_dir=tmp_path / "chains")
    _call_chain(reader)
    store = ProposalStore(bus_path=tmp_path / "p.jsonl", db_path=tmp_path / "p.db")
    _patch_default_store(monkeypatch, store)
    _, record = build_and_persist_multi_leg(
        store=store,
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        nav=1_000_000.0,
        options_buying_power=500_000.0,
        held_shares=1000,
        reader=reader,
    )
    assert record is not None

    from hermes_quant.tools import quant_approve

    out = json.loads(
        quant_approve({"proposal_id": record.proposal_id, "size_override_pct": 2.0})
    )

    assert out["success"] is False
    assert out["error"] == "fill_size_invariant"
    assert out["state"] == "pending"
    assert store.get(record.proposal_id).state == "pending"
    assert _execution_records(exec_path) == []


def test_quant_approve_multileg_nan_kelly_rejected_before_bus_write(
    gate_on,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    _hitl_home(monkeypatch, tmp_path)
    exec_path = tmp_path / "executions.jsonl"
    _patch_multileg_executions_path(monkeypatch, exec_path)
    reader = ChainSnapshotReader(chains_dir=tmp_path / "chains")
    _call_chain(reader)
    store = ProposalStore(bus_path=tmp_path / "p.jsonl", db_path=tmp_path / "p.db")
    _patch_default_store(monkeypatch, store)
    _, record = build_and_persist_multi_leg(
        store=store,
        symbol="NVDA",
        asof=ASOF,
        strategy_kind="covered_call",
        nav=1_000_000.0,
        options_buying_power=500_000.0,
        held_shares=1000,
        reader=reader,
        advisor_result={"risk_gate": {"pass": True, "kelly_fraction": float("nan")}},
    )
    assert record is not None

    from hermes_quant.tools import quant_approve

    out = json.loads(quant_approve({"proposal_id": record.proposal_id}))

    assert out["success"] is False
    assert out["error"] == "fill_size_invariant"
    assert out["state"] == "pending"
    assert store.get(record.proposal_id).state == "pending"
    assert _execution_records(exec_path) == []
