"""cs44: the family-PARENT ExecutionRecord (asset_class=="multi_leg", role=="parent",
NO reactor_metadata.quantity) must be SKIPPED by both PortfolioState folds.

The multi-leg reactor (react/multileg.py) writes ONE parent + one child-per-leg to
executions.jsonl. The CHILDREN carry the real positions + the full real cash (option
legs ×premium×contracts×100, equity leg ×shares). The PARENT is an audit rollup that
carries NO quantity — folding it creates a PHANTOM ("paper-default","multi_leg",
underlying) position AND books a meaningless extra cash delta on top of the children
(double-count). state.db's equity_total is the gate-SIZED NAV, so a phantom row +
double-counted cash corrupts a live risk-gate input.

reconstruct_from() reads EVERY record from executions.jsonl (including the parent that
_write_family appends), so the rebuild fold is where the bug bites. _apply_execution
gets the same skip for defense-in-depth (a manual replay / future caller could feed
the parent dict directly).

RED today (phantom row + double cash); GREEN after the parent-marker skip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_quant.state.portfolio_state import PortfolioState

_ACCT = "paper-default"
_ASOF = "2026-06-13T15:00:00.000000Z"


def _parent_record() -> dict[str, Any]:
    """A multi_leg family PARENT (the audit rollup) — NO reactor_metadata.quantity.

    Mirrors react/multileg.py:_build_records parent: asset_class=="multi_leg",
    role=="parent", a net_fill fill_price, a fill_size_pct NAV fraction.
    """
    return {
        "proposal_id": "mleg_prop_1",
        "signal_id": None,
        "asset": "AAPL",
        "asset_class": "multi_leg",
        "timeframe": "",
        "asof_decision": _ASOF,
        "asof_execution": _ASOF,
        "target_position_pct": 0.10,
        "decision_price": -1.50,
        "fill_price": -1.50,  # net_fill (a credit)
        "fill_size_pct": 0.10,
        "reactor_name": "multileg-paper",
        "human_in_the_loop": True,
        "approver_user_id": None,
        "reactor_metadata": {
            "multi_leg_id": "mleg_prop_1",
            "strategy_kind": "covered_call",
            "role": "parent",
            "paper": True,
        },
        "bar_ts": None,
        "account_id": _ACCT,
    }


def _option_child() -> dict[str, Any]:
    """The short-call option child — us_option, signed contracts in metadata.quantity."""
    return {
        "proposal_id": "mleg_prop_1",
        "signal_id": None,
        "asset": "AAPL260116C00200000",
        "asset_class": "us_option",
        "timeframe": "",
        "asof_decision": _ASOF,
        "asof_execution": _ASOF,
        "target_position_pct": -0.10,
        "decision_price": 1.50,
        "fill_price": 1.50,  # per-contract premium
        "fill_size_pct": -0.10,
        "reactor_name": "multileg-paper",
        "human_in_the_loop": True,
        "approver_user_id": None,
        "reactor_metadata": {
            "multi_leg_id": "mleg_prop_1",
            "leg_index": 0,
            "role": "leg",
            "quantity": -1.0,  # signed contracts (short 1)
            "paper": True,
        },
        "bar_ts": None,
        "account_id": _ACCT,
    }


def _equity_child() -> dict[str, Any]:
    """The +100-share covered-call equity child — equity, signed shares in metadata."""
    return {
        "proposal_id": "mleg_prop_1",
        "signal_id": None,
        "asset": "AAPL",
        "asset_class": "equity",
        "timeframe": "",
        "asof_decision": _ASOF,
        "asof_execution": _ASOF,
        "target_position_pct": 0.10,
        "decision_price": 200.0,
        "fill_price": 200.0,  # per-share
        "fill_size_pct": 0.10,
        "reactor_name": "multileg-paper",
        "human_in_the_loop": True,
        "approver_user_id": None,
        "reactor_metadata": {
            "multi_leg_id": "mleg_prop_1",
            "role": "equity_leg",
            "quantity": 100.0,  # signed shares (long 100)
            "paper": True,
        },
        "bar_ts": None,
        "account_id": _ACCT,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture()
def ps(tmp_path: Path) -> PortfolioState:
    return PortfolioState(state_db_path=tmp_path / "state.db")


@pytest.fixture()
def executions_path(tmp_path: Path) -> Path:
    return tmp_path / "executions.jsonl"


# ---------------------------------------------------------------------------
# Children-only cash, computed independently of the fold, as the GREEN target.
# Option child: signed_contracts(-1) × premium(1.50) × 100 = -150 ⇒ +150 cash (credit).
# Equity child: signed_shares(+100) × price(200) × 1 = +20000 ⇒ -20000 cash (debit).
# Net children cash delta = +150 - 20000 = -19850 off the 100_000 bootstrap.
# ---------------------------------------------------------------------------
_INITIAL_CASH = 100_000.0
# delta_cash = -signed_qty * price * multiplier
_OPTION_CASH = -1.0 * (-1.0) * 1.50 * 100.0  # short 1 contract @ $1.50 prem => +150.0 credit
_EQUITY_CASH = -1.0 * (100.0) * 200.0 * 1.0  # long 100 sh @ $200 => -20000.0 debit
_CHILDREN_ONLY_CASH = _INITIAL_CASH + _OPTION_CASH + _EQUITY_CASH  # 80_150.0


class TestMultiLegParentSkipRebuild:
    def test_no_phantom_multi_leg_position(
        self, ps: PortfolioState, executions_path: Path
    ):
        """reconstruct_from a full family ⇒ NO ("multi_leg", underlying) phantom row."""
        _write_jsonl(executions_path, [_parent_record(), _option_child(), _equity_child()])
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions(_ACCT)
        # The phantom would key as ("multi_leg", "AAPL").
        assert ("multi_leg", "AAPL") not in positions, (
            f"family-parent folded into a phantom multi_leg position: {positions}"
        )
        # The two real children survive.
        assert ("us_option", "AAPL260116C00200000") in positions
        assert ("equity", "AAPL") in positions
        assert positions[("us_option", "AAPL260116C00200000")].quantity == pytest.approx(-1.0)
        assert positions[("equity", "AAPL")].quantity == pytest.approx(100.0)

    def test_cash_not_double_counted(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Cash reflects ONLY the children's real notionals, not the parent's extra delta."""
        _write_jsonl(executions_path, [_parent_record(), _option_child(), _equity_child()])
        ps.reconstruct_from(executions_path)
        cash = ps.get_cash(_ACCT)
        assert cash is not None
        assert cash.balance_usd == pytest.approx(_CHILDREN_ONLY_CASH), (
            f"cash double-counted by the parent fold: {cash.balance_usd} "
            f"!= children-only {_CHILDREN_ONLY_CASH}"
        )

    def test_children_only_book_byte_identical(
        self, ps: PortfolioState, executions_path: Path, tmp_path: Path
    ):
        """A children-ONLY rebuild (no parent) yields the SAME positions+cash as the
        full family — i.e. the parent contributes exactly nothing once skipped."""
        # full family
        _write_jsonl(executions_path, [_parent_record(), _option_child(), _equity_child()])
        ps.reconstruct_from(executions_path)
        full_positions = {k: (v.quantity, v.avg_entry_price) for k, v in ps.get_positions(_ACCT).items()}
        full_cash = ps.get_cash(_ACCT).balance_usd

        # children only, fresh db
        ps2 = PortfolioState(state_db_path=tmp_path / "state2.db")
        path2 = tmp_path / "executions2.jsonl"
        _write_jsonl(path2, [_option_child(), _equity_child()])
        ps2.reconstruct_from(path2)
        children_positions = {k: (v.quantity, v.avg_entry_price) for k, v in ps2.get_positions(_ACCT).items()}
        children_cash = ps2.get_cash(_ACCT).balance_usd

        assert full_positions == children_positions
        assert full_cash == pytest.approx(children_cash)


