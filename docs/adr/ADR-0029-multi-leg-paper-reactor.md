# ADR-0029: Multi-Leg Paper Reactor

**Status:** Proposed
**Date:** 2026-05-24
**Related:** ADR-0008 (signal bus), ADR-0014 (chat-mode advisor), ADR-0015 (HITL propose-decide-react), ADR-0016 (autonomous mode), ADR-0027 (options risk gate), ADR-0028 (options data layer)

---

## Context

The hermes-quant reactor surface defined in ADR-0008 and extended through ADR-0014/0015/0016 was designed around single-instrument equity orders. Each `Proposal` carries one symbol, one side, one quantity expressed as a fraction of NAV from the discrete action space (0, ±0.05, ±0.10, ±0.15, ±0.20). The HITL flow approves or rejects the proposal as a single atomic unit, the broker adapter emits one order, and the settlement loop emits one `RealizedOutcome` row per fill. This shape works cleanly for stocks but breaks down completely for options strategies.

Options force a different topology for four concrete reasons:

1. **Atomicity.** A vertical spread, iron condor, or covered call only has the risk profile we modeled if *all* legs fill together. Per-leg sequencing leaves naked exposure between fills, which violates ADR-0027's defined-risk requirement.
2. **Symbol shape.** Equities use plain tickers; options use OCC-21-character contract symbols (e.g., `NVDA260526C00145000`) that are not interchangeable with the underlying ticker and must be constructed deterministically from `(underlying, expiry, right, strike)`.
3. **Paper-specific settlement delay.** Alpaca's paper engine simulates assignment, exercise, and expiry, but the resulting non-trade activities (NTAs) only surface on `/v2/account/activities` the *next calendar day*, even though buying power and positions update instantly. This breaks the "settle on fill" assumption baked into the equity settlement loop.
4. **Net-greeks reasoning.** A human approving a spread cannot evaluate it leg-by-leg; the relevant figures are *net* delta/gamma/theta/vega and the bracket of `(max_loss, max_gain, breakevens)`. The current proposal display has no slot for these.

ADR-0028 already gives us OCC-keyed snapshots, per-leg greeks, and a normalized `OptionLeg` dataclass. ADR-0027 gives us the pre-trade risk gate that rejects undefined-risk structures. What we're missing is the reactor-side wiring: how a multi-leg strategy is proposed, displayed, approved atomically, routed to the broker as a single `mleg` order, and reconciled across the next-day NTA delay. This ADR specifies that wiring and locks it to **paper trading + HITL only** until we have empirical evidence that the loop is well-behaved.

---

## Decision

We introduce a multi-leg paper reactor consisting of seven sub-decisions, D1–D7. Together they cover symbol handling, order shape, settlement reconciliation, outcome accounting, the proposal dataclass, the HITL approval semantics, and the live-trading non-goal.

### D1 — OCC symbol normalization

All options symbols passing through hermes-quant are OCC-21 strings, constructed and parsed exclusively through a new helper module `hermes_quant/options/occ.py`:

```python
def format_occ(underlying: str, expiry: date, right: Literal["C", "P"], strike: Decimal) -> str:
    """Build an OCC-21 symbol. Strike is multiplied by 1000 and zero-padded to 8 digits."""

def parse_occ(symbol: str) -> OccComponents:
    """Inverse of format_occ. Raises OccParseError on malformed input."""

@dataclass(frozen=True)
class OccComponents:
    underlying: str
    expiry: date
    right: Literal["C", "P"]
    strike: Decimal
```

Rationale: Alpaca's data API and order API both key on the same OCC string, with no Alpaca-side alias or shorthand. Centralizing format/parse in one module means the rest of the codebase never builds OCC strings by hand and we get a single place to add fuzz tests. The strike is `Decimal`, not `float`, to avoid the rounding hazard at `×1000` (e.g., `145.005 * 1000` in float yields `145004.999...`).

### D2 — Adopt `order_class: "mleg"` uniformly

All option orders — including degenerate single-leg cases like a long call thesis — are submitted with `order_class: "mleg"` and a `legs` array. We do **not** maintain a parallel single-leg option order path.

```python
{
  "order_class": "mleg",
  "legs": [
    {"symbol": "NVDA260526C00145000", "side": "buy", "qty": "1", "type": "limit", "limit_price": "2.50"},
    {"symbol": "NVDA260526C00150000", "side": "sell", "qty": "1", "type": "limit", "limit_price": "1.80"}
  ],
  "time_in_force": "day"
}
```

