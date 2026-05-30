# Research — Pre-trade admissibility & short-sale mechanics (ShortabilityOracle)

> Date: 2026-05-30 · Lens: make paper "as accurate as live" · Status: research input for an
> upcoming ADR (ShortabilityOracle + admissibility engine, **default-OFF** behind a
> `HERMES_QUANT_*` flag, eval-gated before any live influence).
>
> **Why this matters (the P0):** the book currently holds **38 synthetic SHORT equity
> positions that would be UNTRADEABLE live** — no locate, no borrow, several almost certainly
> HTB/NTB or non-marginable small-caps. A 6-model critique unanimously flagged this as the
> true P0: the sim is silently booking P&L on shorts a real Alpaca account would *reject at
> submission* or *auto-cancel overnight*. The fix is a pre-trade **admissibility gate** that
> answers "could this order even be accepted live?" *before* the deterministic risk gate
> (ADR-0004) sees it, plus a **borrow-aware carry cost** so short P&L isn't fictitiously free.
>
> Posture note: this is **evidence/admissibility**, not authority. Per the rails, an
> admissibility check may only *silence* (reject/flatten → 0.0), never amplify. It sits
> upstream of the risk gate as a hard, deterministic precondition.

---

## TL;DR (the minimal honest model)

1. **Alpaca shortability is a single boolean check.** On the asset endpoint
   (`GET /v2/assets/{symbol}`, `TradingClient.get_asset()`), the live source of truth is
   **`easy_to_borrow: bool`** — Alpaca's own docs say it is *"the best way to check whether the
   name is currently available to short at Alpaca."* Alpaca **only opens shorts in ETB names**;
   an ETB→HTB flip **auto-cancels open short orders before market open**. There is **no
   `shortable_shares` quantity field** (unlike Lean/IBKR). Whole shares only — **fractional
   shorts are rejected (HTTP 422)**.

2. **Admissibility states are deterministic and checkable pre-submission.** An order is
   REJECTED if: not shortable (`easy_to_borrow=False` / `shortable=False`), fractional-short,
   insufficient buying power (Alpaca prices an opening short at
   `MAX(limit, 1.03×ask) × qty` against BP), PDT 4th-day-trade under \$25k equity, or account
   equity < \$2,000 (no margin/short at all). PARTIAL = marketable but liquidity-constrained.

3. **Borrow carry is a daily accrual on notional; dividends-on-short are a liability.**
   Minimal honest model: `daily_borrow = short_mkt_value × annual_cbr / 360`, accrued every
   *calendar* day held (weekend = 3 days on Friday). ETB ≈ 0.25–1% APR; HTB ≈ 5–300%+ APR.
   Separately, on ex-div the short owes a **payment-in-lieu (PIL)** = `shares × dividend/share`
   debited on pay date. Reg-T short BPR ≈ **150% of market value** (100% covered by proceeds +
   50% initial margin); Alpaca maintenance is higher of \$5/sh-or-30% (≥\$5 stock).

4. **Canonical reference design = QuantConnect Lean `IShortableProvider`.** Three methods:
   `ShortableQuantity(sym,t) → null (unrestricted) | 0 (NTB) | int`, `FeeRate(sym,t)`,
   `RebateRate(sym,t)`; default is `NullShortableProvider` (infinite — *our current bug*).
   Orders exceeding availability are rejected with `ExceedsShortableQuantity`. **Adopt this
   shape:** a `ShortabilityOracle` with `(admissible, reason, borrow_cbr)`, seeded from Alpaca's
   `easy_to_borrow` (live) or a static ETB allowlist (offline/backtest), default-OFF.

---

## 1. Short-sale mechanics: ETB / HTB / NTB, locate, borrow fee, Alpaca exposure

### Classification (FinWiz, Pomegra, IBKR)
| Class | Locate required | Typical CBR (annual) | Availability | Examples |
|---|---|---|---|---|
| **ETB** (easy-to-borrow) | No — auto-locate | 0 / 0.25–1.00% | abundant | AAPL, MSFT, SPY, most S&P 500 |
| **HTB (mild)** | Yes | 1–10% (+ \$0.01–0.10/sh locate) | limited | mid-caps w/ elevated short interest |
| **HTB (severe)** | Yes | 20–300%+ (+ \$0.50–5.00+/sh locate) | scarce | low-float / squeeze / meme names |
| **NTB** (not-borrowable) | — | n/a | none | broker can't source a locate → short cannot execute |

