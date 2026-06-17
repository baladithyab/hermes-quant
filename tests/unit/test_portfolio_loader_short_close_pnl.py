"""Regression: portfolio_loader.reconstruct_portfolio realized-P&L sign on
short full-closes.

Position-lifecycle defect: the v0.1.1 full-close branch in
``daemon/portfolio_loader.py`` computes realized P&L as

    realized = (fill - avg_old) * (-signed_qty) * (1 if old_qty > 0 else -1)

For a LONG full close this is correct. For a SHORT full close (open with a
``sell`` while flat, then ``buy`` back to flat) the formula INVERTS the sign:
a short covered at a profit is booked as a loss and a short covered at a loss
is booked as a profit. The bus-cash accounting is correct (it tracks the true
notional), so ``realized_pnl_total`` silently diverges from the realized cash
delta — and ``reconstruct_portfolio`` feeds the per-tick risk gate.

The dangerous polarity is the "short loss booked as profit" case: a real
drawdown reads as a gain in the realized-P&L view.

These cases are reachable: opening with a ``sell`` while flat is the
``is_same_direction`` (old_qty == 0) branch (NOT gated), and buying it back to
exactly flat is the ``is_full_close`` branch (NOT gated — only PARTIAL closes
and direction FLIPS raise NotImplementedError).
"""

from __future__ import annotations

import pytest

from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
from hermes_quant.daemon.signal_bus import emit_execution_record


def _exec(side, qty, fill, *, fees=0.0, account_id="alpaca-paper", asset_class="crypto"):
    return {
        "schema_version": 1,
        "exec_id": f"exec-{side}-{qty}-{fill}",
        "asof": "2026-05-13T10:00:00Z",
        "asset": "BTC/USDT",
        "side": side,
        "qty": qty,
        "fill_price": fill,
        "decision_price": fill,
        "fees": fees,
        "account_id": account_id,
        "asset_class": asset_class,
    }


def test_short_cover_at_profit_realizes_positive_pnl(tmp_path):
    """Sell 1 @ 100, buy back 1 @ 80 → +20 realized profit (NOT -20)."""
    bus = tmp_path / "execs.jsonl"
    emit_execution_record(_exec("sell", 1.0, 100.0), path=bus)
    emit_execution_record(_exec("buy", 1.0, 80.0), path=bus)

    p = reconstruct_portfolio("alpaca-paper", "crypto", initial_cash=100_000.0, bus_path=bus)

    assert "BTC/USDT" not in p.positions
    # Short sold @100, covered @80 → made 20 per unit. Cash confirms it:
    # sell credits +100, buy debits -80 → net +20.
    assert p.cash == pytest.approx(100_020.0)
    assert p.realized_pnl_total == pytest.approx(20.0)


def test_short_cover_at_loss_realizes_negative_pnl(tmp_path):
    """Sell 1 @ 100, buy back 1 @ 130 → -30 realized loss (NOT +30).

    This is the dangerous polarity: a real loss must not read as a gain in the
    realized-P&L view that feeds the risk gate.
    """
    bus = tmp_path / "execs.jsonl"
    emit_execution_record(_exec("sell", 1.0, 100.0), path=bus)
    emit_execution_record(_exec("buy", 1.0, 130.0), path=bus)

    p = reconstruct_portfolio("alpaca-paper", "crypto", initial_cash=100_000.0, bus_path=bus)

    assert "BTC/USDT" not in p.positions
    # Short sold @100, covered @130 → lost 30 per unit. Cash confirms:
    # sell credits +100, buy debits -130 → net -30.
    assert p.cash == pytest.approx(99_970.0)
    assert p.realized_pnl_total == pytest.approx(-30.0)


def test_long_full_close_realized_pnl_unchanged(tmp_path):
    """Non-vacuity guard: the LONG full-close happy path stays exact.

    Buy 1 @ 60k, sell 1 @ 62k → +2000 (matches the existing v0.1.1 contract).
    """
    bus = tmp_path / "execs.jsonl"
    emit_execution_record(_exec("buy", 1.0, 60_000.0), path=bus)
    emit_execution_record(_exec("sell", 1.0, 62_000.0), path=bus)

    p = reconstruct_portfolio("alpaca-paper", "crypto", initial_cash=100_000.0, bus_path=bus)

    assert "BTC/USDT" not in p.positions
    assert p.realized_pnl_total == pytest.approx(2_000.0)


def test_realized_pnl_matches_cash_delta_long_loss(tmp_path):
    """Non-vacuity guard: a LONG closed at a loss is still negative.

    Buy 1 @ 60k, sell 1 @ 58k → -2000.
    """
    bus = tmp_path / "execs.jsonl"
    emit_execution_record(_exec("buy", 1.0, 60_000.0), path=bus)
    emit_execution_record(_exec("sell", 1.0, 58_000.0), path=bus)

    p = reconstruct_portfolio("alpaca-paper", "crypto", initial_cash=100_000.0, bus_path=bus)

    assert "BTC/USDT" not in p.positions
    assert p.realized_pnl_total == pytest.approx(-2_000.0)
