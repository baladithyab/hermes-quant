# Research: Alpaca PAPER multi-leg options EXECUTION mechanics (N3/B01 reactor build)

**Date:** 2026-05-30
**Author:** research subagent (deep-work backlog loop — N3/B01 multi-leg paper reactor)
**Scope:** Concrete-enough-to-implement mechanics for the ADR-0029 multi-leg PAPER reactor:
the mleg order API + fill response, paper-account fill/assignment behavior, the exact
`PaperReactor` interface the multi-leg reactor must MIRROR, covered-call two-leg state
reconciliation, PMCC shadow validation, and wheel/PMCC roll order construction.
**Companion (does NOT supersede):** `docs/research/2026-05-30-r-options-execution.md` covered the
gate formulas + chain data shape; THIS brief covers the *execution/reactor* side. Where they
overlap (the mleg POST body), this brief confirms the same shape from the alpaca-py SDK source.
**Posture rails (NON-NEGOTIABLE):** paper-only this loop (no live broker order rail — ADR-0029
D7); deterministic gate is FINAL authority and a precondition the reactor CONSUMES, never
bypasses; money via CLI/HITL only, never a tool that auto-fires; whole reactor DEFAULT-OFF behind
`HERMES_QUANT_MULTILEG_REACTOR=1` (set NOWHERE); exactly-once/idempotency (ADR-0078);
admissibility (ADR-0077) applies to the equity legs of a covered call; all times UTC;
`asof = decision/pub time`.

---

## TL;DR (the 5 load-bearing facts)

1. **mleg order shape (confirmed against alpaca-py source + Level-3 docs):** ONE `POST /v2/orders`
   with `order_class:"mleg"`, OUTER `qty` (spread count, REQUIRED), OUTER `type` + `limit_price`
   (`limit_price` POSITIVE = net **debit**, NEGATIVE = net **credit** — the ONE net price HITL
   approves), `time_in_force`, and a `legs[]` array of **2–4 unique-symbol, options-ONLY** legs,
   each `{symbol(OCC-21), ratio_qty, side, position_intent ∈ {buy_to_open|sell_to_open|
   buy_to_close|sell_to_close}}`. Per-leg `type`/`limit_price`/`qty` do NOT exist — legs inherit
   from the outer order. (alpaca-py: `LimitOrderRequest(qty=, limit_price=, order_class=
   OrderClass.MLEG, legs=[OptionLegRequest(symbol=, ratio_qty=, side=, position_intent=)])`.)
2. **Paper fills atomically, but ASYNC.** Multi-leg "fill together or not at all" is the explicit
   point of mleg — paper does NOT leg out / partial-leg an accepted spread. BUT the `200`/`accepted`
   only means *received*; you MUST poll `GET /v2/orders/{id}` for terminal state. Paper simulates
   fills off **real-time NBBO quotes** (a marketable limit fills at/inside the quote); paper-only
   accounts get IEX data and options-chain data can lag ~15 min. The returned `Order` carries a
   `legs[]` list of child `Order` objects, each with **per-leg `filled_avg_price`, `filled_qty`,
   `status`** — that is the per-leg fill detail the reactor records.
3. **The interface to MIRROR** is `PaperReactor.execute(proposal, *, fill_size_pct,
   approver_user_id=None) -> ExecutionRecord` (`react/base.py` `Reactor` Protocol; the scaffold
   `react/multileg.py:MultiLegPaperReactor` already conforms). It appends ONE JSONL line per
   execution to `~/.hermes/quant/executions.jsonl` via `signal_bus.append_locked` (flock+fsync),
   logs, then best-effort calls `PortfolioState.apply_execution(record_dict)` (which writes
   `state.db`, keyed `(account_id, asset_class, symbol)`). Slippage attaches via the
   `HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2` branch (ADR-0070); idempotency (ADR-0078) attaches via a
   stable `client_order_id` / dedup key the writer must set (the `react/order_state.py` machine is
   NOT yet present — define the key now). The equity `ExecutionRecord` is single-instrument; the
   multi-leg reactor must write **one parent record + one child record per leg** (see §3.4).
