# Wave D — ADR-0029 multi-leg PAPER reactor go-live (N3 / B01)

**Date:** 2026-05-30
**Author:** plan subagent (deep-work backlog loop — N3/B01 multi-leg paper reactor)
**Status:** implementation-ready (docs-only this loop — DO NOT build here)
**Track:** B01 go-live (flips `HERMES_QUANT_MULTILEG_REACTOR=1` *capable*, but the flag stays
unset everywhere; this wave fills the reactor BODY, not the operator's flip).
**Backlog:** N3 (single biggest vision unlock — covered_call/CSP/wheel/LEAPS can be RANKED +
GATED today but cannot FIRE: `PaperReactor` is equity-only).

**Grounded in (all READ):**
- `docs/research/2026-05-30-r-multileg-paper-execution.md` — the execution/reactor research
  (mleg order shape, async poll, per-leg fill response, two-row reconciliation, PMCC shadow
  validation). Companion (does NOT supersede): `docs/research/2026-05-30-r-options-execution.md`.
- `docs/adr/ADR-0029-multi-leg-paper-reactor.md` (+ BOTH 2026-05-24 amendments — authoritative
  over the ADR body: D7 type-level live guard; per-leg `position_intent` + `ratio_qty`, outer
  `qty`/`type`/`limit_price`).
- `docs/plans/wave-b2-options.md` — the foundation wave (this plan is its B01 go-live successor;
  §6 reconciles the 6-PR plan against it).
- ADR-0027 (options gate — precondition the reactor CONSUMES), ADR-0028 (options data layer),
  ADR-0070 (slippage price layer), ADR-0077 (pre-trade admissibility), ADR-0078 (order-lifecycle
  + exactly-once idempotency), ADR-0079 / `docs/design/pdr-unified-architecture.md` (PDR).
- Existing code: `hermes_quant/react/paper.py` (the reactor to MIRROR), `react/multileg.py`
  (the inert SCAFFOLD to flesh out), `react/base.py` (`Reactor` Protocol + `ExecutionRecord`),
  `react/live.py` (`LiveBroker`/`LiveTradingApproval` — the live type-level guard), `react/
  slippage_model.py` (ADR-0070), `options/data.py` (`OptionLeg`/`StockLeg`/`NetGreeks`/
  `aggregate_net_greeks`), `options/occ.py`, `risk/options_gate.py` (`options_gate` →
  `OptionsGateResult`), `daemon/signal_bus.py` (`append_locked`, `EXECUTION_BUS_PATH`),
  `state/portfolio_state.py` (`apply_execution`, `(account_id, asset_class, symbol)` PK,
  `processed_fills` idempotency ledger), `proposals.py` (`Proposal`, `ProposalStore`),
  `tools.py::quant_approve` (the HITL approve→react seam), `shadow/pmcc.py` (counterfactual).

**Posture rails (NON-NEGOTIABLE — every box must hold at merge; §10 self-check):**
- Deterministic gate is FINAL authority. The reactor CONSUMES an already-passed
  `OptionsGateResult` as a PRECONDITION and re-asserts it; it NEVER re-sizes, amplifies, or
  bypasses the gate. A reactor that fires an un-gated or gate-rejected proposal is a P0 bug.
- Money via CLI/HITL only. The reactor is NEVER reachable from a tool handler that auto-fires
  (AGENTS.md "money never goes through tools"). It is invoked only from the operator-confirmed
  `quant_approve` seam (HITL) — and this wave does NOT route it autonomously (ADR-0016 deferred).
- Whole reactor DEFAULT-OFF behind `HERMES_QUANT_MULTILEG_REACTOR=1` — set NOWHERE in repo or
  deploy `.env`. The flip is the operator's deliberate later act after the ADR-0029 D7 evidence
  window. With the flag unset, `execute()` raises `MultiLegReactorDisabled` and writes nothing
  (bit-identical to the scaffold today).
- PAPER-only this loop. No live broker order rail (ADR-0029 D7): live multi-leg stays behind the
  `LiveTradingApproval` type-level guard in `react/live.py`. No code path in this wave reaches a
  live mleg submission; `LiveMultiLegNotAuthorized` remains defined-but-unreachable.
- Exactly-once / idempotency (ADR-0078): a stable `client_order_id` / `multi_leg_id` dedup key;
  a re-`execute()` of an already-recorded proposal is a no-op returning the existing record.
- Admissibility (ADR-0077) applies to the EQUITY leg of a covered call (the +100 shares), mirror
  of `PaperReactor._admissibility_reject`. Option legs are out of admissibility scope.
- All times UTC; `asof_decision` = advisor/decision wall-clock; `asof_execution` = when React
  fired (`%Y-%m-%dT%H:%M:%SZ`).
- Discrete sizing untouched: the reactor fills the gate-admitted `contracts`; it never widens.

---

## 0. Scope of THIS wave (and what stays OUT)

The foundation (Wave B2) shipped three default-OFF pieces. This wave makes the reactor FIRE
on paper. The single behavioral change: `MultiLegPaperReactor.execute()` gains a body that, when
the flag is set, fills an already-gated + already-HITL-approved `MultiLegProposal` on Alpaca paper
(or a deterministic paper-fill model when no creds), writes per-leg fills to `executions.jsonl`,
and reconciles into `state.db` positions + the PMCC shadow.

**IN scope (this wave):**

1. **`MultiLegProposal` schema** (ADR-0029 D5, both amendments reconciled) — a frozen dataclass
   carrying the gate result, the typed legs, the net debit/credit, net greeks, and the strategy
   kind. → `hermes_quant/options/multileg.py`.
2. **Reactor BODY** — fill in `MultiLegPaperReactor.execute()` behind the existing flag gate:
   precondition re-assert (gate pass + HITL approval), idempotency claim, paper fill (broker poll
   OR deterministic model), admissibility on the equity leg of a CC, ADR-0070 slippage on the
   equity leg only, ONE parent + one child-per-leg `ExecutionRecord` written to the bus,
   PortfolioState reconciliation, PMCC-shadow record on PMCC opens.
   → `hermes_quant/react/multileg.py` (flesh out), `hermes_quant/react/mleg_fill.py` (NEW — paper
   fill model + broker poll, split out so the reactor stays thin).
3. **PaperBroker mleg submit + poll** — `PaperBroker.submit_mleg_order(legs, *, outer_qty,
   net_limit_price, tif, client_order_id)` + `poll_order(order_id)`; INERT (deterministic local
   fill) unless creds present; live path stays absent (mirrors `react/live.py`).
   → `hermes_quant/react/mleg_fill.py`.
4. **Per-leg execution-record shape + reconciliation** — Shape (B) from research §3.4: one parent
   record + one child `ExecutionRecord` per leg, all sharing `multi_leg_id` in `reactor_metadata`.
   Extend `PortfolioState.apply_execution` to store SIGNED CONTRACT/SHARE quantities for
   `us_option`/`equity` legs (research §4.1 known follow-up) without breaking the equity NAV-frac
   path. → `hermes_quant/state/portfolio_state.py`.
5. **Dispatch + HITL wiring** — `tools.py::quant_approve` dispatches on proposal kind: equity →
   `PaperReactor` (unchanged), multi-leg → `MultiLegPaperReactor`. A `select_reactor(proposal)`
   helper centralizes the dispatch. NOT autonomous (no `autonomous.py` change this wave).
6. **PMCC-shadow reconciliation join** — on a PMCC open, the reactor calls `record_pmcc(...)` with
   `note=multi_leg_id`; a `reconcile/pmcc_shadow.py` helper compares the daily `mark_pmcc` model
   `net_value`/`net_delta`/`net_theta_day` against the reactor's real per-leg marks.
7. **Tests** + the eval gate (a covered call + a CSP fill end-to-end on paper, validated against
   the shadow).

**OUT of scope (deferred; do NOT build here — reconciled in §6):**

- **Live `submit_mleg_order`** / promotion (ADR-0029 D7) — gated by `LiveTradingApproval`; the
  live method stays absent. (6-PR PR-3b live polling stays paper-only here.)
- **`reconcile_options_ntas`** (next-day NTA settlement loop, ADR-0029 D3) — needs accumulated
  live-ish paper fills + the activities endpoint. The reactor only writes the durable `OCC symbol
  → multi_leg_id` index this wave so the future NTA loop can join. (6-PR PR-4.)
- **`StrategyBuilder` recipe emitters** + the HITL `MultiLegProposal` chat rendering (ADR-0029 D6
  display block). This wave consumes a `MultiLegProposal` that some upstream PRODUCED; it does not
  build the recipe→proposal producer. The render block is a follow-up. (6-PR PR-5.)
- **Kill-switches** (gamma-spike / BPR / theta-bleed / pin-risk position-flatten, ADR-0027 D5) —
  fire from settlement_loop on live positions. (6-PR PR-6.) The gate's pin-risk *new-entry*
  silence already ships (O7).
- **`order_state.py` `OrderState`/`OrderEvent` machine** (ADR-0078 D78.1) — NOT yet present. The
  reactor defines its own exactly-once (`client_order_id` + bus dedup, ADR-0078 D78.3/D78.4 shape)
  and wires THROUGH the machine when it lands, but must not depend on it existing (research §3.5).
- **Autonomous multi-leg** (ADR-0016) — HITL-only this wave.
- **Historical option-chain backtest** — no historical chains in any Alpaca tier; paper loop is
  live-snapshot-only (ADR-0028 D4).

---

## 1. The execution shape (the 5 load-bearing facts, from research)

1. **Opening a CC/CSP is NOT an mleg order.** mleg `legs[]` are options-ONLY, 2–4 unique legs.
   - **Covered call** = (a) a SEPARATE equity order BUY 100 shares (the equity leg — admissibility
     applies here), THEN (b) a single-leg L1 option order SELL 1 call `sell_to_open`.
   - **Cash-secured put** = a single-leg L1 option order SELL 1 put `sell_to_open` (collateral is
     reserved cash, not a leg).
   - **Vertical / condor / butterfly / PMCC / roll** = ONE mleg order (≥2 option legs).
2. **mleg body** (verified live, ADR-0029 amendment): ONE `POST /v2/orders`,
   `order_class:"mleg"`, OUTER `qty` (spread count), OUTER `type:"limit"` + `limit_price`
   (POSITIVE = net DEBIT paid; NEGATIVE = net CREDIT received — the ONE net price HITL approved),
   `time_in_force`, `legs[]` each `{symbol(OCC-21), ratio_qty, side, position_intent ∈
   {buy_to_open|sell_to_open|buy_to_close|sell_to_close}}`. Per-leg `type`/`limit_price`/`qty`
   do NOT exist (legs inherit).
3. **Atomic, but ASYNC.** mleg fills together or not at all (no partial-leg within a lot). A `200`
   means *received*; the reactor MUST poll `GET /v2/orders/{id}` for terminal status. The returned
   parent `Order.legs[]` carries per-leg `filled_avg_price`/`filled_qty`/`status` — that is the
   per-leg fill detail the reactor records.
4. **A covered call reconciles as TWO position rows** (Alpaca lists each option leg as its own
   OCC-keyed `Position`): `(equity, NVDA, +100)` and `(us_option, NVDA…C…, -1)`. `PortfolioState`'s
   `(account_id, asset_class, symbol)` PK already supports this — the reactor just emits the right
   per-leg child `ExecutionRecord`s.
5. **The interface to MIRROR** is `PaperReactor.execute(proposal, *, fill_size_pct,
   approver_user_id=None) -> ExecutionRecord` — the scaffold already conforms. Keep the EXACT
   signature; the proposal becomes a `MultiLegProposal`, the return stays one `ExecutionRecord`
   (the PARENT; child records are written to the bus as side-effects).

---

## 2. File-by-file specification

### 2.1 `hermes_quant/options/multileg.py` (NEW) — `MultiLegProposal` (ADR-0029 D5)

The frozen carrier the reactor consumes. Reconciles ADR-0029 D5 body with both amendments and the
existing `options/data.py` `OptionLeg` (NOT a new leg type — reuse it).

```python
"""hermes_quant.options.multileg — MultiLegProposal carrier (ADR-0029 D5).

The reactor's INPUT. Produced upstream (recipe → options_gate → HITL approve) and
consumed by MultiLegPaperReactor. Immutable: a revision is a NEW proposal_id
(ADR-0029 D5). Carries the ALREADY-PASSED gate result and the ALREADY-APPROVED
net price so the reactor's job is to FILL, never to re-decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from hermes_quant.options.data import NetGreeks, OptionLeg, StockLeg


@dataclass(frozen=True)
class MultiLegProposal:
    proposal_id: str                       # prop_<ISO>_<underlying>_<rand6> (proposals.py shape)
    asof: datetime                         # UTC decision/publication time (fidelity anchor)
    strategy_kind: str                     # 'covered_call'|'cash_secured_put'|'vertical_spread'|
                                           # 'iron_condor'|'calendar_spread'|'butterfly'|'pmcc'|'roll'
    underlying: str
    option_legs: tuple[OptionLeg, ...]     # the OPTION legs (OCC-21 + position_intent + ratio_qty)
    stock_leg: StockLeg | None             # the EQUITY leg of a covered call (None otherwise)
    outer_qty: int                         # spread count (mleg OUTER qty); single-leg uses contracts
    net_debit_credit: Decimal              # signed: +debit paid / -credit received (the HITL price)
    net_greeks: NetGreeks                  # gate-aggregated at admitted size
    bpr_estimate: Decimal                  # buying-power reduction, USD (from OptionsGateResult)
    max_loss: Decimal | None               # USD; None for share/cash-collateralized (CC/CSP)
    max_gain: Decimal | None               # None = unbounded → must have been blocked by 0027
    breakeven_underlying: tuple[Decimal, ...]
    rationale: str
    source_recipe_id: str
    # Gate PRECONDITION (set by options_gate; the reactor re-asserts, never recomputes):
    risk_gate_pass: bool                   # MUST be True for the reactor to fill
    risk_gate_bucket: str                  # OptionsGateResult.bucket.value
    risk_gate_reason: str | None           # None when admitted
    risk_gate_warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_mleg(self) -> bool:
        """True iff this routes as a single mleg order (>=2 option legs, no stock leg
        in legs[]). CC/CSP open as single-leg L1 + (CC) a separate equity order."""
        return len(self.option_legs) >= 2

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return tuple(leg.symbol for leg in self.option_legs)

    @classmethod
    def from_gate_result(
        cls, *, gate_result, ...,   # OptionsGateResult + the recipe-supplied fields
    ) -> "MultiLegProposal": ...
```

Notes:
- `from_gate_result(...)` is the ONLY constructor the producer should use — it copies
  `admitted`/`bucket`/`reason`/`net_greeks`/`bpr_estimate`/`max_loss`/`contracts` straight off the
  `OptionsGateResult`, so the proposal CANNOT carry a gate verdict that disagrees with the gate.
  This is the structural "the reactor consumes the gate, never bypasses it" guarantee.
- `risk_gate_pass=False` proposals are persisted for replay/audit (ADR-0029 D5) but the reactor
  raises `GateRejectedProposal` if asked to fill one (§2.2 step 1). The HITL surface filters them
  before approval (ADR-0029 D5).
- `net_debit_credit` and `max_loss`/`bpr_estimate` are `Decimal` (money — never float; ADR-0029
  D1 strike-rounding rationale extends to net price).
- No `qty` field on the option leg (deprecated, ADR-0029 amendment): `outer_qty` × `leg.ratio_qty`
  = the leg's concrete contract count.

A `proposal_kind` discriminator (`"equity"` | `"multi_leg"`) is added to the proposal-store
record (ADR-0029 Implementation Sketch) so the dispatch in §2.5 keys on it without isinstance.

### 2.2 `hermes_quant/react/multileg.py` (FLESH OUT) — the reactor body

Keep the scaffold's class/flag/exception surface. Replace the `NotImplementedError` after the flag
gate with the body. The algorithm MIRRORS `PaperReactor.execute()` (research §3.2), extended for N
legs.

```python
def execute(
    self,
    proposal: MultiLegProposal,          # Any at the Protocol boundary; runtime-typed here
    *,
    fill_size_pct: float,                # the gate-admitted contract count is the truth; this is
                                         # carried for Protocol parity + the equity-leg NAV frac
    approver_user_id: str | None = None,
) -> ExecutionRecord:                    # returns the PARENT record; children written as side-effect
    if not self._enabled():
        raise MultiLegReactorDisabled(...)          # unchanged — first check, default-OFF
    return self._execute_enabled(proposal, fill_size_pct=fill_size_pct,
                                 approver_user_id=approver_user_id)
```

`_execute_enabled(...)` steps (each is a tested unit):

1. **Precondition re-assert (gate is FINAL authority).** If `proposal.risk_gate_pass is not True`
   → raise `GateRejectedProposal(proposal.proposal_id, proposal.risk_gate_reason)`. The reactor
   NEVER re-runs `options_gate` (that would let a reactor-time recompute disagree with the
   already-approved verdict) — it TRUSTS the proposal's copied gate result, which `from_gate_result`
   guarantees came from the gate. This is the consume-never-bypass invariant.
2. **Idempotency claim (ADR-0078 D78.3 shape).** Compute
   `multi_leg_id = proposal.proposal_id` and
   `client_order_id = _stable_coid(proposal)` = a deterministic sha256 hex of
   `(proposal_id, strategy_kind, tuple(sorted((leg.symbol, leg.position_intent))), outer_qty)`.
   Before any write, check the bus for an existing parent record with this `multi_leg_id`
   (read the tail; `_already_recorded(multi_leg_id)`); if found, LOG and RETURN the existing
   parent (a re-`execute()` is a no-op — exactly-once). The same `client_order_id` is passed to the
   broker so Alpaca also rejects a duplicate (broker-side defense-in-depth, research §3.5). When
   the ADR-0078 `fired_ledger` lands, claim there too (`INSERT OR IGNORE`); until then the bus
   dedup is authoritative.
3. **Timestamps.** `asof_decision` = `proposal.asof` (UTC ISO) ; `now = asof_execution` =
   `datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`. `decision_price` (parent) = signed
   `proposal.net_debit_credit` (float for the record; the Decimal source stays on the proposal).
4. **Admissibility on the equity leg of a CC (ADR-0077 / ADR-0079).** If `proposal.stock_leg is
   not None` and the stock leg opens a SHORT (qty<0 — not the CC case, CC is +100 long, so a no-op
   for CC, but the guard is correct for collars), call the SAME `_admissibility_reject(...)` logic
   `PaperReactor` uses (reuse via a shared helper, see §2.6). DEFAULT-OFF behind
   `HERMES_QUANT_ADMISSIBILITY`; flag-off is a bit-for-bit no-op. The CC's +100-long equity leg is
   admissible by construction; the guard exists so a future short-stock collar leg is covered. On
   reject → write a no-fill parent audit record (mirror `PaperReactor._admissibility_reject`'s
   no-bus-write record) and return it without filling.
5. **Submit + poll the fill (`react/mleg_fill.py`, §2.3).** Build the order(s) per `strategy_kind`:
   - CC: equity order BUY `stock_leg.qty` shares + single-leg L1 SELL the call.
   - CSP: single-leg L1 SELL the put.
   - mleg structures: ONE `submit_mleg_order(option_legs, outer_qty=, net_limit_price=
     net_debit_credit, tif="day", client_order_id=)`.
   Poll to terminal status. Collect per-leg `filled_avg_price`/`filled_qty`/`status` + the equity
   fill. On `rejected`/`expired`/zero-fill → write a no-fill parent record (status in metadata) and
   return; never fabricate a fill.
6. **Slippage (ADR-0070), asymmetric (research §2.2 / §3.6).** Option legs: `fill_price =
   broker filled_avg_price` (paper already fills at the live NBBO — keep passthrough). EQUITY leg of
   a CC: apply the v0.2 envelope (`HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2` → `apply_slippage(...)`
   seeded by `(proposal_id, asof_execution)` for replay equality), else passthrough. Record the
   asymmetry in `reactor_metadata.option_pricing = "iex_possibly_delayed"`.
7. **Build records — Shape (B) (research §3.4).** ONE PARENT `ExecutionRecord` +
   one CHILD `ExecutionRecord` per leg (option legs + the CC equity leg), all sharing
   `reactor_metadata.multi_leg_id`:
   - **Parent:** `asset = underlying`, `asset_class = "multi_leg"`, `fill_price =
     signed net debit/credit`, `fill_size_pct = fill_size_pct`,
     `reactor_metadata = {"multi_leg_id", "strategy_kind", "outer_qty", "net_greeks": {...},
     "client_order_id", "broker_order_id", "leg_symbols": [...], "paper": True, "role": "parent"}`.
     This is the record `execute()` RETURNS and the PMCC-shadow / HITL audit join on.
   - **Child (per option leg):** `asset = OCC-21`, `asset_class = "us_option"`, `fill_price =
     leg.filled_avg_price`, `fill_size_pct` = the signed per-leg NAV-fraction proxy,
     `reactor_metadata = {"multi_leg_id", "leg_index", "position_intent", "ratio_qty",
     "contracts": outer_qty*ratio_qty, "quantity": signed_contracts, "role": "leg", "paper": True,
     "option_pricing": "iex_possibly_delayed"}`.
   - **Child (CC equity leg):** `asset = underlying`, `asset_class = "equity"`, `fill_price = the
     (slipped) share fill`, `reactor_metadata = {"multi_leg_id", "quantity": +shares,
     "role": "equity_leg", "paper": True}`.
8. **Write atomically.** Serialize each record with `json.dumps(..., separators=(",",":"),
   sort_keys=True)+"\n"`; write ALL records (parent first, then children) under a SINGLE
   `append_locked(self.executions_path)` critical section so the parent+children family is
   crash-atomic on the bus (no half-written family). Mirror `paper._record_to_dict`.
9. **Reconcile into `state.db` (best-effort, non-blocking — ADR-0031 silence-by-default).** For
   each CHILD record, inject `account_id="paper-default"` and call
   `PortfolioState.apply_execution(child_dict)`. The parent is NOT applied (it is an audit/rollup,
   not a position). Each child carries `reactor_metadata.quantity` (signed contracts/shares) so the
   extended `apply_execution` (§2.4) stores true contract counts, not the NAV-fraction proxy. The
   `processed_fills` ledger keyed `(proposal_id, asof_execution)` already dedups — BUT all children
   share one `proposal_id`, so extend the ledger key to `(proposal_id, asof_execution, asset,
   asset_class)` for multi-leg (§2.4) so the second leg is not swallowed as a duplicate of the
   first.
10. **PMCC-shadow record (research §4.2).** If `strategy_kind == "pmcc"`, build a `PMCCPosition`
    from the two call legs (`opened_at = proposal.asof`, `spot_at_open`, `note = multi_leg_id`) and
    `record_pmcc(pos)`. This activates the daily counterfactual the ADR-0029 confidence path needs.
11. **Reflection hook** (`HERMES_QUANT_REFLECTION=1`, default OFF) on a close — same as
    `PaperReactor`. A "close" for multi-leg = a `*_to_close` position_intent leg.
12. `logger.info(...)` a one-line audit; `return parent`.

New exceptions in `react/multileg.py`:
- `GateRejectedProposal(RuntimeError)` — `risk_gate_pass is not True`.
- `MultiLegFillRejected(RuntimeError)` — broker returned `rejected`/`expired` (caught internally;
  surfaced as a no-fill record, not raised to the caller, so the proposal can be retried).
- Reuse `LiveMultiLegNotAuthorized` (already defined; stays unreachable).

`__init__` now mirrors `PaperReactor.__init__` fully (mkdir + touch the bus) — but ONLY when the
reactor is constructed; the disabled `execute()` still writes nothing because step 1 returns before
any write. (The existing scaffold tests assert "bus not created while disabled" via `execute()`
raising before any write; keep that property — do the mkdir/touch lazily inside `_execute_enabled`,
NOT in `__init__`, so the `test_execute_raises_disabled_*` tests stay green.)

### 2.3 `hermes_quant/react/mleg_fill.py` (NEW) — PaperBroker mleg submit + poll + paper-fill model

Split out so `multileg.py` stays the orchestrator and the broker/fill mechanics are independently
testable. NO alpaca-py import at module top (lazy inside the method — keeps `import
hermes_quant.react.multileg` clean without the `[alpaca]` extra).

```python
@dataclass(frozen=True)
class LegFill:
    symbol: str                  # OCC-21 (option) or ticker (equity)
    filled_avg_price: float
    filled_qty: float            # signed: + long / - short, in contracts (option) or shares (equity)
    status: str                  # 'filled' | 'partially_filled' | 'rejected' | 'expired'
    position_intent: str | None

@dataclass(frozen=True)
class MlegFillResult:
    broker_order_id: str
    client_order_id: str
    status: str                  # parent status (atomicity gate)
    legs: tuple[LegFill, ...]
    net_fill_price: float        # signed net debit/credit actually filled
    source: str                  # 'alpaca_paper' | 'deterministic_model'

class PaperBroker:
    """Paper multi-leg + single-leg option + equity submit/poll.

    Two modes, selected at submit time:
      - LIVE-PAPER: HERMES_QUANT_MULTILEG_REACTOR=1 AND APCA creds present → real
        Alpaca paper POST /v2/orders + GET /v2/orders/{id} poll (async; bounded
        retries/backoff to terminal status).
      - DETERMINISTIC MODEL: no creds (CI / offline) → fill each leg at its
        decision-time mid (from greeks_at_decision-bearing snapshot the proposal
        carries) so the e2e eval gate runs with NO network. Deterministic given the
        proposal (replay-equality).
    Live (non-paper) account → raise LiveMultiLegNotAuthorized (never reached this wave).
    """
    def submit_mleg_order(self, option_legs, *, outer_qty, net_limit_price, tif,
                          client_order_id) -> MlegFillResult: ...
    def submit_single_leg_option(self, leg, *, qty, limit_price, tif,
                                 client_order_id) -> LegFill: ...     # CC short call / CSP short put
    def submit_equity(self, *, symbol, qty, decision_price, client_order_id) -> LegFill: ...  # CC +100
    def poll_order(self, broker_order_id, *, timeout_s=30) -> MlegFillResult: ...
```

- The deterministic model fills options at `mid` (or `last`) from the per-leg `greeks_at_decision`
  snapshot carried on the proposal's legs; if a leg lacks a decision-time price, it falls back to
  the proposal's `net_debit_credit` apportioned by `ratio_qty`. This is the NO-CREDS path the eval
  gate uses, and it is fully deterministic (no RNG) so replays match byte-for-byte.
- The live-paper path: POST the body (research §1.1 shape), then `poll_order` with bounded backoff;
  treat `accepted`/`new`/`pending_new` as non-terminal, `filled`/`partially_filled`/`rejected`/
  `expired`/`canceled` as terminal. `partially_filled` at the SPREAD-count level is recorded with
  the actual `filled_qty` (research §2.1 — never assume full fill).
- `client_order_id` is the idempotency handle (Alpaca rejects a duplicate → exactly-once on retry).

### 2.4 `hermes_quant/state/portfolio_state.py` (EXTEND) — contract/share quantities + per-leg dedup

Two surgical changes (research §4.1 known follow-up):

1. **Quantity unit for options/equity legs.** `_apply_execution_unsafe` currently tracks `quantity`
   in NAV-fraction units (`fill_size_pct`). For `asset_class in {"us_option","equity"}` records that
   carry `reactor_metadata.quantity` (signed contracts/shares), store THAT instead (true position
   size). Equity records WITHOUT a `quantity` (the existing equity path) keep the NAV-fraction
   behavior unchanged — a pure superset, no regression. Gate on the presence of
   `reactor_metadata.quantity`, not on asset_class, so the equity path is bit-identical until a
   multi-leg child supplies a `quantity`.
2. **Per-leg idempotency key.** The `processed_fills` UNIQUE is `(proposal_id, asof_execution)`.
   A multi-leg family shares `proposal_id` + `asof_execution` across all children, so leg 2 would
   be swallowed as a duplicate of leg 1. Extend the ledger key to include `asset` + `asset_class`
   (a `processed_fills_v2` table or an added column with a migration; the existing equity rows
   read with `asset=NULL` → treated as the legacy single-fill key). Each child claims its OWN
   `(proposal_id, asof_execution, asset, asset_class)`; re-applying the SAME child is still a no-op.

Both changes are additive and gated on multi-leg-only inputs; the equity reconciliation path stays
bit-identical (regression test: replay an equity fixture → identical state.db rows).

### 2.5 `hermes_quant/tools.py::quant_approve` (EXTEND) — dispatch on proposal kind

Today `quant_approve` hardcodes `reactor = PaperReactor()`. Introduce a dispatch helper (new
`hermes_quant/react/dispatch.py`):

```python
def select_reactor(proposal) -> Reactor:
    """Return the reactor for a proposal. Equity Proposal -> PaperReactor.
    MultiLegProposal (proposal_kind == 'multi_leg') -> MultiLegPaperReactor.
    The multi-leg reactor is DEFAULT-OFF; if the flag is unset its execute()
    raises MultiLegReactorDisabled, surfaced to the operator as a clear
    'multi-leg reactor not enabled' error — NEVER a silent equity fill."""
```

`quant_approve` calls `select_reactor(proposal)` instead of `PaperReactor()`. The fill-size
resolution (`size_override` > advisor Kelly) is unchanged; for a multi-leg proposal the
gate-admitted `contracts` is the source of truth (carried on the proposal), and `fill_size_pct` is
the equity-leg NAV fraction. The existing "fire React BEFORE state advance; on failure stay
pending" flow is preserved verbatim — so a `MultiLegReactorDisabled` raise leaves the proposal
pending and surfaces the error, exactly the desired default-OFF behavior. **This is the ONLY money
seam touched, and it stays HITL/CLI-only and operator-confirmed (AGENTS.md money-never-through-
tools).** `autonomous.py` is NOT changed (no autonomous multi-leg this wave).

`MultiLegPaperReactor` is added to `react/__init__.__all__` ONLY in this wave (the Wave-B2
regression guard `test_not_in_react_all` is updated — see §3) so the dispatch can import it from the
package. It remains un-fired unless the flag is set.

### 2.6 Shared admissibility helper (refactor, no behavior change)

`PaperReactor._admissibility_reject` is equity-specific and private. Extract the core
(`admit_or_reject(...)` call + the no-fill record construction) into a module-level helper in a new
`hermes_quant/react/admissibility_precondition.py` so BOTH `PaperReactor` and `MultiLegPaperReactor`
call it for the equity leg, without `multileg.py` importing `paper.py`. Behavior bit-identical for
the equity path (regression-tested).

### 2.7 `hermes_quant/reconcile/pmcc_shadow.py` (NEW) — counterfactual validator

```python
def reconcile_pmcc_shadow(*, asof: date, spot_by_symbol: dict[str, float],
                          real_marks_by_mleg_id: dict[str, float]) -> list[PMCCShadowDivergence]:
    """For each recorded PMCC shadow position (load_pmcc_positions), mark_pmcc at
    today's spot and compare the MODEL net_value/net_delta/net_theta_day against the
    reactor's REAL per-leg marks (joined on note == multi_leg_id). Returns per-position
    divergence rows for the 60-day evidence window. The net_theta_day SIGN is the
    structural sanity check: a 'pmcc' marking net-NEGATIVE theta from real marks is a
    build bug, surfaced as a divergence with severity='build_bug_suspected'."""
```

Pure read/compare; writes nothing to executions/state. Consumes the `note=multi_leg_id` join the
reactor stamped in step 10. This is the documented daily counterfactual that "activates implicitly
once the multi-leg reactor lands" (research §4.2).

---

## 3. Test files (all deterministic, no network — AGENTS.md testing discipline)

Unit tests under `tests/unit/`; the live probe under `tests/integration/` (skipped by default).

### 3.1 `tests/unit/test_multileg_proposal.py` (NEW)
- `MultiLegProposal.from_gate_result` copies the gate verdict verbatim: a constructed proposal's
  `risk_gate_pass`/`bucket`/`net_greeks`/`bpr_estimate`/`max_loss` EQUAL the source
  `OptionsGateResult` (the structural consume-never-bypass guarantee).
- `is_mleg` True for vertical/condor/pmcc/roll (≥2 option legs), False for CC/CSP (single option
  leg). `all_symbols` round-trips through `parse_occ`.
- `net_debit_credit` / `max_loss` / `bpr_estimate` are `Decimal` (type assert; float rejected).

### 3.2 `tests/unit/test_multileg_reactor_scaffold.py` (EXTEND the existing file)
Keep all current scaffold tests (flag-gate, no-write-while-disabled, Protocol conformance, name).
- UPDATE `test_not_in_react_all`: assert `MultiLegPaperReactor` IS now exported (this wave wires
  dispatch). (The Wave-B2 guard inverts — document the deliberate change.)
- ADD: with the flag SET but a `risk_gate_pass=False` proposal → `GateRejectedProposal` raised
  BEFORE any bus write (gate-is-final-authority; assert bus unchanged).
- ADD: with the flag SET and a `risk_gate_pass=True` proposal → fills via the DETERMINISTIC model
  (no creds), returns a parent `ExecutionRecord`, and writes parent + N children to the bus.

### 3.3 `tests/unit/test_multileg_reactor_fill.py` (NEW) — the heart
Use the deterministic (no-creds) `PaperBroker` model + a tmp bus + a tmp `state.db`.
- **Covered call e2e (the eval-gate case, unit form):** a CC proposal (stock_leg +100 NVDA,
  one short call `sell_to_open`, `risk_gate_pass=True`, bucket `covered_call`). After `execute()`:
  - parent record: `asset_class="multi_leg"`, `fill_price == net credit (negative)`,
    `reactor_metadata.strategy_kind=="covered_call"`, `multi_leg_id == proposal_id`.
  - TWO children on the bus: `(equity, NVDA, quantity=+100)` and `(us_option, NVDA…C…,
    quantity=-1)`.
  - `state.db` after reconcile: TWO position rows — `(equity, NVDA, qty=+100)` and `(us_option,
    NVDA…C…, qty=-1)` (the research §4.1 two-row golden).
- **Cash-secured put e2e:** single short put `sell_to_open`, bucket `cash_secured_put`. ONE option
  child `(us_option, …P…, quantity=-1)`; one `state.db` option row; no equity row.
- **PMCC e2e:** two call legs (long LEAPS `buy_to_open` + short near-dated `sell_to_open`). mleg
  path; TWO option children; `record_pmcc` called with `note == multi_leg_id` (assert the shadow
  store got a row with the right note); net_theta_day from `mark_pmcc` is POSITIVE (structural
  sanity).
- **Idempotency (ADR-0078):** call `execute()` TWICE on the same proposal → second call is a no-op
  returning the existing parent; the bus has exactly ONE family (parent+children counted once);
  `state.db` rows are NOT double-applied (the per-leg `processed_fills` key holds).
- **Gate-rejected refusal:** `risk_gate_pass=False` → `GateRejectedProposal`, nothing written.
- **Broker reject → no-fill record:** deterministic model forced to return `status="rejected"` →
  a no-fill parent record (status in metadata), no position rows, no fabricated fill.
- **Admissibility on a short-stock collar leg (flag ON):** a stock_leg with qty<0 routed through
  the shared precondition → on inadmissible verdict, a no-fill record; flag OFF → bit-for-bit no-op
  (CC's +100 long always admissible).
- **Slippage asymmetry:** with `HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2`, the CC equity child's
  `fill_price != decision_price` (slipped) while the option child's `fill_price ==
  filled_avg_price` (passthrough). Deterministic seed → replay equality.

### 3.4 `tests/unit/test_mleg_fill_paperbroker.py` (NEW)
- Deterministic model fills each leg at decision-time mid; `net_fill_price` equals the signed sum;
  `source == "deterministic_model"`; no network.
- `submit_*` raises `LiveMultiLegNotAuthorized` if handed a non-paper account (guard reachable
  only via an explicit non-paper construction; never in normal flow).
- mleg body builder emits the research §1.1 shape: outer `qty`/`type`/`limit_price`, per-leg
  `position_intent` + `ratio_qty`, NO equity leg in `legs[]` (assert a CC raises if the equity leg
  is mistakenly passed into `submit_mleg_order`).
- `client_order_id` is stable across two builds of the same proposal (idempotency handle).

### 3.5 `tests/unit/test_portfolio_state_multileg.py` (NEW)
- A child record with `reactor_metadata.quantity=-1`, `asset_class="us_option"` → `state.db`
  `quantity == -1` (CONTRACTS, not NAV fraction).
- An equity record WITHOUT `reactor_metadata.quantity` (legacy path) → unchanged NAV-fraction
  behavior (regression: replay an existing equity fixture, assert identical rows).
- Two children sharing `(proposal_id, asof_execution)` but different `asset` → BOTH applied (the
  per-leg key fix); re-applying the same child → no-op (idempotency held).

### 3.6 `tests/unit/test_reconcile_pmcc_shadow.py` (NEW)
- Record a PMCC shadow (`note=multi_leg_id`); `reconcile_pmcc_shadow` marks it and returns a
  divergence row joined on the id; a forced net-negative real-theta input flags
  `severity="build_bug_suspected"`.

### 3.7 `tests/unit/test_multileg_dispatch.py` (NEW)
- `select_reactor(equity_proposal)` → `PaperReactor`; `select_reactor(multi_leg_proposal)` →
  `MultiLegPaperReactor`. With the flag unset, approving a multi-leg proposal surfaces
  `MultiLegReactorDisabled` (NOT a silent equity fill; assert the error and the pending state).

### 3.8 `tests/integration/test_multileg_paper_roundtrip.py` (NEW, skipped by default)
- Marked `requires_network`; runs only with `--run-integration` + `HERMES_QUANT_MULTILEG_REACTOR=1`
  + paper creds. Submits a real CC + a real debit vertical to Alpaca paper; asserts terminal status
  with per-leg `filled_avg_price`. Probes a calendar spread (different expiries) to record the
  ADR-0029 OQ1 answer. Observational; never runs in CI.

---

## 4. Implementation order (within this wave)

1. `options/multileg.py` (`MultiLegProposal` + `from_gate_result`) + `test_multileg_proposal.py`.
   No deps beyond `options/data.py`. Green.
2. `react/mleg_fill.py` (`PaperBroker` + deterministic model + body builder) +
   `test_mleg_fill_paperbroker.py`. Green (no network).
3. `react/admissibility_precondition.py` extract + refactor `paper.py` to use it (regression: the
   full existing `test_paper*` suite stays green — behavior bit-identical).
4. `state/portfolio_state.py` extend (quantity unit + per-leg key) + `test_portfolio_state_
   multileg.py` + the equity regression test. Green.
5. `react/multileg.py` body + `test_multileg_reactor_fill.py` + extend
   `test_multileg_reactor_scaffold.py`. Green.
6. `react/dispatch.py` + `tools.py` wiring + `test_multileg_dispatch.py`. Update
   `react/__init__.__all__`. Green.
7. `reconcile/pmcc_shadow.py` + `test_reconcile_pmcc_shadow.py`. Green.
8. `tests/integration/test_multileg_paper_roundtrip.py` (skipped-by-default stub).
9. Update `options/__init__.py` to export `MultiLegProposal`; do NOT export the reactor body
   beyond the `react` package.

---

## 5. Verification gate (before claiming done — superpowers:verification-before-completion)

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest \
  tests/unit/test_multileg_proposal.py tests/unit/test_multileg_reactor_scaffold.py \
  tests/unit/test_multileg_reactor_fill.py tests/unit/test_mleg_fill_paperbroker.py \
  tests/unit/test_portfolio_state_multileg.py tests/unit/test_reconcile_pmcc_shadow.py \
  tests/unit/test_multileg_dispatch.py \
  tests/unit/test_options_gate.py tests/unit/test_shadow_pmcc.py -q
# Regression: the equity reactor + state path is bit-identical.
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/unit/ -k "paper or portfolio_state" -q
~/.hermes/hermes-agent/venv/bin/python3 -m ruff check hermes_quant/react/ hermes_quant/options/ \
  hermes_quant/reconcile/ hermes_quant/state/portfolio_state.py
~/.hermes/hermes-agent/venv/bin/python3 -m mypy hermes_quant/react/ hermes_quant/options/multileg.py
# DEFAULT-OFF proof: with NO HERMES_QUANT_MULTILEG_REACTOR set, execute() raises
# MultiLegReactorDisabled and writes nothing.
~/.hermes/hermes-agent/venv/bin/python3 -c "import hermes_quant.react.multileg, hermes_quant.react.mleg_fill, hermes_quant.options.multileg; print('import-clean')"
# The flag is set NOWHERE:
grep -rn "HERMES_QUANT_MULTILEG_REACTOR=1" --include='*.py' --include='*.env' --include='*.sh' . \
  | grep -v "tests/\|docs/\|raise MultiLegReactorDisabled" && echo "FLAG SET SOMEWHERE — FAIL" || echo "flag unset (correct)"
```

---

## 6. The EVAL GATE (the merge-blocking acceptance proof)

**A covered call AND a cash-secured put fill end-to-end on PAPER, validated against the PMCC
shadow, deterministically and offline.** Concretely, a single eval driver
(`ops/scripts/quant-multileg-eval.py`, runnable in CI) that, with `HERMES_QUANT_MULTILEG_REACTOR=1`
set FOR THE EVAL PROCESS ONLY (never in deploy), against tmp bus + tmp state.db + the deterministic
no-creds `PaperBroker`:

1. Constructs a CC `MultiLegProposal` (NVDA +100 shares + 1 short call, `risk_gate_pass=True`,
   bucket `covered_call`) via `from_gate_result` off a real `options_gate(...)` admit, fires
   `MultiLegPaperReactor.execute()`, and asserts:
   - parent `ExecutionRecord` returned, `asset_class="multi_leg"`, signed net price recorded;
   - `executions.jsonl` has exactly ONE family: parent + `(equity NVDA +100)` + `(us_option …C…
     -1)` children;
   - `state.db` has the TWO position rows `(equity,NVDA,+100)` and `(us_option,…C…,-1)`
     (research §4.1 golden), quantities in SHARES/CONTRACTS not NAV fraction.
2. Constructs a CSP `MultiLegProposal` (1 short put, bucket `cash_secured_put`), fires, asserts ONE
   option child + ONE `(us_option,…P…,-1)` state row, no equity row.
3. Fires the SAME CC proposal a SECOND time → asserts the no-op (exactly-once: one family on the
   bus, state.db unchanged) — the ADR-0078 idempotency proof.
4. For a PMCC proposal: fires, asserts `record_pmcc(note=multi_leg_id)` wrote the shadow row, then
   `reconcile_pmcc_shadow(...)` joins the real per-leg marks to the model `mark_pmcc` and asserts
   `net_theta_day > 0` (the structural PMCC sanity — net-negative theta is a build bug).
5. Asserts the gate-is-final rail: a `risk_gate_pass=False` proposal → `GateRejectedProposal`, zero
   writes.

**Gate passes iff:** all five assertions hold AND the run is byte-deterministic across two
invocations (replay equality) AND `~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/unit/
test_multileg_*.py tests/unit/test_mleg_fill_paperbroker.py tests/unit/test_portfolio_state_
multileg.py tests/unit/test_reconcile_pmcc_shadow.py -q` is green AND ruff+mypy clean AND the
default-OFF proof (§5) holds (flag unset → `MultiLegReactorDisabled`, nothing written) AND
`grep` confirms `HERMES_QUANT_MULTILEG_REACTOR=1` is set NOWHERE in repo/deploy. This is the
go/no-go for merging the reactor body; the OPERATOR'S flip to enable it on a real cron is a
SEPARATE, later, deliberate act after the ADR-0029 D7 60-day / N≥100 evidence window.

---

## 7. Reconciliation with the existing 6-PR plan

The 6-PR plan (Phase A data → Phase B gate → Phase C reactor → Phase D observation; per
wave-b2-options.md §3). Wave B2 delivered the foundation of PRs 1–3a default-OFF. THIS wave
delivers the **PR-3a reactor BODY + the paper half of PR-3b** (paper fill/poll only; live deferred),
plus the per-leg reconciliation and the PMCC-shadow validation loop.

| 6-PR plan element | THIS wave? | File(s) | Notes |
|---|---|---|---|
| PR-1 / Phase A — options data layer (OCC, `OptionLeg`/`NetGreeks`, chain reader) | DONE (B2) | `options/occ.py`, `options/data.py` | foundation; reused, not re-built |
| PR-2 / Phase B — options-aware gate | DONE (B2) | `risk/options_gate.py` | the PRECONDITION this reactor consumes |
| PR-3a — multi-leg order shape + reactor | **YES (body)** | `react/multileg.py`, `react/mleg_fill.py`, `options/multileg.py` | scaffold → live body; mleg shape (research §1.1); `MultiLegProposal` (D5) |
| PR-3b — `submit_mleg_order` + lifecycle polling | **PARTIAL (paper only)** | `react/mleg_fill.py` (`PaperBroker`) | LIVE stays gated by `LiveTradingApproval` (D7); paper submit/poll + deterministic model land here |
| PR-4 — `reconcile_options_ntas` (next-day NTA) | NO (deferred) | — | reactor writes the `OCC→multi_leg_id` index this wave so the future NTA loop can join (research §2.3) |
| PR-5 — `StrategyBuilder` emitters + HITL render | NO (deferred) | — | this wave CONSUMES a `MultiLegProposal`; the recipe→proposal producer + chat render (D6 block) are follow-ups |
| PR-6 — kill-switches in settlement_loop | NO (deferred) | — | position-flatten kill-switches (ADR-0027 D5); gate's pin-risk new-entry silence already ships (O7) |

**Backlog mapping:** this wave delivers **N3 / B01 go-live** (the reactor can FIRE on paper),
consuming B02 (data) + B03 (gate). Live promotion (ADR-0029 D7) and the autonomous path (ADR-0016)
remain later, correctly blocked on the 60-day / N≥100 evidence window. The plan deliberately does
NOT build PR-5's producer — the reactor is provable end-to-end with a test-constructed
`MultiLegProposal`, and building the recipe producer concurrently would "stack a bigger lie on the
unfixed producer" (wave-b2 §3 sequencing note). The eval gate uses `from_gate_result` off a REAL
`options_gate` admit, so the consume-the-gate path IS exercised even without the recipe producer.

---

## 8. PDR alignment (ADR-0079 / pdr-unified-architecture.md)

The reactor is the REACTION stage of Perception→Decision→Reaction. The gate verdict
(`OptionsGateResult`) is the DECISION-stage carrier; `MultiLegProposal.from_gate_result` is the
structural seam that makes "a reactor fires an un-gated proposal" unrepresentable — the proposal
cannot carry a gate verdict that disagrees with the gate (the same correctness-by-construction
spirit as ADR-0029 D6's atomic-HITL-approval and ADR-0078's dedup-key identity). When PDR-1's
`PerceptionFrame` lands (separate track), the `MultiLegProposal` becomes the Reaction-stage payload
the frame carries; this wave's `from_gate_result` constructor is forward-compatible (it already
copies the full gate result, the data the frame would carry). No PerceptionFrame code is built here.

---

## 9. Open questions (record data, decide later)

1. **Calendar/diagonal in one mleg** (ADR-0029 OQ1): research says supported; the integration
   probe (§3.8) records the live answer. Until verified, the deterministic model accepts
   different-expiry legs (it fills each independently), so CI is unaffected.
2. **Spread-count partial fill** (research §2.1): paper CAN return `partially_filled` at the
   spread-count level. The reactor records the actual `filled_qty`; the position rows reflect the
   real fill, not `outer_qty`. Whether to auto-cancel the remainder vs. let it work is an empirical
   question for the first 30 days (ADR-0078 EOD-sweep territory).
3. **Paper option pricing is IEX / ~15-min-laggy** (research §2.2): tagged in
   `reactor_metadata.option_pricing`; a known paper-vs-live gap, surfaced for the evidence window.
4. **NAV-fraction `fill_size_pct` on child records** is an approximate proxy retained only for the
   settlement-loop calibrator's existing reader; the AUTHORITATIVE size is
   `reactor_metadata.quantity` (signed contracts/shares). Revisit when the calibrator learns the
   contract unit.

---

## 10. Rails self-check (every box must be true at merge)

- [ ] Reactor CONSUMES `OptionsGateResult` via `MultiLegProposal.from_gate_result`; NEVER re-runs
      or bypasses `options_gate`; `risk_gate_pass is not True` → `GateRejectedProposal`, zero writes.
- [ ] Whole reactor DEFAULT-OFF behind `HERMES_QUANT_MULTILEG_REACTOR=1`; flag set NOWHERE; flag
      unset → `MultiLegReactorDisabled` + nothing written (scaffold tests stay green).
- [ ] Money seam is HITL/CLI-only (`quant_approve`), operator-confirmed; NOT autonomous; NOT a
      tool that auto-fires (AGENTS.md money-never-through-tools).
- [ ] PAPER-only: no live mleg path reachable; `LiveMultiLegNotAuthorized`/`LiveTradingApproval`
      guard intact; `LiveBroker.submit_mleg_order` stays absent.
- [ ] Exactly-once (ADR-0078): stable `client_order_id` + bus `multi_leg_id` dedup; re-`execute()`
      is a no-op; per-leg `processed_fills` key so no leg is swallowed.
- [ ] Admissibility (ADR-0077) applied to the equity leg of a CC via the shared precondition;
      flag-off is a bit-for-bit no-op; option legs out of scope.
- [ ] Slippage (ADR-0070) asymmetric: passthrough on option legs (paper fills at NBBO), v0.2
      envelope on the CC equity leg; deterministic seed → replay equality.
- [ ] Two-row reconciliation: a CC lands as `(equity,…,+100)` + `(us_option,…,-1)` in `state.db`;
      quantities in shares/contracts (extended `apply_execution`), equity path bit-identical.
- [ ] PMCC opens call `record_pmcc(note=multi_leg_id)`; `reconcile_pmcc_shadow` validates the
      counterfactual; net-negative PMCC theta flagged as a build bug.
- [ ] All times UTC; `asof_decision`=proposal.asof, `asof_execution`=fire time; `Decimal` money on
      the proposal; discrete sizing untouched (reactor fills gate-admitted contracts, never widens).
