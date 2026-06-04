# ADR-0029: Multi-Leg Paper Reactor

**Status:** Accepted (2026-05-24), implemented
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


---

## Amendment 2026-05-24 -- D7 paper-to-live promotion: type-level prevention plus statistical criteria

**Source**: `docs/reviews/2026-05-24-synthesis-adrs-0026-0030.md` CP0-1 (convergent: Gemini + Grok)
**Reviewer**: Gemini-3.1-Pro + Grok-4.3
**Status**: Adopted

### What changed

D7 (this ADR, line 194) currently guards live multi-leg via a runtime check: `account.is_paper is False -> raise LiveMultiLegNotAuthorized`. Two reviewers independently flagged this as structurally weak:

1. The boolean is racy with respect to staging-environment misconfiguration (a wrongly-configured environment whose `is_paper` flips can authorize live without any code change).
2. The "60 days, n approximately 40 outcomes" promotion criterion is too small a sample for the gate's risk-relevant events (gamma spikes, pin risk, BPR breaches) to have statistically registered.

Strengthen along both axes:

**Type-level prevention:**

Live-trading client construction MUST require a `LiveTradingApproval` Pydantic model whose `__init__` runs the gate's promotion check. The `LiveBroker` class has NO `submit_mleg_order` method until that approval object is passed at construction time. Until a future ADR-00XX explicitly defines the approval contract and lands the method, the only class with `submit_mleg_order` is `PaperBroker`. The shape:

```python
class LiveTradingApproval(BaseModel):
    """Constructed only by passing every promotion gate. Cannot be instantiated by mistake."""
    approval_id: str
    issued_at: datetime
    paper_outcomes_count: int          # >= 100
    rolling_30d_realized_sharpe: float # point estimate
    sharpe_95ci_lower: float           # bootstrap lower bound, must be >= 1.0
    rolling_30d_max_drawdown_pct: float # <= 0.01 (1%)
    no_killswitch_in_trailing_14d: bool
    immutable_breaches_in_window: int  # must == 0
    weekly_retro_evidence_ids: list[str]  # ADR-0026 link
    promoter_human_id: str             # the human who approved the promotion via CLI

    @model_validator(mode="after")
    def _enforce_thresholds(self) -> "LiveTradingApproval":
        if self.paper_outcomes_count < 100:
            raise ValueError("paper_outcomes_count must be >= 100")
        if self.sharpe_95ci_lower < 1.0:
            raise ValueError("sharpe_95ci_lower must be >= 1.0")
        if self.rolling_30d_max_drawdown_pct > 0.01:
            raise ValueError("rolling_30d_max_drawdown_pct must be <= 1%")
        if not self.no_killswitch_in_trailing_14d:
            raise ValueError("kill-switch trigger in trailing 14d disqualifies promotion")
        if self.immutable_breaches_in_window != 0:
            raise ValueError("any immutable-rule breach disqualifies promotion")
        return self


class LiveBroker:
    """Live multi-leg requires LiveTradingApproval at construction time."""
    def __init__(self, ..., approval: LiveTradingApproval):
        self._approval = approval
        # submit_mleg_order is bound here, not at class-definition time;
        # importing LiveBroker without an approval gives a class with no
        # multi-leg method.
        ...
```

The `LiveTradingApproval` constructor is the only place the thresholds live; mis-spelling a threshold OR forgetting one is a `ValidationError` at construction time. This is correctness-by-construction in the same spirit as D6's atomic-HITL-approval shape.

**Statistical promotion criteria (the actual numbers):**

Replace the implicit "60 calendar days, ~40 outcomes" criterion with explicit statistical thresholds:

