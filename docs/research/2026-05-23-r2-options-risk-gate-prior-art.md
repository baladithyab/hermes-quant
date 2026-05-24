# Research Note: Options-Aware Risk Gate Prior Art (ADR-0027)

## 1. Goal & Context
This research examines how various trading frameworks (LEAN, NautilusTrader, hftbacktest) and retail brokers (Tastyworks, IBKR) model options risk, margin, and exposure at the pre-trade gate. The objective is to recommend concrete numeric defaults and logic changes for extending hermes-quant's equity-only risk gate (`gate.py` per ADR-0004 & ADR-0009) to handle options strategies safely.

Hermes-quant's core philosophy (ADR-0004):
* **Silence by default:** Negative bias; false-positive penalty > false-negative.
* **Hard deterministic rules:** The RL/Aggregator cannot bypass them.
* **Anti-leverage gambling:** Discrete sizing, ¼-Kelly.

Priority options strategies: `covered_call`, `cash_secured_put`, `wheel`, `swing_directional` (30-90 DTE), `leaps_thesis`.

---

## 2. Survey of Prior Art

### 2.1 LEAN (QuantConnect)
*   **Margin Model:** Does not automatically infer account limits (e.g., PDT, leverage rules) from the broker; these must be explicitly configured in the algorithm.
*   **Derivatives Margin:** Long options have zero maintenance margin; initial margin is strictly the premium paid. Naked options/spreads rely on complex Buying Power Models matching Reg-T or Portfolio Margin.
*   **Pre-Trade Controls (`RiskManagementModel`):** Checks available buying power before order submission to avoid broker rejection. Often uses `MaximumDrawdownPercentPerSecurity`, `TrailingStopRiskManagementModel`, and `MaximumUnrealizedProfitPercentPerSecurity`.

### 2.2 NautilusTrader
*   **Architecture:** Focuses heavily on performance and deterministic simulation, maintaining a strict Rust-based Event-Driven core.
*   **Options Support:** Executes limit-style orders for options. It supports venue-provided Greeks but expects the user logic to aggregate portfolio risk.
*   **Margin Methods:** Supports Isolated, Cross, and Portfolio Margin (where offsetting positions drastically reduce combined margin requirements).

### 2.3 hftbacktest
*   **Scope:** Primarily geared toward ultra-low latency, full tick, Level-2/3 order book crypto market making.
*   **Risk Limits:** Enforces hard position limits to prevent single strategies from over-accumulating inventory, though options-specific Greek gating is not a primary first-class citizen compared to inventory/latency risk.

### 2.4 Tastyworks (tastytrade)
*   **Margin/BPR (Buying Power Reduction):** Uses heavily risk-based methodology (Portfolio Margin). Requires $100k+ balance.
*   **Stress Testing:** Calculates margin on indices/products by stressing the underlying by -15% / +10% in low volatility environments and capturing the worst-case loss as the BPR.
*   **Option Buying Power:** Buying stock reduces BP by 50% of notional (Reg-T), but naked option BPR is significantly more complex, relying on strike distance and underlying price.

### 2.5 Interactive Brokers (IBKR)
*   **Margin Methodology:** Highly proprietary stress-testing algorithm generating an "Exposure Analysis".
*   **Standard Reg-T Minimums:**
    *   Covered Put: Initial Stock Margin + In-the-Money Amount.
    *   Call Spread: Short Strike - Long Strike (max loss).
    *   Long Call/Put: Paid in full (premium).
*   **Exposure Fee:** Added daily charge based on worst-case stress tests of the portfolio, recalibrated frequently.

---

## 3. Options Portfolio Risk (Academic/Industry Consensus)

*   **Delta-Neutrality & Gating:** A portfolio's directional risk is governed by Net Delta. While stock delta is linear (1.0), options delta accelerates (Gamma). You cannot delta-hedge safely without capping Gamma.
*   **Gamma Exposure (GEX):** High negative Gamma implies accelerating losses in volatile moves. Positive Gamma implies profits, but costs Theta.
*   **Vega & Theta:** Vega is the exposure to implied volatility collapse/spike (event risk). Theta is the deterministic time-decay budget.
*   **Aggregation:** Greeks are additive laterally (Per-Book = sum of Per-Leg). E.g., Verticals require summing the leg deltas to understand net directional exposure.

---