- **Locate requirement** is regulatory, not a courtesy: SEC **Reg SHO Rule 203(b)** requires the
  broker have "reasonable grounds to believe the security can be borrowed … *before effecting*
  a short sale," documented *prior* to execution. (Source: SEC "Key Points About Regulation
  SHO", sec.gov/investor/pubs/regsho.htm.) An ETB list *is* the broker's standing locate.
- **Cost-to-borrow (CBR / borrow fee)** is an APR set by stock-loan supply/demand, anchored to a
  **General Collateral (GC)** reference rate (~0.05–0.50% for liquid names). It is *separate*
  from any per-share locate fee. HTB rates float daily, intraday during squeezes.
- **ETB lists change daily** — a name ETB today can be HTB tomorrow if short demand spikes. So
  shortability is a *point-in-time* property, not a static attribute (matters for `asof`).

### Alpaca API — exact fields (CONFIRMED, load-bearing)
`GET /v2/assets/{symbol_or_asset_id}` → `Asset` model. In `alpaca-py` this is
`alpaca.trading.models.Asset` (`alpaca/trading/models.py`), queried via
`TradingClient.get_asset(symbol)` or `TradingClient.get_all_assets(GetAssetsRequest(...))`.

```python
class Asset(BaseModel):
    id: UUID
    asset_class: AssetClass = Field(alias="class")
    exchange: AssetExchange
    symbol: str
    name: Optional[str] = None
    status: AssetStatus            # active / inactive
    tradable: bool                 # can be traded at all
    marginable: bool               # can be held on margin (short REQUIRES this)
    shortable: bool                # asset *can* be shorted
    easy_to_borrow: bool           # ETB right now — the live shortability gate
    fractionable: bool             # fractional shares allowed (long only for sells)
```

REST also returns (per `GET /v2/assets` OpenAPI schema, docs.alpaca.markets):
- `margin_requirement_long: str` and **`margin_requirement_short: str`** — per-asset margin %
  (equities only). `maintenance_margin_requirement` is **deprecated** in favor of these.
- `attributes: list[str]` — enum incl. `ptp_no_exception` (PTP without qualified notice; blocked
  from purchase by default, 10% withholding risk for non-US), `ptp_with_exception`, `ipo`
  (limit-only pre-listing), `has_options`, `options_late_close`, `fractional_eh_enabled`,
  `overnight_tradable`, `overnight_halted`. The endpoint accepts an `attributes=` filter.
- **No `shortable_shares` / shortable-quantity field exists.** Alpaca exposes shortability as a
  *boolean*, not a borrowable-share count. (Confirmed via deepwiki on `alpacahq/alpaca-py` and
  the public OpenAPI; the Rust `alpaca-websocket` struct mirrors the same 5 bools.)

**Alpaca's own guidance (docs.alpaca.markets/reference/getassets):**
> *"filtering for `easy_to_borrow = True` is the best way to check whether the name is currently
> available to short at Alpaca."*

**Alpaca operational short rules (docs.alpaca.markets/us/docs/margin-and-short-selling) —
directly explains our 38-position bug:**
- *"Alpaca currently only supports opening short positions in easy-to-borrow (ETB) securities."*
- *"Any open short order in a stock that changes from ETB to HTB overnight will be automatically
  cancelled prior to market open."*
- HTB you already hold short is **not force-closed** unless the lender recalls — but you **incur a
  daily stock borrow fee**, and *"We do not currently provide HTB rates via our API."*
- **Margin/short requires ≥ \$2,000 account equity** (else 1× BP, no short).

→ **Takeaway:** the honest live-admissibility predicate at Alpaca is
`tradable AND marginable AND shortable AND easy_to_borrow AND (qty is whole)`. Anything failing
that is exactly what live would reject — and is the set our 38 synthetic shorts violate.

---

## 2. Reg-T / BPR, whole-share enforcement, SSR