4. **A covered call reconciles into `state.db` as TWO independent position rows**, exactly as Alpaca
   returns them: `(asset_class="equity", symbol="NVDA", qty=+100)` and
   `(asset_class="us_option", symbol="NVDA260626C00160000", qty=-1)`. Alpaca lists each option leg
   as its OWN `Position` (OCC-keyed, `asset_class=us_option`, signed `qty`) — there is no combined
   "spread position" object. PortfolioState's `(account_id, asset_class, symbol)` PK already supports
   this with ZERO schema change; the reactor just emits the right per-leg `ExecutionRecord`s. The
   PMCC shadow (`shadow/pmcc.py`) marks the SAME two-leg structure to Black-Scholes, so the
   reactor's real per-leg fills validate against the shadow `net_value`/`net_delta`/`net_theta_day`
   counterfactual (§4).
5. **A roll = ONE mleg order** with two legs: `buy_to_close` the current short + `sell_to_open` the
   next short (atomic → no naked window). PMCC and wheel-CC rolls are identical in shape; only the
   strikes/expiries differ. CC/CSP **opening** is NOT an mleg order (options-only legs, ≥2 legs) —
   it is a single-leg L1 option order (`{symbol, qty, side, type, limit_price}`) gated by the
   options gate's CC/CSP collateral bucket; only true ≥2-option-leg structures (vertical, condor,
   PMCC, roll) use mleg.

---

## 1. The Alpaca multi-leg (mleg) order API

### 1.1 Endpoint + shape (raw REST — the path hermes-quant uses)

`POST /v2/orders` on `https://paper-api.alpaca.markets`, auth `APCA-API-KEY-ID` /
`APCA-API-SECRET-KEY`. Canonical 4-leg iron-condor body (Alpaca Level-3 docs):

```jsonc
POST /v2/orders
{
  "order_class": "mleg",
  "qty": "1",                 // OUTER: spread quantity (REQUIRED for mleg)
  "type": "limit",            // OUTER: net debit/credit order type
  "limit_price": "1.80",      // OUTER: POSITIVE = net DEBIT, NEGATIVE = net CREDIT
  "time_in_force": "day",
  "legs": [
    {"symbol": "AAPL250117P00190000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
    {"symbol": "AAPL250117P00195000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
    {"symbol": "AAPL250117C00205000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
    {"symbol": "AAPL250117C00210000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"}
  ]
}
```

Constraints (verified, alpaca-py `OrderRequest` mleg validation + docs):
- **2–4 legs**, each a **unique** OCC-21 symbol, **options-only** (NO equity leg inside an mleg
  `legs[]` — a covered-call equity leg is a *separate* equity order).
- `position_intent` per leg ∈ `{buy_to_open, buy_to_close, sell_to_open, sell_to_close}`. This is
  load-bearing: the broker uses it for margin treatment (a `buy_to_close` releases short collateral;
  a `buy_to_open` does not).
- `ratio_qty` is the leg multiplier (`"2"` for ratio spreads); concrete contracts = `ratio_qty *
  outer qty`.
- OUTER `type` + `limit_price` only; per-leg `type`/`limit_price`/`qty` do not exist (legs inherit;
  they come back NULL on the child leg objects).
