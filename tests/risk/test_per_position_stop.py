"""Tests for the per-position unrealized-loss stop monitor (the 2026-06-04 ASTS fix).

The decision core is pure, so the dangerous parts — the long/short SIGN and the
NaN/inf finite-guards — are proven here in isolation. RED-proof: the ASTS-replay test
goes RED if the long-loss sign is inverted; the winning-short test goes RED (fires a
stop on a profit) if the short sign is wrong.
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.risk.per_position_stop import (
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    evaluate_stop,
    evaluate_take_profit,
    position_unrealized_loss_pct,
    weighted_avg_entry_from_lots,
)


# --------------------------------------------------------------------------- #
# AG-EQ-1 take-profit — symmetric to the stop, reuses the same sign primitive.
# --------------------------------------------------------------------------- #
def test_long_winner_takes_profit_at_target():
    # Long up 20% >= 16% TP default -> take.
    d = evaluate_take_profit(symbol="W", held_fraction=0.20, entry_price=100.0, mark_price=120.0)
    assert d.should_take is True
    assert d.gain_pct == pytest.approx(0.20, abs=1e-9)


def test_long_winner_below_target_holds():
    d = evaluate_take_profit(symbol="W", held_fraction=0.20, entry_price=100.0, mark_price=110.0)
    assert d.should_take is False  # +10% < 16%
    assert d.gain_pct == pytest.approx(0.10, abs=1e-9)


def test_losing_position_never_takes_profit():
    # A loser has negative gain — TP must never fire on it.
    d = evaluate_take_profit(symbol="L", held_fraction=0.20, entry_price=100.0, mark_price=80.0)
    assert d.should_take is False
    assert d.gain_pct == pytest.approx(-0.20, abs=1e-9)


def test_short_winner_takes_profit():
    # Short at 100, price fell to 80 = +20% gain on a short -> take.
    d = evaluate_take_profit(symbol="S", held_fraction=-0.20, entry_price=100.0, mark_price=80.0)
    assert d.should_take is True
    assert d.gain_pct == pytest.approx(0.20, abs=1e-9)


def test_short_loser_never_takes_profit():
    # Short at 100, price rose to 120 = a LOSS on a short -> must not take profit.
    d = evaluate_take_profit(symbol="S", held_fraction=-0.20, entry_price=100.0, mark_price=120.0)
    assert d.should_take is False
    assert d.gain_pct == pytest.approx(-0.20, abs=1e-9)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_tp_nonfinite_mark_holds(bad):
    d = evaluate_take_profit(symbol="N", held_fraction=0.20, entry_price=100.0, mark_price=bad)
    assert d.should_take is False and d.gain_pct is None


@pytest.mark.parametrize("bad_thr", [float("nan"), float("inf"), 0.0, -0.16])
def test_tp_nonfinite_threshold_falls_back_to_default(bad_thr):
    d = evaluate_take_profit(
        symbol="T", held_fraction=0.20, entry_price=100.0, mark_price=120.0, threshold_pct=bad_thr
    )
    assert d.should_take is True  # 20% >= 16% default
    assert DEFAULT_TAKE_PROFIT_PCT == 0.16


def test_tp_and_sl_cannot_both_fire():
    # The same position can't be both a >=16% gain and a >=8% loss.
    long_up = (0.20, 100.0, 130.0)  # +30%
    sl = evaluate_stop(symbol="X", held_fraction=long_up[0], entry_price=long_up[1], mark_price=long_up[2])
    tp = evaluate_take_profit(symbol="X", held_fraction=long_up[0], entry_price=long_up[1], mark_price=long_up[2])
    assert tp.should_take is True and sl.should_stop is False


# --------------------------------------------------------------------------- #
# The motivating case: ASTS long, -20.9%, MUST fire an 8% stop.
# --------------------------------------------------------------------------- #
def test_asts_long_blowup_fires_stop():
    d = evaluate_stop(
        symbol="ASTS",
        held_fraction=0.20,  # 20% NAV long
        entry_price=118.17,
        mark_price=93.44,  # -20.93%
        threshold_pct=0.08,
    )
    assert d.should_stop is True
    assert d.loss_pct is not None and d.loss_pct == pytest.approx(0.2092, abs=1e-3)


def test_long_loss_just_under_threshold_holds():
    d = evaluate_stop(
        symbol="X", held_fraction=0.20, entry_price=100.0, mark_price=92.5, threshold_pct=0.08
    )
    # -7.5% loss is inside the 8% stop -> HOLD.
    assert d.should_stop is False
    assert d.loss_pct == pytest.approx(0.075, abs=1e-6)


def test_long_winner_never_stops():
    d = evaluate_stop(
        symbol="WIN", held_fraction=0.20, entry_price=100.0, mark_price=130.0, threshold_pct=0.08
    )
    assert d.should_stop is False
    # A 30% GAIN is a NEGATIVE loss.
    assert d.loss_pct == pytest.approx(-0.30, abs=1e-6)


# --------------------------------------------------------------------------- #
# Short sign correctness — the fail-open trap. A SHORT loses when price RISES.
# --------------------------------------------------------------------------- #
def test_short_loss_fires_stop():
    # Short at 100, price rose to 110 = a 10% LOSS on a short -> fire 8% stop.
    d = evaluate_stop(
        symbol="S", held_fraction=-0.20, entry_price=100.0, mark_price=110.0, threshold_pct=0.08
    )
    assert d.should_stop is True
    assert d.loss_pct == pytest.approx(0.10, abs=1e-6)


def test_winning_short_never_stops():
    # Short at 100, price FELL to 80 = a 20% PROFIT on a short. Must NOT stop.
    # If the long/short sign were wrong, this would read as a +20% loss and FIRE
    # a stop on a winning position (the fail-open dangerous bug).
    d = evaluate_stop(
        symbol="S2", held_fraction=-0.20, entry_price=100.0, mark_price=80.0, threshold_pct=0.08
    )
    assert d.should_stop is False
    assert d.loss_pct == pytest.approx(-0.20, abs=1e-6)


# --------------------------------------------------------------------------- #
# Finite-guards — a NaN/inf must HOLD, never fabricate a stop or suppress one.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_mark_holds(bad):
    d = evaluate_stop(
        symbol="N", held_fraction=0.20, entry_price=100.0, mark_price=bad, threshold_pct=0.08
    )
    assert d.should_stop is False
    assert d.loss_pct is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
def test_bad_entry_price_holds(bad):
    d = evaluate_stop(
        symbol="E", held_fraction=0.20, entry_price=bad, mark_price=90.0, threshold_pct=0.08
    )
    assert d.should_stop is False
    assert d.loss_pct is None


def test_flat_position_holds():
    d = evaluate_stop(
        symbol="F", held_fraction=0.0, entry_price=100.0, mark_price=50.0, threshold_pct=0.08
    )
    assert d.should_stop is False
    assert d.loss_pct is None


@pytest.mark.parametrize("bad_thr", [float("nan"), float("inf"), 0.0, -0.08])
def test_nonfinite_threshold_falls_back_to_default(bad_thr):
    # A garbage threshold must NOT silently disarm the stop (ar08/ar09 family):
    # it falls back to the 8% default. A -20% long loss still fires.
    d = evaluate_stop(
        symbol="T", held_fraction=0.20, entry_price=100.0, mark_price=80.0, threshold_pct=bad_thr
    )
    assert d.should_stop is True  # 20% loss >= 8% default
    assert d.loss_pct == pytest.approx(0.20, abs=1e-6)


def test_default_threshold_constant_is_eight_pct():
    assert DEFAULT_STOP_LOSS_PCT == 0.08


# --------------------------------------------------------------------------- #
# weighted_avg_entry_from_lots — FIFO open-lot cost basis.
# --------------------------------------------------------------------------- #
def test_weighted_avg_entry_single_lot():
    lots = [{"qty": 0.20, "price": 118.17}]
    assert weighted_avg_entry_from_lots(lots) == pytest.approx(118.17, abs=1e-6)


def test_weighted_avg_entry_multi_lot():
    # Two adds: 0.2 @ 200 and 0.2 @ 220 -> weighted avg 210.
    lots = [{"qty": 0.2, "price": 200.0}, {"qty": 0.2, "price": 220.0}]
    assert weighted_avg_entry_from_lots(lots) == pytest.approx(210.0, abs=1e-6)


def test_weighted_avg_entry_skips_bad_lots():
    lots = [
        {"qty": 0.2, "price": 100.0},
        {"qty": float("nan"), "price": 999.0},  # skipped
        {"qty": 0.2, "price": float("inf")},  # skipped
        {"qty": -0.1, "price": 50.0},  # skipped (qty<=0)
    ]
    assert weighted_avg_entry_from_lots(lots) == pytest.approx(100.0, abs=1e-6)


def test_weighted_avg_entry_empty_is_none():
    assert weighted_avg_entry_from_lots([]) is None
    assert weighted_avg_entry_from_lots([{"qty": 0.0, "price": 100.0}]) is None


# --------------------------------------------------------------------------- #
# position_unrealized_loss_pct direct (the primitive).
# --------------------------------------------------------------------------- #
def test_loss_pct_primitive_long_and_short_symmetry():
    # A long down 10% and a short up 10% are both a +10% loss.
    long_loss = position_unrealized_loss_pct(held_fraction=0.2, entry_price=100.0, mark_price=90.0)
    short_loss = position_unrealized_loss_pct(held_fraction=-0.2, entry_price=100.0, mark_price=110.0)
    assert long_loss == pytest.approx(0.10, abs=1e-9)
    assert short_loss == pytest.approx(0.10, abs=1e-9)
