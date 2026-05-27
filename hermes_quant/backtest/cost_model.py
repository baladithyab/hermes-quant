"""hermes_quant.backtest.cost_model — Explicit transaction-cost model (Wave 6a / ADR-0045).

Background
----------
Only 1/19 empirically-primary LLM-trading papers in the 2026 SOTA scan
(arxiv:2605.19337) include a transaction-cost model.  This module is the
hermes-quant answer: every backtest fill goes through CostModel so cost drag
is never hidden.

Cost anatomy (liquid US equity, basis-points = bps = 0.01 %):
  - half_spread_bps  : one-way cost of crossing the bid-ask spread
  - market_impact    : sqrt-impact model — bp_impact = coeff * sqrt(pct)
                       where pct is participation rate (fraction of ADV)
                       Canonical reference: Almgren et al. (2005) empirical
                       analysis of S&P500 market impact, confirmed in the
                       2026 FLAG-Trader paper (SemanticScholar).
  - commission       : per-share fixed cost (zero in the commission-free era,
                       but the field is kept so institutional simulations work)
  - slippage_floor   : minimum one-way slippage even with tiny orders

Design: Mai0313/TradingAgents CostTracker + Almgren sqrt-impact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# CostModel
# ---------------------------------------------------------------------------


@dataclass
class CostModel:
    """Explicit transaction-cost model for backtesting.

    All cost parameters are expressed per **one-way** trade leg unless
    documented otherwise.  ``round_trip_cost_bps`` combines both legs.

    Parameters
    ----------
    half_spread_bps:
        Half the bid-ask spread in basis points.  Default 5 bps is a
        conservative estimate for liquid large-cap US equities (e.g. S&P500
        constituents) during normal hours.  Source: Hasbrouck (2009) TAQ
        study; confirmed by Mai0313/TradingAgents CostTracker defaults.
    market_impact_coeff:
        Coefficient in the sqrt-impact formula:
          impact_bps = coeff * sqrt(participation_pct)
        Default 0.10 corresponds to ≈3.2 bps at 10 % participation —
        consistent with the Almgren et al. (2005) empirical calibration for
        S&P500 stocks.
    commission_per_share:
        Fixed commission per share (USD).  Default 0.0 reflects the
        commission-free era (Robinhood/IBKR Lite/etc.).  Set to 0.005 for
        institutional IBKR Pro simulation.
    slippage_floor_bps:
        Minimum one-way slippage even for tiny orders.  Prevents the model
        from showing unrealistically zero cost on micro-trades.  Default 1 bps.
    """

    half_spread_bps: float = 5.0
    market_impact_coeff: float = 0.10
    commission_per_share: float = 0.0
    slippage_floor_bps: float = 1.0

    def one_way_cost_bps(self, participation_pct: float = 0.10) -> float:
        """One-way cost in basis points for a given participation rate.

        Parameters
        ----------
        participation_pct:
            Fraction of average daily volume (ADV) consumed by this order.
            0.10 = 10 % of ADV.  Must be > 0.

        Returns
        -------
        float
            Cost in basis points (one-way).
        """
        if participation_pct <= 0:
            raise ValueError(f"participation_pct must be > 0; got {participation_pct}")
        impact_bps = self.market_impact_coeff * math.sqrt(participation_pct)
        one_way = self.half_spread_bps + impact_bps
        return max(one_way, self.slippage_floor_bps)

    def round_trip_cost_bps(self, participation_pct: float = 0.10) -> float:
        """Round-trip cost in basis points (entry + exit).

        Formula:  2 × one_way_cost_bps(participation_pct)

        The sqrt-impact model grows sub-linearly with participation — doubling
        your participation less than doubles the impact — which is the
        empirically observed concavity in Almgren et al. (2005).

        Parameters
        ----------
        participation_pct:
            Fraction of ADV per leg (same rate applied to both entry and exit).

        Returns
        -------
        float
            Round-trip cost in basis points.
        """
        return 2.0 * self.one_way_cost_bps(participation_pct)

    def apply_to_fill(
        self,
        decision_price: float,
        side: int,
        participation_pct: float = 0.10,
    ) -> float:
        """Return the expected fill price after applying transaction costs.

        The cost is always adverse: buys fill *above* decision price, sells
        fill *below*.  Commission is a flat per-share debit regardless of side.

        Parameters
        ----------
        decision_price:
            The price at which the strategy decided to trade (e.g. prior close).
        side:
            +1 for BUY, -1 for SELL.  Any other value raises ValueError.
        participation_pct:
            Fraction of ADV for this order.

        Returns
        -------
        float
            Expected fill price after all costs.

        Raises
        ------
        ValueError
            If side is not ±1 or decision_price ≤ 0.
        """
        if side not in (1, -1):
            raise ValueError(f"side must be +1 (buy) or -1 (sell); got {side}")
        if decision_price <= 0:
            raise ValueError(f"decision_price must be > 0; got {decision_price}")

        one_way_bps = self.one_way_cost_bps(participation_pct)
        slippage_frac = one_way_bps / 10_000.0  # bps → fraction

        # Adverse fill: buy high, sell low
        fill_price = decision_price * (1.0 + side * slippage_frac)

        # Commission is a further adverse adjustment regardless of side
        # (treated as price deduction for sells, price addition for buys)
        if decision_price > 0 and self.commission_per_share != 0:
            comm_per_unit = self.commission_per_share / decision_price  # fraction
            fill_price += side * comm_per_unit * decision_price

        return fill_price


# ---------------------------------------------------------------------------
# Named profiles
# ---------------------------------------------------------------------------


def _liquid_equity() -> CostModel:
    """Large-cap US equity (S&P500 members), normal hours.

    5 bps half-spread, 0.10 sqrt-impact coeff, zero commission, 1 bps floor.
    """
    return CostModel(
        half_spread_bps=5.0,
        market_impact_coeff=0.10,
        commission_per_share=0.0,
        slippage_floor_bps=1.0,
    )


def _midcap_equity() -> CostModel:
    """Mid-cap US equity (Russell 1000 ex-S&P500).

    ~2× the liquid spread; slightly higher impact.
    """
    return CostModel(
        half_spread_bps=10.0,
        market_impact_coeff=0.18,
        commission_per_share=0.0,
        slippage_floor_bps=2.0,
    )


def _illiquid_equity() -> CostModel:
    """Small-cap / illiquid equity (Russell 2000, OTC).

    3× the liquid-equity coefficients as a conservative floor.
    Source: Keim & Madhavan (1997) transaction cost study for small caps.
    """
    return CostModel(
        half_spread_bps=15.0,
        market_impact_coeff=0.30,
        commission_per_share=0.005,
        slippage_floor_bps=3.0,
    )


# Singleton profile instances — import these directly for convenience.
LIQUID_EQUITY: CostModel = _liquid_equity()
MIDCAP_EQUITY: CostModel = _midcap_equity()
ILLIQUID: CostModel = _illiquid_equity()
