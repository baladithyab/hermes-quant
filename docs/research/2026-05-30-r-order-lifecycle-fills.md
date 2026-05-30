# Research: order-lifecycle state machine + fill realism for a paper-trading sim

> **Date:** 2026-05-30 · **Author:** research agent (deep-work loop) ·
> **Scope:** the six-model-critique P0 fidelity layer — `OrderState` machine,
> realistic partial fills, exactly-once idempotency across 6 event stores +
> serialization of the 11 synchronous cron firing surfaces.
> **Posture:** money-software (AGENTS.md). Silence-by-default; the deterministic
> risk gate (ADR-0004) is FINAL authority; every new capability ships
> DEFAULT-OFF behind a `HERMES_QUANT_*` flag, eval-gated before live influence.
> All times UTC; `asof` = decision/publication time (no look-ahead).
>
> **What exists today (grounded in code):** `PaperReactor.execute`
> (`hermes_quant/react/paper.py:54`) writes one `ExecutionRecord` with
> `fill_price = decision_price` and `fill_size_pct = target_pct` — a 100%,
> one-price, instantaneous fill with **no `OrderState`** (the record IS the
> terminal state). ADR-0070's `apply_slippage` (`react/slippage_model.py:138`)
> is built and default-OFF (`HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2`). The
> proposal store (`proposals.py`) already has a `pending → {approved | rejected
> | expired}` *approval* machine in SQLite — but that is the HITL approval
> lifecycle, **not** the order/fill lifecycle. Idempotency today is a single
> ad-hoc guard: `fired_today()` in `quant-autonomous-tick.py:144` reads
> `autonomous-tick.jsonl` and dedups one fill **per symbol per ET day**. There
> is **no global tick lock** serializing the 11 cron surfaces, and append-only
> writes are serialized only by `fcntl.flock` per-file (`signal_bus.py:44`),
> which gives byte-atomicity, **not** logical dedup.

---

## 1. Canonical order-lifecycle state machines

Three authoritative sources, mapped to a minimal set. The convergent core
across all three is: **one pre-fill state, one partial state, one done-good
state, and a small set of done-bad/terminal states.**

### 1a. FIX protocol `OrdStatus(39)` — the canonical reference

The FIX spec (FIX Trading Community, *Order State Changes*; FIX 4.3 Appendix D;
fiximate `OrdStatusCodeSet`) defines `OrdStatus` with an explicit **precedence
ordering** so that when an order is simultaneously in multiple states, the
highest-precedence value is reported. The full code set (tag 39):

| Code | Status | Precedence | Note |
|---|---|---|---|
| `0` | New | 2 | Outstanding, no executions |
| `1` | Partially Filled | 3 | Executions + remaining qty |
| `2` | Filled | 7 | Completely filled, 0 remaining (terminal) |
| `3` | Done for Day | 9 | Day order, no further fills this session |
| `4` | Canceled | 4 | With or without prior executions (terminal) |
| `6` | Pending Cancel | 11 | Cancel *requested*, NOT yet canceled |
| `7` | Stopped | 6 | Price guaranteed at exchange |
| `8` | Rejected | 2 | Broker/exchange refused (terminal) — *can* follow New |
| `9` | Suspended | 5 | Client-requested suspend |
| `A` | Pending New | 2 | Received, not yet accepted |
| `B` | Calculated | 8 | Done for day + commission/settlement computed |
| `C` | Expired | 4 | Killed by `TimeInForce(59)` (terminal). FOK/IOC use **Canceled** as terminal, not Expired |
| `E` | Pending Replace | 10 | Replace requested |

Two load-bearing facts for us:
- **`Rejected` can follow `New`** — an order can be acknowledged then rejected
  afterward (broker risk check). So `REJECTED` is reachable from the working
  state, not only at submission. (FIX *Order State Changes*, precedence note.)
- The companion field **`ExecType(150)`** carries the *reason* for the message
  (Trade / New / Canceled / …) separately from `OrdStatus(39)` (the resulting
  *state*). A defensible audit trail records both: *what state are we in* and
  *what event got us here*. Our `ExecutionRecord` today conflates them.

The standard partial-fill accounting tuple is **`CumQty` / `LeavesQty` /
`LastQty`** (cumulative filled, remaining, this-fill qty); `OrderQty = CumQty +
LeavesQty`. A part-filled day order ends the day at `Done for Day` with
`LeavesQty=0` and no further executions (FIX A.1.b matrix).

