"""Unit tests for PortfolioState.get_marked_equity read-time MTM (ADR-0086 Phase 1).

Deterministic, no network. Verifies the signed mark-to-market formula matches the
incident regression lock: unrealized_i = weight_i * NAV_ref * (mark_i / entry_i - 1).
"""

from __future__ import annotations

import socket

import pytest

from hermes_quant.state.portfolio_state import PortfolioState


def _exec_rec(**kw):
    """Helper to build an execution record dict for apply_execution."""
    base = dict(
        proposal_id="test_prop",
        asof_execution="2026-06-02T12:00:00Z",
        account_id="paper-default",
        asset_class="equity",
        fill_price=100.0,
        fill_size_pct=0.0,
    )
    base.update(kw)
    return base


def test_marked_equity_signed_mtm(tmp_path) -> None:
    """REGRESSION LOCK: build the incident book subset (SMCI short at entry 41.70,
    mark 49.70) and verify unrealized = -1.0 * 100000 * (49.70/41.70 - 1) ≈ -19184.
    Magnitude and sign must match the verified incident number.
    """
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # SMCI short: weight -1.0 (100% short), entry 41.70
    ps.apply_execution(
        _exec_rec(
            asset="SMCI",
            fill_size_pct=-1.0,
            fill_price=41.70,
            proposal_id="smci_short",
        )
    )

    # Inject the incident mark: 49.70
    result = ps.get_marked_equity("paper-default", {"SMCI": 49.70})

    # Expected unrealized: -1.0 * 100000 * (49.70 / 41.70 - 1)
    expected_unrealized = -1.0 * 100_000 * (49.70 / 41.70 - 1.0)
    assert abs(result.total_unrealized - expected_unrealized) < 50, (
        f"Expected {expected_unrealized:.2f}, got {result.total_unrealized:.2f}"
    )
    # Sign check: adverse move on a short → negative unrealized
    assert result.total_unrealized < 0
    # Marked equity < cost basis when short loses
    assert result.marked_equity < result.cost_basis_equity
    assert result.equity_basis == "mark"
    assert result.n_positions == 1
    assert result.n_marked == 1


def test_marked_equity_short_reduces_equity(tmp_path) -> None:
    """Short position with adverse mark (mark > entry) reduces marked_equity."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # Short 50% at entry 100
    ps.apply_execution(
        _exec_rec(
            asset="XYZ",
            fill_size_pct=-0.5,
            fill_price=100.0,
            proposal_id="xyz_short",
        )
    )

    # Mark climbs to 120 → short loses
    result = ps.get_marked_equity("paper-default", {"XYZ": 120.0})

    # Unrealized ≈ -0.5 * nav_ref * (120/100 - 1) ≈ -0.5 * nav_ref * 0.2
    # With nav_ref ≈ cost_basis_equity ≈ 100k, expect roughly -10k
    assert result.total_unrealized < 0
    assert result.marked_equity < result.cost_basis_equity
    # Rough magnitude check (allow for cost_basis variations)
    assert abs(result.total_unrealized) > 9_500
    assert abs(result.total_unrealized) < 10_500


def test_marked_equity_falls_back_when_mark_absent(tmp_path) -> None:
    """When a position has no injected mark, equity_basis != 'mark'."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.apply_execution(
        _exec_rec(
            asset="ABC",
            fill_size_pct=0.2,
            fill_price=50.0,
            proposal_id="abc_long",
        )
    )
    ps.apply_execution(
        _exec_rec(
            asset="DEF",
            fill_size_pct=0.1,
            fill_price=75.0,
            proposal_id="def_long",
        )
    )

    # Only inject mark for ABC, not DEF
    result = ps.get_marked_equity("paper-default", {"ABC": 55.0})

    # DEF falls back to entry → zero unrealized contribution from DEF
    # ABC: +0.2 * 100000 * (55/50 - 1) = 20000 * 0.1 = 2000
    assert result.equity_basis == "mixed"  # one marked, one not
    assert result.n_positions == 2
    assert result.n_marked == 1
    assert abs(result.total_unrealized - 2000.0) < 1e-6