Rationale: Alpaca treats multi-leg generically — there is no native enum for `iron_condor` or `vertical_spread`. The strategy *kind* is a hermes-quant concept, not a broker concept. By normalizing every options order to the `mleg+legs` shape, the broker adapter has exactly one code path for options, the proposal store has one schema, and replay works uniformly. A small `StrategyBuilder` helper (in `hermes_quant/options/multileg.py`) emits the correct `legs` array for each supported recipe (`covered_call`, `cash_secured_put`, `vertical_spread`, `iron_condor`, `calendar_spread`, `butterfly`, `leaps_thesis`, `swing_directional`).

### D3 — Next-day NTA reconciliation in settlement_loop

The existing settlement loop assumes outcomes finalize on fill. For options, expiry, assignment, and exercise are **non-trade activities** that only appear on `/v2/account/activities` the *next* calendar day. We add a dedicated step `reconcile_options_ntas` that runs at **06:00 ET each weekday** before market open:

1. Pull `/v2/account/activities` for the prior session date, filtered to NTA types (`OPEXP`, `OPASN`, `OPXRC`).
2. For each NTA, look up the originating `multi_leg_id` via the OCC symbol → open-position index.
3. Emit a `RealizedOutcome` with `outcome_type ∈ {expiry_otm, expiry_itm, assignment, exercise}` and stamp `nta_visible_at` with the activity's surface time.
4. If assignment occurred, write the resulting underlying share delta as a separate `RealizedOutcome` row keyed to the same `multi_leg_id`.

Positions and buying power update instantly on the paper side, so the *cash and Greek* views are correct intraday; the NTA reconciliation is purely for outcome accounting and replay determinism.

### D4 — `RealizedOutcome` extension

`RealizedOutcome` gains the following fields (existing equity rows leave them as defaults):

```python
outcome_type: Literal["fill", "expiry_otm", "expiry_itm",
                      "assignment", "exercise", "closed_early"]
multi_leg_id: str | None             # groups all legs of one proposal
per_leg_outcomes: tuple[LegOutcome, ...]   # per-leg P&L, fill price, fees
net_pnl_at_close: Decimal | None     # if any leg closed before expiry
net_pnl_at_expiry: Decimal | None    # terminal P&L after settlement
assigned_underlying_qty: int         # signed; 0 for non-assignment
nta_visible_at: datetime | None      # when /activities surfaced this
```

`net_pnl_at_close` and `net_pnl_at_expiry` are kept distinct because a trader may close one leg of a spread early and let the other expire; the two figures are not always equal and the analytics layer needs both.

### D5 — `MultiLegProposal` dataclass

Multi-leg strategies flow through a frozen dataclass that supersedes the equity `Proposal` for options:

```python
@dataclass(frozen=True)
class MultiLegProposal:
    proposal_id: str                       # uuid7
    asof: datetime                         # UTC
    strategy_kind: str                     # 'covered_call' | 'vertical_spread' | ...
    underlying: str
    legs: tuple[OptionLeg, ...]            # OptionLeg from ADR-0028
    net_greeks: NetGreeks                  # delta, gamma, theta, vega — net
    bpr_estimate: Decimal                  # buying power reduction
    max_loss: Decimal                      # USD, defined-risk only
    max_gain: Decimal | None               # None = unbounded → blocked by 0027
    breakeven_underlying: tuple[Decimal, ...]
    rationale: str
    source_recipe_id: str
    risk_gate_pass: bool                   # set by ADR-0027 gate
    risk_gate_warnings: tuple[str, ...]
```

The proposal is immutable; if the recipe wants to revise, it emits a new `proposal_id`. `risk_gate_pass=False` proposals are persisted (for replay/audit) but never surfaced to the HITL channel.

### D6 — Atomic HITL approval

The chat surface (per ADR-0014/0015) renders a multi-leg proposal as a single block with leg-by-leg indentation:

```
[PROP ab12…] covered_call NVDA  Δ -0.30 / Γ -0.02 / Θ +0.45/d / ν -0.18
  ├ BUY  100 NVDA @ market (collateral)
  └ SELL 1  NVDA 260526C 145.00 @ 2.50
  BPR $14,250 · max_loss $14,250 · max_gain $1,250 · BE $142.50
  [approve] [reject]  source: recipe.cc_v3
```