### 1b. Alpaca `OrderStatus` (our actual paper broker)

`alpaca-py` (`alpaca/trading/enums.py`) — the states Alpaca's REST API actually
emits: `new`, `partially_filled`, `filled`, `done_for_day`, `canceled`,
`expired`, `replaced`, `pending_cancel`, `pending_replace`, `pending_review`,
`accepted`, `pending_new`, `accepted_for_bidding`, `stopped`, `rejected`,
`suspended`, `calculated`, `held`. Alpaca's own docs flag the **"common"**
states as `new / partially_filled / filled / done_for_day / canceled / expired
/ replaced / pending_cancel / pending_replace` and the rest as "rarely seen."
Partial-fill accounting is on the `Order` model: `filled_qty`,
`filled_avg_price`, and `leaves_qty` on `TradeActivity` (= FIX `LeavesQty`).
`TimeInForce`: `day, gtc, opg, cls, ioc, fok` (OPG = market/limit-on-open, CLS =
market/limit-on-close → the auction surfaces in §4).

**Idempotency is first-class at Alpaca:** every order request carries a
client-supplied **`client_order_id`**; resubmitting the same `client_order_id`
returns the same order (`get_order_by_client_id`). This is the broker-side
exactly-once primitive — and it directly motivates the dedup-key design in §3.

### 1c. nautilus_trader & Lean (open-source sim cross-check)

- **nautilus_trader** (`model/enums.py`): `INITIALIZED, DENIED, EMULATED,
  RELEASED, SUBMITTED, ACCEPTED, REJECTED, CANCELED, EXPIRED, TRIGGERED,
  PENDING_UPDATE, PENDING_CANCEL, PARTIALLY_FILLED, FILLED`. Note the extra
  *pre-venue* states (`INITIALIZED/DENIED/EMULATED/RELEASED`) — Nautilus models
  client-side emulation and a local risk `DENIED`, analogous to **our risk gate
  silencing before any order exists.**
- **Lean** (`OrderStatus`): `New, Submitted, PartiallyFilled, Filled, Canceled,
  CancelPending, Invalid, None, UpdateSubmitted`. `Invalid` = pre-market
  rejection for insufficient capital — Lean's analogue of `REJECTED`.

### → Minimal `OrderState` set for hermes-quant (the recommendation)

The task names five states `{PENDING, PARTIAL, FILLED, REJECTED, CANCELED}`.
That set is **correct and sufficient** for a daily-cadence paper equity sim —
it is the intersection of FIX/Alpaca/Nautilus/Lean minus the venue-protocol
chatter (`Pending New`, `Accepted`, `Pending Cancel`, `Replaced`,
`Done For Day`, `Calculated`, `Suspended`) we don't need for a single-operator
daily picker. Map them as:

```
                 ┌─────────── REJECTED   (gate/admissibility/broker refusal — terminal)
                 │
 (risk gate) → PENDING ──fill<100%──► PARTIAL ──remainder fills──► FILLED (terminal)
                 │            │                    │
                 │            └────────────────────┴──► (EOD: PARTIAL→CANCELED, see §2)
                 └──cancel/expire/no-fill──────────────► CANCELED  (terminal)
```

- **`PENDING`** = order accepted by the (paper) reactor, no fill yet. Subsumes
  FIX `New`/`Pending New`/`Accepted` and Lean `New`/`Submitted`.
- **`PARTIAL`** = `0 < cum_qty < order_qty`. Carry FIX-style accounting
  `(order_qty, cum_qty, leaves_qty, fill_avg_price)`.
- **`FILLED`** = `leaves_qty == 0` (terminal).
- **`REJECTED`** = refused before any fill — by our **risk gate / pre-trade
  admissibility (ShortabilityOracle)** *or* by the broker. Terminal. (Keep the
  FIX lesson: reject is reachable *after* PENDING too, e.g. paper broker
  refuses a locate.)
- **`CANCELED`** = working order killed with `leaves_qty > 0` — including the
  **day-order EOD sweep** (our `DoneForDay`+`Expired` collapse to `CANCELED`
  for the unfilled remainder; see §2/§4).

