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


def test_cash_unit_true_shares_vs_legacy_navfraction(tmp_path) -> None:
    """REGRESSION LOCK (P1-A / "0da3" unit bug): when a record carries a true
    share/contract count in reactor_metadata.quantity (the Alpaca-paper reactor
    path), the CASH delta must use real notional = signed_shares * price, NOT
    fill_size_pct (NAV fraction) * price. The legacy path (no quantity) must stay
    bit-identical to the NAV-fraction proxy.

    Buying 100 shares @ $50 = $5,000 real cash out. The pre-fix bug computed
    0.05 * 50 = $2.50, understating cash by 3 orders of magnitude and corrupting
    partition NAV.
    """
    from hermes_quant.state.portfolio_state import _default_initial_cash

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    init = _default_initial_cash()

    # Alpaca-style: explicit signed shares in reactor_metadata.quantity.
    ps.apply_execution(
        _exec_rec(
            account_id="alpaca-paper",
            asset="X",
            symbol="X",
            fill_size_pct=0.05,  # NAV-fraction proxy (must NOT drive cash here)
            fill_price=50.0,
            proposal_id="alpaca_x",
            reactor_metadata={"quantity": 100.0},  # true shares
        )
    )
    alpaca_cash = ps.get_cash("alpaca-paper")
    # Cash dropped by the REAL notional 100 * 50 = 5000, not 0.05 * 50 = 2.5.
    assert abs((init - alpaca_cash.balance_usd) - 5000.0) < 1e-6, (
        f"alpaca-paper cash drop {init - alpaca_cash.balance_usd} != 5000 "
        "(P1-A unit fix regressed)"
    )

    # Legacy path: no quantity -> NAV-fraction proxy, bit-identical to before.
    ps.apply_execution(
        _exec_rec(
            account_id="paper-default",
            asset="Y",
            symbol="Y",
            fill_size_pct=0.05,
            fill_price=50.0,
            proposal_id="legacy_y",
            reactor_metadata={},
        )
    )
    legacy_cash = ps.get_cash("paper-default")
    assert abs((init - legacy_cash.balance_usd) - 2.5) < 1e-6, (
        f"paper-default cash drop {init - legacy_cash.balance_usd} != 2.5 "
        "(legacy NAV-fraction path must be unchanged)"
    )


def test_option_cash_uses_contract_multiplier_x100(tmp_path):
    """ADR-0088 F1: a us_option fill_price is a PER-CONTRACT premium; cash impact
    must be premium x contracts x 100. A short put @ $2.00 credits $200 (not $2.00);
    a long call @ $1.50 x2 debits $300. Equity fills stay x1 (bit-identical)."""
    from hermes_quant.state.portfolio_state import PortfolioState

    def _rec(asset, asset_class, qty, price):
        return {
            "account_id": "t",
            "asset": asset,
            "asset_class": asset_class,
            "fill_price": price,
            "fill_size_pct": 0.0,
            "asof_execution": "2026-06-05T12:00:00Z",
            "reactor_metadata": {"quantity": qty},
        }

    ps = PortfolioState(state_db_path=tmp_path / "s.db")
    # SHORT 1 put @ $2.00 -> CREDIT $200 (premium 2.00 x 1 contract x 100).
    ps.apply_execution(_rec("AAPL260101P00150000", "us_option", -1.0, 2.00))
    c = ps.get_cash("t")
    assert c.balance_usd == 100_200.0, f"short put credit wrong: {c.balance_usd}"
    # equity_total values the short option position at qty*avg*100.
    assert c.equity_total == 100_400.0, f"option equity_total wrong: {c.equity_total}"
    # LONG 2 calls @ $1.50 -> DEBIT $300 (1.50 x 2 x 100).
    ps.apply_execution(_rec("AAPL260101C00160000", "us_option", 2.0, 1.50))
    assert ps.get_cash("t").balance_usd == 99_900.0
    # EQUITY 100 sh @ $50 -> DEBIT $5000 (multiplier 1, NOT 100).
    ps.apply_execution(_rec("MSFT", "equity", 100.0, 50.0))
    assert ps.get_cash("t").balance_usd == 94_900.0, "equity multiplier must be 1"


