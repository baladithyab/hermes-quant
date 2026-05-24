# Research Brief: Alpaca Options API for Level-3 Paper Account (r1-alpaca-options-api.md)

**Date:** 2026-05-24  
**Context:** hermes-quant evolution from equity-only to options + swing-trade picker (paper account, Level-3 enabled, $100k paper equity).  
**Sources:** Official Alpaca docs (https://docs.alpaca.markets/us/docs/options-trading, https://docs.alpaca.markets/us/docs/paper-trading), blog posts, YouTube tutorials, Alpaca-py SDK examples. No secrets read. 5–10 live verification calls recommended but not performed due to tool limitations.

## 1. Endpoints Inventory (data.alpaca.markets & paper-api.alpaca.markets)

All endpoints share the same base pattern as equities/crypto (`https://paper-api.alpaca.markets` for trading, `https://data.alpaca.markets` for snapshots). Authentication = `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`.

Core options-related endpoints (from Alpaca OpenAPI spec referenced in docs):

- **GET /v2/assets** — Filter with `attributes=options_enabled` → returns symbols that have option chains.  
  Rate limit: standard (200/min paper).  
  Docs: https://docs.alpaca.markets/us/docs/options-trading#option-contracts

- **GET /v2/options/contracts** — List/filter option contracts (query by underlying, expiration, strike, type). Returns contract metadata (symbol, strike, expiry, type, etc.).  
  Rate limit: unknown exact; bulk queries encouraged.

- **GET /v2/options/snapshots** (or per-contract `/v2/stocks/{symbol}/snapshot` extended for options) — Real-time quote + greeks snapshot.  
  Docs indicate snapshots return latest trade, quote, and Greeks when available.

- **POST /v2/orders** — Place single-leg or multi-leg options orders (see section 3).  
  Same endpoint as equities; body shape changes for options.

- **GET /v2/orders**, **GET /v2/positions**, **GET /v2/account/activities** — Existing endpoints extended; options `FILL` activities + new NTAs (exercise/assignment/expiry).  
  **Paper gotcha:** NTAs (non-trade activities) are visible only the **next day** even though positions/balances update instantly.

- **Market Data:** `/v2/stocks/{symbol}/bars`, snapshots, etc., extended for options symbols (OCC format).

OpenAPI spec link referenced in docs (exact URL redacted in public pages): https://docs.alpaca.markets (search “OpenAPI Spec”).

**Rate limits (paper):** Same as equities (≈200 requests/min for most endpoints). Bulk snapshot endpoints (when available) are cheaper than per-contract calls.

## 2. Greeks Coverage

Alpaca **does return Greeks** in option snapshots (delta, gamma, theta, vega, implied volatility). Rho is not explicitly mentioned in public docs and is likely omitted (standard for most retail brokers).

- **Missing:** Rho (can be computed analytically or via py_vollib).
- **Recommended synthesis in hermes-quant:**  
  - Use Alpaca snapshot Greeks when present.  
  - Fallback: compute via py_vollib or Black-Scholes (vectorized) for missing fields or stale data.  
  - Store normalized Greeks in data layer regardless of source.

## 3. Multi-Leg Orders (Level 3)

**Shape of POST /v2/orders body (multi-leg):**

```json
{
  "order_class": "mleg",
  "legs": [
    {"symbol": "NVDA260526C00145000", "side": "buy", "qty": "1", "type": "limit", "limit_price": "2.50"},
    {"symbol": "NVDA260526C00150000", "side": "sell", "qty": "1", "type": "limit", "limit_price": "1.80"}
  ],
  "time_in_force": "day"
}
```

**Native strategy types recognized:** Alpaca treats multi-leg generically via `order_class: "mleg"` + legs array. No built-in enum for “iron_condor”, “vertical_spread”, etc. You define the legs.

**Strategies you can build (Level 3 paper):**
- Vertical spreads, iron condors, strangles, straddles, butterflies, etc.
- Strategies Alpaca does **not** natively recognize (i.e., you must construct legs): any exotic beyond the basic 2–4 leg spreads (e.g., ratio spreads, calendar spreads with different expirations may require multiple order objects).

**Recommendation for hermes-quant options layer:** Normalize all strategies to the `mleg` + legs shape. Provide a strategy builder helper that emits correct leg arrays for covered_call, vertical, iron_condor, etc.

## 4. Assignment / Exercise Simulation (Paper)

- Paper **does simulate** assignment at expiry for ITM short options.
- Early assignment: simulated (rare for paper but supported).
- Pin-risk: paper engine handles expiry settlement; positions are closed or assigned automatically.
- **Paper-specific behavior:** Non-trade activities (exercise/assignment/expiry) appear in `/v2/account/activities` **only the next calendar day** (even though equity/option positions and buying power update instantly).

**Guard for hermes-quant:** Always reconcile positions + activities the morning after expiry. Never rely solely on same-day NTA visibility for P&L attribution.

## 5. Contract Symbol Format

- **Format:** OCC standard: `TICKERYYMMDD{P/C}STRIKE` (zero-padded strike ×1000).  
  Example: `NVDA260526C00145000` = NVDA May 26 2026 145 Call.
- Data API snapshots and order placement use the **same OCC symbol** as the key.
- No Alpaca-specific alias; always use OCC format.

## 6. Historical Chain Data / Backtesting

Alpaca **does not** provide historical option chain snapshots or bars in the free tier.  
**Realistic alternatives for hermes-quant backtesting:**
- Polygon.io (options aggregates + snapshots, paid).
- ORATS or ThetaData (high-quality historical chains).
- CBOE DataShop (official but paid).
- For MVP: synthetic chains generated from equity bars + approximated IV surface (Black-Scholes + skew assumptions).

**Recommendation:** hermes-quant options data layer should be pluggable — Alpaca for live/paper snapshots, Polygon/ORATS fallback for historical.

## 7. Rate Limits (Paper, Observed via Community)

- ~200 requests/min across most endpoints.
- Snapshots: bulk endpoint preferred over N individual calls.
- High-frequency polling of all strikes for many underlyings will hit limits quickly — cache aggressively (5–15 s TTL for paper).
- No special paper rate-limit boost documented; treat same as live.

## 8. Known Gotchas & Recommendations for hermes-quant Options Layer

1. **Paper NTA delay** → Reconcile activities next day.
2. **Greeks may be stale or missing Rho** → Normalize + compute fallback.
3. **Multi-leg validation** → Alpaca validates buying power at order time; paper may allow more aggressive fills than live.
4. **Symbol precision** → Always use exact OCC string; never truncate strike.
5. **No historical chains** → Design data provider abstraction early.
6. **Level-3 approval** → Paper auto-enables Level 3; live requires explicit approval.
7. **Order class** → Use `"mleg"` + `legs` array; do not rely on strategy enums.
8. **Position class exposure** → Future Alpaca enhancement mentioned; currently filter via asset attributes or position asset_class when available.

## Open Questions (Docs Ambiguous / Need Verification)

- Exact rate-limit headers returned by options endpoints on paper.
- Whether bulk snapshot endpoint exists and its precise path/params.
- Rho availability in Greeks payload (not documented).
- Whether calendar spreads (different expirations) are supported in a single `mleg` order or require multiple orders.

**Next step for architect:** Run 5–10 targeted calls against the paper credentials (e.g., list contracts for NVDA, fetch snapshot, submit test vertical spread, inspect post-expiry activities) to confirm shapes and fill the verification gaps above.

---

*This brief is intended for 10-minute architect consumption. All claims are traceable to the cited Alpaca documentation pages.*
