# ADR-0078: Order-lifecycle state machine + fill realism + exactly-once idempotency

**Status:** Proposed
**Date:** 2026-05-30
**Wave:** D (paper-trading fidelity)
**Supersedes:** nothing
**Cites:** [ADR-0001](ADR-0001-sidecar-architecture.md) (replayability — append-only event log, never a mutable status column), [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate is FINAL authority), [ADR-0029](ADR-0029-multi-leg-paper-reactor.md) (multi-leg reactor; `EXPIRED` reserved for GTC/GTD options), [ADR-0070](ADR-0070-paper-execution-fidelity.md) (slippage *price*; this ADR adds the orthogonal *structural* realism + reuses `seed_for_fill`), `hermes_quant.react.paper.PaperReactor.execute`, `hermes_quant.daemon.signal_bus.append_locked` (existing per-file `flock`), `ops/scripts/quant-autonomous-tick.py::fired_today` (the ad-hoc per-symbol-per-ET-day dedup this generalizes)

Grounded in [docs/research/2026-05-30-r-order-lifecycle-fills.md](../research/2026-05-30-r-order-lifecycle-fills.md).

---

## Context

Three structural fidelity gaps in the paper-execution path, all surfaced by the
six-model critique and root-caused in the research note. None are "the math is
wrong" bugs — they are "the simulation hides a class of reality, and a class of
reality can corrupt the book."

### C1. Paper fills are 100% at one price — partial/reject never happen

`PaperReactor.execute` (`hermes_quant/react/paper.py:54`) writes a single
`ExecutionRecord` with `fill_price = decision_price` (the price part is what
ADR-0070 addresses) and `fill_size_pct = target_pct` — a **100%, single-event,
instantaneous fill**. The `ExecutionRecord` IS the terminal state; there is **no
`OrderState`**, no `PENDING`, no `PARTIAL`, no `REJECTED`, no `CANCELED`. A paper
order that a live broker would partially fill, reject (no locate, halted name,
risk refusal), or leave working-then-cancel-at-EOD instead lands as a clean full
fill in the book.