- **`limit_price` sign convention (alpaca-py SDK):** for `OrderClass.MLEG`, a **positive**
  `limit_price` = the net **debit** you pay; a **negative** `limit_price` = the net **credit** you
  receive. (This is the SDK's documented convention. Note ADR-0029's amendment example used
  `"1.80"` as a credit-vertical price — when building the body, set the sign from the structure:
  debit structures positive, credit structures negative. RECORD the signed net price as the
  reactor's `fill_price`.)
- **Calendar/diagonal (different expirations across legs) IS supported** in one mleg order (resolves
  ADR-0029 Open Question 1).
- Async: a `200` means *received*; poll `GET /v2/orders/{id}` for `filled`. (Alpaca forum: "All
  orders are simply requests… always query the order status, never assume filled.")

### 1.2 alpaca-py SDK surface (cross-check / if ever preferred over raw HTTP)

The reactor stays HTTP-direct (consistent with the hourly tick), but the SDK is the authoritative
field reference (deepwiki `alpacahq/alpaca-py`, merged PR #552):

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionIntent

order = LimitOrderRequest(
    qty=1,                          # OUTER spread count (required for MLEG)
    limit_price=1.80,               # +debit / -credit
    time_in_force=TimeInForce.DAY,
    order_class=OrderClass.MLEG,
    legs=[
        OptionLegRequest(symbol="AAPL250117P00190000", ratio_qty=1,
                         side=OrderSide.BUY,  position_intent=PositionIntent.BUY_TO_OPEN),
        OptionLegRequest(symbol="AAPL250117P00195000", ratio_qty=1,
                         side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
    ],
)
resp = TradingClient(key, secret, paper=True).submit_order(order)   # async; poll resp.id
```

- `OptionLegRequest` fields: `symbol: str`, `ratio_qty: float`, `side: Optional[OrderSide]`,
  `position_intent: Optional[PositionIntent]` (at least ONE of side/intent required).
- `PositionIntent` = `BUY_TO_OPEN / BUY_TO_CLOSE / SELL_TO_OPEN / SELL_TO_CLOSE`.
- Outer `OrderRequest` (`LimitOrderRequest`/`MarketOrderRequest`) for MLEG requires `qty`, `legs`
  (2–4, unique), `order_class=MLEG`, `time_in_force`; `LimitOrderRequest` adds `limit_price`.
- `client_order_id: Optional[str]` is on `OrderRequest` (and echoed on the returned `Order`) —
  THIS is the idempotency handle (§3.5).

### 1.3 How a covered call / CSP / vertical is submitted

| Structure | Order(s) | Why |
|---|---|---|
| **Covered call** | (a) BUY 100 shares = a *separate equity* order, THEN (b) SELL 1 call = a *single-leg L1 option* order (`{symbol, qty, side:"sell", type, limit_price, position_intent:"sell_to_open"}`). | mleg legs are options-only; you cannot put the equity leg in `legs[]`. Alpaca validates the short-call against held shares. |
| **Cash-secured put** | Single-leg L1 option order, SELL 1 put `sell_to_open`. | Lone short put; collateral is reserved cash, not a second leg. |
| **Vertical spread** (debit/credit) | ONE mleg order, 2 option legs (buy + sell, same right, same expiry, different strikes). | Atomicity — both legs fill together. |
| **Iron condor / butterfly** | ONE mleg order, 4 / 3 legs. | Same atomicity guarantee. |
| **PMCC** | ONE mleg order, 2 call legs (long deep-ITM LEAPS `buy_to_open` + short near-dated OTM `sell_to_open`). | 2 option legs → mleg. |
| **Roll** (wheel-CC / PMCC short) | ONE mleg order, 2 legs (`buy_to_close` current short + `sell_to_open` next short). | Atomic → no naked window. |

`ratio_qty` is `"1"` for all vanilla spreads above; `"2"`+ only for ratio/backspread recipes.

### 1.4 The fill response (per-leg fills)

The submit returns an `Order` object; the spread is the **parent**, and `Order.legs` is a list of
child `Order` objects — ONE per leg. Per-leg fill detail lives on each child:

- Parent `Order`: `id`, `client_order_id`, `status` (`OrderStatus.FILLED` / `PARTIALLY_FILLED` /
  `accepted` / `new` / `canceled` / `rejected` / `expired`), `order_class="mleg"`, `qty`,
  `limit_price`, `legs: list[Order]`.
- Each child leg `Order`: `symbol` (OCC-21), `side`, `position_intent`, `ratio_qty`,
  **`filled_avg_price`** (per-leg execution price), **`filled_qty`**, **`status`**. (Per-leg
  `limit_price`/`type` are NULL — inherited.)

So the reactor's record builder reads `parent.status` for atomicity and iterates `parent.legs` for
the per-leg `filled_avg_price`/`filled_qty` that feed each per-leg `ExecutionRecord` and the
`state.db` position update.

---

## 2. Paper-account specifics

### 2.1 Atomic? Partial-leg?

**Yes, atomic; no partial-leg.** The whole purpose of mleg per Alpaca: "Placing these four legs as a
single MLeg order ensures they fill together or not at all… reduces the risk of partial fills." An
accepted paper spread does NOT leg out into a half-filled naked position. (A spread can still be
`PARTIALLY_FILLED` at the *spread-count* level — e.g. 3 of 5 lots of a vertical filled — but each
filled lot has all its legs; you never get leg-1 filled and leg-2 unfilled within a lot.) This is the
correctness property ADR-0029 §Context point 1 relies on, and it is enforced by the broker, not by
hermes-quant — but the reactor must still record `parent.status` and reconcile actual `filled_qty`
(may be < outer `qty` on a thin book) rather than assume full fill.

### 2.2 How are option fills PRICED in paper?

Paper "simulates the order filling based on the real-time quotes" — i.e. against the live **NBBO**
(bid/ask), NOT an arbitrary mid. A marketable limit (debit limit ≥ ask-sum / credit limit ≤ bid-sum)
fills; a resting limit fills only when the quote crosses it. Practical implications for the reactor:
- Paper-ONLY accounts receive **IEX** equity data and options-chain data that the community reports
  can lag ~**15 minutes** — so paper option fill prices are noisier and later than a paid feed would
  give. Treat the paper `filled_avg_price` as the source of truth for the record, but flag in
  `reactor_metadata` that paper option pricing is IEX/possibly-delayed (a known fee/price-divergence
  gap, ADR-0029 Open Question 3).
- There is no separate "paper data" — data is keyed on the data subscription, not the account; only
  *order routing* is simulated. So the chain snapshot the gate saw and the paper fill come from the
  same feed tier.
- Because paper fills at the live quote, the ADR-0070 slippage model is **not strictly needed** for
  the option legs (the quote already embeds the spread). Recommendation: for option legs set
  `fill_price = parent net (signed)` from `filled_avg_price`s and keep the v0.1 passthrough; reserve
  the v0.2 slippage envelope for the EQUITY leg of a covered call (where decision_price→fill needs
  the spread/impact model). Document this asymmetry in the reactor.

### 2.3 Assignment / expiration / exercise in paper

Alpaca's paper engine SIMULATES assignment, exercise, and expiry — but the resulting **non-trade
activities (NTAs)** only surface on `GET /v2/account/activities` the **next calendar day**, even
though buying power + positions update instantly intraday. NTA types: `OPEXP` (expiration), `OPASN`
(assignment), `OPXRC` (exercise); each is a `NonTradeActivity` with `date`, `net_amount`,
`description`, `status`, `symbol`, `qty`. This is the source of ADR-0029 D3's next-day
`reconcile_options_ntas` step (06:00 ET) — OUT of scope for the reactor itself, but the reactor must
write a durable `OCC symbol → multi_leg_id` index so the next-day reconcile can join NTAs back to the
originating spread. Assignment of a short call in a covered call produces an equity position delta
(−100 shares at strike) that surfaces as the NTA, not as a fill — the reactor does NOT see it at
execution time.

---

## 3. The `PaperReactor` interface to MIRROR (the heart of the build)

Source: `hermes_quant/react/base.py` (Protocol + `ExecutionRecord`), `hermes_quant/react/paper.py`
(reference impl), `hermes_quant/react/multileg.py` (the scaffold to fill in).

### 3.1 The Protocol surface (already conformed by the scaffold)

```python
# react/base.py — the contract every reactor mirrors
class Reactor(Protocol):
    name: str
    requires_credentials: bool
    def execute(self, proposal: Any, *, fill_size_pct: float,
                approver_user_id: str | None = None) -> ExecutionRecord: ...
```

`react/multileg.py:MultiLegPaperReactor` already has `name="multileg-paper"`,
`requires_credentials=False`, and an `execute()` that raises `MultiLegReactorDisabled` unless
`HERMES_QUANT_MULTILEG_REACTOR=1`, else `NotImplementedError`. The go-live body fills in the
`execute()` after the flag check. **Keep the exact signature** — the proposal becomes a
`MultiLegProposal` (ADR-0029 D5) but the keyword args (`fill_size_pct`, `approver_user_id`) and
return type stay so it drops into the existing react dispatch unchanged.

### 3.2 What `PaperReactor.execute()` does, step by step (the algorithm to mirror)

1. Extract `decision_price` + `signal_id` from the proposal's embedded `advisor_result`
   (`_extract_decision_price` / `_extract_signal_id`). For multi-leg, "decision_price" becomes the
   **net debit/credit at decision time** (from the gate's `bpr_estimate`/mid math), not a single
   underlying close.
2. Compute `asof_decision` (prefer `decision_wall_clock`, else `as_of`, else now), `bar_ts`,
   `now = asof_execution` (UTC `%Y-%m-%dT%H:%M:%SZ`).
3. Slippage branch: `HERMES_QUANT_PAPER_SLIPPAGE_MODEL` (`v0.1` passthrough `fill_price =
   decision_price`; `v0.2` calls `slippage_model.apply_slippage(...)` seeded by `(proposal_id,
   asof_execution)` for replay determinism). See §3.6 for the multi-leg recommendation.
4. Build an `ExecutionRecord` (frozen dataclass, §3.3).
5. Serialize with `json.dumps(..., separators=(",",":"), sort_keys=True)+"\n"` and append via
   `with append_locked(self.executions_path) as fd: os.write(fd, line)` — the SAME flock+fsync bus
   helper `signal_bus` uses (`EXECUTION_BUS_PATH = ~/.hermes/quant/executions.jsonl`).
6. `logger.info(...)` a one-line audit.
7. Best-effort `PortfolioState.apply_execution(record_dict)` (failure must NOT block the fill —
   silence-by-default, ADR-0031); inject `account_id` (default `"paper-default"`).
8. Optional Wave-4 reflection hook on close (`HERMES_QUANT_REFLECTION=1`).
9. `return record`.

### 3.3 What it writes to `executions.jsonl`

`ExecutionRecord` (`react/base.py`) → JSONL via `_record_to_dict`. Fields:
`proposal_id, signal_id, asset, asset_class, timeframe, asof_decision, asof_execution,
target_position_pct, decision_price, fill_price, fill_size_pct, reactor_name, human_in_the_loop,
approver_user_id, reactor_metadata, bar_ts`. The daemon's `settlement_loop` tails this file and joins
each row back to its signal to build `RealizedOutcome`s (per-analyst calibration). It already
defaults `asset_class` and tolerates extra fields.

### 3.4 What CHANGES for multi-leg (the new shape the reactor must emit)

The equity `ExecutionRecord` is single-instrument (`asset`, scalar `fill_price`,
`fill_size_pct`). A spread has N legs. Two viable shapes; **recommend (B)** for replay/calibration
fidelity and to mirror how Alpaca + PortfolioState model it:

- **(A) one fat parent record** with a `legs: [...]` list inside `reactor_metadata`. Simplest, but
  the settlement loop + PortfolioState read scalar `asset`/`fill_size_pct` and would need a parser.
- **(B, recommended) one parent record + one child `ExecutionRecord` per leg**, all sharing a
  `multi_leg_id` (= `proposal_id`) in `reactor_metadata`. Each child uses:
  `asset = OCC-21 symbol`, `asset_class = "us_option"` (equity leg of a CC: `asset_class="equity"`),
  `fill_price = leg.filled_avg_price`, `fill_size_pct` = the signed per-leg contribution, plus
  `reactor_metadata = {"multi_leg_id": proposal_id, "leg_index": i, "position_intent": ...,
  "ratio_qty": ..., "net_debit_credit": signed_net, "strategy_kind": ..., "paper": True,
  "option_pricing": "iex_possibly_delayed"}`. The parent record carries the net debit/credit as
  `fill_price` and `strategy_kind`/`net_greeks` in `reactor_metadata` for the HITL audit and the
  PMCC-shadow join. This makes each leg flow through `PortfolioState.apply_execution` unchanged
  (one position row per OCC symbol — §4) and keeps the executions bus uniform.

### 3.5 Idempotency (ADR-0078) attach point

The ADR-0078 `OrderState`/`OrderEvent` machine is documented to live at
`hermes_quant/react/order_state.py` but **is NOT present yet** (confirmed:
`admissibility/order_state.py` is only the admissibility→sizing bridge and explicitly says the
ADR-0078 machine is elsewhere/out of scope). So the reactor must define exactly-once itself:
- Generate a stable **`client_order_id` = a deterministic hash of `(proposal_id, strategy_kind,
  tuple(leg symbols+intents), outer_qty)`**. Pass it to `submit_order` (alpaca-py
  `OrderRequest.client_order_id`); Alpaca rejects a duplicate `client_order_id`, giving broker-side
  exactly-once. On a retry after a crash-between-submit-and-record, re-submitting with the same
  `client_order_id` returns the existing order rather than double-firing.
- On the bus side, dedupe on `(proposal_id, multi_leg_id)` before append (a second `execute()` for
  an already-recorded proposal is a no-op that returns the existing record). Record the
  `client_order_id` in `reactor_metadata`.
This is the structural exactly-once guarantee ADR-0078 demands; wire it through the (future)
`react/order_state.py` machine when it lands, but the reactor must not depend on it existing.

### 3.6 Slippage (ADR-0070) attach point

Same branch as equity (`HERMES_QUANT_PAPER_SLIPPAGE_MODEL`). Recommendation (from §2.2): for the
**option legs**, paper already fills at the live NBBO so keep passthrough (`fill_price =
filled_avg_price`); apply the v0.2 envelope only to the **equity leg** of a covered call (a market
buy of 100 shares, where decision_price→fill needs spread+impact). Seed remains `(proposal_id,
asof_execution)` for replay equality.

---

## 4. Covered-call two-leg reconciliation into `state.db` + PMCC shadow validation

### 4.1 Two position rows, mirroring Alpaca

Alpaca returns each option leg as its OWN `Position` (OCC-keyed, `asset_class="us_option"`, signed
`qty`, `avg_entry_price`, `market_value`, `unrealized_pl`) — there is NO combined "spread position"
object. `PortfolioState` (`state/portfolio_state.py`) already keys positions by `(account_id,
asset_class, symbol)` (PK) and `apply_execution(record)` reads `asset_class`, `fill_size_pct`,
`fill_price`. So a covered call lands as TWO rows with NO schema change:

| account_id | asset_class | symbol | qty | source record |
|---|---|---|---|---|
| paper-default | `equity` | `NVDA` | +100 | child record: BUY 100 shares (equity order) |
| paper-default | `us_option` | `NVDA260626C00160000` | −1 | child record: SELL 1 call (`sell_to_open`) |

Caveat to fix in the go-live wave: `apply_execution` today tracks `qty` in **NAV-fraction units**
(`fill_size_pct`), not contracts/shares. For options the natural unit is **signed contract count**
(and shares for the equity leg). The reactor's per-leg child records should carry an explicit
`quantity`/`contracts` in `reactor_metadata` and the go-live wave should extend
`PortfolioState.apply_execution` to store contract/share quantities for `us_option`/`equity` legs
(the NAV-fraction proxy is approximate and will misstate option position sizes). This is a known
follow-up, not a blocker for the reactor's bus write.

### 4.2 PMCC shadow as the counterfactual validator

`shadow/pmcc.py` models the SAME two-leg structure (`PMCCPosition` = long deep-ITM LEAPS call +
short near-dated OTM call) and marks it to Black-Scholes on demand (`mark_pmcc → PMCCMark` with
`net_value`, `unrealized_pnl`, `net_delta`, `net_theta_day`, `long_dte`, `short_dte`). It writes
nothing to `executions.jsonl` / `state.db` — pure shadow at
`~/.hermes/quant/shadow/pmcc-positions.jsonl`. The validation loop once the reactor fires:
1. On reactor open of a PMCC, also `record_pmcc(PMCCPosition(...))` with the SAME `opened_at`
   (decision time), legs, `spot_at_open` — keyed so it can be joined to the reactor's `multi_leg_id`
   (add `note=multi_leg_id` or an explicit field).
2. Daily, `mark_pmcc(pos, spot=…)` gives the *model* `net_value`; compare against the reactor's
   real per-leg marks (sum of leg `market_value` from `/v2/positions`). Divergence = model vs.
   market (IV smile, early-exercise premium, paper data lag) — surfaced for the 60-day evidence
   window. The shadow `net_theta_day` sign (typically POSITIVE for a PMCC — collect decay) is the
   structural sanity check: a "PMCC" coming back net-negative theta from real marks is a build bug.
The shadow is the documented, daily-marked counterfactual ADR-0029's confidence path needs; it
"activates implicitly once the multi-leg reactor lands."

---

## 5. Roll mechanics (wheel / PMCC) as orders

A roll closes the current short and opens the next short. Express as ONE mleg order (atomic, no naked
window):

```jsonc
POST /v2/orders   // PMCC / wheel-CC short-leg roll
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "-0.45",            // NEGATIVE = net CREDIT collected on the roll (roll "up/out")
  "time_in_force": "day",
  "legs": [
    {"symbol": "NVDA260626C00160000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_close"},   // close current short
    {"symbol": "NVDA260717C00165000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}     // open next short
  ]
}
```

- `position_intent`: the closing leg is `buy_to_close` (releases the short collateral); the opening
  leg is `sell_to_open`. Getting these right is what lets the broker net the margin correctly.
- **Triggers** (ADR-0029 OQ2; record data, decide later): short leg ≤ 21 DTE, OR short breached
  (deep ITM → assignment/pin risk), OR target % of max profit captured. Pin-risk auto-close at 15:30
  ET vs. let-it-settle stays an empirical question for the first 30 days.
- The reactor records the roll as a parent `ExecutionRecord` (`strategy_kind="roll"`,
  `net_debit_credit` = the signed roll price) + two child leg records; PortfolioState then zeroes the
  closed short's `us_option` row and opens the new one. The wheel CC roll is identical in shape —
  only strikes/expiries differ. The wheel STATE MACHINE (CSP→assignment→CC→called-away→CSP) lives
  above the reactor; the reactor only executes the discrete open/close/roll orders the gate admits,
  with the ADR-0027 D7 invariant of exactly ONE active option leg per underlying enforced upstream.

---

## Sources

- alpaca-py SDK source (deepwiki `alpacahq/alpaca-py`): `OptionLegRequest`
  (`symbol`/`ratio_qty`/`side`/`position_intent`), `OrderRequest`/`LimitOrderRequest`/
  `MarketOrderRequest` MLEG validation (`qty`+`legs` required, 2–4 unique legs, MLEG `limit_price`
  +debit/−credit sign), `PositionIntent` enum (BUY/SELL_TO_OPEN/CLOSE), `Order.legs` list of child
  `Order` with per-leg `filled_avg_price`/`filled_qty`/`status`, `Position` options fields
  (OCC-keyed, `us_option`, `qty`/`avg_entry_price`/`side`/`market_value`/`unrealized_pl`),
  `NonTradeActivity` (OPEXP/OPASN/OPXRC), `OrderStatus.FILLED`/`PARTIALLY_FILLED`,
  `client_order_id` idempotency, `TradingClient.submit_order`/`get_all_positions`.
- Alpaca docs: Options Level 3 Trading (mleg fill-together-or-not-at-all, how-to-submit cURL),
  Options Orders (single-leg covered-call / CSP payloads), Paper Trading + Paper Trading
  Specification (simulate fills off real-time quotes; IEX for paper-only), Placing Orders
  (OrderStatus table), Create an Order. Alpaca blog "Multi-Leg (Level 3) Options Trading Now
  Available" (paper auto-Level-3). Alpaca forum: async fills ("always query status, never assume
  filled"), paper options pricing / ~15-min chain lag.
- Internal: `hermes_quant/react/base.py` (`Reactor` Protocol, `ExecutionRecord`),
  `hermes_quant/react/paper.py` (the impl to mirror), `hermes_quant/react/multileg.py` (the
  scaffold), `hermes_quant/react/slippage_model.py` (ADR-0070), `hermes_quant/daemon/signal_bus.py`
  (`append_locked`, `EXECUTION_BUS_PATH`), `hermes_quant/daemon/settlement_loop.py` (reader side),
  `hermes_quant/state/portfolio_state.py` (`apply_execution`, `(account_id, asset_class, symbol)`
  PK), `hermes_quant/shadow/pmcc.py` (counterfactual harness),
  `hermes_quant/admissibility/order_state.py` (confirms the ADR-0078 machine is NOT yet present),
  `docs/adr/ADR-0029-multi-leg-paper-reactor.md` (+ both 2026-05-24 amendments),
  `docs/research/2026-05-30-r-options-execution.md` (gate/data companion).
```
