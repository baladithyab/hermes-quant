
# Bug Hunt: Portfolio State & Reactor (2026-06-02)

This document enumerates correctness and financial-numerical bugs found in the `hermes-quant` portfolio state and reactor code.

---

## P0: Critical Bugs

### 1. Incorrect Cash Delta Calculation on Fills

*   **File**: `hermes_quant/state/portfolio_state.py`
*   **Lines**: `_apply_execution_unsafe`:~582, `_replay_record`:~656
*   **Severity**: P0
*   **Reasoning**: `delta_cash = -fill_size_pct * fill_price` is dimensionally incorrect. `fill_size_pct` is a NAV fraction, not a share quantity. Multiplying it by a price/share results in a meaningless value and completely incorrect cash accounting.
*   **Suggested Fix**: The cash delta should be based on the notional value of the trade. E.g., `delta_cash = -fill_size_pct * nav`. This requires looking up the NAV at the time of the trade.

### 2. Incorrect Equity Calculation for Short Positions

*   **File**: `hermes_quant/state/portfolio_state.py`
*   **Lines**: `reconstruct_from`:~377, `_apply_execution_unsafe`:~586
*   **Severity**: P0
*   **Reasoning**: The equity calculation `abs(p['quantity']) * p['avg_entry_price']` uses `abs()` on the quantity. This treats short positions as assets, adding their notional value to equity instead of subtracting it as a liability. This massively inflates equity for any portfolio with short positions.
*   **Suggested Fix**: Use signed quantity: `p['quantity'] * mark_price`. This requires a mark price (see next bug).

### 3. Equity Calculation Uses Average Entry Price, Not Mark Price

*   **File**: `hermes_quant/state/portfolio_state.py`
*   **Lines**: `reconstruct_from`:~377, `_apply_execution_unsafe`:~586
*   **Severity**: P0
*   **Reasoning**: Portfolio equity is calculated using `avg_entry_price`. Equity must be calculated using the *current market price* (mark price) of positions, not their historical cost basis. This results in a completely inaccurate representation of the portfolio's current value.
*   **Suggested Fix**: The equity calculation function needs access to a market data source to get the current price for each symbol and use that to mark positions to market.

---

## P2: Minor/Non-Critical Bugs

### 1. Overly Conservative Net Headroom Calculation

*   **File**: `hermes_quant/risk/portfolio_normalize.py`
*   **Line**: `_headroom`:~151
*   **Severity**: P2
*   **Reasoning**: The `scale_to_fit` normalization policy calculates net headroom as `min(long_room, short_room)`. It then uses this single symmetric value to scale all new trades, regardless of their direction. This is overly conservative and can lead to unnecessarily small trade sizes.
*   **Suggested Fix**: The `_normalize_scale_to_fit` function should consider the sign of the batch's `net_demand`. If `net_demand` is positive, it should be scaled against `long_room`; if negative, against `short_room`.

