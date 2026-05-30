# Research: Multi-leg options execution + options-aware risk for Alpaca paper (ADR-0027/0028/0029)

**Date:** 2026-05-30
**Author:** research subagent (deep-work backlog loop)
**Scope:** Verify/refresh the Alpaca options API shape, the alpaca-py multi-leg surface, the
collateral-secured (NOT defined-risk-only) gate formulas, wheel/PMCC mechanics, and the minimal
honest greeks for risk gating. Reconcile against the existing 6-PR plan
(`~/.hermes/plans/2026-05-28_multi-leg-options-implementation.md`) and ADR-0027/0028/0029.
**Posture rails:** paper-only, silence-by-default, deterministic gate is FINAL, every new
capability ships DEFAULT-OFF behind a `HERMES_QUANT_*` flag, all times UTC, `asof = decision/pub time`.

> This brief **supersedes** the 2026-05-23 R1 note (`docs/research/2026-05-23-r1-alpaca-options-api.md`)
> on the points where R1 hedged ("verification not performed"). The R1 note's open questions are now
> answered from current (2025-2026) Alpaca docs + the merged alpaca-py multi-leg PR (#552) + deepwiki.

---

## TL;DR (the 5 load-bearing facts)

1. **Yes, Alpaca paper supports full Level-3 multi-leg options** (paper accounts are auto-approved
   for Level 3). Multi-leg orders use `order_class: "mleg"` + a `legs[]` array, **2-4 legs, options-only**.
   The single-leg covered-call / CSP path is **Level 1**, not multi-leg.
2. **The mleg HTTP body the existing plan codes against is WRONG.** Correct, verified shape (raw REST,
   the path hermes-quant uses): `type` + `limit_price` are at the **outer order level**; each leg has
   `symbol` (OCC-21), `ratio_qty`, `side` (`buy`/`sell`), and `position_intent` ∈
   `{buy_to_open, sell_to_open, buy_to_close, sell_to_close}` — NOT `open`/`close`, and NOT per-leg
   `qty`/`type`/`limit_price`. The plan's golden test (`tests/.../test_multileg_paper.py`) and PR-3a
   sketch must be corrected before coding. ADR-0029's **Amendment 2026-05-24 already has it right** —
   trust the amendment, fix the plan.
3. **Collateral-secured gate formulas (mirror Alpaca's own broker validation, fail-closed BEFORE the
   order):**
   - **Covered call:** admit iff `held_shares[underlying] >= 100 * contracts`. (Alpaca error otherwise:
     `insufficient underlying qty available for covered call (required: 100 ...)`.)
   - **Cash-secured put:** admit iff `options_buying_power >= (strike * 100 * contracts) - premium_received`.
     (Alpaca error: `insufficient options buying power for cash-secured put (required: 20310, available: 9395)`.)
   - **Defined-risk spread (vertical/condor/butterfly):** admit iff `max_loss = (width - net_credit) * 100 * contracts`
     is finite and `<= caps`. The ONLY reject-as-naked case: a short leg with neither covering stock,
     cash collateral, NOR a wider long leg.
4. **`open_interest` is NOT on the snapshot/chain greeks payload.** The option-chain / snapshot endpoint
   returns `latest_trade`, `latest_quote`, `greeks{delta,gamma,theta,vega,rho}`, and `implied_volatility`.
   `open_interest` (+ `open_interest_date`, `close_price`, `size`) lives on the **`/v2/options/contracts`
   contract-metadata** endpoint. Liquidity filtering needs BOTH calls. (Note: Alpaca DOES return `rho`,
   contradicting R1 §2's "rho omitted" claim.)
5. **Calendar / different-expiration spreads ARE supported** in a single mleg order (Alpaca's own Level-3
   launch lists "credit, debit, and calendar" spreads). This **resolves ADR-0029 Open Question 1** — keep
   `calendar_spread` in the supported recipe list. **No historical option chains** in any Alpaca tier
   (R1 §6 still holds) → options backtest stays deferred (ADR-0028 D4); the paper loop is live-snapshot-only.

---

## 1. Alpaca options API — concrete shapes

### 1.1 Endpoints (base `https://paper-api.alpaca.markets` trading, `https://data.alpaca.markets` data; auth `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`)

| Endpoint | Purpose | Key response fields |
|---|---|---|
| `GET /v2/assets?attributes=options_enabled` | which underlyings have chains | asset list |
| `GET /v2/options/contracts?underlying_symbols=NVDA&expiration_date=...&strike_price_gte=...&type=call` | **contract metadata** | `symbol` (OCC-21), `strike_price`, `expiration_date`, `type`, `style`, `open_interest`, `open_interest_date`, `close_price`, `size`, `status` |
| `GET /v2/options/snapshots/{underlying}` (or `?symbols=...`) | **live snapshot per contract** | `latest_trade`, `latest_quote{bid,ask,bid_size,ask_size}`, `greeks{delta,gamma,theta,vega,rho}`, `implied_volatility` |
| `GET /v1beta1/options/snapshots/{underlying}` (chain form) | full chain snapshot | dict keyed by OCC symbol → same snapshot shape |
| `POST /v2/orders` | single-leg (CC/CSP/long call/put) AND multi-leg (`mleg`) | order obj w/ `id`, `status`, `legs[]` |
| `GET /v2/account` | options BPR field | **`options_buying_power`** (distinct from equity `buying_power`) |
| `GET /v2/positions` | open positions, OCC-keyed for options | qty, avg_entry_price, asset_class=`us_option` |
| `GET /v2/account/activities` | fills + **NTAs** | `OPEXP` (expiry), `OPASN` (assignment), `OPXRC` (exercise) — **next-calendar-day only** on paper |

**Rate limit:** ~200 req/min paper (same as equities). Prefer the chain/bulk snapshot over per-contract.
Cache 5-15 s TTL.

### 1.2 OCC-21 symbol format (confirmed)

`ROOT(≤6, left-justified) + YYMMDD + {C|P} + STRIKE*1000 zero-padded to 8`.
Example: `NVDA260526C00145000` = NVDA 2026-05-26 $145.00 Call. The plan's `occ.py` format/parse +
Decimal-strike + 30-row fuzz test (Task A1) is correct and unchanged by this research. **Use `Decimal`
for strike construction** — `145.005` in float rounds wrong after `*1000`.

### 1.3 Multi-leg order — VERIFIED correct shape (raw REST)

This is the **canonical iron-condor body** straight from `docs.alpaca.markets/us/docs/options-level-3-trading`:

```jsonc
POST /v2/orders
{
  "order_class": "mleg",
  "qty": "1",                 // spread quantity (outer)
  "type": "limit",            // OUTER level — net debit/credit type
  "limit_price": "1.80",      // OUTER level — the ONE net price the operator approves
  "time_in_force": "day",
  "legs": [
    {"symbol": "AAPL250117P00190000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
    {"symbol": "AAPL250117P00195000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
    {"symbol": "AAPL250117C00205000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
    {"symbol": "AAPL250117C00210000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"}
  ]
}
```

Constraints (verified):
- **2-4 legs**, each leg a **unique** OCC symbol, **options-only** (no equity leg inside an mleg order).
- `position_intent` per leg ∈ `{buy_to_open, buy_to_close, sell_to_open, sell_to_close}`.
- `ratio_qty` is the leg multiplier (`"2"` for ratio spreads); concrete contracts = `ratio_qty * outer qty`.
- `type` + `limit_price` are **outer-level** (the legs inherit; per-leg `type`/`limit_price` come back NULL).
- **Calendar/diagonal (different expirations across legs) is supported** (resolves ADR-0029 OQ1).
- A `200` only means the request was *received*; you MUST poll `GET /v2/orders/{id}` for fill state
  (Alpaca forum confirms async fills on paper too) — feeds the order-lifecycle state machine the
  six-model critique demands.

### 1.4 alpaca-py SDK surface (if ever preferred over raw HTTP)

The plan stays **HTTP-direct** (consistent with the existing hourly-tick), which is correct. For
reference / golden-test cross-check, the alpaca-py equivalent (merged PR #552):

```python
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionIntent

order = LimitOrderRequest(
    qty=1, limit_price=1.80, time_in_force=TimeInForce.DAY, order_class=OrderClass.MLEG,
    legs=[
        OptionLegRequest(symbol="AAPL250117P00190000", ratio_qty=1,
                         side=OrderSide.BUY,  position_intent=PositionIntent.BUY_TO_OPEN),
        OptionLegRequest(symbol="AAPL250117P00195000", ratio_qty=1,
                         side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
    ],
)
```

`OptionLegRequest` fields: `symbol`, `ratio_qty: float`, `side: Optional[OrderSide]`,
`position_intent: Optional[PositionIntent]` (at least one of side/intent required).
`PositionIntent` enum = `BUY_TO_OPEN / BUY_TO_CLOSE / SELL_TO_OPEN / SELL_TO_CLOSE`.

Chain snapshot: `OptionHistoricalDataClient.get_option_chain(OptionChainRequest(underlying_symbol="AAPL"))`
→ dict of `OptionsSnapshot{symbol, latest_trade, latest_quote, implied_volatility,
greeks: OptionsGreeks{delta,gamma,theta,vega,rho}}`. **`OptionsSnapshot` has no `open_interest`** — get
it from `TradingClient.get_option_contracts(...)`.

---

## 2. Collateral-secured vs defined-risk gate (ADR-0027, corrected posture)

The six-model critique's Gemini catch is correct and load-bearing: a strict **defined-risk-only** gate
(`max_gain is None → reject`) rejects **every CC and CSP** — the exact strategies the whole effort
exists to enable. The gate must classify each structure into one of THREE admissible buckets, else reject:

### 2.1 The three admissible buckets + the formulas (mirror Alpaca's broker validation, run BEFORE the order, fail-closed)

| Bucket | Admit iff | Collateral / max-loss formula | Alpaca level |
|---|---|---|---|
| **Covered call (CC)** | `held_shares[underlying] >= 100 * contracts` | covered; "risk" is stock basis − premium down to 0; gate on share sufficiency, NOT raw max-loss | L1 |
| **Cash-secured put (CSP)** | `options_buying_power >= (strike*100*contracts) - premium_received` | `BPR_csp = strike*100*contracts - premium_received` | L1 |
| **Defined-risk spread** (vertical, iron condor, butterfly) | `max_loss <= caps` | debit vertical: `max_loss = net_debit*100*c`; credit vertical / condor / fly: `max_loss = (width - net_credit)*100*c` | L3 |

**The ONLY reject-as-naked case:** a short option leg with neither (a) ≥100 covering shares per contract,
(b) `strike*100` cash collateral, NOR (c) a wider long leg capping the loss. v0.5.0 keeps "no naked"
(ADR-0027 D2 O2).

> **NOTE — covered call is NOT a single mleg order.** mleg legs are options-only. A covered call is either
> (a) own 100 shares (equity order, separate) THEN sell 1 call (single-leg L1 option order, `qty/side/type`),
> or (b) the broker's covered-call validation on a lone short-call order that checks your existing share
> inventory. The plan's PR-3a golden test that puts a `{"symbol":"NVDA","side":"buy","qty":"100"}` equity
> leg inside an mleg `legs[]` array will be **rejected by Alpaca**. Correct it: CC/CSP = single-leg option
> order path; only true spreads (≥2 option legs) use mleg.

### 2.2 BPR per structure (the binding constraint, ADR-0027 D1 O6 / D2)

- **Long call/put / LEAPS / long vertical (debit):** `BPR = premium_paid` (= max loss).
- **CSP:** `BPR = strike*100*contracts - premium_received` (Alpaca's `required_options_buying_power`).
- **CC:** `BPR = 0` incremental options-BP (the 100 shares are already the collateral; selling the call
  *adds* premium credit). Gate on share inventory.
- **Credit vertical / iron condor:** `BPR = (width - net_credit)*100*contracts` = max loss.
- **Gate buffer (ADR-0027 D2):** silence if `total_BPR + new_BPR > 0.80 * NAV`; kill-switch at 0.95.

### 2.3 Net-greeks aggregation for a 2-leg (and N-leg) spread

```python
def aggregate_net_greeks(legs):  # ADR-0029 D5 / ADR-0027 D6
    net = NetGreeks(0,0,0,0)
    for leg in legs:
        n = leg.contracts  # ratio_qty * order_qty
        sgn = +1 if leg.side == "buy" else -1   # SHORT flips sign
        net.delta += sgn * leg.greeks.delta * n
        net.gamma += sgn * leg.greeks.gamma * n
        net.theta += sgn * leg.greeks.theta * n
        net.vega  += sgn * leg.greeks.vega  * n
    return net  # *100 for $-per-$1-move when applying caps
```

Worked 2-leg debit call vertical (buy 30Δ / sell 18Δ, same expiry): `net_delta = +0.30 - 0.18 = +0.12`
(materially < naive sum 0.48 → the whole reason ADR-0027 mandates net-greeks before per-position checks).
PMCC: `net_theta = long_theta − short_theta` where short_theta is negative, so net is typically
**positive** (collect decay) — see `shadow/pmcc.py:mark_pmcc`, which already implements this correctly.

Caps (ADR-0027 D2 defaults, regime-conditional per critique): `|net_delta*spot| <= 0.50*NAV`;
`|net_vega * 1pt| <= 0.10*NAV`.

---

## 3. Wheel / PMCC mechanics (leg definitions + roll logic)

### 3.1 Wheel state machine (CSP → assignment → CC → called-away → CSP)

```
[FLAT] --sell CSP--> [SHORT_PUT]
   ^                     | put expires OTM (keep premium) --> back to FLAT (re-sell CSP)
   |                     | put assigned ITM --> acquire 100sh @ strike
   |                     v
[CALLED_AWAY] <--call assigned ITM-- [LONG_STOCK + SHORT_CALL] <--sell CC-- [LONG_STOCK]
   |  (shares sold @ call strike)            ^  call expires OTM (keep premium) --> re-sell CC
   +--re-sell CSP--> [SHORT_PUT]
```

Gate invariant (ADR-0027 D4 row 3 / D7): **exactly ONE active option leg per underlying** — a CSP OR a
CC, never both. When a CC-methodology and a CSP-methodology both fire on the same name, the gate tags
`composite_intent="wheel"` and budgets collateral ONCE (CSP cash and CC stock-basis don't double-count).

### 3.2 PMCC (poor-man's covered call) — already shadow-tracked

2 legs: **long deep-ITM LEAPS call** (≈80Δ, the synthetic 100 shares) + **rolling short near-dated OTM
call** (≈20-30Δ). Net debit = `(long_premium - short_credit)*100`. Already modeled in
`hermes_quant/shadow/pmcc.py` (`PMCCPosition`, `mark_pmcc`, net_delta/net_theta to Black-Scholes). It
writes nothing to executions — it's the counterfactual validation harness that **activates implicitly
once the multi-leg reactor lands**. PMCC is a 2-option-leg **mleg** order (long+short call).

### 3.3 Roll logic (the rolling short leg)

A roll = `buy_to_close` the current short + `sell_to_open` the next one, ideally as ONE mleg order so it
fills atomically (no naked window). Triggers: short leg ≤ 21 DTE, OR short leg breached (deep ITM →
assignment/pin risk), OR target % of max profit captured. ADR-0029 OQ2 (pin-risk auto-close at 15:30 ET
vs let-it-settle) stays an empirical question for the first 30 days of paper data.

---

## 4. Minimal honest greeks for risk gating

The gate does NOT need a full risk engine — it needs three honest numbers per structure (ADR-0027):

1. **Net delta** (`Σ sgn*delta*100*n`) → directional stock-equivalent exposure → `|net_delta*spot| <= cap*NAV`.
   This replaces the broken `signal.direction * notional` linear sizing.
2. **Net theta (per-day)** → income/decay sign sanity (CC/CSP/PMCC should be net-positive theta;
   a "covered call" coming back net-negative theta is a structural bug).
3. **Defined max-loss + BPR** (the §2 formulas) → the hard collateral/sizing gate. Max-loss is the
   numerator for contract-count sizing: `contracts = floor(target_nav / max_loss_per_contract)`.

**Greek source:** trust Alpaca snapshot greeks when fresh (`asof` within ~60 s of quote); else compute via
the **already-vendored optlib kernel** (`hermes_quant/options/pricing/`, exposed by
`hermes_quant/options/greeks.py:european_greeks/american_greeks/implied_vol`). Do NOT add `py_vollib`
as a dep — ADR-0028 D3's `py_vollib` reference is superseded by the vendored optlib (AGENTS.md "what NOT
to build: custom options pricing engine (optlib vendored)"). US equity options are American-style; optlib's
`american()` (Bjerksund-Stensland) is available for deep-ITM short-DTE where European under-prices.
Fail-closed: if `mid<=0`, `dte<=0`, or `spot<=0` → raise / silence; never return zero-greeks.

---

## 5. Reconciliation with the existing 6-PR plan + ADR-0027/0028/0029

The plan is **structurally sound** and the architecture (Phase A data layer → Phase B gate → Phase C
reactor → Phase D observation) is right. Corrections / confirmations:

| # | Plan/ADR location | Status | Action |
|---|---|---|---|
| R1 | Plan PR-3a `test_mleg_order_request_shape_covered_call` golden body (`position_intent:"open"/"close"`, per-leg `qty`/`type`/`limit_price`, equity leg inside mleg) | **WRONG** — 422 against live API | Rewrite golden test to §1.3 shape; `type`/`limit_price` outer; `position_intent` ∈ buy/sell_to_open/close; NO equity leg in mleg |
| R2 | ADR-0029 **Amendment 2026-05-24** (position_intent + ratio_qty + outer type/price) | **CORRECT** | Trust the amendment over the plan's PR-3a sketch and over R1 §3 |
| R3 | Plan A2 `OptionLeg.position_intent: Literal["open","close"]` | **WRONG values** | Use `buy_to_open/buy_to_close/sell_to_open/sell_to_close` (matches ADR amendment) |
| R4 | ADR-0027 corrected to **collateral-secured** (Phase B header) | **CORRECT** | Implement §2.1 three-bucket classifier; CC=share-check, CSP=options_BP-check |
| R5 | Covered call as a single mleg order with an equity leg | **WRONG** | CC/CSP = **single-leg L1** option order path; only ≥2-option-leg spreads use mleg (§2.1 NOTE) |
| R6 | ADR-0028 D3 `py_vollib` for greek completion | **SUPERSEDE** | Use vendored optlib (`options/greeks.py`); do not add py_vollib dep |
| R7 | ADR-0029 OQ1 "calendar spreads in one mleg?" | **RESOLVED: yes** | Keep `calendar_spread`; verify once with a sandbox probe (Test Plan item 5) |
| R8 | ADR-0028 D1 `open_interest` on snapshot | **WRONG source** | OI from `/v2/options/contracts`, not the snapshot; chain reader must join both |
| R9 | R1 §2 "rho omitted" | **OUTDATED** | Alpaca returns rho in `greeks`; rho-completion fallback no longer needed |
| R10 | ADR-0028 D4 / R1 §6 no historical chains | **STILL TRUE** | Options backtest deferred; paper loop is live-snapshot-only; `live_allowed:false` until paid provider |
| R11 | Plan Phase B/C default-on assumption | **RAIL** | Ship the whole reactor DEFAULT-OFF behind `HERMES_QUANT_MULTILEG=0`; flip is operator's call after eval |

**Open questions the plan must still resolve (unchanged):** strike-selection delta per recipe
(CC sell ~0.20-0.30Δ?), exact net-greeks caps, which playbook plays go multi-leg vs equity (rec: CC/CSP/
wheel/PMCC multi-leg-or-L1; swing equity; LEAPS the call), and whether to trust Alpaca's paper
`options_buying_power` or shadow-track BPR (rec: gate on our own §2.2 formula, reconcile against
`options_buying_power` for drift — fail-closed if ours says reject).

**Sequencing note (six-model 6/6 verdict, from the wiki):** the paper→live **fidelity foundation**
(admissibility/ShortabilityOracle, order-lifecycle state machine, exactly-once idempotency,
borrow-aware P&L) should land BEFORE options fire live influence, so options don't "stack a bigger lie on
the unfixed short-book lie." Building the options rail DEFAULT-OFF in parallel is fine and safe; the
*flip* waits on the fidelity layer + the 60-day paper evidence window (ADR-0029 D7).

---

## Sources

- Alpaca docs: Options Trading Overview, Options Level 3 Trading, Get Option Contracts, Option chain,
  Snapshots, Create an Order, Margin & Short Selling (docs.alpaca.markets, 2025-2026).
- Alpaca blog "Multi-Leg (Level 3) Options Trading Now Available" + support pages (paper auto-Level-3;
  calendar spreads listed).
- alpaca-py: merged PR #552 (multi-leg), `OptionLegRequest`/`PositionIntent`/`OrderClass.MLEG`,
  `OptionHistoricalDataClient.get_option_chain`, `OptionsSnapshot`/`OptionsGreeks` (deepwiki
  alpacahq/alpaca-py + SDK API reference).
- Alpaca community forum: async fill semantics, multi-leg protocol.
- Internal: `docs/adr/ADR-0027/0028/0029`, `docs/research/2026-05-23-r1-alpaca-options-api.md`,
  `~/.hermes/plans/2026-05-28_multi-leg-options-implementation.md`, `hermes_quant/options/greeks.py`,
  `hermes_quant/shadow/pmcc.py`, six-model critique (via `docs/research/2026-05-30-understanding-wiki.md`).
```