## 4. Recommendations for ADR-0027

To extend ADR-0004 for options, the risk gate must transcend simple strictly-positive asset values and evaluate non-linear contractual risk.

### 4.1 Hard Rules (Enforced BEFORE Paper Proposal)

1.  **Max Loss (Margin) Validation (replaces simple `max_position_pct`):**
    *   *Rule:* `Expected Max Loss <= (cfg.max_position_pct * NAV)`
    *   *Calculation:*
        *   Long Option: Max Loss = Premium Paid.
        *   Covered Call / CSP: Max Loss = Strike (for CSP) or Underlying Basis.
        *   Spreads: Max Loss = Width of strikes - Net Premium Received.
    *   *Why:* Options employ leverage. Using strict notional value breaks for naked or spread options. We cap the *maximum theoretical loss* to the existing `max_position_pct` (default 20%).
2.  **Portfolio Gamma Cap:**
    *   *Rule:* `|Net Portfolio Gamma| <= 0.05 * NAV` (normalized to underlying price moves).
    *   *Why:* High absolute Gamma means Delta will flip dramatically. A highly negative Gamma portfolio will blow through Delta limits instantly on a gap move.
3.  **BPR (Buying Power Reduction) Buffer:**
    *   *Rule:* `Total Portfolio BPR <= 0.80 * Total Available Equity` (Leaves 20% cash buffer).
    *   *Calculation:* Use a Reg-T approximation (e.g., Short Naked = 20% of underlying - OTM amount + premium).
4.  **No Naked Uncovered (for early versions):**
    *   *Rule:* Reject short legs lacking long underlying (Covered Call) or Cash (CSP), or a wider long leg (Spreads).

### 4.2 Soft Warnings (Surface in Discord, Don't Halt)

1.  **Event Risk (Earnings / Dividends):**
    *   *Warning:* "Earnings release (Date X) occurs before Expiration (Date Y). Vega crush expected."
2.  **Pin Risk / Expiry Proximity:**
    *   *Warning:* "Options expiring in <= 3 DTE. Gamma risk extremely elevated."
3.  **Low Liquidity / Wide Spread:**
    *   *Warning:* "Option bid/ask spread > 10% of premium. Severe slippage likely."

### 4.3 Sizing: ¼-Kelly Mapping to Contract Count

ADR-0004's Kelly formula targets a fraction of NAV. For options: 
*   `kelly_nav_target = Kelly Fraction * NAV`
*   **Directional Options (Long Calls/Puts/LEAPS):** 
    Sizing refers to the premium spent. 
    `Contracts = floor(kelly_nav_target / (Option Premium * 100))`
*   **Income Options (CSP/CC/Wheels):** 
    Sizing refers to the underlying collateral (the risk of assignment). 
    `Contracts = floor(kelly_nav_target / (Strike Price * 100))`

*Recommendation:* Do not size based on "Delta equivalents" initially (e.g., sizing 2 50-delta options to match 100 shares). Size based on **Capital at Risk**, honoring the discrete step sizes (`action_step = 0.05`).

### 4.4 Kill-Switch Conditions (Auto-Halt)

1.  **Gamma Spike (The Widowmaker):**
    *   *Trigger:* Short Gamma exposure exceeds `X%` of NAV.
    *   *Action:* Halt new entries.
2.  **Margin Call Proximity:**
    *   *Trigger:* Aggregate BPR exceeds 95% of Available Equity. (Leaves 5% absolute floor).
    *   *Action:* Flatten out-of-the-money legs to reduce BPR, Halt.
3.  **Theta Bleed Limit:**
    *   *Trigger:* Daily Theta decay exceeds 2% of NAV.
    *   *Action:* Halt entries. (Prevent "death by a thousand cuts" holding too many long premium options).

### 4.5 Multi-leg Net-Greeks

*   **Aggregation Rule:** The Risk Gate *must* aggregate legs prior to evaluation. 
    *   `Net Delta = Sum(Leg_Delta * Side * 100 * Contracts)`
    *   `Net Gamma = Sum(Leg_Gamma * Side * 100 * Contracts)`
    *   `Net Vega = Sum(Leg_Vega * Side * 100 * Contracts)`
*   *Implementation Note:* For a vertical spread, the gate evaluates the `Net Delta` against the `target_position` edge requirement, and evaluates the Strike Width against the Margin Validation rule.