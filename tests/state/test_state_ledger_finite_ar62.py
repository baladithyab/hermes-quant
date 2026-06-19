"""ar62 — the state-ledger folds must REJECT a non-finite fill record (sticky inf/nan poison).

Found by the parallel find->fix workflow (wf_1a36618e). _read_all_jsonl decodes with json.loads at
DEFAULT settings (parses Infinity/-Infinity/NaN + the 1e400 overflow literal into inf/nan WITHOUT
raising), and producer float() coercions pass inf through. A non-finite fill_price flows into
`delta_cash = -cash_basis * fill_price * mult` -> inf, `equity = new_cash + sum(...)` -> inf, which
(being inf, not nan) slips past SQLite's NOT-NULL and is PERSISTED — get_cash() then returns inf
forever and react/paper.py's `equity_total > 0` (inf>0 True) hands inf back as the account NAV.
Fix: _validate_fill_numerics rejects the record in BOTH folds (incremental + rebuild); get_cash
finite-guards a legacy poisoned row -> None (bootstrap).
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.state.portfolio_state import PortfolioState


@pytest.fixture
def ps(tmp_path) -> PortfolioState:
    return PortfolioState(state_db_path=tmp_path / "state.db")


def _make_record(*, proposal_id: str, fill_size_pct: float, fill_price: float,
                 asset: str = "AAPL", asset_class: str = "equity"):
    return {
        "proposal_id": proposal_id,
        "asset": asset,
        "asset_class": asset_class,
        "fill_size_pct": fill_size_pct,
        "fill_price": fill_price,
        "asof_execution": "2026-06-05T15:00:00+00:00",
        "reactor_metadata": {"paper": True},
    }


@pytest.mark.parametrize("bad_price", [float("inf"), float("-inf"), float("nan")])
def test_ar62_non_finite_fill_price_rejected_not_persisted(ps, bad_price):
    """A non-finite fill_price must NOT poison balance_usd/equity_total. The fold rejects
    the record (apply_execution swallows + audits) so get_cash returns either None or a
    finite row — never a sticky inf/nan."""
    ps.apply_execution(_make_record(proposal_id="bad", fill_size_pct=-0.05, fill_price=bad_price))
    cash = ps.get_cash("paper-default")
    if cash is not None:
        assert math.isfinite(cash.balance_usd), cash.balance_usd
        assert math.isfinite(cash.equity_total), cash.equity_total
    for pos in ps.get_positions("paper-default").values():
        assert math.isfinite(pos.quantity)
        assert math.isfinite(pos.avg_entry_price)


def test_ar62_non_finite_does_not_poison_subsequent_good_fill(ps):
    """Sticky-poison guard: a rejected inf fill must not leave the account poisoned for a
    later good fill."""
    ps.apply_execution(_make_record(proposal_id="bad", fill_size_pct=-0.05, fill_price=float("inf")))
    ps.apply_execution(_make_record(proposal_id="good", fill_size_pct=0.05, fill_price=100.0))
    cash = ps.get_cash("paper-default")
    assert cash is not None
    assert math.isfinite(cash.balance_usd) and math.isfinite(cash.equity_total)


def test_ar62_finite_fill_byte_identical(ps):
    """A finite fill is recorded normally (the guard is a no-op on good input)."""
    ps.apply_execution(_make_record(proposal_id="ok", fill_size_pct=0.05, fill_price=100.0))
    cash = ps.get_cash("paper-default")
    assert cash is not None and math.isfinite(cash.balance_usd)
    assert ps.get_positions("paper-default")  # a position was booked