### Reg-T short initial margin / BPR
- **Reg T (12 CFR §220.12(c)) + FINRA 4210:** short sale of a non-exempt equity requires
  **150% of current market value** at initiation. Mechanically: short *proceeds* cover 100%, and
  the customer posts **50% additional initial margin**. (Sources: Cornell LII e-CFR 12 CFR
  220.12; Investopedia short-margin; innreg FINRA 4210.)
  - Example: short 1,000 @ \$10 ⇒ value \$10,000 ⇒ requirement \$15,000 (\$10k proceeds + \$5k IM).
- **Minimal BPR model for the sim:** `bpr_short = 1.50 × short_market_value` for an initial open;
  treat that as the buying-power consumed. (Maintenance is lower; Alpaca EOD maintenance for
  shorts is `max($5/sh, 30%)` for ≥\$5 names, `max($2.50/sh, 100%)` for <\$5 — penny/sub-\$5
  shorts are *very* expensive and small-cap shorts are the realistic blockers.)
- **Alpaca opening-short BP check (docs "Placing Orders"):** the order's *calculated value* =
  `MAX(limit_price, 1.03 × current_ask) × qty` (market short uses `1.03 × ask × qty`). Compared
  vs available BP; **open orders also consume BP** until filled/cancelled; buy-to-cover does
  **not** replenish BP until executed.