Add **`EXPIRED` only if** we ever ship GTC/GTD multi-day options orders
(ADR-0029); for day-cadence equities, fold expiry into `CANCELED`. Record a
separate **`event_type`** field (FIX `ExecType` analogue: `accept | fill |
partial_fill | reject | cancel | expire`) so the *transition reason* is
queryable independently of the resulting *state* — this fixes the wiki's
`n_distinct_analysts=None` class of "the gate fired but it isn't in the audit
trail" gaps. Persist as an **append-only event log + a derived current-state
projection**, never an in-place mutable status column (replayability, ADR-0001).

---

## 2. Partial-fill modeling in paper sims

**The headline finding that should calm the operator's "paper lies" fear:**
even Lean — a production-grade backtester — fills market orders **100% at one
price in a single `OrderEvent` by default**. From `EquityFillModel.MarketFill`:
`fill.FillQuantity = order.Quantity; fill.Status = OrderStatus.Filled;` — volume
/ liquidity caps (`MaximumOrderVolume`) do **not** cause partial fills unless you
install a custom `IFillModel` (`PartialMarketFillModel`,
`CustomPartialFillModelAlgorithm`). So hermes-quant's current 100%-fill is
**not aberrant**; it is the *industry-default sim behavior*. The gap vs. live
is real but it is the same gap every sim has. This means **partial-fill modeling
is a default-OFF refinement, not a correctness bug** — consistent with our
flag-gated rollout discipline.

How the three sims model partial fills when you *do* turn them on:

- **nautilus_trader** (the most realistic): the `OrderMatchingEngine` tracks
  per-price-level liquidity via `bid_consumption`/`ask_consumption` maps so a
  single market-data update can't over-fill. Queue position is modeled two ways:
  (a) **probabilistically** via `FillModel.prob_fill_on_limit` (`0.0` = back of
  queue / never fills on a touch, `0.5` = coin-flip, `1.0` = front of queue),
  and (b) **deterministically** via `queue_position=True`, where a limit order
  doesn't fill until the volume that was ahead of it at placement trades through
  (`determine_trade_fill_qty`). `LimitOrderPartialFillModel` caps fill qty per
  price *touch*. TIF: `DAY, GTC, GTD, IOC, FOK`; for FOK/IOC, if
  `determine_trade_fill_qty` yields no allowed qty the order is canceled
  immediately.
- **Lean**: `PartialMarketFillModel` splits a market order into N fills, **one
  partial per time-step**, each capped by `absoluteRemaining`. Limit fills only
  when the bar penetrates the limit (`buy: bar.Low < LimitPrice`); fill price =
  limit price (or the open on a favorable gap). Stop fills at stop price ±
  slippage.
- **backtrader**: fills are gated by **Volume Fillers** (`backtrader.fillers`):
  `FixedSize` (cap each fill at N shares), `FixedBarPerc` (fill up to X% of the
  bar's volume), and `BarPointPercVolume` (distribute bar volume across the
  price range and fill the portion at your price). Default = *ignore volume,
  fill fully*. (backtrader docs `api/core/backtrader.fillers`; *Volume Filling*
  blog 2016-07-14.)

### → A defensible simple model for daily-cadence equities

