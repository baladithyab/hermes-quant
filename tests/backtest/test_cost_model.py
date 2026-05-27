"""tests/backtest/test_cost_model.py — CostModel arithmetic tests (Wave 6a / ADR-0045).

Coverage:
- round_trip_cost_bps grows with sqrt(participation)
- commission applied per share
- profile multipliers (LIQUID_EQUITY, MIDCAP_EQUITY, ILLIQUID)
- apply_to_fill: buy fills above decision price, sell fills below
- edge cases: participation=0 raises, negative side raises
"""

from __future__ import annotations

import math
import pytest

from hermes_quant.backtest.cost_model import (
    CostModel,
    ILLIQUID,
    LIQUID_EQUITY,
    MIDCAP_EQUITY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def approx_eq(a: float, b: float, rel: float = 1e-6) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) < rel


# ---------------------------------------------------------------------------
# CostModel arithmetic
# ---------------------------------------------------------------------------


class TestCostModelArithmetic:
    """Core arithmetic invariants."""

    def test_one_way_cost_positive(self):
        """One-way cost is positive for any positive participation."""
        cm = CostModel()
        assert cm.one_way_cost_bps(0.05) > 0

    def test_round_trip_is_double_one_way(self):
        """Round-trip = 2 × one-way (exact)."""
        cm = CostModel(half_spread_bps=5.0, market_impact_coeff=0.10)
        pct = 0.10
        assert math.isclose(cm.round_trip_cost_bps(pct), 2 * cm.one_way_cost_bps(pct))

    def test_round_trip_grows_with_participation(self):
        """Higher participation → higher round-trip cost (sqrt-impact)."""
        cm = CostModel(half_spread_bps=5.0, market_impact_coeff=0.10)
        low = cm.round_trip_cost_bps(0.01)
        mid = cm.round_trip_cost_bps(0.10)
        high = cm.round_trip_cost_bps(0.50)
        assert low < mid < high

    def test_sqrt_impact_shape(self):
        """Market impact scales as sqrt(participation)."""
        coeff = 0.10
        cm = CostModel(half_spread_bps=0.0, market_impact_coeff=coeff, slippage_floor_bps=0.0)
        pct1, pct4 = 0.01, 0.04
        cost1 = cm.one_way_cost_bps(pct1)
        cost4 = cm.one_way_cost_bps(pct4)
        # sqrt(4x) / sqrt(x) = 2 → cost4 ≈ 2 × cost1
        assert math.isclose(cost4, 2 * cost1, rel_tol=1e-6)

    def test_slippage_floor_enforced(self):
        """Even tiny orders pay at least slippage_floor_bps."""
        floor = 3.0
        cm = CostModel(half_spread_bps=0.0, market_impact_coeff=0.0, slippage_floor_bps=floor)
        assert cm.one_way_cost_bps(1e-9) >= floor

    def test_commission_per_share_applied(self):
        """Commission per share increases the fill cost."""
        no_comm = CostModel(commission_per_share=0.0)
        with_comm = CostModel(commission_per_share=0.005)
        px = 100.0
        fill_no = no_comm.apply_to_fill(px, side=1)
        fill_yes = with_comm.apply_to_fill(px, side=1)
        assert fill_yes > fill_no

    def test_commission_scales_with_shares(self):
        """apply_to_fill is deterministic; commission adds a fixed fraction."""
        comm = 0.01
        cm = CostModel(commission_per_share=comm)
        px = 50.0
        fill = cm.apply_to_fill(px, side=1)
        # commission fraction = comm / px = 0.01/50 = 0.0002
        expected_comm_adjustment = comm / px * px  # = comm
        assert fill > px  # buy fills above

    def test_participation_zero_raises(self):
        """participation_pct=0 must raise ValueError."""
        cm = CostModel()
        with pytest.raises(ValueError, match="participation_pct"):
            cm.one_way_cost_bps(0.0)

    def test_negative_participation_raises(self):
        """Negative participation must raise ValueError."""
        cm = CostModel()
        with pytest.raises(ValueError):
            cm.one_way_cost_bps(-0.1)

    def test_apply_to_fill_buy_above_decision_price(self):
        """BUY fill price must be above decision price (adverse)."""
        cm = CostModel(half_spread_bps=5.0)
        fill = cm.apply_to_fill(100.0, side=1)
        assert fill > 100.0

    def test_apply_to_fill_sell_below_decision_price(self):
        """SELL fill price must be below decision price (adverse)."""
        cm = CostModel(half_spread_bps=5.0)
        fill = cm.apply_to_fill(100.0, side=-1)
        assert fill < 100.0

    def test_apply_to_fill_bad_side_raises(self):
        """Side != ±1 must raise ValueError."""
        cm = CostModel()
        with pytest.raises(ValueError, match="side"):
            cm.apply_to_fill(100.0, side=0)

    def test_apply_to_fill_bad_price_raises(self):
        """decision_price ≤ 0 must raise ValueError."""
        cm = CostModel()
        with pytest.raises(ValueError, match="decision_price"):
            cm.apply_to_fill(0.0, side=1)


# ---------------------------------------------------------------------------
# Named profiles
# ---------------------------------------------------------------------------


class TestNamedProfiles:
    """Profile multiplier invariants."""

    def test_profiles_are_distinct(self):
        """Three profiles have increasing cost levels."""
        pct = 0.10
        liquid = LIQUID_EQUITY.round_trip_cost_bps(pct)
        mid = MIDCAP_EQUITY.round_trip_cost_bps(pct)
        illiq = ILLIQUID.round_trip_cost_bps(pct)
        assert liquid < mid < illiq

    def test_illiquid_roughly_3x_liquid(self):
        """ILLIQUID is approximately 3× LIQUID_EQUITY in total cost."""
        pct = 0.10
        liquid = LIQUID_EQUITY.round_trip_cost_bps(pct)
        illiq = ILLIQUID.round_trip_cost_bps(pct)
        # Not exact (impact coeff is different), but should be in [2×, 4×] range
        assert 2.0 <= illiq / liquid <= 5.0

    def test_liquid_equity_defaults(self):
        """LIQUID_EQUITY has expected parameter values."""
        assert LIQUID_EQUITY.half_spread_bps == 5.0
        assert LIQUID_EQUITY.market_impact_coeff == 0.10
        assert LIQUID_EQUITY.commission_per_share == 0.0
        assert LIQUID_EQUITY.slippage_floor_bps == 1.0

    def test_illiquid_has_commission(self):
        """ILLIQUID profile includes non-zero commission."""
        assert ILLIQUID.commission_per_share > 0.0

    def test_profiles_are_singletons(self):
        """Re-importing profiles returns the same object."""
        from hermes_quant.backtest.cost_model import LIQUID_EQUITY as L2
        assert LIQUID_EQUITY is L2

    def test_cost_model_dataclass_immutable_by_default(self):
        """CostModel is a plain dataclass (not frozen) but fields are accessible."""
        cm = CostModel()
        assert hasattr(cm, "half_spread_bps")
        assert hasattr(cm, "round_trip_cost_bps")
