"""Tests for hermes_quant.risk.ptrar — Per-Trip Risk-Adjusted Return.

ADR-0099 Part B: PTRAR places equity and defined-risk options on the same
footing by normalising realized P&L against committed capital-at-risk.

Keystone parity property (RED-proof):
  An equity trade returning +2 % of notional and an options trade returning
  +2 % of max_loss must produce IDENTICAL PTRAR = 0.02.

  RED verification: reverting the max_loss normalization in ptrar_options
  (replacing `max_loss_usd` with a constant denominator) makes the options
  PTRAR diverge from the equity PTRAR — this test then FAILS (RED).

Coverage:
  - ptrar_equity: normal path, non-finite inputs, zero capital, None on zero
  - ptrar_options: normal path, None max_loss, inf max_loss, non-finite pnl
  - ptrar_sharpe: normal path, <2 points, non-finite annual_freq, all-None
  - ptrar_for_trip: equity dispatch, options dispatch, multi_leg parent skip,
                    unknown asset_class, non-finite realized_return
  - Parity property (keystone): equity PTRAR == options PTRAR for equal
    risk-normalised returns
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_quant.risk.ptrar import (
    ptrar_equity,
    ptrar_for_trip,
    ptrar_options,
    ptrar_sharpe,
)


# ---------------------------------------------------------------------------
# Minimal SettledRoundTrip stub (avoids importing pandas + settlement_loop)
# ---------------------------------------------------------------------------


@dataclass
class _FakeTrip:
    """Minimal stand-in for daemon.settlement_loop.SettledRoundTrip."""

    asset_class: str
    entry_price: float
    qty: float
    realized_return: float
    notional_multiplier: float = 1.0
    multi_leg_id: str | None = None
    true_units: float | None = None


# ---------------------------------------------------------------------------
# ptrar_equity
# ---------------------------------------------------------------------------


class TestPtrarEquity:
    """Unit tests for the equity PTRAR primitive."""

    def test_positive_return(self):
        # realized_pnl = +$200 on a $10_000 notional ($100 × 100 shares)
        # PTRAR = 200 / 10_000 = 0.02
        result = ptrar_equity(
            realized_pnl_usd=200.0,
            entry_price=100.0,
            abs_position_usd=100.0,
        )
        assert result == pytest.approx(0.02, rel=1e-9)

    def test_negative_return(self):
        # realized_pnl = -$150 on $10_000 → PTRAR = -0.015
        result = ptrar_equity(
            realized_pnl_usd=-150.0,
            entry_price=100.0,
            abs_position_usd=100.0,
        )
        assert result == pytest.approx(-0.015, rel=1e-9)

    def test_zero_entry_price_returns_none(self):
        assert ptrar_equity(100.0, 0.0, 100.0) is None

    def test_negative_entry_price_returns_none(self):
        assert ptrar_equity(100.0, -50.0, 100.0) is None

    def test_zero_abs_position_returns_none(self):
        assert ptrar_equity(100.0, 100.0, 0.0) is None

    def test_negative_abs_position_returns_none(self):
        assert ptrar_equity(100.0, 100.0, -10.0) is None

    def test_nan_pnl_returns_none(self):
        assert ptrar_equity(float("nan"), 100.0, 100.0) is None

    def test_inf_pnl_returns_none(self):
        assert ptrar_equity(float("inf"), 100.0, 100.0) is None

    def test_nan_entry_price_returns_none(self):
        assert ptrar_equity(200.0, float("nan"), 100.0) is None

    def test_inf_entry_price_returns_none(self):
        assert ptrar_equity(200.0, float("inf"), 100.0) is None

    def test_nan_position_returns_none(self):
        assert ptrar_equity(200.0, 100.0, float("nan")) is None

    def test_inf_position_returns_none(self):
        assert ptrar_equity(200.0, 100.0, float("inf")) is None

    def test_zero_pnl_is_valid(self):
        # A breakeven trade: PTRAR = 0 (valid, not excluded)
        result = ptrar_equity(0.0, 100.0, 100.0)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ptrar_options
# ---------------------------------------------------------------------------


class TestPtrarOptions:
    """Unit tests for the options PTRAR primitive."""

    def test_positive_return(self):
        # P&L = +$40 on max_loss = $2_000 → PTRAR = 0.02
        result = ptrar_options(realized_pnl_usd=40.0, max_loss_usd=2000.0)
        assert result == pytest.approx(0.02, rel=1e-9)

    def test_negative_return(self):
        # P&L = -$100 on max_loss = $2_000 → PTRAR = -0.05
        result = ptrar_options(realized_pnl_usd=-100.0, max_loss_usd=2000.0)
        assert result == pytest.approx(-0.05, rel=1e-9)

    def test_none_max_loss_returns_none(self):
        # Undefined-risk structure → fail-CLOSED
        assert ptrar_options(100.0, None) is None

    def test_zero_max_loss_returns_none(self):
        assert ptrar_options(100.0, 0.0) is None

    def test_negative_max_loss_returns_none(self):
        assert ptrar_options(100.0, -500.0) is None

    def test_inf_max_loss_returns_none(self):
        assert ptrar_options(100.0, float("inf")) is None

    def test_nan_max_loss_returns_none(self):
        assert ptrar_options(100.0, float("nan")) is None

    def test_nan_pnl_returns_none(self):
        assert ptrar_options(float("nan"), 2000.0) is None

    def test_inf_pnl_returns_none(self):
        assert ptrar_options(float("inf"), 2000.0) is None

    def test_zero_pnl_is_valid(self):
        result = ptrar_options(0.0, 2000.0)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ptrar_sharpe
# ---------------------------------------------------------------------------


class TestPtrarSharpe:
    """Unit tests for the cross-trip PTRAR Sharpe."""

    def test_normal_series(self):
        # ptrar_series with some spread → valid Sharpe
        series = [0.02, 0.05, -0.01, 0.03, 0.04]
        result = ptrar_sharpe(series, annual_freq=252.0)
        assert result is not None
        assert math.isfinite(result)

    def test_less_than_two_points_returns_none(self):
        assert ptrar_sharpe([], annual_freq=252.0) is None
        assert ptrar_sharpe([0.02], annual_freq=252.0) is None
        assert ptrar_sharpe([None], annual_freq=252.0) is None

    def test_all_none_returns_none(self):
        assert ptrar_sharpe([None, None, None], annual_freq=252.0) is None

    def test_one_finite_rest_none_returns_none(self):
        assert ptrar_sharpe([0.02, None, None], annual_freq=252.0) is None

    def test_two_finite_with_nones_is_valid(self):
        # After filtering, 2 finite points → should compute
        result = ptrar_sharpe([None, 0.02, None, 0.04], annual_freq=252.0)
        assert result is not None
        assert math.isfinite(result)

    def test_non_finite_annual_freq_returns_none(self):
        series = [0.02, 0.05, -0.01]
        assert ptrar_sharpe(series, annual_freq=float("nan")) is None
        assert ptrar_sharpe(series, annual_freq=float("inf")) is None
        assert ptrar_sharpe(series, annual_freq=0.0) is None
        assert ptrar_sharpe(series, annual_freq=-1.0) is None

    def test_known_value(self):
        # Hand-computed: series = [0.02, 0.04]
        # mean = 0.03, std = sqrt((0.0001 + 0.0001) / 1) = sqrt(0.0002) ≈ 0.014142
        # sharpe = 0.03 / 0.014142 * sqrt(1) ≈ 2.1213
        series = [0.02, 0.04]
        result = ptrar_sharpe(series, annual_freq=1.0)
        assert result == pytest.approx(0.03 / math.sqrt(0.0002), rel=1e-6)

    def test_excludes_non_finite_values(self):
        # Mix of valid and non-finite: only finite ones count
        series_full = [0.02, 0.04]
        series_with_nan = [0.02, float("nan"), 0.04, float("inf")]
        r_full = ptrar_sharpe(series_full, annual_freq=252.0)
        r_mixed = ptrar_sharpe(series_with_nan, annual_freq=252.0)
        assert r_full == pytest.approx(r_mixed, rel=1e-9)

    def test_all_identical_values_zero_mean_returns_zero(self):
        # std = 0, mean = 0 → 0.0 (not inf, not None)
        result = ptrar_sharpe([0.0, 0.0, 0.0], annual_freq=252.0)
        assert result == 0.0

    def test_all_identical_nonzero_returns_signed_inf(self):
        # std = 0, mean > 0 → +inf (constant positive edge)
        result = ptrar_sharpe([0.03, 0.03, 0.03], annual_freq=252.0)
        assert result == math.inf


# ---------------------------------------------------------------------------
# ptrar_for_trip
# ---------------------------------------------------------------------------


class TestPtrarForTrip:
    """Unit tests for the unified dispatch entry."""

    def test_equity_dispatch(self):
        # Entry $100, 50 shares, +2% realized_return
        # pnl_usd = 0.02 * 100 * 50 * 1.0 = $100
        # capital = 100 * 50 * 1.0 = $5_000
        # PTRAR = 100 / 5_000 = 0.02
        trip = _FakeTrip(
            asset_class="equity",
            entry_price=100.0,
            qty=50.0,
            realized_return=0.02,
            notional_multiplier=1.0,
        )
        result = ptrar_for_trip(trip)
        assert result == pytest.approx(0.02, rel=1e-9)

    def test_us_equity_dispatch(self):
        trip = _FakeTrip(
            asset_class="us_equity",
            entry_price=100.0,
            qty=50.0,
            realized_return=0.02,
            notional_multiplier=1.0,
        )
        result = ptrar_for_trip(trip)
        assert result == pytest.approx(0.02, rel=1e-9)

    def test_crypto_dispatch(self):
        trip = _FakeTrip(
            asset_class="crypto",
            entry_price=50000.0,
            qty=0.1,
            realized_return=0.02,
            notional_multiplier=1.0,
        )
        result = ptrar_for_trip(trip)
        assert result == pytest.approx(0.02, rel=1e-9)

    def test_us_option_dispatch(self):
        # wave1-review FIX (was VACUOUS): max_loss_usd MUST differ from
        # capital_at_risk (= entry_price*qty*multiplier) or a broken dispatch that
        # uses capital instead of max_loss passes anyway.
        # 1 contract at $5 premium, multiplier=100 -> capital_at_risk = $500.
        # max_loss_usd = $250 (a $2.50-wide debit spread) — DISTINCT from $500.
        # pnl_usd = 0.02 * 5 * 1 * 100 = $10
        # PTRAR (correct, uses max_loss) = 10 / 250 = 0.04
        # PTRAR (broken, uses capital)   = 10 / 500 = 0.02  -> test would catch it
        trip = _FakeTrip(
            asset_class="us_option",
            entry_price=5.0,
            qty=1.0,
            realized_return=0.02,
            notional_multiplier=100.0,
        )
        result = ptrar_for_trip(trip, max_loss_usd=250.0)
        assert result == pytest.approx(0.04, rel=1e-9), (
            "ptrar_for_trip must normalize an option by max_loss_usd ($250), NOT "
            f"capital_at_risk ($500); got {result!r} (0.02 means the broken "
            "capital-at-risk dispatch is back)"
        )

    def test_multi_leg_parent_returns_none(self):
        trip = _FakeTrip(
            asset_class="multi_leg",
            entry_price=100.0,
            qty=1.0,
            realized_return=0.02,
        )
        assert ptrar_for_trip(trip) is None

    def test_unknown_asset_class_returns_none(self):
        trip = _FakeTrip(
            asset_class="unknown_class",
            entry_price=100.0,
            qty=50.0,
            realized_return=0.02,
        )
        assert ptrar_for_trip(trip) is None

    def test_non_finite_realized_return_returns_none(self):
        trip = _FakeTrip(
            asset_class="equity",
            entry_price=100.0,
            qty=50.0,
            realized_return=float("nan"),
        )
        assert ptrar_for_trip(trip) is None

    def test_options_without_max_loss_returns_none(self):
        trip = _FakeTrip(
            asset_class="us_option",
            entry_price=5.0,
            qty=1.0,
            realized_return=0.02,
            notional_multiplier=100.0,
        )
        # No max_loss_usd provided → fail-CLOSED
        assert ptrar_for_trip(trip) is None

    def test_options_with_inf_max_loss_returns_none(self):
        trip = _FakeTrip(
            asset_class="us_option",
            entry_price=5.0,
            qty=1.0,
            realized_return=0.02,
            notional_multiplier=100.0,
        )
        assert ptrar_for_trip(trip, max_loss_usd=float("inf")) is None


# ---------------------------------------------------------------------------
# KEYSTONE: parity property (the ADR-0099 §B guarantee)
# ---------------------------------------------------------------------------


class TestParityProperty:
    """The keystone test: equal risk-normalised return → identical PTRAR.

    ADR-0099 §B: "An equity trade returning +2% of position and an options
    trade returning +2% of max_loss have IDENTICAL PTRAR."

    RED proof: comment out the max_loss normalisation in ptrar_options (i.e.
    replace ``/ max_loss_usd`` with ``/ 1.0`` or any constant != max_loss_usd)
    and this test FAILS because options_ptrar will equal
    pnl_options / 1.0 (≈ 40.0) instead of 0.02.
    """

    def test_equity_and_options_equal_ptrar_at_2pct(self):
        """Keystone: +2% risk-normalised return → PTRAR = 0.02 for BOTH classes."""
        # --- Equity side ---
        # 100 shares at $100 entry → notional = $10_000
        # +2% of notional = +$200 P&L
        equity_notional = 100.0 * 100.0  # entry_price × abs_position_usd
        equity_pnl = 0.02 * equity_notional  # $200
        ptrar_eq = ptrar_equity(
            realized_pnl_usd=equity_pnl,
            entry_price=100.0,
            abs_position_usd=100.0,
        )

        # --- Options side ---
        # max_loss = $2_000 (e.g. a bull-put spread with $20 wide × 1 contract × 100)
        # +2% of max_loss = +$40 P&L
        max_loss = 2000.0
        options_pnl = 0.02 * max_loss  # $40
        ptrar_opt = ptrar_options(
            realized_pnl_usd=options_pnl,
            max_loss_usd=max_loss,
        )

        # Both must be exactly 0.02
        assert ptrar_eq == pytest.approx(0.02, rel=1e-9), (
            f"Equity PTRAR {ptrar_eq!r} diverged from 0.02"
        )
        assert ptrar_opt == pytest.approx(0.02, rel=1e-9), (
            f"Options PTRAR {ptrar_opt!r} diverged from 0.02"
        )

        # The parity property itself: the two numbers must be equal
        assert ptrar_eq == pytest.approx(ptrar_opt, rel=1e-9), (
            f"PTRAR parity violation: equity={ptrar_eq!r}, options={ptrar_opt!r} — "
            "a risk-normalised +2% return must yield identical PTRAR for both classes."
        )

    def test_parity_at_various_return_levels(self):
        """Parity holds at -10%, 0%, +5%, +50% risk-normalised return."""
        entry_price = 50.0
        shares = 200.0
        equity_notional = entry_price * shares  # $10_000

        max_loss = 1500.0  # options max_loss (different magnitude, same fraction)

        for frac in (-0.10, 0.0, 0.05, 0.50):
            eq_pnl = frac * equity_notional
            opt_pnl = frac * max_loss

            ptrar_eq = ptrar_equity(eq_pnl, entry_price, shares)
            ptrar_opt = ptrar_options(opt_pnl, max_loss)

            assert ptrar_eq == pytest.approx(frac, rel=1e-9), (
                f"frac={frac}: equity PTRAR {ptrar_eq!r} != {frac}"
            )
            assert ptrar_opt == pytest.approx(frac, rel=1e-9), (
                f"frac={frac}: options PTRAR {ptrar_opt!r} != {frac}"
            )
            assert ptrar_eq == pytest.approx(ptrar_opt, rel=1e-9), (
                f"frac={frac}: PTRAR parity violation "
                f"equity={ptrar_eq!r} != options={ptrar_opt!r}"
            )

    def test_ptrar_for_trip_parity(self):
        """ptrar_for_trip also upholds parity via its dispatch."""
        # --- Equity trip ---
        # Entry $100, 100 shares, +2% return
        # pnl_usd = 0.02 * $100 * 100 * 1.0 = $200
        # capital = $100 * 100 * 1.0 = $10_000 → PTRAR = 0.02
        equity_trip = _FakeTrip(
            asset_class="equity",
            entry_price=100.0,
            qty=100.0,
            realized_return=0.02,
            notional_multiplier=1.0,
        )

        # --- Options trip ---
        # wave1-review FIX (was VACUOUS): the prior test set max_loss = $4*1*100 =
        # $400 = capital_at_risk, so a broken dispatch using capital passed. Here the
        # option's capital_at_risk = $4*1*100 = $400 but max_loss_usd = $200 (a
        # $2-wide spread) — DISTINCT. Parity is engineered on the RETURN, not by
        # making max_loss == capital: we pick the option pnl so pnl/max_loss == 0.02.
        # pnl_usd target = 0.02 * $200 = $4  ->  realized_return = pnl / (entry*qty*mult)
        #                = $4 / ($4*1*100) = 0.01.
        max_loss_usd = 200.0  # DISTINCT from capital_at_risk ($400)
        target_ptrar = 0.02
        options_pnl_usd = target_ptrar * max_loss_usd  # $4
        options_return = options_pnl_usd / (4.0 * 1.0 * 100.0)  # 0.01 (!= the equity 0.02)
        options_trip = _FakeTrip(
            asset_class="us_option",
            entry_price=4.0,
            qty=1.0,
            realized_return=options_return,
            notional_multiplier=100.0,
        )

        ptrar_eq = ptrar_for_trip(equity_trip)
        ptrar_opt = ptrar_for_trip(options_trip, max_loss_usd=max_loss_usd)

        assert ptrar_eq == pytest.approx(0.02, rel=1e-9)
        assert ptrar_opt == pytest.approx(0.02, rel=1e-9), (
            "ptrar_for_trip must normalize the option by max_loss_usd ($200) — a "
            f"broken capital-at-risk ($400) dispatch yields 0.01; got {ptrar_opt!r}"
        )
        assert ptrar_eq == pytest.approx(ptrar_opt, rel=1e-9), (
            "ptrar_for_trip parity violation: "
            f"equity={ptrar_eq!r} != options={ptrar_opt!r}"
        )
