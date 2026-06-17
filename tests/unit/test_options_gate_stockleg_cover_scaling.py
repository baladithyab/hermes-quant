"""RED→GREEN: the covered-call StockLeg cover MUST scale with the option-leg lot
count at EVERY gate net-greeks aggregation (first-pass AND the at-size re-check).

Defect family: options greeks / multileg leg construction. ``aggregate_net_greeks``
scales OPTION legs by ``order_qty`` (data.py: ``units = sign * ratio_qty * order_qty
* 100``) but treats ``StockLeg.qty`` as an ALREADY-scaled absolute share count
(``delta += 1.0 * qty``, NOT scaled by order_qty). The covered-call recipe builds a
ONE-LOT cover (``StockLeg(qty=100)``) and hands the gate that same 1-lot stock leg.

Two distinct fail-OPEN holes follow from aggregating a 1-lot stock cover against an
N-lot option footprint:

  1. FIRST-PASS hole (``structural_contracts > 1``, e.g. a ``ratio_qty=2`` short
     call): the first net-greeks aggregation runs at ``order_qty=structural_contracts``
     and, when the sized contract count equals it, the at-size re-check is SKIPPED —
     so the first-pass aggregate is the ONLY net-delta check AND the reported
     ``net_greeks``. A 1-lot cover (qty=100) against a 2x short call understates the
     net delta so badly the sign flips (reads NET-SHORT when the covered position is
     strongly net-LONG), so ``abs(net_delta * spot)`` evaluates a tiny number and the
     net-delta cap fails open.

  2. AT-SIZE re-check hole (``contracts != structural_contracts``, e.g. a 1-lot
     ``ratio_qty=1`` short call sized up to 2 lots): the re-check re-aggregates at the
     admitted ``contracts`` but leaves the cover at 1 lot, understating the
     directional exposure of the N-lot position the order actually establishes.

Both holes make the deterministic gate (the FINAL authority that may only REJECT)
report a wrong net_greeks and evaluate the net-delta cap against an understated
exposure — fail-open. These tests pin the TRUE per-lot-scaled net delta.
"""

from __future__ import annotations

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
)
from hermes_quant.risk.options_gate import (
    OptionsRiskConfig,
    StructureBucket,
    options_gate,
)

CFG = OptionsRiskConfig()


@pytest.fixture(autouse=True)
def _gate_enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")


def _short_call(symbol: str, *, delta: float, ratio_qty: int = 1) -> OptionLeg:
    return OptionLeg(
        symbol=symbol,
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=ratio_qty,
        # theta>0 (short = collecting decay) so O4 never silences; gamma/vega tiny so
        # only the net-delta footprint is the variable under test.
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.0001, theta=0.05, vega=0.0001, rho=0.0
        ),
    )


def test_first_pass_cover_scales_with_ratio_qty() -> None:
    """structural_contracts>1 (ratio_qty=2): the FIRST-pass net_greeks must count the
    cover at 100 * structural_contracts shares, not 1 lot. RED: reported delta is
    grossly understated (sign-flipped negative) without scaling."""
    stock = StockLeg(underlying="NVDA", qty=100, basis_per_share=10.0)
    sc = _short_call("NVDA260717C00012000", delta=0.30, ratio_qty=2)
    res = options_gate(
        [stock, sc],
        strategy_kind="covered_call",
        underlying="NVDA",
        spot=10.0,
        nav=100_000.0,
        held_shares=400,  # classifier needs >=100*structural_contracts; ample
        options_buying_power=500_000.0,
        premium_received=250.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=CFG,
        strike=120.0,
        basis_per_share=10.0,
        min_dte=30,
    )
    assert res.admitted is True
    assert res.bucket == StructureBucket.COVERED_CALL
    # With basis=spot=10 and a small kelly target the gate sizes 1 lot; the short
    # leg's ratio_qty=2 makes structural_contracts=2, which is the first-pass
    # order_qty AND (since contracts==structural_contracts here) the only check.
    structural = 2  # sum(ratio_qty) for the single ratio_qty=2 short call
    # The gate's COHERENT cover is _shares_needed(structural_contracts) = 100*2 = 200
    # (exactly what the classifier requires of held_shares), and the short footprint
    # is delta * ratio_qty * order_qty * 100 at order_qty=structural_contracts.
    short_delta = 0.30 * 2 * structural * 100.0
    cover = 100.0 * structural
    coherent_delta = cover - short_delta
    assert res.net_greeks.delta == pytest.approx(coherent_delta), (
        f"first-pass net delta wrong: reported {res.net_greeks.delta} != coherent "
        f"{coherent_delta} (cover must be 100*structural_contracts, matching the "
        "classifier's _shares_needed)"
    )
    # The covered (long-cover-dominated) net delta is strongly POSITIVE; the pre-fix
    # 1-lot-cover aggregate read NEGATIVE (sign-flipped) — that is the fail-open.
    assert res.net_greeks.delta > 0.0


def test_at_size_recheck_cover_scales_with_admitted_lots() -> None:
    """contracts != structural_contracts (1-lot ratio sized to N lots): the at-size
    re-check must count the cover at 100 * contracts shares. RED: reported delta uses
    a 1-lot cover, understating the N-lot position's directional exposure."""
    stock = StockLeg(underlying="NVDA", qty=100, basis_per_share=10.0)
    sc = _short_call("NVDA260717C00012000", delta=0.30, ratio_qty=1)
    res = options_gate(
        [stock, sc],
        strategy_kind="covered_call",
        underlying="NVDA",
        spot=10.0,
        nav=100_000.0,
        held_shares=400,
        options_buying_power=500_000.0,
        premium_received=250.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=CFG,
        strike=120.0,
        basis_per_share=10.0,
        min_dte=30,
    )
    assert res.admitted is True
    n = res.contracts
    assert n >= 2, "test needs the gate to size up >1 lot to exercise the re-check"
    true_delta = 100.0 * n - 0.30 * n * 100.0  # cover 100*n shares, short 0.30 * n * 100
    assert res.net_greeks.delta == pytest.approx(true_delta), (
        f"at-size net delta understated: reported {res.net_greeks.delta} != TRUE "
        f"{true_delta} for {n} lots (cover left at 1 lot)"
    )


def test_one_lot_recipe_shaped_cc_byte_identical() -> None:
    """Non-vacuity / no-regression: the production recipe path (ratio_qty=1, a 1-lot
    cover that is NOT sized up) must be unchanged — net delta = 100 - delta*100."""
    stock = StockLeg(underlying="NVDA", qty=100, basis_per_share=150.0)
    sc = _short_call("NVDA260717C00160000", delta=0.25, ratio_qty=1)
    res = options_gate(
        [stock, sc],
        strategy_kind="covered_call",
        underlying="NVDA",
        spot=150.0,
        nav=1_000_000.0,
        held_shares=100,
        options_buying_power=500_000.0,
        premium_received=250.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=CFG,
        strike=160.0,
        basis_per_share=150.0,
        min_dte=30,
    )
    assert res.admitted is True
    # With basis=spot=150, sizing target floors to 1 lot -> structural==contracts==1.
    assert res.contracts == 1
    assert res.net_greeks.delta == pytest.approx(100.0 - 0.25 * 100.0)