class TestMultiLegParentSkipIncremental:
    def test_apply_parent_is_noop(self, ps: PortfolioState):
        """apply_execution on the bare PARENT dict mutates NOTHING (no phantom, no cash)."""
        ps.apply_execution(_parent_record())
        assert ps.get_positions(_ACCT) == {}
        # No cash row is written by a pure-skip (the parent never touches cash).
        assert ps.get_cash(_ACCT) is None

    def test_apply_children_after_parent_unaffected(self, ps: PortfolioState):
        """Feeding parent then both children (the live _reconcile_state order minus the
        parent skip) yields children-only positions + cash."""
        ps.apply_execution(_parent_record())  # skipped
        ps.apply_execution(_option_child())
        ps.apply_execution(_equity_child())
        positions = ps.get_positions(_ACCT)
        assert ("multi_leg", "AAPL") not in positions
        assert positions[("us_option", "AAPL260116C00200000")].quantity == pytest.approx(-1.0)
        assert positions[("equity", "AAPL")].quantity == pytest.approx(100.0)
        cash = ps.get_cash(_ACCT)
        assert cash is not None
        assert cash.balance_usd == pytest.approx(_CHILDREN_ONLY_CASH)


class TestNonMultiLegByteIdentical:
    """The skip fires ONLY on the parent marker — an equity/option-only book is
    byte-identical (the regression rail)."""

    def test_plain_equity_book_unaffected(
        self, ps: PortfolioState, executions_path: Path
    ):
        rec = {
            "proposal_id": "eq_1",
            "asset": "MSFT",
            "asset_class": "equity",
            "asof_decision": _ASOF,
            "asof_execution": _ASOF,
            "fill_price": 300.0,
            "fill_size_pct": 0.04,
            "reactor_name": "paper",
            "reactor_metadata": {"paper": True},
            "account_id": _ACCT,
        }
        _write_jsonl(executions_path, [rec])
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions(_ACCT)
        assert ("equity", "MSFT") in positions
        assert positions[("equity", "MSFT")].quantity == pytest.approx(0.04)
        cash = ps.get_cash(_ACCT)
        assert cash.balance_usd == pytest.approx(_INITIAL_CASH - 0.04 * 300.0)