- `N >= 100` settled paper multi-leg outcomes (not calendar days; trade count is what matters for sample-size).
- Realized Sharpe over rolling 30 days, computed as `mean_daily_return / std_daily_return * sqrt(252)`.
- 95% bootstrap CI on Sharpe via `scipy.stats.bootstrap` with `method="BCa"`, `n_resamples >= 9999`. The gate uses the LOWER bound of the CI, not the point estimate; this is a forcing function against small-sample optimism.
- Lower-bound Sharpe `>= 1.0`.
- Rolling 30-day max drawdown `<= 1%` of NAV.
- Zero kill-switch triggers in trailing 14 days.
- Zero immutable-rule breaches (silence-by-default, no-naked, BPR buffer 25%, action-discrete) over the rolling 30-day window.
- 3-of-3 calibrator targets (per ADR-0023) within +/-5% of realized rate over the same window.
- Weekly retro (ADR-0026) outputs include a `promotion_readiness: true` field; the gate reads it as one of its inputs (the meta-retro must independently agree the loop has converged).

The `LiveTradingApproval` model is constructed by a CLI command (`hermes quant live promote --dry-run` first, then `... --confirm` with explicit human typed-acknowledgement of every threshold). The CLI computes all metrics from the local journal + retro outputs; it does not call the LLM. The CLI rejects (non-zero exit, no approval object) on any threshold miss.

**Test plan additions:**

- `test_live_broker_has_no_mleg_method_without_approval`: `inspect.getmembers(LiveBroker)` does not include `submit_mleg_order` when constructed without an approval; even import-time monkey-patching cannot add it because the binding is per-instance.
- `test_live_trading_approval_rejects_subhundred_n`: `LiveTradingApproval(paper_outcomes_count=99, ...)` raises `ValidationError`.
- `test_live_trading_approval_uses_lower_ci_not_point`: fixture with point Sharpe 1.5 but lower-CI 0.7; assert `ValidationError` (the LOWER bound, not the point estimate, is the gate).
- `test_live_trading_approval_drawdown_threshold`: fixture with drawdown 1.001%; assert rejected.
- `test_live_trading_approval_killswitch_window`: fixture with kill-switch 13 days ago; assert rejected (14d window is exclusive).
- `test_live_trading_approval_calibrator_drift`: fixture where one calibrator is 5.1% off realized; assert rejected.
- `test_promotion_cli_dry_run_idempotent`: `... --dry-run` twice on the same journal produces byte-identical output (deterministic).
- `test_promotion_requires_retro_readiness_flag`: even if all numerics pass, missing `promotion_readiness: true` from the latest meta-retro causes the approval to fail.

### Why

The synthesis (CP0-1) collapsed two reviewers' independent findings into the same defect: the runtime boolean is too easy to flip wrongly, and 60 calendar days at ~5 trades/week is sample-size theater for events whose tail probability is what the system most needs to learn about. The structural fix (no live method until an approval object exists) is the cleanest defense against accidental promotion via misconfig; the statistical fix (bootstrap CI lower bound, immutable-breach floor, retro-loop concurrence) ensures the promotion is evidence-driven, not calendar-driven. Both together keep "paper -> live" a deliberate human act gated by both type-system AND data, with each layer compensating for the other's failure mode.

### Affected sections of this ADR

- D7 "Paper + HITL only; live multi-leg deferred" (line 135 and surrounding) -- replaces the runtime boolean with a type-level guard plus statistical promotion criteria.
- Implementation map (line 194) -- `submit_mleg_order` on `LiveBroker` is contingent on `LiveTradingApproval` injection; `PaperBroker.submit_mleg_order` remains unconditional.
- Test plan (existing item 6 "Live-blocking guard") -- extended with the eight tests above. Item 6 itself is preserved as a residual sanity check at the broker layer.

---

## Amendment 2026-05-24 -- Multi-leg order schema: position_intent and ratio_qty per leg, qty deprecated for multi-leg

**Source**: `docs/research/2026-05-24-r5-alpaca-live-probe.md` "R1 sec 3 was MOSTLY RIGHT" section
**Reviewer**: Live probe (orchestrator, paper credentials)
**Status**: Adopted