The consequence is not merely optimistic P&L. The **positions reconciler shipped
2026-05-27 to handle the partial/reject case has never once been exercised** —
because the paper layer cannot produce a partial or a reject. We are shipping a
recovery path with zero test coverage against the exact inputs it exists for.
(Note: 100%-at-one-price is the *industry-default* sim behavior — even Lean's
`EquityFillModel.MarketFill` fills fully in one `OrderEvent` unless you install a
custom `IFillModel`. So this is a default-OFF *refinement*, not a correctness bug
in the existing default — the rails' flag-gated-rollout discipline applies.)

### C2. No idempotency across 6 event stores — the 880%-gross artifact

There are 6 append-only event stores under `~/.hermes/quant/`
(`executions.jsonl`, `signals.jsonl`, `decisions.jsonl`, `reflections.jsonl`,
`proposals.jsonl`, `propagation-log.jsonl`) plus SQLite projections (`state.db`,
`proposals.db`, `watermarks.db`). Today they are serialized only by
`fcntl.flock` (`daemon/signal_bus.py::append_locked`), which gives **byte-level
append atomicity** (no torn lines) and **zero logical dedup**. Two cron surfaces
that both decide "buy AAPL today" each append a valid line.

The only existing dedup is `fired_today()` (`ops/scripts/quant-autonomous-tick.py:144`),
an ad-hoc guard that re-reads a JSONL and suppresses one fill **per symbol per ET
day** for **one** of the eleven surfaces. The Kimi root-cause of the **880%-gross
"blown-up book"** artifact was an `executions.jsonl` reconstruction that
**double-counted** because a fill family re-fired and was reconciled *once, not
root-fixed*. The rail that came out of it — *never trust an `executions.jsonl`
reconstruction over the `state.db` positions table* — is a band-aid over a missing
structural exactly-once primitive.

### C3. No tick semaphore — 11 synchronous crons race on shared SQLite

Eleven synchronous cron surfaces (`autonomous-tick`, `playbook-tick`,
`daily-interim`, …) fire on overlapping schedules and all mutate the same
`state.db`. SQLite WAL mode permits many readers but **serializes writers to
exactly one**; with no coordination, two surfaces firing in the same minute hit
either `SQLITE_BUSY` (→ a silent-error cron crashes or retries-and-double-writes,
the worst failure mode per the rails) or a **check-then-act race across stores**
(claim → broker → append executions → update state.db is *not* atomic across
stores; interleaving produces the half-written cross-store state Kimi described
as "`state.db` cached corruption"). There is **no global tick lock**. This is the
direct structural root of the 880% artifact in C2.

---

## Considered options

### Option (a) — Full `OrderState` machine + dedup keys + flock tick semaphore *(chosen)*

Ship all three together: an `OrderState` enum with an append-only `OrderEvent`
log + derived current-state projection; a deterministic `{cron}:{ts}:{symbol}:{hash}`
dedup key enforced by a SQLite `UNIQUE` ledger (`INSERT OR IGNORE`); and a global
`fcntl.flock` tick semaphore serializing the eleven firing surfaces. Behavior-changing
parts (partial/reject modeling) are DEFAULT-OFF behind `HERMES_QUANT_ORDER_LIFECYCLE=1`;
the hardening parts (dedup ledger, tick lock) are pure-safety serialization with no
happy-path behavior change and are argued always-on (see Decision §D78.6).

- **Pros:** Fixes all three gaps as one coherent layer — and they *are* one layer:
  the dedup key is the order's identity, the state machine is what that identity
  transitions through, and the tick lock is what stops two identities from racing.
  The partial/reject states finally exercise the never-tested positions reconciler
  (C1). The dedup ledger gives the structural exactly-once that retires the
  "trust state.db over JSONL" band-aid (C2). The tick lock removes the cross-store
  race at the root (C3). Mirrors Alpaca's first-class `client_order_id` idempotency
  — we set `client_order_id = dedup_key` for broker-side defense-in-depth.
- **Cons:** Largest surface area of the three options; touches `PaperReactor`, a
  new event store, every SQLite open (`busy_timeout` + WAL), and every cron entry
  point (wrap in the tick lock). More to get right, more to test. Mitigated by the
  flag (partial/reject OFF by default → bit-identical fills until flipped) and by
  the hardening pieces being independently provable.

### Option (b) — Idempotency-only, without partial-fill modeling

Ship the dedup ledger + tick semaphore (C2, C3) but keep the 100%-one-price fill
(skip C1's `OrderState`/partial/reject).

- **Pros:** Smallest, fastest, highest-certainty fix. Directly kills the 880%
  double-count (the actual money-bug that already bit us) without touching fill
  semantics. Zero change to the fill distribution → zero regime shift for downstream
  reflectors/calibrators.
- **Cons:** Leaves the positions reconciler **still untested** — the partial/reject
  inputs it exists for still never occur. Leaves live-vs-paper structurally divergent
  (paper still cannot "not fill"). We would have to author a *second* ADR later for
  the state machine, and retrofit `OrderState` onto an `executions.jsonl` schema that
  this ADR can design correctly the first time. Defers, doesn't avoid, the work.

### Option (c) — Partial-fill modeling, without serialization

Ship the `OrderState` machine + partial/reject (C1) but skip the dedup ledger and
tick lock (C2, C3).

- **Pros:** Exercises the reconciler; improves structural fill fidelity.
- **Cons:** **Strictly more dangerous than today.** Partial fills mean *more*
  events per decision (a `PENDING` → `PARTIAL` → `FILLED` family instead of one
  record). Under the un-serialized 11-cron race (C3) and with no dedup (C2), a
  re-fired *family* double-counts worse than a re-fired single record did — it would
  *amplify* the 880% failure mode, not fix it. Building fill realism on top of an
  un-serialized store is building on the exact crack that broke. **Rejected:** never
  ship the event-multiplying feature without first shipping the serialization that
  makes events safe to multiply.

---

## Decision

Adopt **Option (a)**. Concretely:

### D78.1 `OrderState` enum + append-only `OrderEvent` log

New module `hermes_quant/react/order_state.py`:

```python
class OrderState(str, Enum):
    PENDING  = "pending"    # accepted by reactor, no fill yet  (FIX New/Accepted, Lean New/Submitted)
    PARTIAL  = "partial"    # 0 < cum_qty < order_qty
    FILLED   = "filled"     # leaves_qty == 0                   (terminal)
    REJECTED = "rejected"   # refused before any fill — gate/admissibility/broker (terminal)
    CANCELED = "canceled"   # working order killed, leaves_qty > 0 — incl. EOD day-order sweep (terminal)
    # EXPIRED reserved for ADR-0029 GTC/GTD multi-day options; equities fold expiry into CANCELED.
```

Allowed transitions (anything else is a programming error → raise, never silently
coerce):

```
 (risk gate / admissibility) ─reject─► REJECTED                         (terminal)
 (risk gate, admitted)       ─accept─► PENDING ─reject─► REJECTED        (broker refusal after accept)
                                        │ ─fill<100%─► PARTIAL ─remainder─► FILLED   (terminal)
                                        │                 │ ─EOD sweep / cancel─► CANCELED
                                        │ ─fill=100%──────────────────────► FILLED   (terminal)
                                        └ ─cancel / expire / no-fill──────► CANCELED  (terminal)
```

The persisted unit is an append-only **`OrderEvent`**, never a mutable status
column (ADR-0001 replayability). Each event carries:

- `event_type` — the FIX `ExecType(150)` analogue: `accept | partial_fill | fill |
  reject | cancel | expire`. This records the *transition reason* independently of
  the resulting *state*, fixing the "the gate fired but it isn't in the audit trail"
  class of observability gaps.
- FIX-style partial-fill accounting: `order_qty`, `cum_qty`, `leaves_qty`,
  `fill_avg_price` (`order_qty = cum_qty + leaves_qty`).
- `dedup_key` (D78.3), `asof` (UTC decision time), `proposal_id`, `cron_surface`.

**Current state is a derived projection** over the event log, never stored as
authoritative mutable state. `REJECTED` is reachable *after* `PENDING` (broker
refuses a locate / halt), not only at submission — preserve the FIX lesson.

### D78.2 Partial-fill model — conservative, deterministic, DEFAULT-OFF

Behind `HERMES_QUANT_ORDER_LIFECYCLE=1` (when unset/`0`, `PaperReactor` emits a
single `accept`+`fill` `OrderEvent` family that is **economically bit-identical**
to today's one `ExecutionRecord` — full fill, no partial, no reject):

1. **Liquidity cap = % of bar volume** (backtrader `FixedBarPerc` idea):
   `allowed_qty = min(order_qty, participation_pct × bar_volume)`, conservative
   `participation_pct` (1–5%). For our ≤50 liquid names at ≤20% NAV this is
   ~always 100% → **the common case stays a full fill** (honest: we deliberately
   trade liquid names). It only bites thin names / outsized orders — exactly where
   live would too.
2. **Marketable-limit as the default paper order type** (bounds worst-case
   slippage AND models the real "gapped away → didn't fill" → `PARTIAL`/`CANCELED`
   outcome). Resting-limit fill-on-touch uses a **deterministic** fill probability
   seeded by **`seed_for_fill(proposal_id, asof_execution)`** — reused verbatim
   from `react/slippage_model.py:120` so replays are bit-identical (ADR-0009
   replay-equality).
3. **TIF = DAY only** for equities. The **EOD sweep** job transitions any
   surviving `PENDING`/`PARTIAL` day order's unfilled remainder → `CANCELED` (FIX
   `DoneForDay`). **This is the input the positions reconciler has never seen.**
4. **Halted symbol → `REJECTED` at the gate**, never a phantom fill. This is
   pre-trade admissibility (the ShortabilityOracle's sibling), so it belongs in the
   gate, not the fill model.
5. **Fill price** continues to come from ADR-0070's `apply_slippage`, applied
   per-fill-event. This ADR is the *structural* (quantity/timing/reject) layer;
   ADR-0070 is the *price* layer. Orthogonal, composable.

No full LOB simulator — out of scope and over-engineered for a single-operator
daily picker.

### D78.3 Dedup key — the exactly-once identity

```
dedup_key = f"{cron_surface}:{decision_bucket_ts}:{symbol}:{payload_hash}"
```

- `cron_surface` — which of the 11 firing surfaces (also fixes the `play_tag`-all-
  read-`advisor` observability gap: the surface is now in the key).
- `decision_bucket_ts` — the **decision-time bucket** (ET trading-day for daily
  cadence, or the 30-min tick boundary for the autonomous tick), **UTC, derived
  from `asof` (decision/publication time), NEVER wall-clock-now** (lookahead-honesty
  rail). Generalizes `fired_today()`'s per-symbol-per-ET-day guard.
- `symbol` — the instrument (OCC-21 string for options once ADR-0029 lands).
- `payload_hash` — short hash of the *decision-defining* fields (direction,
  target_pct, recipe/play). A genuinely different decision (flipped direction,
  resized) is **not** suppressed; an identical re-fire **is**.

Set Alpaca `client_order_id = dedup_key` so the **broker** also rejects the
duplicate even if our local guard is bypassed — defense-in-depth, matching the
codebase's existing three-validation-points style.

### D78.4 Idempotent append — SQLite `UNIQUE` + `INSERT OR IGNORE` ledger

`flock` cannot express "insert only if absent" across a crash/retry. The
exactly-once write primitive is a SQLite `UNIQUE` constraint + claim-before-write,
in a small ledger table (extend `watermarks.db` or a new `orders.db`):

```sql
CREATE TABLE IF NOT EXISTS fired_ledger (
  dedup_key  TEXT PRIMARY KEY,   -- {cron}:{ts}:{symbol}:{hash}
  cron       TEXT NOT NULL,
  asof       TEXT NOT NULL,      -- UTC decision time
  created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO fired_ledger(dedup_key, cron, asof, created_at) VALUES (?, ?, ?, ?);
```

`changes() == 1` ⇒ we own this fire ⇒ proceed to broker + append the JSONL stores.
`changes() == 0` ⇒ a prior fire already claimed this key ⇒ **silently skip**
(silence-by-default). The JSONL stores stay append-only and human-greppable; the
SQLite ledger is the *authority* on "did this fire?" — consistent with the
"state.db over JSONL-reconstruction" rail, and the structural fix that finally
retires it as a band-aid. This extends the existing `watermarks.db` pattern.

### D78.5 Global tick semaphore — `flock` on `tick.lock`

New `hermes_quant/daemon/tick_lock.py` providing `with global_tick_lock():`, an
exclusive `fcntl.flock` on `~/.hermes/quant/tick.lock` (generalizing the
per-append `signal_bus.append_locked` pattern to a *whole-tick* critical section).
Each of the 11 surfaces acquires it before its `decide → gate → react → record`
sequence and releases after — turning "11 crons racing on state.db" into "11 crons
taking turns." Additionally, every SQLite open gets `PRAGMA journal_mode=WAL;` +
`PRAGMA busy_timeout=<ms>;` and the claim+cross-store write is wrapped in one
transaction. The lock is advisory + local-home (no NFS) — the caveat
`signal_bus.py` already documents.

### D78.6 What is DEFAULT-OFF vs ALWAYS-ON — and why

- **DEFAULT-OFF behind `HERMES_QUANT_ORDER_LIFECYCLE=1`:** the partial-fill /
  reject / EOD-cancel **behavior change** (D78.2). It changes the fill distribution
  consumed by reflectors / calibrators / Sharpe estimates, so it is an operator-flip
  decision after a side-by-side tick-log audit (the B12 / ADR-0070 discipline). When
  OFF, fills are economically identical to today.
- **ALWAYS-ON (justified):** the dedup ledger (D78.3/D78.4) and the tick semaphore
  (D78.5). These are **pure-safety serialization with no happy-path behavior
  change** — they can only ever *prevent* a double-fire or a cross-store race, never
  *cause* one. A correct system fires each decision exactly once whether or not the
  guard is present; the guard only changes behavior in the buggy duplicate-fire case
  the 880% artifact proved is reachable. Gating a guard that can only make a money-bug
  *less* likely behind an opt-in flag would mean shipping the known money-bug as the
  default — which violates silence-by-default. **Therefore the hardening ships on by
  default**, with an escape hatch `HERMES_QUANT_TICK_LOCK=0` / `HERMES_QUANT_DEDUP_LEDGER=0`
  to disable *only if* a regression is observed (kept so a serialization deadlock can
  never wedge the daemon with no way out). The state machine and partial-fill modeling
  remain gated.

The risk gate (ADR-0004) stays FINAL authority throughout: a `REJECTED` order is a
gate/admissibility decision, and nothing in this lifecycle layer can amplify a
position or override a silence — the LLM/committee remain evidence-only.

---

## Consequences

**Positive:**

- The positions reconciler is finally exercised by real `PARTIAL`/`CANCELED`/`REJECTED`
  inputs (closes the never-tested-recovery-path gap).
- The 880%-gross double-count is **structurally** impossible (dedup ledger), not
  reconciled-after-the-fact.
- The 11-cron cross-store race is removed at the root (tick semaphore + WAL +
  `busy_timeout`), retiring the "silent cron crash on `SQLITE_BUSY`" failure mode.
- A FIX-grade audit trail (`event_type` + `cum/leaves/avg` accounting) makes "what
  state, via what event" queryable — fixes a class of "fired but not in the trail"
  observability gaps.
- Paper fidelity moves closer to live (orders can *not* fill), composing cleanly
  with ADR-0070's price model.

**Negative / risks:**

- **Largest blast radius of the Wave-D fidelity ADRs.** Touches `PaperReactor`, a
  new event store, every SQLite open, and all 11 cron entry points. Mitigated by:
  partial/reject is flag-gated (OFF → bit-identical fills); the hardening is
  independently provable and reversible via escape-hatch env.
- **Regime shift when the flag flips** (same hazard as ADR-0070): downstream
  reflectors/calibrators consuming `executions.jsonl` see partial/reject events for
  the first time. Mitigated by the side-by-side burn-in before the operator flips
  `HERMES_QUANT_ORDER_LIFECYCLE=1`.
- **Tick-lock contention / deadlock risk.** A held `flock` from a crashed cron
  could stall the others. Mitigated by: `flock` is released on process death by the
  kernel; a `busy_timeout` bound on the SQLite side; and the `HERMES_QUANT_TICK_LOCK=0`
  escape hatch. Lock-hold time must be bounded (the critical section is decide→record,
  not any network wait that can hang).
- **`payload_hash` design is load-bearing.** Too coarse → suppresses a legitimately
  different decision (a silent missed trade — the dangerous direction). Too fine →
  fails to dedup a true re-fire. Must hash exactly the decision-defining fields and
  is covered by an explicit test (below). Erring toward *finer* (fail-to-dedup is
  caught by the ledger UNIQUE on identical re-fires; over-suppression is silent).
- **Schema migration of `executions.jsonl`.** New `OrderEvent` records coexist with
  legacy terminal `ExecutionRecord` rows. Readers must tolerate both; the projection
  treats a legacy record as `accept`+`fill`.

**Out of scope:**

- Borrow / locate fees and hard-to-borrow availability on shorts — the
  ShortabilityOracle owns admissibility; this layer only consumes its
  `REJECTED` verdict.
- Full limit-order-book simulation / queue-position microstructure — over-engineered
  for a daily picker.
- Auction (`opg`/`cls`) fill *timing* — noted in research §4 as a future amendment;
  ADR-0070 already models the auction *premium*.
- `EXPIRED` state for GTC/GTD multi-day options — reserved for ADR-0029.

---

## Rollout

1. **Land DEFAULT-OFF + hardening-on.** Merge `order_state.py`, the dedup ledger,
   and `tick_lock.py`. `HERMES_QUANT_ORDER_LIFECYCLE` unset → fills bit-identical to
   today. Dedup ledger + tick lock on by default (D78.6); escape-hatch env documented.
2. **Burn-in the hardening (always-on path) for one trading day.** Confirm no
   `SQLITE_BUSY` crashes, no lock stalls, and that the dedup ledger row-count tracks
   the executions count 1:1 (no suppressed-but-should-have-fired, no duplicate-passed).
3. **Side-by-side the lifecycle flag.** Replay recent `executions.jsonl` with
   `HERMES_QUANT_ORDER_LIFECYCLE=1`; diff the fill distribution and P&L delta vs OFF.
   Verify partial/reject/EOD-cancel events appear and that the **positions reconciler
   consumes them correctly** (the whole point).
4. **Operator flip.** After a clean side-by-side, the operator sets
   `HERMES_QUANT_ORDER_LIFECYCLE=1` on the cron wrappers. The flip is the operator's
   call, not the code's.
5. **Follow-ups (separate ADRs):** auction-timing order types; `EXPIRED` for ADR-0029
   options; the realized-cost calibration loop (ADR-0070 §D70.5) now also consuming
   partial-fill cost.

---

## Implementation hooks

- New `hermes_quant/react/order_state.py`: `OrderState` enum (+ reserved `EXPIRED`),
  append-only `OrderEvent` dataclass (`event_type`, `order_qty/cum_qty/leaves_qty/
  fill_avg_price`, `dedup_key`, `asof`, `proposal_id`, `cron_surface`), and a
  `project_state(events) -> OrderState` projection with a strict transition validator.
- `hermes_quant/react/paper.py::PaperReactor.execute`: claim `dedup_key` via the
  ledger *before* writing; emit `OrderEvent`s instead of one terminal
  `ExecutionRecord`; gate partial/reject behind `HERMES_QUANT_ORDER_LIFECYCLE`
  (default OFF → single `accept`+`fill` family, economically identical). Reuse
  `seed_for_fill` (`slippage_model.py:120`).
- New `fired_ledger` table (extend `watermarks.db` or new `orders.db`); generalize
  `fired_today()` (`quant-autonomous-tick.py:144`) to the `{cron}:{ts}:{symbol}:{hash}`
  key; set Alpaca `client_order_id = dedup_key`.
- New `hermes_quant/daemon/tick_lock.py`: `global_tick_lock()` context manager
  (exclusive `flock` on `tick.lock`); add `PRAGMA journal_mode=WAL` + `busy_timeout`
  to every SQLite open; wrap each cron's decide→gate→react→record in the lock.
- EOD sweep job: `PENDING`/`PARTIAL` day orders → `CANCELED` at session close.

---

## Verification

```python
# 1. Transition validator rejects illegal transitions; accepts the legal lattice.
from hermes_quant.react.order_state import OrderState, project_state, OrderEvent
assert project_state([accept_ev, partial_ev, fill_ev]) is OrderState.FILLED
assert project_state([accept_ev, cancel_ev]) is OrderState.CANCELED
# FILLED is terminal — a fill after FILLED is a programming error, not a coercion:
import pytest
with pytest.raises(ValueError):
    project_state([accept_ev, fill_ev, fill_ev])

# 2. Default-OFF is economically bit-identical to today (one full fill).
#    With HERMES_QUANT_ORDER_LIFECYCLE unset, PaperReactor emits accept+fill;
#    summed cum_qty == order_qty, fill_avg_price == apply_slippage(decision_price).

# 3. Dedup: identical re-fire is suppressed, different decision is NOT.
k1 = dedup_key("autonomous-tick", bucket_ts, "AAPL", payload_hash(side=+1, pct=0.10, play="csp"))
k2 = dedup_key("autonomous-tick", bucket_ts, "AAPL", payload_hash(side=-1, pct=0.10, play="csp"))
assert k1 != k2                      # flipped direction → different key → both fire
# claim(k1) -> changes()==1 (own it); claim(k1) again -> changes()==0 (skip, silence-by-default)

# 4. decision_bucket_ts derives from asof (decision time), never wall-clock-now.
```

```bash
# Post-burn-in (hardening always-on): ledger 1:1 with executions, no double-count.
~/.hermes/hermes-agent/venv/bin/python3 -c "
import json, sqlite3
from pathlib import Path
fills = [json.loads(l) for l in Path('~/.hermes/quant/executions.jsonl').expanduser().read_text().splitlines() if l.strip()]
today = [f for f in fills if f.get('asof','').startswith('2026-05-31')]
keys = {f.get('dedup_key') for f in today}
print(f'executions={len(today)} distinct_dedup_keys={len(keys)} (expect equal: no double-count)')
assert len(today) == len(keys), 'DUPLICATE FIRE DETECTED'
"
```