def test_get_marked_equity_no_network(tmp_path, monkeypatch) -> None:
    """get_marked_equity must not make any network call (monkeypatch socket to raise)."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.apply_execution(
        _exec_rec(
            asset="NET",
            fill_size_pct=0.1,
            fill_price=100.0,
            proposal_id="net_long",
        )
    )

    # Monkeypatch socket.socket to raise if any network call is attempted
    def _no_socket(*args, **kwargs):
        raise RuntimeError("Network call attempted in get_marked_equity hot path")

    monkeypatch.setattr(socket, "socket", _no_socket)

    # This must succeed without network
    result = ps.get_marked_equity("paper-default", {"NET": 110.0})
    assert result.marked_equity > result.cost_basis_equity  # long profits


def test_marked_equity_empty_account(tmp_path) -> None:
    """Empty account (no positions, no cash) returns sensible defaults."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    result = ps.get_marked_equity("paper-default", {})

    # No cash record yet → cost_basis defaults to _default_initial_cash (100k)
    assert result.cost_basis_equity == 100_000.0
    assert result.marked_equity == 100_000.0
    assert result.total_unrealized == 0.0
    assert result.equity_basis == "entry"
    assert result.n_positions == 0
    assert result.n_marked == 0


def test_marked_equity_long_profits(tmp_path) -> None:
    """Long position with favorable mark (mark > entry) increases marked_equity."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.apply_execution(
        _exec_rec(
            asset="LONG",
            fill_size_pct=0.3,
            fill_price=100.0,
            proposal_id="long_pos",
        )
    )

    # Mark climbs to 110 → long profits
    result = ps.get_marked_equity("paper-default", {"LONG": 110.0})

    # Unrealized = +0.3 * 100000 * (110/100 - 1) = 30000 * 0.1 = 3000
    assert result.total_unrealized > 0
    assert result.marked_equity > result.cost_basis_equity
    assert abs(result.total_unrealized - 3000.0) < 1e-6
    assert result.equity_basis == "mark"


def test_marked_equity_short_profits(tmp_path) -> None:
    """Short position with favorable mark (mark < entry) increases marked_equity."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.apply_execution(
        _exec_rec(
            asset="SHORT",
            fill_size_pct=-0.4,
            fill_price=100.0,
            proposal_id="short_pos",
        )
    )

    # Mark drops to 90 → short profits
    result = ps.get_marked_equity("paper-default", {"SHORT": 90.0})

    # Unrealized ≈ -0.4 * nav_ref * (90/100 - 1) ≈ -0.4 * nav_ref * (-0.1) ≈ +4k
    assert result.total_unrealized > 0
    assert result.marked_equity > result.cost_basis_equity
    # Rough magnitude check (allow for cost_basis variations)
    assert result.total_unrealized > 3_800
    assert result.total_unrealized < 4_200
    assert result.equity_basis == "mark"


def test_marked_equity_mixed_book(tmp_path) -> None:
    """Mixed long/short book with multiple positions and marks."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # Long 20% at 100
    ps.apply_execution(
        _exec_rec(
            asset="LONG1",
            fill_size_pct=0.2,
            fill_price=100.0,
            proposal_id="l1",
        )
    )
    # Short 30% at 50
    ps.apply_execution(
        _exec_rec(
            asset="SHORT1",
            fill_size_pct=-0.3,
            fill_price=50.0,
            proposal_id="s1",
        )
    )
    # Long 10% at 200
    ps.apply_execution(
        _exec_rec(
            asset="LONG2",
            fill_size_pct=0.1,
            fill_price=200.0,
            proposal_id="l2",
        )
    )

    marks = {
        "LONG1": 110.0,  # +0.2 * nav_ref * (110/100 - 1) ≈ +2k
        "SHORT1": 45.0,  # -0.3 * nav_ref * (45/50 - 1) ≈ +3k
        "LONG2": 180.0,  # +0.1 * nav_ref * (180/200 - 1) ≈ -1k
    }
    result = ps.get_marked_equity("paper-default", marks)

    # Total unrealized ≈ 2k + 3k - 1k ≈ 4k (allow for cost_basis variations)
    assert result.total_unrealized > 3_800
    assert result.total_unrealized < 4_200
    assert result.marked_equity > result.cost_basis_equity
    assert result.equity_basis == "mark"
    assert result.n_positions == 3
    assert result.n_marked == 3


def test_marked_equity_zero_avg_entry_price_skipped(tmp_path) -> None:
    """Position with avg_entry_price <= 0 is skipped (guard against division by zero)."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # Manually inject a position with zero avg_entry_price (should never happen,
    # but guard is in place)
    ps.apply_execution(
        _exec_rec(
            asset="ZERO",
            fill_size_pct=0.1,
            fill_price=0.0,  # invalid
            proposal_id="zero_price",
        )
    )

    # Even with a mark, this position is skipped
    result = ps.get_marked_equity("paper-default", {"ZERO": 100.0})

    # Position exists but contributes zero unrealized (skipped)
    assert result.total_unrealized == 0.0
    assert result.n_positions == 1
    assert result.n_marked == 0  # skipped before marking
    assert result.equity_basis == "entry"