### What changed

R1 documented the multi-leg POST schema using `qty` and `side` per leg. The 2026-05-24 live probe submitted a real vertical spread to the Alpaca paper API and observed the accepted shape differs:

Working shape (verified live 2026-05-24, paper API returned `accepted`):

```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "time_in_force": "day",
  "limit_price": "0.01",
  "legs": [
    {"symbol": "NVDA260612C00140000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
    {"symbol": "NVDA260612C00145000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}
  ]
}
```

Differences from R1 / original ADR-0029 D2 (lines 56-58):

1. `position_intent` is required at the per-leg level. Values: `buy_to_open`, `buy_to_close`, `sell_to_open`, `sell_to_close`. The original ADR did not include this field.
2. `ratio_qty` is the leg multiplier (always `"1"` for vanilla spreads, can be `"2"` etc. for ratio spreads). The original ADR used `qty` at the leg level, which is wrong for multi-leg -- outer `qty` is the spread quantity and per-leg `ratio_qty` is the leg multiplier.
3. `type` and `limit_price` are at the OUTER order level, not per-leg. Per-leg `type` and `limit_price` are NULL in the broker's response -- legs inherit from the parent order class.

Updated `OptionLeg` dataclass (this ADR D5, around line 105):

```python
@dataclass(frozen=True)
class OptionLeg:
    symbol: str                      # OCC string
    side: Literal["buy", "sell"]
    position_intent: Literal["buy_to_open", "buy_to_close",
                              "sell_to_open", "sell_to_close"]
    ratio_qty: int = 1               # leg multiplier within the spread

    # Deprecated for multi-leg; kept as alias for single-leg back-compat.
    # New code MUST use ratio_qty + outer qty. The single-leg compat shim
    # raises DeprecationWarning when `qty` is read off a multi-leg leg.
    @property
    def qty(self) -> int:
        warnings.warn(
            "OptionLeg.qty is deprecated for multi-leg use; "
            "use ratio_qty (per-leg multiplier) and the parent "
            "MultiLegProposal.qty (spread count) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.ratio_qty
```

`MultiLegProposal.limit_price` becomes the spread's net debit/credit; per-leg `limit_price` is removed from the schema (the legs inherit). This matches D6's atomic-approval posture: there is ONE net price the user approves.

Broker adapter (`hermes_quant/broker/alpaca.py` `submit_mleg_order`) constructs the POST body as documented above. The single-leg path remains independent and continues to use the legacy `qty + side + type + limit_price` shape per the equity-options-single-leg flow (which is not multi-leg-class).

### Why

The probe confirmed the live API rejects the original ADR's schema with `422`-class errors. Discovering this at implementation time would cost a half-day round-trip; documenting the corrected schema in the ADR is free and prevents the entire wave from going down the wrong path. Marking `qty` deprecated rather than removed preserves single-leg-flow compatibility (single-leg orders DO use `qty` at the order level) while keeping the multi-leg flow type-correct. The position_intent field also matters semantically: the broker uses it to determine margin treatment (a `buy_to_close` short leg releases collateral; a `buy_to_open` does not), so the field is not just a cosmetic label.

### Affected sections of this ADR

- D2 "Multi-leg order body shape" (lines 56-58) -- example body updated to include `position_intent` and `ratio_qty`; outer `type` and `limit_price` clarified.
- D5 `OptionLeg` dataclass (line 105 and surrounding) -- adds `position_intent` (required) and `ratio_qty` (defaults to 1); `qty` becomes a deprecation-warning property.
- Implementation map (line 194) -- `submit_mleg_order` constructs the corrected POST body.
- Test plan item 5 "Alpaca paper roundtrip" -- must assert the request body contains `position_intent` per leg and outer `qty` matches the spread count.
- References block -- `docs/research/2026-05-24-r5-alpaca-live-probe.md` is added as the live-verification source.