# ---------------------------------------------------------------------------
# cs15: equity_total signed net-liq fix (ADR-0086 Phase 1 missed half).
#
# The cached equity_total folds positions as cash + Σ abs(qty)*avg*mult, which
# treats a SHORT (negative qty) as a positive asset. A short fill ALREADY booked
# its proceeds into cash, so abs() ADDS the same notional a second time with the
# wrong sign -> ~2*|notional| inflation. The fix replaces abs(qty) with signed
# qty (net-liq) behind HERMES_QUANT_SIGNED_EQUITY (default-OFF). Flag OFF is
# bit-for-bit current on every book (the helper returns abs()); flag ON returns
# NAV-neutral at entry for a short. equity_total is the gate-SIZED NAV consumed by
# _account_nav_usd (react/paper.py, autonomous.py), so the live flip is deferred.
# ---------------------------------------------------------------------------

# Legacy NAV-fraction short: fill_size_pct=-0.2, no reactor_metadata.quantity.
# cash_basis = -0.2 -> delta_cash = +0.2*100 = +20 -> cash 100_020.
# position qty = -0.2, avg = 100, |notional| = 20.
_SHORT_FILL_SIZE_PCT = -0.2
_SHORT_FILL_PRICE = 100.0
_INIT_CASH = 100_000.0
_SHORT_CASH = 100_020.0  # cash after short open: proceeds leg booked (+20)
_SHORT_EQUITY_ABS = 100_040.0  # OFF: cash + abs(-0.2)*100 = +20 ON TOP (double-count)
_SHORT_EQUITY_SIGNED = 100_000.0  # ON: cash + (-0.2)*100 = NAV-neutral at entry


def _apply_legacy_short(tmp_path, db_name="s.db"):
    """Apply the seed short (legacy NAV-fraction path) into a fresh db; return ps."""
    ps = PortfolioState(state_db_path=tmp_path / db_name)
    ps.apply_execution(
        _exec_rec(
            account_id="paper-default",
            asset="S",
            symbol="S",
            fill_size_pct=_SHORT_FILL_SIZE_PCT,
            fill_price=_SHORT_FILL_PRICE,
            proposal_id="short_s",
        )
    )
    return ps


def test_equity_total_short_inflation_flag_off_documents_bug(tmp_path, monkeypatch):
    """RED-PROOF (GREEN on current code = proof of the bug): with the flag UNSET,
    a short 0.2 @100 from 100k inflates equity_total to 100_040 (cash 100_020 +
    abs(-0.2)*100 = +20 ON TOP of the +20 proceeds already in cash) -> ~2*|notional|
    overstatement. This documents the live behavior the fix corrects."""
    monkeypatch.delenv("HERMES_QUANT_SIGNED_EQUITY", raising=False)
    cash = _apply_legacy_short(tmp_path).get_cash("paper-default")
    assert cash.balance_usd == _SHORT_CASH, f"short proceeds leg wrong: {cash.balance_usd}"
    # The bug: equity_total = 100_040, NOT the true net-liq 100_000. +40 = 2*|notional|.
    assert cash.equity_total == _SHORT_EQUITY_ABS, (
        f"flag-OFF equity_total {cash.equity_total} != {_SHORT_EQUITY_ABS} "
        f"(short double-count inflation; |notional|=20, inflation=40=2*|notional|)"
    )


def test_equity_total_short_signed_navneutral_incremental(tmp_path, monkeypatch):
    """GREEN UNDER FIX (incremental fold): with HERMES_QUANT_SIGNED_EQUITY=1, the
    same short is NAV-neutral at entry (equity_total == 100_000) because opening a
    position at its fill mark is NAV-neutral; cash is unchanged (100_020)."""
    monkeypatch.setenv("HERMES_QUANT_SIGNED_EQUITY", "1")
    cash = _apply_legacy_short(tmp_path).get_cash("paper-default")
    assert cash.balance_usd == _SHORT_CASH, f"cash must be unchanged by the fix: {cash.balance_usd}"
    assert cash.equity_total == _SHORT_EQUITY_SIGNED, (
        f"signed equity_total {cash.equity_total} != {_SHORT_EQUITY_SIGNED} "
        f"(short should be NAV-neutral at entry, not inflated)"
    )