We trade ≤50 liquid names, ≤20% NAV/pick, once or a few times per day. The
realistic and *cheap* model, default-OFF behind a new
`HERMES_QUANT_PARTIAL_FILL_MODEL` flag (mirroring ADR-0070's pattern):

1. **Liquidity cap = % of bar volume** (backtrader `FixedBarPerc` idea). Allowed
   fill qty = `min(order_qty, participation_pct × bar_volume)` with a
   conservative `participation_pct` (e.g. 1–5%). For our position sizes against
   top-100-liquid names this is ~always 100% → **the common case stays a full
   fill**, which is honest (we deliberately trade liquid names). The model only
   bites on thin names / outsized orders — exactly where live would too.
2. **Marketable orders fill same-bar; resting limits use a fill probability** on
   a touch (Nautilus `prob_fill_on_limit`), deterministic given a per-order RNG
   seed `hash(proposal_id, asof)` — **reuse ADR-0070's `seed_for_fill` pattern
   verbatim** so replays are bit-identical.
3. **TIF = DAY only** for equities. The **EOD sweep**: any order still `PENDING`
   or `PARTIAL` at session close → unfilled remainder transitions to
   `CANCELED` (= FIX `DoneForDay`). This is the **case the 2026-05-27 positions
   reconciler exists for but has never been exercised** (wiki §3.B) — shipping
   `PARTIAL`/`CANCELED` is what finally tests that path.
4. **Fill price** continues to come from ADR-0070's `apply_slippage`; partial
   fills just apply it per-fill-event. Symmetric entry/exit keeps round-trip
   honest.

Keep it deterministic, keep it conservative, and keep it OFF until a side-by-side
tick-log audit (the operator-flip discipline). Do **not** build a full LOB
simulator — out of scope and over-engineered for a 1-user daily picker
(matches the "what NOT to build" list).

---

## 3. Idempotency / exactly-once for the event-sourced stores

**The problem (from the wiki, root-caused by Kimi):** the 880%-gross "blown-up
book" artifact was an `executions.jsonl` reconstruction that **double-counted**
because a fill family re-fired and was reconciled *once, not root-fixed*. The
rails say: *never trust an `executions.jsonl` reconstruction over the `state.db`
positions table.* The fix is structural exactly-once, not after-the-fact
reconciliation.

There are **6 append-only event stores** in play (grep-verified, all under
`~/.hermes/quant/`): `executions.jsonl`, `signals.jsonl`, `decisions.jsonl`,
`reflections.jsonl`, `proposals.jsonl`, `propagation-log.jsonl` (plus
`audit_log.jsonl`, `packets.jsonl`, `hypotheses.jsonl`, etc.), and SQLite
projections `state.db` / `proposals.db` / `watermarks.db`. Today they're
serialized only by `fcntl.flock` (`signal_bus.py:44`) — that gives **byte-level
append atomicity** (no torn lines), but **zero logical dedup**: two cron
surfaces that both decide "buy AAPL today" each append a valid line.

### 3a. Dedup-key design — `{cron}:{ts}:{symbol}:{hash}`

Adopt a **deterministic idempotency key** as the exactly-once primitive,
computed *before* any write and *before* the broker call:

```
dedup_key = f"{cron_surface}:{decision_bucket_ts}:{symbol}:{payload_hash}"
```

- **`cron_surface`** — which of the 11 firing surfaces (`autonomous-tick`,
  `playbook-tick`, `daily-interim`, …). Distinguishes legitimately-different
  decisions about the same symbol from re-fires of the *same* decision. (Also
  fixes the `play_tag`-all-reads-`advisor` observability gap, wiki §3.F.)
- **`decision_bucket_ts`** — the **decision time bucket** (e.g. ET trading-day
  for daily cadence, or the 30-min tick boundary for the autonomous tick),
  **UTC, derived from `asof` (decision/publication time), never wall-clock-now**
  (lookahead honesty rail). This is the generalization of the existing
  `fired_today()` per-symbol-per-ET-day guard.
- **`symbol`** — the instrument (OCC-21 string for options once ADR-0029 lands).
- **`payload_hash`** — short hash of the *decision-defining* fields
  (direction, target_pct, recipe/play). Means a genuinely *different* decision
  (flipped direction, resized) is NOT suppressed, but an identical re-fire is.

This is the local mirror of Alpaca's `client_order_id` idempotency (§1b): set
`client_order_id = dedup_key` so the **broker** also rejects the duplicate even
if our local guard is bypassed — defense-in-depth, exactly the codebase's
existing three-validation-points style.

### 3b. Idempotent append → use SQLite `INSERT OR IGNORE` on a UNIQUE key

`flock` cannot do "insert only if absent" across a crash/retry. The exactly-once
write primitive should be a SQLite **`UNIQUE` constraint + `INSERT OR IGNORE`**
(or `ON CONFLICT(dedup_key) DO NOTHING`, SQLite `lang_conflict`) in a small
**ledger table** that gates every event-store append:

```sql
CREATE TABLE IF NOT EXISTS fired_ledger (
  dedup_key TEXT PRIMARY KEY,    -- {cron}:{ts}:{symbol}:{hash}
  cron      TEXT NOT NULL,
  asof      TEXT NOT NULL,       -- UTC decision time
  created_at TEXT NOT NULL
);
-- claim-before-write: 1 row changed = we own this fire; 0 = duplicate, skip
INSERT OR IGNORE INTO fired_ledger(dedup_key, cron, asof, created_at)
VALUES (?, ?, ?, ?);
```

`changes() == 1` ⇒ proceed to broker + append to the JSONL stores;
`changes() == 0` ⇒ a prior fire already claimed this key ⇒ **silently skip**
(silence-by-default). The JSONL stores stay append-only and human-greppable;
the SQLite ledger is the *authority* on "did this fire?" — consistent with the
"`state.db` over JSONL-reconstruction" rail. This extends the existing
`watermarks.db` pattern already in the repo.

### 3c. Why a global tick semaphore / `flock` matters under SQLite WAL

SQLite in **WAL mode allows many concurrent readers + one writer**, but
**writes are still serialized** — "at most one writer to proceed concurrently"
(sqlite.org `wal.html`, `begin-concurrent.md`). Without coordination, two of the
11 cron surfaces firing in the same minute hit two failure modes:

1. **`SQLITE_BUSY`** — the second writer's transaction fails unless a
   `busy_timeout` is set; a naive cron then either crashes (silent-error cron,
   the worst failure mode per the rails) or, worse, retries and double-writes.
2. **Check-then-act races** — even with `INSERT OR IGNORE` per-store, a tick
   that spans *multiple* stores (claim ledger → call broker → append
   executions → update state.db) is not atomic across stores; two interleaved
   ticks can produce a half-written cross-store state — the `state.db is cached
   corruption` class Kimi described.

**Mitigation (two layers):**
- **Per-DB:** open every connection with `PRAGMA journal_mode=WAL;` +
  `PRAGMA busy_timeout=<ms>;` and wrap the claim+write in one transaction.
- **Global:** a **single tick semaphore** — an exclusive `fcntl.flock` on a
  dedicated `~/.hermes/quant/tick.lock` file (the pattern `signal_bus.py`
  already uses, generalized to a *whole-tick* critical section rather than a
  *single-append* one). Any of the 11 surfaces acquires it before the
  decide→gate→react→record sequence and releases after. This **serializes the 11
  synchronous firing surfaces** so only one tick mutates shared state at a time
  — turning "11 crons racing on `state.db`" into "11 crons taking turns." The
  lock is advisory + local-home (no NFS), exactly the caveat `signal_bus.py:67`
  already documents.

Ship default-OFF behind e.g. `HERMES_QUANT_TICK_LOCK=1` only if a no-lock
regression is feared; given it's a pure-safety serialization with no behavior
change on the happy path, it's the rare capability that's defensible to enable
by default once tested (it can only *prevent* double-fires, never cause one).

---

## 4. Fill realism beyond slippage

ADR-0070 already models spread-cross + impact + latency-drift + auction-premium
*price*. The missing **structural** realism (orthogonal to price):

- **Marketable-limit vs market.** A market order assumes a fill at *some* price;
  a **marketable limit** (limit at/through the touch) bounds worst-case fill but
  can go unfilled if the market gaps away — producing a `CANCELED`/`PARTIAL`,
  not a guaranteed fill. For paper honesty, model the operator's actual order
  type. Recommendation: **marketable-limit as the default paper order type**
  (bounds slippage, models real "didn't fill" outcomes) rather than an
  always-fills market order — this is the single biggest *behavioral* fidelity
  gain and naturally exercises the `PARTIAL`/`CANCELED` states from §1/§2.
- **Auction (open/close) fills.** Alpaca TIF `opg` (MOO/LOO) and `cls`
  (MOC/LOC) fill **only** in the open/close auction at the official auction
  print — not at an arbitrary intraday tick. Lean's `EquityFillModel` models
  this precisely: `MarketOnOpen` waits for `OfficialOpen`/`OpeningPrints`,
  `MarketOnClose` waits for `OfficialClose`/`ClosingPrints`. ADR-0070's
  `is_late_session_equity` (`slippage_model.py:240`) already detects the
  post-15:30 ET window and adds the auction *premium*; pairing that with a
  `cls`/`opg` order type would model auction *timing* (fill happens at the
  auction, not now) — relevant because daily-cadence picks often want
  on-close/on-open execution.
- **Halts / LULD.** A halted ticker cannot fill. The data layer already drops
  zero-volume (halted) bars at the boundary (AGENTS.md "Bar data validation");
  the order model should mirror this: an order against a halted symbol →
  `REJECTED` (or held → next-session), never a phantom fill. This is the
  pre-trade-admissibility surface (the ShortabilityOracle's sibling, wiki §3.B)
  and belongs in the *gate*, not the fill model.
- **Borrow / locate on shorts.** Out of scope for *this* note (ADR-0070
  explicitly defers it; the ShortabilityOracle owns it) but it's the reason 38
  synthetic shorts are untradeable-live — a `REJECTED` at admissibility, not a
  fill-model concern.

---

## Implementation hooks (for the architecture phase / ADRs)

- New module `hermes_quant/react/order_state.py`: an `OrderState` enum
  `{PENDING, PARTIAL, FILLED, REJECTED, CANCELED}` (+ `EXPIRED` reserved for
  ADR-0029 GTC options) and an append-only `OrderEvent` (`event_type` = FIX
  `ExecType` analogue, `cum_qty/leaves_qty/order_qty/fill_avg_price`,
  `dedup_key`). Current-state is a *projection*, never a mutable column.
- `PaperReactor.execute` (`react/paper.py:54`): claim `dedup_key` via the
  SQLite ledger *before* writing; emit `OrderEvent`s rather than one terminal
  `ExecutionRecord`; gate partial fills behind `HERMES_QUANT_PARTIAL_FILL_MODEL`
  (default OFF → bit-identical to today). Reuse `seed_for_fill`
  (`slippage_model.py:120`) for deterministic fill RNG.
- New `fired_ledger`/idempotency table (extend `watermarks.db` or a new
  `orders.db`); generalize `fired_today()` (`quant-autonomous-tick.py:144`) to
  the `{cron}:{ts}:{symbol}:{hash}` key; set Alpaca `client_order_id = dedup_key`.
- New `hermes_quant/daemon/tick_lock.py`: a `with global_tick_lock():` context
  manager (exclusive `flock` on `tick.lock`) wrapping each cron's
  decide→gate→react→record critical section. Add `PRAGMA journal_mode=WAL` +
  `busy_timeout` to every SQLite open.
- EOD sweep job: transition surviving `PENDING`/`PARTIAL` day orders to
  `CANCELED` at session close — finally exercises the never-tested positions
  reconciler.

## Sources

- FIX Trading Community — *Order State Changes* (`OrdStatus(39)` precedence,
  `ExecType(150)`, part-fill matrices A.1.b);
  fiximate `OrdStatusCodeSet` (code values 0–E); FIX 4.3 Appendix D (Onix);
  fix.dev FIX 4.3 ExecutionReport (35=8). https://www.fixtrading.org/online-specification/order-state-changes/
- `alpacahq/alpaca-py` — `alpaca/trading/enums.py` (`OrderStatus`, `TimeInForce`),
  `models.py` (`filled_qty/filled_avg_price/leaves_qty`, `client_order_id`
  idempotency, `get_order_by_client_id`). (via deepwiki)
- `nautechsystems/nautilus_trader` — `model/enums.py` `OrderStatus`;
  `OrderMatchingEngine` liquidity consumption + `queue_position` +
  `FillModel.prob_fill_on_limit`; `LimitOrderPartialFillModel`; TIF FOK/IOC
  immediate-cancel. (via deepwiki + exa `model/enums.py`, `docs/concepts/{orders,execution}.md`)
- `QuantConnect/Lean` — `OrderStatus` enum; `EquityFillModel.MarketFill` (100%
  one-price default); `PartialMarketFillModel`/`CustomPartialFillModel`;
  MarketOnOpen/Close auction fills. (via deepwiki)
- `mementum/backtrader` — `backtrader.fillers` (`FixedSize`, `FixedBarPerc`,
  `BarPointPercVolume`); *Volume Filling* blog (2016-07-14). https://www.backtrader.com/blog/posts/2016-07-14-volume-filling/volume-filling/
- SQLite — `wal.html` (single-writer serialization under WAL),
  `begin-concurrent.md`, `lang_conflict.html`/`lang_insert.html`
  (`INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`), `doc/wal-lock.md`. https://sqlite.org/wal.html
- Internal grounding: `hermes_quant/react/paper.py:54`,
  `react/slippage_model.py:120,138,240`, `daemon/signal_bus.py:44`,
  `proposals.py` (approval machine), `ops/scripts/quant-autonomous-tick.py:144`,
  `docs/adr/ADR-0070-paper-execution-fidelity.md`,
  `docs/adr/ADR-0029-multi-leg-paper-reactor.md`.