### Whole-share enforcement (no fractional shorts)
- Alpaca: *"We do not support short sales in fractional orders. All fractional sell orders are
  marked long."* A fractional order that would open/increase a short → **HTTP 422
  `fractional orders are not allowed to short`** (alpaca-trade-api issue #405; "30 Common
  Errors" #21). Also a sequence of two sells out-of-hours that would net short is rejected.
- → **Sim rule:** short size must be an integer share count; reject (or floor-to-0) any
  fractional short. This also interacts with our discrete NAV ladder: converting a
  `±0.05…±0.20 × NAV` target into shares must `floor()` to whole shares for shorts.

### SSR — Short-Sale Restriction (Reg SHO Rule 201, "alternative uptick rule")
- **Trigger:** intraday decline **≥ 10% from the prior day's official close**. Detected by the
  listing exchange the instant the threshold is breached. (SEC Reg SHO; NYSE Reg SHO Resource
  Guide; OFR WP 23-08.)
  - `ssr_trigger_price = prev_close × 0.90`; SSR active once a regular-hours print ≤ that.
  - **Prior-close basis only** — pre/post-market lows do **not** trigger it.
- **Duration:** rest of the trigger day **+ all of the next trading day**.
- **Effect:** short sells may only execute **above the National Best Bid** (non-marketable
  short only). Does *not* ban shorting; constrains *execution price/timing*. Long sales
  unaffected.
- **Detection in our sim (deterministic, from bars we already have):**
  `ssr_active(t) = (intraday_low_since_open(t) ≤ prev_close × 0.90)`, latched until next
  session close. Honest minimal handling: when SSR is active, a *marketable* (at/below-bid)
  short is non-fillable → model as **partial/deferred fill or reject**, never a guaranteed
  hit-the-bid fill. (We don't have NBB tick data, so the conservative posture is: flag SSR and
  treat marketable shorts as not-immediately-fillable.)

---

## 3. Borrow-aware P&L: daily borrow accrual + dividend-on-short

### Daily borrow fee accrual (the minimal honest carry model)
Borrow fee is an APR on **current short market value**, accrued daily (Pomegra; IBKR
short-sale-cost page):
```
daily_borrow_fee = short_market_value × annual_cbr / DAY_COUNT
short_market_value = |short_shares| × close_price   # marks daily → rising price ⇒ higher fee
```
- `DAY_COUNT`: stock-loan convention is **/360** (money-market basis); some retail docs quote
  /365 or /252. Use **/360** as the default for fidelity; the choice is small vs CBR uncertainty.
- Accrues on **calendar days held**, including weekends/holidays (Friday settlement carries
  **3 days** of fee). IBKR: "fees are charged based on settled positions."
- Example: short \$100k @ 2% APR ⇒ \$100k×0.02/360 ≈ **\$5.56/day**; ≈ \$167/30d. At HTB 50% ⇒
  ≈ \$139/day — alpha-destroying, which is the whole point of modeling it.
- **CBR source tiers (honest degradation):**
  1. **ETB → low fixed CBR** (e.g. 0.30% APR default for `easy_to_borrow=True`).
  2. **HTB / not-ETB → punitive default** (e.g. ≥ 30% APR sentinel) *and/or* mark inadmissible —
     because Alpaca *won't open* the position anyway, so for our purposes not-ETB ⇒ reject.
  3. Alpaca does **not** expose HTB rates via API, so any HTB CBR is necessarily an estimate; the
     fidelity-honest move is to refuse the position rather than fake a precise fee.

### Dividend-on-short liability (payment-in-lieu / PIL)
- A short seller is **not** entitled to the dividend and **must pay it to the lender**: on the
  dividend **pay date** the account is debited `|short_shares| × dividend_per_share` (Schwab
  "Ins and outs of short selling"; IBKR PIL glossary; SEC Reg SHO C). PIL is ordinary income to
  the recipient (1099-MISC), but for our paper P&L it's simply a **cash debit on pay date** for
  any short held across the **ex-dividend date**.
- Minimal model: at ex-div, if a short is open, schedule a liability
  `pil = |short_shares| × cash_dividend_per_share` realized on pay date. (Closing before ex-div
  avoids it — a real tactical lever the sim should represent honestly.)

---

## 4. Order admissibility states (ACCEPTED / PARTIAL / REJECTED)

Pre-submission, deterministic, in roughly the order Alpaca applies them. An admissibility engine
should evaluate these and return a typed `reason` on rejection (silence-by-default):

| State | Triggers (any) |
|---|---|
| **REJECTED** | • Not shortable: `shortable=False` OR `easy_to_borrow=False` (opening short). <br>• Not marginable / account equity < \$2,000 (no short capability). <br>• **Fractional short** (non-integer short qty) → 422. <br>• **Insufficient BP:** `MAX(limit,1.03×ask)×qty > available_bp` (open orders already consume BP). <br>• **PDT:** would be 4th day-trade in 5 business days with realtime equity < \$25k (Alpaca paper *simulates* this). <br>• PTP `ptp_no_exception` (blocked by default); `ipo` attribute + non-limit order. <br>• Unsettled-funds / settlement violation (cash-account good-faith). |
| **PARTIAL** | • Marketable but liquidity-constrained (real life). Alpaca **paper** does *not* cap qty vs NBBO size, but **randomly partial-fills ~10% of the time** then re-evaluates the remainder. <br>• SSR active + marketable short → only the non-marketable (above-NBB) slice can fill ⇒ deferred/partial. |
| **ACCEPTED** | All hard gates pass AND (for shorts) `easy_to_borrow=True` AND whole-share AND BP sufficient. |

**Critical paper-vs-live caveats (docs.alpaca.markets/us/docs/paper-trading):**
- Paper **does simulate** PDT checks and BP checks and the ETB-only short rule + overnight
  ETB→HTB auto-cancel. So *much* of admissibility is already enforced live-side by Alpaca.
- Paper **does NOT** charge **Borrow Fees** — the feature table literally lists
  *"Borrow Fees: ⛔️ (Coming Soon!)"* for paper vs ✅ for live. **This is exactly our fidelity
  gap: paper short P&L is missing the carry, so we must model it ourselves.**
- Paper does **not** check order qty against NBBO size (overstated fills possible).

→ Two distinct fidelity holes to close: **(a)** the 38 shorts likely violate the ETB-only rule
(live would reject/auto-cancel) — an *admissibility* bug; **(b)** even admissible shorts accrue
**zero borrow fee in paper** — a *carry* bug. The ShortabilityOracle fixes both.

---

## 5. How other open-source sims model shortability / borrow

| Project | Shortability model | Borrow / carry cost | Verdict for us |
|---|---|---|---|
| **QuantConnect Lean** | **`IShortableProvider`** (canonical). `ShortableQuantity(sym,t)→ null=unrestricted / 0=NTB / int=cap`; `FeeRate`, `RebateRate`. `Security.ShortableProvider`; `IBrokerageModel.GetShortableProvider`. Default `NullShortableProvider` = **infinite** (⇐ our current de-facto state). `LocalDiskShortableProvider` reads CSV per-symbol-per-date; `InteractiveBrokersShortableProvider` ships real IBKR data. | `FeeRate` = borrow APR; `RebateRate` = benchmark − fee; charged via `ShortMarginInterestRateModel` (`IMarginInterestRateModel`). | **Adopt this interface shape.** Order rejected with `OrderResponseErrorCode.ExceedsShortableQuantity` when qty > availability or NTB. This is the reference design. |
| **Interactive Brokers (model)** | ETB vs HTB; real shortable-shares feed. | Borrow fee vs short-proceeds interest ⇒ **net rebate** (can be negative); `cost = collateral × fee_rate`, daily, settled positions. | Source for the daily-accrual formula + day-count. |
| **backtrader** | No shortability gate (assumes you can short). | `CommInfo` credit/interest scheme charges interest on short stock & ETFs (per-day on cash). | Carry-cost pattern only; no admissibility. |
| **vectorbt** | Shorting supported via negative size; **no borrow/locate model** (open issue #11). | none | Don't copy — it's the "free short" trap we're fixing. |
| **nautilus_trader** | No CTB/locate concept. *Does* reject shorts on a **CASH** account (short only allowed on **MARGIN**); Bybit spot-margin auto-borrows. | Margin/borrow only via exchange integration; no generic CTB. | Confirms "account-type gate" is necessary-but-insufficient; we need the ETB/CTB layer on top. |
| **RustyBT** (zipline-lineage) | — | Dedicated "Borrow Costs & Financing" module (daily borrow accrual). | Confirms daily-accrual borrow cost is the standard minimal carry model. |

**Minimal honest model (synthesis):** copy **Lean's `IShortableProvider` tri-state contract**,
seeded from Alpaca's `easy_to_borrow` boolean (which is broker truth) rather than a phantom
shares-count we can't get. Since Alpaca only opens ETB shorts and doesn't expose HTB rates, the
honest mapping is binary at the admissibility layer (ETB-admissible / else-reject), with a daily
borrow accrual on the admissible set.

---

## Proposed shape (for the ADR — NOT built here, default-OFF)

```python
# hermes_quant/admissibility/shortability.py   (sketch — gated by HERMES_QUANT_ADMISSIBILITY)
@dataclass(frozen=True)
class ShortabilityVerdict:
    admissible: bool
    reason: str | None          # "NOT_SHORTABLE" | "NOT_ETB" | "FRACTIONAL_SHORT"
                                # | "NOT_MARGINABLE" | "INSUFFICIENT_BPR" | "PTP_BLOCKED" | None
    annual_cbr: float           # cost-to-borrow APR used for carry accrual (0.0 for longs)

class ShortabilityOracle(Protocol):
    def verdict(self, symbol: str, side: str, qty: int, asof: datetime) -> ShortabilityVerdict: ...

# Implementations (mirror Lean's provider tiers):
#  - AlpacaShortabilityOracle   → live: get_asset(); admissible iff tradable & marginable
#                                  & shortable & easy_to_borrow & whole-share; cbr=ETB default
#  - StaticETBAllowlistOracle   → offline/backtest: point-in-time ETB set + per-name CBR table
#  - NullShortabilityOracle     → today's behavior (everything admissible) == the bug; default
#                                  ONLY when flag is OFF, to preserve current outputs bit-for-bit
```

**Wiring (rails-compliant):**
- Sits **upstream** of the ADR-0004 risk gate as a hard precondition. It can only **silence**
  (reject → no order; or flatten an inadmissible held short → 0.0 multiplier). It can **never**
  amplify or override the gate.
- **Default-OFF** behind `HERMES_QUANT_ADMISSIBILITY=1` (and a separate
  `HERMES_QUANT_BORROW_COST=1` for the carry accrual, so the admissibility gate and the P&L
  carry can be flag-flipped independently). With flags off, `NullShortabilityOracle` preserves
  current behavior exactly — safe construction; the flip is the operator's call.
- **Eval gate before any live influence:** replay the existing 38 synthetic shorts through the
  oracle; expected outcome = the bulk are flagged `NOT_ETB`/`NOT_SHORTABLE` and the borrow
  accrual measurably degrades their fictitious P&L. That side-by-side tick log is the promotion
  artifact (same pattern as ADR-0070/0071 promotion in B12).
- **`asof` honesty:** shortability and CBR are point-in-time. For backtest, the ETB set / CBR
  must be the value *as of decision time*, not today's — else look-ahead. (Alpaca only gives
  *current* `easy_to_borrow`, so historical admissibility needs the static allowlist path or a
  recorded snapshot; document the limitation.)

---

## Concrete formulas (cheat-sheet)

```
# Shortability (live, Alpaca):
admissible_short = asset.tradable and asset.marginable and asset.shortable \
                   and asset.easy_to_borrow and (qty == floor(qty))

# Reg-T short initial BPR:
bpr_short_open = 1.50 * short_market_value          # 100% proceeds + 50% IM

# Alpaca opening-short BP charge (market order):
order_value = 1.03 * current_ask * qty              # limit: MAX(limit, 1.03*ask)*qty

# Daily borrow carry (accrued every calendar day held; Fri = ×3):
daily_borrow = abs(short_shares) * close_price * annual_cbr / 360

# Dividend-on-short liability (debited pay date if short across ex-div):
pil = abs(short_shares) * cash_dividend_per_share

# SSR trigger / latch:
ssr_trigger_price = prev_close * 0.90
ssr_active(t)     = latched_true_once( intraday_low_since_open(t) <= ssr_trigger_price )
                    # active rest-of-day + entire next trading day; marketable shorts non-fillable
```

---

## Sources
- SEC, *Key Points About Regulation SHO* — Rule 201 (SSR), Rule 203(b) locate, dividend-on-short.
  https://www.sec.gov/investor/pubs/regsho.htm
- NYSE, *Short Selling & Reg SHO Resource Guide* (Rule 201 circuit breaker, 10%/prior-close,
  next-day duration). · OFR WP 23-08 (Rule 201 mechanics, NBB constraint).
- Cornell LII e-CFR **12 CFR §220.12(c)** (Reg-T short = 150% MV); FINRA **Rule 4210** (innreg);
  Investopedia *Minimum Margin Requirements for an Equities Short Sale Account*.
- Alpaca docs: *Get Assets / Retrieve All Assets* (Asset schema, `easy_to_borrow` guidance,
  `attributes`, `margin_requirement_short`); *Margin and Short Selling* (ETB-only opens,
  ETB→HTB overnight auto-cancel, \$2k equity, maintenance table, no-HTB-rate-via-API);
  *Placing Orders* (opening-short BP = `MAX(limit,1.03×ask)×qty`); *Paper Trading*
  (PDT sim, random 10% partial fills, **Borrow Fees ⛔ Coming Soon**); *Fractional Trading* +
  *How to Fix 30 Common Errors* #21/#25/#27 (no fractional shorts → 422; insufficient BP 403;
  account-not-allowed-to-short 403). `alpacahq/alpaca-py` `alpaca/trading/models.py::Asset`.
- QuantConnect **Lean**: `IShortableProvider` (`ShortableQuantity`/`FeeRate`/`RebateRate`),
  `NullShortableProvider`, `LocalDiskShortableProvider`, `InteractiveBrokersShortableProvider`,
  `ShortMarginInterestRateModel`, `OrderResponseErrorCode.ExceedsShortableQuantity`.
  (deepwiki QuantConnect/Lean; lean.io class reference.)
- IBKR *Short Sale Cost* (borrow fee vs short-proceeds interest, net rebate, settled basis) +
  *Payment in Lieu of Dividends* glossary. Pomegra *Borrow Fee and Rebate* / *Locating Shares*
  (daily accrual formula, GC rate, day-count). FinWiz *Short Locates* (ETB/HTB table, locate fee).
  Schwab *How to Short a Stock* (dividend deducted on pay date).
- backtrader *Commissions: Credit* (interest on short cash); polakowo/**vectorbt** issue #11
  (no borrow model); **nautilus_trader** (cash-account short reject; no generic CTB — deepwiki);
  RustyBT *Borrow Costs & Financing*.
```