def test_equity_total_signed_rebuild_incremental_parity(tmp_path, monkeypatch):
    """FOLD PARITY (flag ON): the same short run through reconstruct_from (rebuild)
    and through apply_execution (incremental) into SEPARATE dbs must yield the
    identical signed equity_total (100_000). Both folds call _equity_qty_factor on
    the same per-position signed quantity, so they agree by construction."""
    import json

    monkeypatch.setenv("HERMES_QUANT_SIGNED_EQUITY", "1")

    # Incremental fold.
    incr_cash = _apply_legacy_short(tmp_path, db_name="incr.db").get_cash("paper-default")

    # Rebuild fold: write the same record to a JSONL and reconstruct.
    exec_path = tmp_path / "executions.jsonl"
    exec_path.write_text(
        json.dumps(
            _exec_rec(
                account_id="paper-default",
                asset="S",
                symbol="S",
                fill_size_pct=_SHORT_FILL_SIZE_PCT,
                fill_price=_SHORT_FILL_PRICE,
                proposal_id="short_s",
            )
        )
        + "\n"
    )
    ps_rebuild = PortfolioState(state_db_path=tmp_path / "rebuild.db")
    ps_rebuild.reconstruct_from(exec_path)
    rebuild_cash = ps_rebuild.get_cash("paper-default")

    assert incr_cash.equity_total == _SHORT_EQUITY_SIGNED
    assert rebuild_cash.equity_total == _SHORT_EQUITY_SIGNED
    assert rebuild_cash.equity_total == incr_cash.equity_total, (
        f"fold parity broken: rebuild {rebuild_cash.equity_total} != "
        f"incremental {incr_cash.equity_total}"
    )
    # Cash also agrees across folds (proceeds leg identical).
    assert rebuild_cash.balance_usd == incr_cash.balance_usd == _SHORT_CASH


def test_equity_total_long_byte_identical_across_flag(tmp_path, monkeypatch):
    """LONG BYTE-IDENTICAL: for qty>0, _equity_qty_factor returns qty whether the
    flag is OFF (abs(qty)==qty) or ON (qty). A long 0.2 @100 yields equity_total ==
    100_020 in BOTH regimes, and the two values are equal."""
    monkeypatch.delenv("HERMES_QUANT_SIGNED_EQUITY", raising=False)
    ps_off = PortfolioState(state_db_path=tmp_path / "long_off.db")
    ps_off.apply_execution(
        _exec_rec(account_id="paper-default", asset="L", symbol="L", fill_size_pct=0.2, fill_price=100.0)
    )
    eq_off = ps_off.get_cash("paper-default").equity_total

    monkeypatch.setenv("HERMES_QUANT_SIGNED_EQUITY", "1")
    ps_on = PortfolioState(state_db_path=tmp_path / "long_on.db")
    ps_on.apply_execution(
        _exec_rec(account_id="paper-default", asset="L", symbol="L", fill_size_pct=0.2, fill_price=100.0)
    )
    eq_on = ps_on.get_cash("paper-default").equity_total

    assert eq_off == eq_on, f"long equity_total changed across flag: OFF {eq_off} != ON {eq_on}"
    # A long debits cash by the notional then adds it back as an asset -> NAV-neutral
    # at entry in BOTH regimes (cash 99_980 + 0.2*100 = 100_000).
    assert eq_off == 100_000.0, f"long equity_total {eq_off} unexpected (NAV-neutral at entry)"


def test_equity_total_us_option_short_signed_navneutral(tmp_path, monkeypatch):
    """us_option SHORT (mult x100) under flag ON: short 2 contracts @ $1.50 credits
    cash +300 (2*1.50*100) and equity_total is NAV-neutral (100_000) with the x100
    multiplier preserved -> proves the sign fix multiplies INTO the multiplier,
    not over it. Mirrors the abs-value option anchor below."""
    monkeypatch.setenv("HERMES_QUANT_SIGNED_EQUITY", "1")
    ps = PortfolioState(state_db_path=tmp_path / "opt.db")
    ps.apply_execution(
        {
            "account_id": "t",
            "asset": "AAPL260101P00150000",
            "asset_class": "us_option",
            "fill_price": 1.50,
            "fill_size_pct": 0.0,
            "asof_execution": "2026-06-05T12:00:00Z",
            "reactor_metadata": {"quantity": -2.0},  # short 2 contracts
        }
    )
    c = ps.get_cash("t")
    # proceeds = 2 * 1.50 * 100 = 300 credit.
    assert c.balance_usd == 100_300.0, f"option short credit wrong: {c.balance_usd}"
    # signed equity: 100_300 + (-2)*1.50*100 = 100_300 - 300 = 100_000 (mult preserved).
    assert c.equity_total == 100_000.0, (
        f"us_option short signed equity_total {c.equity_total} != 100_000 "
        f"(x100 multiplier must be preserved in the signed term)"
    )