Approval and rejection are **whole-proposal atomic**. The proposal-store schema enforces this at the type level: an approval row must reference the `proposal_id`, never an individual leg index. Attempting to write a partial-approval row raises `PartialApprovalForbidden` at the store boundary. On approval, the broker adapter emits exactly one `mleg` order; on rejection, no order is emitted and the proposal moves to `rejected` state with the user's optional reason string.

This is not just a UX choice — it is a correctness invariant. A leg-by-leg approval flow would let a user accept the short call and reject the underlying purchase of a covered call, producing a naked short position that ADR-0027 would have blocked at proposal time. By making the schema reject partial approvals, we make that bug unrepresentable.

### D7 — Paper + HITL only; live multi-leg deferred

This ADR scope is explicitly **paper trading with human-in-the-loop approval**. Autonomous (ADR-0016) multi-leg execution and live-broker multi-leg execution are out of scope and remain blocked until:

1. Paper has accumulated **60+ trading days** of multi-leg activity, *and*
2. The ADR-0027 risk gate has shown a green-flag rate consistent with its calibration on real (paper) outcomes, *and*
3. A subsequent ADR (TBD, working title ADR-00XX "Live Multi-Leg Promotion") explicitly authorizes the transition.

Until then, any code path that would route a multi-leg order to a live account raises `LiveMultiLegNotAuthorized` at the broker adapter boundary. This is a hard refusal, not a config flag — flipping it requires a code change reviewed against the promotion ADR.

---

## Consequences

**Positive.** Spreads execute atomically at the broker boundary, eliminating the partial-fill naked-position class of bug entirely. Options outcomes are fully replayable from disk: every proposal, approval, fill, and NTA is persisted with stable IDs, so a settlement-loop bug can be diagnosed by replaying yesterday's NTAs against today's code. The paper-only posture gives us a confidence path — we accumulate evidence on the same code that will eventually run live, rather than rewriting at the live boundary.

**Negative.** The settlement loop now has next-day asynchronous behavior, which is genuinely harder to reason about than the synchronous equity path. Reconciling NTAs against open positions requires a durable index of `OCC symbol → multi_leg_id` that survives process restarts. OCC parsing must be bulletproof — a single off-by-one in the strike-padding logic silently corrupts every downstream record. We also accept that buying power, position, and outcome views are temporarily inconsistent for ITM expiries between market close and the next 06:00 ET reconcile pass; downstream consumers must tolerate this window.

**Neutral.** One additional dataclass (`MultiLegProposal`) and one additional outcome enum variant set. The HITL surface gains a multi-line rendering path but reuses the existing approve/reject buttons.

---

## Alternatives Considered

**Per-leg orders with post-fill aggregation.** Submit each leg as an independent single-leg order and aggregate fills client-side into a synthetic spread record. Rejected: any partial-fill scenario leaves a naked position between leg fills. We could try to unwind on partial fill, but that's exactly the kind of error-recovery code that goes wrong under stress. The broker offers atomic multi-leg execution natively; refusing to use it is strictly worse.

**Strategy-enum dispatcher with hardcoded strategy types in the order body.** Have hermes-quant emit `order_class: "iron_condor"` etc. and let the broker figure out the legs. Rejected on a factual basis: Alpaca's API has no such enum. Multi-leg is generic — `mleg + legs[]` is the only shape the broker understands, and any strategy-name-aware logic has to live on our side anyway.

**Defer all multi-leg work to the live trading project; skip paper.** Rejected because it inverts the project's posture. Paper *is* the testing ground; the entire point of the propose-decide-react loop is to accumulate evidence on a non-money path before authorizing live. Skipping paper would mean the first real multi-leg order is also the first untested multi-leg order.

**Encode the strategy_kind as a string and skip the leg validation.** Rejected: ADR-0027 needs structured leg data to compute defined-risk; the recipe layer needs structured legs to size correctly. A free-text `strategy_kind` is fine for *display*, but the legs themselves must be typed.

---

## Open Questions

