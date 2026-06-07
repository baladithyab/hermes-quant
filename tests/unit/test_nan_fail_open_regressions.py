"""Regression tests for the NaN-fail-open defect class (deep-review 2026-06-07).

Each test asserts that a non-finite (NaN/inf) input FAILS CLOSED — i.e. the
guard rejects/silences/flattens rather than laundering NaN into a benign value
that bypasses the risk controls. See docs/reviews/2026-06-07-quant-deep-review/.
"""
import math
from datetime import UTC, datetime

import pandas as pd
import pytest

from hermes_quant.protocol import Portfolio, Position


def _pos(**ov):
    base = dict(
        asset="ASTS",
        qty=100.0,
        avg_entry_price=100.0,
        mark_price=100.0,
        unrealized_pnl=0.0,
        realized_fees=0.0,
    )
    base.update(ov)
    return Position(**base)


def _snap(**ov):
    base = dict(
        account_id="t",
        asset_class="equity",
        asof=pd.Timestamp("2026-06-07T18:00:00Z"),
        positions={},
        cash=100000.0,
        equity_total=100000.0,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=100000.0,
        daily_open_equity=100000.0,
    )
    base.update(ov)
    return Portfolio(**base)


# --- protocol.py PortfolioSnapshot properties ---
def test_drawdown_nan_equity_fails_closed():
    assert _snap(equity_total=float("nan")).drawdown_pct >= 0.15


def test_daily_loss_nan_equity_fails_closed():
    assert _snap(equity_total=float("nan")).daily_loss_pct >= 0.05


def test_drawdown_finite_zero_peak_is_benign():
    assert _snap(peak_equity=0.0).drawdown_pct == 0.0


def test_drawdown_normal_path_unchanged():
    # 100k peak, 90k equity -> 10% drawdown, untouched by the guard.
    assert abs(_snap(equity_total=90000.0).drawdown_pct - 0.10) < 1e-9


def test_current_position_pct_nan_mark_returns_nonfinite():
    snap = _snap(positions={"ASTS": _pos(mark_price=float("nan"))})
    assert not math.isfinite(snap.current_position_pct("ASTS"))


def test_current_position_pct_no_position_is_zero():
    assert _snap().current_position_pct("NOPE") == 0.0


def test_current_position_pct_normal_path():
    snap = _snap(positions={"ASTS": _pos(qty=100.0, mark_price=200.0)})
    # 100 * 200 / 100000 = 0.2
    assert abs(snap.current_position_pct("ASTS") - 0.2) < 1e-9


# --- react/slippage_model.py ---
def test_slippage_rejects_nan_decision_price():
    from hermes_quant.react.slippage_model import apply_slippage

    with pytest.raises(ValueError):
        apply_slippage(
            decision_price=float("nan"), target_pct=0.2,
            asof_execution="2026-06-07T18:00:00Z", proposal_id="t",
            asset_class="equity", is_late_session=False,
        )


def test_slippage_rejects_inf_decision_price():
    from hermes_quant.react.slippage_model import apply_slippage

    with pytest.raises(ValueError):
        apply_slippage(
            decision_price=float("inf"), target_pct=0.2,
            asof_execution="2026-06-07T18:00:00Z", proposal_id="t",
            asset_class="equity", is_late_session=False,
        )


# --- admissibility/oracle.py (live path) ---
def test_oracle_nan_account_equity_rejects():
    from hermes_quant.admissibility.oracle import (
        AdmissibilityContext, AdmissibilityState, evaluate_admissibility,
    )

    ctx = AdmissibilityContext(
        tradable=True, marginable=True, shortable=True, easy_to_borrow=True,
        current_ask=100.0, available_bp=1_000_000.0,
        account_equity=float("nan"),
    )
    v = evaluate_admissibility(
        "ASTS", "short", 10.0, datetime(2026, 6, 7, tzinfo=UTC), ctx,
        require_account_context=True,
    )
    assert v.state == AdmissibilityState.REJECTED


def test_oracle_nan_current_ask_rejects():
    from hermes_quant.admissibility.oracle import (
        AdmissibilityContext, AdmissibilityState, evaluate_admissibility,
    )

    ctx = AdmissibilityContext(
        tradable=True, marginable=True, shortable=True, easy_to_borrow=True,
        current_ask=float("nan"), available_bp=1_000_000.0,
        account_equity=100_000.0,
    )
    v = evaluate_admissibility(
        "ASTS", "short", 10.0, datetime(2026, 6, 7, tzinfo=UTC), ctx,
        require_account_context=True,
    )
    assert v.state == AdmissibilityState.REJECTED
