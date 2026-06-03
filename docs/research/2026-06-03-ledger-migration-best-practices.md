# External Best-Practices for the NAV-Fraction → Share-Quantity Ledger Migration (ADR-0086 Phase 2)

**Scope:** Mitigations for the 5 premortem failure modes of changing stored position `quantity`
from a signed NAV-fraction (`-0.2` = 20% short) to actual **share counts**, and cash to real
dollars, in `hermes-quant` (Python paper-trading system).

**Status:** Complete — all 5 modes covered. Synthesized from 8 fast searches + 1 deepwiki call
on `quantopian/zipline` (the canonical reference for mode #1).

---

## Failure Mode #1 — NAV_at_fill bootstrapping circularity

> `shares = fill_size_pct * NAV_at_fill / price`, but NAV derives from positions which derive
> from NAV → first fills 10–100× mis-sized.

### (a) What mature systems do

The premortem assumes a circularity that **does not actually exist in either canonical engine** —
because both snapshot portfolio value at *decision time* from a value that is well-defined even
when positions are empty.

- **zipline** (`deepwiki:quantopian/zipline`): `order_target_percent` / `order_target_value`
  convert percent→shares using `self.portfolio.portfolio_value`, **snapshotted at order-decision
  time** (inside `handle_data`), not at fill time. Crucially:
  `portfolio_value = cash + positions_value`. On the **first orders, `positions_value == 0`, so
  `portfolio_value == capital_base`** — a constant seed, not a derived-from-positions value. There
  is no fixpoint to solve. `_calculate_order_percent_amount` divides by `last_price`; if price is
  missing/zero it raises rather than producing a garbage share count.
  Source: deepwiki `quantopian/zipline`; also `zipline/zipline/api.pyi`
  (https://github.com/quantopian/zipline/blob/master/zipline/api.pyi).
- **QuantConnect LEAN**: `SetHoldings` / `CalculateOrderQuantity` compute units from the **current
  portfolio value and current price at decision time**, adjust for lot size + fee model, and apply
  a **0.25% cash buffer** (`FreePortfolioValuePercentage`) so gaps don't push the position past
  available margin. If the computed quantity rounds to ~0 (below
  `MinimumOrderMarginPortfolioPercentage`) it **places no order at all** rather than emitting a
  degenerate fill. Sources:
  https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/position-sizing ;
  https://www.quantconnect.com/docs/v2/writing-algorithms/migrations/zipline/ordering

**Key insight:** NAV must be sourced from a value that is defined *before* any position exists.
Both engines break the loop by defining `NAV = cash + Σ(position_value)` where the cash term seeds
the very first computation (`positions_value = 0 ⟹ NAV = capital_base`). The fraction→share
conversion is a *decision-time, price-divided* operation, never a fixpoint solve.

### (b) Pattern we should adopt

- **Seed NAV from cash, not from positions.** Define `NAV(t) = cash(t) + Σ_i shares_i · price_i(t)`.
  At t0 with no positions, `NAV = starting_cash`. The first fill's share count is computed against
  this concrete number — no circular dependency.
- **Snapshot NAV at decision time, once per sizing decision**, and pass that frozen scalar into the
  share-conversion. Never recompute NAV mid-conversion (that is what would reintroduce a loop).
- **Guard degenerate inputs:** if `price <= 0` or `price` is stale/missing → raise, don't fill. If
  computed shares rounds below a minimum lot → skip the order (LEAN behavior), don't emit a tiny
  garbage fill.
- Adopt a **cash buffer** (e.g. 0.25–1%) so rounding/gaps can't push the position past cash.

### (c) Code-shape sketch

```python
def shares_for_target_fraction(target_fraction: float, asset: str,
                               nav_at_decision: Dollars, price: Dollars) -> Shares:
    if price <= 0:
        raise ValueError(f"no valid price for {asset}; refusing to size order")
    target_dollars: Dollars = Dollars(target_fraction * float(nav_at_decision))
    raw = target_dollars / price                       # decision-time, price-divided
    qty = round_to_lot(raw)
    if abs(qty) < MIN_LOT:
        return Shares(0)                                # LEAN: place nothing, don't mis-fill
    return Shares(qty)

# NAV seeded from cash; positions_value == 0 at t0 ⟹ NAV == starting_cash. No circularity.
def nav(cash: Dollars, positions: dict[str, Shares], prices: dict[str, Dollars]) -> Dollars:
    pv = sum(float(q) * float(prices[s]) for s, q in positions.items())
    return Dollars(float(cash) + pv)
```

---

## Failure Mode #2 — Share-migration blast radius

> Consumers silently misinterpret `quantity` after the unit flips.

### (a) What mature systems do

- **LEAN** never overloads one field with two meanings: `SecurityHolding.Quantity` is *always*
  shares; portfolio *weight* is a separate concept produced via `PortfolioTarget.Percent`. The two
  unit-systems are kept in distinct types/methods rather than a single polymorphic `quantity`.
  Source: https://www.quantconnect.com/docs/v2/writing-algorithms/portfolio/holdings
- **Event-sourcing schema-versioning guidance** (Oskar Dudycz / event-driven.io; OneUptime):
  treat a unit change as a **breaking schema change** and make it explicit, never silent. The
  recommended mechanic is to **rename the field when its semantics change** so old readers fail
  loudly instead of silently mis-reading. Sources:
  https://event-driven.io/en/simple_events_versioning_patterns ;
  https://oneuptime.com/blog/post/2026-01-30-event-driven-versioning-strategies/view

### (b) Pattern we should adopt

- **Rename on semantic change.** Do NOT keep the field named `quantity` while flipping its meaning.
  Introduce a new field name (`shares`) so any un-migrated consumer raises `KeyError`/type error
  rather than silently treating shares as a fraction. This is the single highest-leverage mitigation
  for blast radius — it converts silent corruption into a loud, greppable failure.
- **Stamp a schema/unit version** in the record (`schema_version`, `quantity_unit="shares"`), and
  have readers assert the version they expect.
- **Two-phase deploy** (event-driven.io): Phase A deploys code that understands *both* the old
  fraction field and the new shares field (dual-read), mark old obsolete; Phase B removes old-field
  handling once no legacy records flow.

### (c) Code-shape sketch

```python
@dataclass(frozen=True)
class Position:
    schema_version: int          # bump to 2 for share semantics
    shares: Shares               # NEW name — old `quantity` is GONE, not reused
    # quantity: float            # ← deliberately removed; un-migrated readers now crash loudly

def load_position(raw: dict) -> Position:
    v = raw.get("schema_version", 1)
    if v == 1:                                   # legacy NAV-fraction record
        return upcast_v1_fraction_to_v2_shares(raw)
    assert raw["quantity_unit"] == "shares", raw # fail loud on unit mismatch
    return Position(2, Shares(raw["shares"]))
```

---

## Failure Mode #3 — Double-clip / race conditions at the order-execution chokepoint

> Concurrent access to a single order-execution chokepoint causes double-fills / lost updates.

### (a) What mature systems do

- **Single-writer / serialized-append** is the standard remedy: funnel all mutations of the ledger
  through one serialization point so read-modify-write cannot interleave. Race-condition literature
  identifies the classic "read-modify-write" hazard (two processes read the same balance, both write
  back) and prescribes serialization or atomic compare-and-set. Sources:
  https://www.aha.io/engineering/articles/off-to-the-races-3-ways-to-avoid-race-conditions ;
  https://aerospike.com/blog/race-conditions-in-high-performance-systems
- **Optimistic concurrency / version stamping**: DynamoDB pattern — each write carries the expected
  `version`; the write succeeds only if the stored version matches (compare-and-set), else retry.
  Source: https://awsfundamentals.com/blog/understanding-and-handling-race-conditions-at-dynamodb
- **SERIALIZABLE isolation** when a DB backs the ledger — reads and writes resolve as-of commit
  time, preventing read-skew that drives double-clips. Source:
  https://dev.to/yugabyte/sql-to-avoid-data-corruption-in-race-conditions-with-serializable-n5c
- Event-sourced stores enforce this natively via **append with expected stream version** (Marten):
  the append fails if another writer advanced the stream. Source:
  https://martendb.io/events/versioning.html

### (b) Pattern we should adopt

- **One serialized writer for the execution ledger.** In a Python process, the cheapest correct
  mechanism is a single `asyncio.Lock`/`threading.Lock` (or a single-consumer queue) wrapping the
  *entire* read-NAV → size → append-fill → update-position critical section, so NAV-read and
  fill-append are atomic (this also re-closes mode #1's loop: nothing can mutate NAV between snapshot
  and append).
- **Append-only log with expected-version (optimistic) check** for durability/idempotency: each
  appended fill carries the ledger sequence number it expected; a mismatch means a concurrent writer
  won → reject + retry. This is the cross-process guard if more than one process can ever write.
- Make the critical section **as small as the read-modify-write**, not the whole strategy tick.

### (c) Code-shape sketch

```python
class ExecutionLedger:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._seq = 0

    async def submit_fill(self, order) -> Fill:
        async with self._lock:                       # single serialized writer
            nav = self.snapshot_nav()                # read
            qty = shares_for_target_fraction(order.frac, order.asset, nav, order.price)
            if qty == 0:
                return Fill.empty(order)
            expected = self._seq                     # optimistic version
            fill = self._append(order, qty, expected_seq=expected)  # CAS append
            self._seq += 1                           # write
            return fill
```

---

## Failure Mode #4 — Replay / reconcile non-idempotency

> Folding a legacy execution log under new share semantics is not idempotent.

### (a) What mature systems do

The event-sourcing community has a mature, named answer: **upcasting at read time + idempotent fold**.

- **Upcasting** (event-driven.io; OneUptime; Marten): old events are *immutable*; you never rewrite
  history. Instead you register an **upcaster** that transforms a v1 event into the current schema
  **at read/replay time**. Upcasters chain (v1→v2→v3) via an `UpcasterRegistry`, and the event store
  wrapper auto-upcasts on read. Sources:
  https://oneuptime.com/blog/post/2026-01-30-event-driven-versioning-strategies/view ;
  https://event-driven.io/en/simple_events_versioning_patterns ;
  https://martendb.io/events/versioning.html
- **Idempotency comes from a pure, deterministic fold**: replaying the same upcast event stream from
  the same seed must always yield identical state. The fold reads inputs only from the (upcast)
  event, never from wall-clock/live NAV. Greg Young's *Versioning in an Event Sourced System* is the
  canonical reference (linked from event-driven.io).
- **Compensate, don't mutate** (Marten): if past data is wrong, append a correcting event rather
  than editing history — keeps replay deterministic.

### (b) Pattern we should adopt

- **Upcast legacy fraction-events to share-events at replay time**, deterministically. The upcaster
  must reconstruct `NAV_at_fill` from data *recorded in the event itself* (or from the running
  replayed NAV), NOT from today's live NAV — otherwise replay is non-idempotent.
- **Pure deterministic fold**: `state_{n+1} = apply(state_n, upcast(event_n))`. No I/O, no clock, no
  external NAV inside `apply`. Re-running over the same log MUST converge to the same ledger.
- **Idempotency keys**: each fill carries a stable id; the fold ignores a fill id it has already
  applied (guards double-replay of overlapping log segments).
- **Snapshot + version the schema** so a replay knows which upcaster chain to run.

### (c) Code-shape sketch

```python
def upcast_fill(ev: dict) -> FillV2:
    if ev["schema_version"] == 2:
        return FillV2(**ev)
    # v1: fraction → shares using NAV RECORDED IN THE EVENT (deterministic), not live NAV
    nav = Dollars(ev["nav_at_fill"])            # must be persisted in legacy log
    shares = Shares(round_to_lot(ev["fraction"] * float(nav) / ev["price"]))
    return FillV2(id=ev["id"], shares=shares, price=Dollars(ev["price"]), schema_version=2)

def replay(events: Iterable[dict], seed: Ledger) -> Ledger:
    state, seen = seed, set()
    for raw in events:
        f = upcast_fill(raw)
        if f.id in seen:        # idempotency key → safe to re-run overlapping segments
            continue
        seen.add(f.id)
        state = apply_pure(state, f)   # no clock, no live NAV, no I/O
    return state
```

> **Migration prerequisite:** if the legacy log did **not** persist `nav_at_fill`, you cannot
> deterministically reconstruct shares. Capture/derive it during a one-time forward replay and
> snapshot the result; thereafter replay from the v2 snapshot. (Flagged as a hard precondition.)

---

## Failure Mode #5 — Unit confusion (shares vs fraction vs dollars)

> Values rendered/passed without semantic clarity.

### (a) What mature systems do

- **Distinct types per unit, checked statically.** Python's `typing.NewType` (PEP 484) creates
  zero-runtime-cost distinct types; **mypy** then rejects passing `Dollars` where `Shares` is
  expected. This is the lightweight, idiomatic answer for money-adjacent code. Sources:
  https://mypy-lang.org ;
  https://www.theodo.com/blog/mypy-get-rid-of-python-bugs-with-static-type-checking
- **pint** for full dimensional analysis: quantities carry units and pint *raises at runtime* on
  nonsensical unit blends. Heavier (runtime objects) but catches conversions `NewType` can't.
  Sources: https://github.com/hgrecco/pint ; https://pint.readthedocs.io/en/stable/getting/tutorial.html
- **LEAN** keeps the units in separate API surfaces (shares = `Quantity`, weight = `PortfolioTarget`,
  account-currency dollars = `GetQuantityValue`) rather than one ambiguous number.

### (b) Pattern we should adopt

- **`NewType` for the three units** (`Shares`, `Dollars`, `NavFraction`) as the primary guard —
  free at runtime, enforced by mypy in CI. This directly reinforces mode #2 (a function expecting
  `Shares` won't silently accept the old fraction `float`).
- **Reserve pint** for the conversion boundary only (the fraction→dollars→shares pipeline), if you
  want runtime dimensional guarantees; don't pint-ify the whole ledger (overhead).
- **Render with the unit attached** in every log/repr (`"qty=300 sh"`, `"$4,512.00"`,
  `"frac=-0.20"`) so humans and downstream parsers never guess.
- Run **mypy in `--strict` on the ledger package** as a CI gate.

### (c) Code-shape sketch

```python
from typing import NewType
Shares      = NewType("Shares", float)
Dollars     = NewType("Dollars", float)
NavFraction = NewType("NavFraction", float)

def size(frac: NavFraction, nav: Dollars, px: Dollars) -> Shares: ...

size(NavFraction(-0.2), Dollars(10_000), Dollars(33.0))   # ok
size(Shares(300), Dollars(10_000), Dollars(33.0))         # mypy ERROR: Shares ≠ NavFraction

def render(q: Shares) -> str:  return f"{float(q):,.0f} sh"     # unit always attached
def render_usd(d: Dollars) -> str: return f"${float(d):,.2f}"
```

---

## Top 5 concrete recommendations for ADR-0086 Phase 2

1. **Seed NAV from cash, snapshot once at decision time** (zipline/LEAN model):
   `NAV = cash + Σ(shares·price)`; at t0 with no positions `NAV = starting_cash`, so the
   circularity in mode #1 simply doesn't arise. Freeze that scalar before sizing and never recompute
   NAV inside the fraction→share conversion. Guard `price<=0` (raise) and sub-lot quantities (skip
   the order, LEAN-style) instead of emitting garbage fills. Add a 0.25–1% cash buffer.

2. **Rename `quantity` → `shares` (do NOT reuse the field)** and stamp `schema_version=2` +
   `quantity_unit="shares"`. Renaming-on-semantic-change converts mode #2's silent misinterpretation
   into a loud `KeyError`/assert. Deploy in two phases (dual-read old+new, then drop old).

3. **Serialize the execution chokepoint behind a single writer.** Wrap the whole
   read-NAV → size → append-fill → update-position critical section in one lock (or single-consumer
   queue) so it is atomic; add an expected-sequence (optimistic CAS) check on append for the
   cross-process case. This kills mode #3 double-clips *and* re-closes mode #1's window.

4. **Make replay idempotent via read-time upcasting + a pure deterministic fold** (event-sourcing
   standard). Reconstruct shares in the upcaster from `nav_at_fill` **persisted in the legacy event**
   (never live NAV); dedupe by stable fill id; never mutate history (compensate with new events).
   **Precondition:** if the legacy log lacks `nav_at_fill`, do a one-time forward replay to capture
   it, snapshot to v2, then replay only from the v2 snapshot.

5. **Introduce `NewType` units (`Shares`/`Dollars`/`NavFraction`) and gate the ledger package with
   mypy `--strict` in CI**; attach units in every render/log line. Optionally use `pint` only at the
   fraction→dollars→shares conversion boundary for runtime dimensional checks. This mitigates mode #5
   and structurally reinforces #2.

---

## Sources

- deepwiki: `quantopian/zipline` — `order_target_percent`/`order_target_value` use
  `portfolio.portfolio_value` at decision time; `portfolio_value = cash + positions_value`
  (= `capital_base` when empty) → no circularity.
- https://github.com/quantopian/zipline/blob/master/zipline/api.pyi
- https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/position-sizing
- https://www.quantconnect.com/docs/v2/writing-algorithms/portfolio/holdings
- https://www.quantconnect.com/docs/v2/writing-algorithms/migrations/zipline/ordering
- https://event-driven.io/en/simple_events_versioning_patterns
- https://oneuptime.com/blog/post/2026-01-30-event-driven-versioning-strategies/view
- https://martendb.io/events/versioning.html  (+ Greg Young, *Versioning in an Event Sourced System*)
- https://www.aha.io/engineering/articles/off-to-the-races-3-ways-to-avoid-race-conditions
- https://aerospike.com/blog/race-conditions-in-high-performance-systems
- https://awsfundamentals.com/blog/understanding-and-handling-race-conditions-at-dynamodb
- https://dev.to/yugabyte/sql-to-avoid-data-corruption-in-race-conditions-with-serializable-n5c
- https://mypy-lang.org
- https://www.theodo.com/blog/mypy-get-rid-of-python-bugs-with-static-type-checking
- https://github.com/hgrecco/pint  ;  https://pint.readthedocs.io/en/stable/getting/tutorial.html