1. **Calendar spreads in a single mleg order.** Does Alpaca paper accept a multi-leg order with legs at *different expirations* (the calendar/diagonal case)? The docs imply yes, but we have not verified this against the paper sandbox. Action: write a sandbox probe in the integration test suite (D5 of the test plan below) that submits a 30/60-day calendar and inspects the response. If rejected, we drop `calendar_spread` from the supported recipe list until live or until Alpaca lifts the restriction.
2. **Pin-risk handling at T-1 expiry.** When a short option closes near-the-money on expiry day, is it better to auto-close at 15:30 ET to avoid pin-risk assignment, or to let the paper engine settle naturally and observe the NTA pattern? This is an empirical question best answered by the first 30 days of paper data; we record the question and the data, and revisit in a follow-up ADR.
3. **Fee modeling.** Paper fees may not match live. We capture per-leg fees in `LegOutcome` but flag that paper-vs-live fee divergence is a known gap in any P&L claim made from paper data alone.

---

## Implementation Sketch

File layout under `hermes_quant/options/`:

```
hermes_quant/options/
├── occ.py            # format_occ, parse_occ, OccComponents, OccParseError
├── multileg.py       # MultiLegProposal, StrategyBuilder, NetGreeks aggregation
├── reactor.py        # propose() → risk_gate → HITL surface; on_approve → broker
├── reconcile.py      # reconcile_options_ntas: 06:00 ET cron step
└── outcomes.py       # RealizedOutcome extensions, LegOutcome
```

Hook points:

- `hermes_quant/reactor.py` (existing) gains a dispatch on proposal type: equity proposals go through the existing path; `MultiLegProposal` instances route through `options.reactor.handle()`.
- `hermes_quant/settlement_loop.py` (existing) gains a new scheduled step that calls `options.reconcile.reconcile_options_ntas(asof_date)` at 06:00 ET. The existing fill-time outcome writer is unchanged; reconcile only writes the next-day NTA outcomes.
- `hermes_quant/broker/alpaca.py` gains `submit_mleg_order(legs, tif)` and a guard that raises `LiveMultiLegNotAuthorized` when `account.is_paper is False`.
- The proposal store schema migration adds a `proposal_kind` discriminator column (`equity` | `multi_leg`) and a check constraint that approvals reference `proposal_id` only, never a leg index.

Key signatures:

```python
# options/multileg.py
class StrategyBuilder:
    def covered_call(self, underlying: str, expiry: date,
                     strike: Decimal, qty: int) -> tuple[OptionLeg, ...]: ...
    def vertical_spread(self, ...) -> tuple[OptionLeg, ...]: ...
    # ... one per supported recipe

def aggregate_net_greeks(legs: tuple[OptionLeg, ...]) -> NetGreeks: ...

# options/reactor.py
def handle(proposal: MultiLegProposal) -> ProposalDecision: ...

# options/reconcile.py
def reconcile_options_ntas(asof: date) -> list[RealizedOutcome]: ...
```

---

## Test Plan

1. **OCC round-trip (unit).** Property test: for random `(underlying, expiry, right, strike)` tuples, `parse_occ(format_occ(...))` is the identity. Includes edge cases: strike `0.50`, strike `9999.999`, expiry on weekends (rejected at format time), lowercase ticker (normalized to upper).

2. **Net-greeks aggregation (unit).** Parameterized cases for each supported recipe — covered call, debit vertical, credit vertical, iron condor, butterfly — with hand-computed expected net greeks. Includes a pure-noise-leg case (two equal-and-opposite legs) where net greeks are zero.

3. **Atomic approval enforcement (unit).** Attempt to write a partial-approval row to the proposal store; assert `PartialApprovalForbidden` is raised. Attempt to approve a `risk_gate_pass=False` proposal; assert it is filtered out before reaching the HITL surface.

4. **NTA reconcile replay (integration, deterministic).** Record a fixture day's `/v2/account/activities` response containing a mix of `OPEXP`, `OPASN`, and `OPXRC` events. Replay through `reconcile_options_ntas` and assert the resulting `RealizedOutcome` rows match a golden file byte-for-byte (modulo timestamps).

5. **Alpaca paper roundtrip (integration, network).** Submit a covered_call and a debit vertical via `submit_mleg_order` against the paper sandbox; assert both return a multi-leg order ID with all legs in `accepted` state. Probe a calendar spread (different expirations) and record the broker response to resolve Open Question 1.

6. **Live-blocking guard (unit).** Construct a broker adapter pointed at a non-paper account; call `submit_mleg_order`; assert `LiveMultiLegNotAuthorized` is raised before any HTTP call is made.

7. **End-to-end paper smoke (manual, weekly).** Once per week during the 60-day evidence window, a maintainer manually approves one proposal in chat and confirms (a) the order fills atomically, (b) intraday position/BP views update, and (c) the next morning's reconcile pass produces no warnings.
