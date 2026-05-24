# ADR-0032: Trading Flow Contract

**Status:** Proposed
**Date:** 2026-05-24
**Wave:** B (load-bearing core abstraction)
**Related:** ADR-0002, ADR-0023, ADR-0027, ADR-0028, ADR-0029, ADR-0030, ADR-0031

---

## Context

ADR-0030 introduced the methodology screener and its 4-namespace DSL (`entry_signal`,
`exit_signal`, `options_chain`, `risk_liquidity`). Screeners answer the question
*"which assets are eligible right now?"* — but a screener alone is not a trading
strategy. A live (or paper) strategy also needs:

1. A **state machine** that tracks the lifecycle of an open position
   (PENDING_ENTRY → ENTERED → PENDING_EXIT → EXITED) so the daemon can never be
   "in the middle of nothing".
2. A **per-flow risk envelope** — sub-caps that sit *underneath* the immutable
   global caps from ADR-0027. A covered-call flow and a vol-arb flow may legally
   share the same global vega budget, but each must declare its own slice.
3. A **mode gate** (`research | paper | shadow | live`) that is checked
   structurally — not as a runtime flag the operator can flip without an audit
   trail.
4. A **screener binding** that points at an existing methodology YAML
   (ADR-0030) without duplicating its rules.

Today, files under `hermes_quant/recipes/*` conflate these concerns: a "recipe"
is half-screener, half-strategy, with the FSM living in Python and the risk
caps living in a config dict. This makes it impossible to:

- statically validate that a flow's vega budget fits inside the global cap;
- replay a flow's history deterministically (the FSM is implicit);
- run multiple flows on the same symbol without first-wins races;
- ship a new strategy without a code review of imperative Python.

ADR-0032 defines the **Trading Flow Contract** — the declarative composable
unit that replaces ad-hoc recipes. Every other moving part of the system
(committee aggregator, daemon tick loop, evidence store, governance plane)
hangs off this contract.

---

## Decision

A **Flow Contract** is a YAML document at
`hermes_quant/recipes/flows/<name>.yaml` that binds together a screener, an
FSM, a risk envelope, an execution policy, and a `mode_allowed` enum. Flow
contracts are parsed by a Pydantic schema, validated against the global risk
caps and ADR-0028 fill models, and compiled to an immutable `CompiledFlow`
runtime object. The daemon's tick loop iterates over compiled flows; every FSM
transition emits an audit event into the ADR-0031 governance plane.

### Canonical example

```yaml
name: covered_call_v1
version: 1
mode_allowed: [research, paper]
screener:
  $ref: methodology://covered_call_screener_v1

fsm:
  initial: PENDING_ENTRY
  states:
    PENDING_ENTRY: {on: [SUBMIT_ENTRY], next: ENTERED}
    ENTERED:      {on: [HIT_TARGET, HIT_STOP, EARNINGS_BLACKOUT], next: PENDING_EXIT}
    PENDING_EXIT: {on: [SUBMIT_EXIT], next: EXITED}
    EXITED:       {terminal: true}

risk_envelope:
  max_open_positions: 5
  max_per_symbol: 1
  max_net_delta_pct_nav: 0.20
  max_net_vega_per_position: 50      # MUST be ≤ ADR-0027 global cap
  cooloff_days_after_loss: 3

execution:
  fill_assumption: midpoint_with_2pct_slip   # ADR-0028 FillModel ID
  rate_limit:
    orders_per_minute: 6
    cancels_per_minute: 12
```

---

## D1 — YAML Schema (Pydantic models)

Module: `hermes_quant/contracts/schema.py` (~250 LOC).

Pydantic v2 models with `model_config = ConfigDict(extra="forbid")` so unknown
top-level keys raise — there is no silent ignore.

```python
class FlowMode(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"

class FlowFSMState(BaseModel):
    on: list[str] = Field(default_factory=list)     # event names
    next: str | None = None                          # target state
    terminal: bool = False

class FlowFSM(BaseModel):
    initial: str
    states: dict[str, FlowFSMState]                  # name → state

class FlowRiskEnvelope(BaseModel):
    max_open_positions: int = Field(ge=0)
    max_per_symbol: int = Field(ge=0)
    max_net_delta_pct_nav: float = Field(ge=0.0, le=1.0)
    max_net_vega_per_position: float = Field(ge=0.0)
    cooloff_days_after_loss: int = Field(ge=0)

class FlowRateLimit(BaseModel):
    orders_per_minute: int = Field(ge=0)
    cancels_per_minute: int = Field(ge=0)

class FlowExecution(BaseModel):
    fill_assumption: str                             # FillModel ID (ADR-0028)
    rate_limit: FlowRateLimit

class FlowScreenerRef(BaseModel):
    ref: str = Field(alias="$ref")                   # methodology://name

class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    version: int = Field(ge=1)
    mode_allowed: list[FlowMode] = Field(min_length=1)
    screener: FlowScreenerRef
    fsm: FlowFSM
    risk_envelope: FlowRiskEnvelope
    execution: FlowExecution
```

Ambiguity is rejected at parse time: `mode_allowed: []` is illegal,
`max_net_delta_pct_nav: 1.5` is illegal, `version: 0` is illegal.

---

## D2 — Validator (cross-checks)

Module: `hermes_quant/contracts/validator.py` (~180 LOC).

Beyond schema parsing, the validator runs **fail-fast cross-checks** that
require external context (global caps, available fill models, available
methodologies):

1. **Global-cap check.** Load ADR-0027 immutable caps; assert
   `flow.risk_envelope.max_net_vega_per_position ≤ global.max_net_vega_per_position`.
   On violation raise `FlowEnvelopeViolatesGlobalCap(flow_name, field, flow_value, global_value)`.
   Same check for `max_net_delta_pct_nav` and `max_open_positions` if those caps
   exist globally.
2. **Fill-model existence.** Resolve `flow.execution.fill_assumption` against
   the ADR-0028 fill-model registry; raise `FillModelNotRegistered` if missing.
3. **Methodology existence.** Resolve the `$ref: methodology://name` against
   the ADR-0030 methodology loader; raise `MethodologyNotFound` if missing or
   if the methodology itself fails ADR-0030 D2 schema validation.
4. **FSM well-formedness.** `fsm.initial` must be a key in `fsm.states`.
   Every `next:` must point to a defined state. Exactly one terminal state
   must be reachable from `initial` via DFS — flows without a sink are
   rejected (`FlowFSMNoTerminalReachable`).
5. **Mode safety.** If `FlowMode.LIVE in mode_allowed`, the validator checks
   for the presence of a `LiveTradingApproval` (ADR-0029); without it the flow
   is rejected at type level — *not at runtime*.

All exceptions inherit from `FlowContractError`; the loader raises on the
first failure with the YAML line number where available.

---

## D3 — Compiler

Module: `hermes_quant/contracts/compiler.py` (~200 LOC).

`compile_flow(yaml_path: Path) -> CompiledFlow` performs:

1. parse YAML → `Flow` (D1);
2. validate (D2);
3. resolve the screener `$ref` to a bound, ADR-0030-validated screener
   object;
4. instantiate a `FSMRuntime` (D4) seeded with the FSM definition;
5. snapshot the risk envelope into an immutable `frozen` dataclass (no
   mutation after compile);
6. bind the fill model from ADR-0028;
7. compute `compiled_hash = sha256(canonical_yaml + global_caps_hash + methodology_hash)`;
8. return `CompiledFlow(flow=..., screener=..., fsm=..., envelope=..., execution=..., compiled_hash=...)`.

Compilation is **idempotent and cached**: a process-wide LRU keyed on
`(yaml_path, mtime_ns)` returns the same `CompiledFlow` instance until the
source file changes. This matters because the daemon tick loop will look up
compiled flows on every iteration and we cannot afford re-parsing.

The compiler is **pure** — no I/O outside reading the YAML, the methodology
file, and the global-cap registry; no network; no LLM calls.

---

## D4 — FSM Execution Semantics

Module: `hermes_quant/contracts/fsm.py` (~120 LOC).

`FSMRuntime` exposes exactly one mutating method:

```python
def transition(self, instance_id: str, event: str, *, asof: datetime,
               symbol: str, flow_name: str) -> FSMTransitionResult: ...
```

Semantics:

- The current state of each open position (`instance_id`) is held in an
  external store (`PositionStateStore`, defined elsewhere) keyed on
  `(flow_name, instance_id)`. The FSM runtime is itself stateless — it is a
  pure function `(state, event) -> state'`.
- An `event` not listed in `states[current].on` raises
  `IllegalFSMTransition`; there is no permissive fallback.
- Every successful transition emits **exactly one** audit event to the
  ADR-0031 governance plane:
  ```python
  AuditEvent(
      kind="flow.fsm.transition",
      flow_name=flow_name,
      symbol=symbol,
      instance_id=instance_id,
      from_state=...,
      to_state=...,
      event=event,
      asof=asof,
      compiled_hash=self.compiled_hash,
  )
  ```
- Hidden state is forbidden: any field the FSM reads to make a decision
  must be either a constant in the YAML or an event payload. No silent
  reads from `self`.

Cross-reference: ADR-0031 (governance plane / audit log) defines the
durability and ordering guarantees of the audit channel. ADR-0032 only
*emits*.

---

## D5 — `mode_allowed` Enforcement

Two enforcement points, both mandatory:

1. **Compile time** (D3 step 2). A flow declaring `mode_allowed: [live]`
   without a `LiveTradingApproval` is rejected — the YAML never produces a
   `CompiledFlow`. This is the structural backstop.
2. **Runtime, in the daemon's tick loop**, **before** the screener runs:

   ```python
   for flow in compiled_flows:
       if daemon.current_mode not in flow.mode_allowed:
           metrics.silenced_by_mode_gate.inc(labels={"flow": flow.name})
           continue                       # skip — no screener call, no API hit
       intents = flow.screener.run(...)
       ...
   ```

   The skip happens *before* any screener execution, so a research-mode
   daemon never invokes the screener of a paper-only flow — no API calls,
   no LLM calls, no work. The `silenced_by_mode_gate` counter is the
   ADR-aligned "silence by default" telemetry.

---

## D6 — Recipe Migration Plan

Existing files under `hermes_quant/recipes/*.yaml` are *screener-only* (the
ADR-0030 shape). During Phase 4 they migrate to flow contracts under
`hermes_quant/recipes/flows/`.

**Backward-compat shim.** A loader that encounters a recipe with **no `fsm`
block** auto-wraps it into a default 2-state FSM:

```yaml
fsm:
  initial: PENDING_ENTRY
  states:
    PENDING_ENTRY: {on: [SCREENER_FIRED], next: EXITED}
    EXITED: {terminal: true}
```

The shim also injects a default `risk_envelope` (zeros — i.e. produces
intents but never sizes them above zero, so the recipe is effectively
research-only) and `mode_allowed: [research]`. This makes the migration
path zero-breakage: existing recipes keep working in research mode while
they are rewritten one at a time. The shim emits a deprecation warning
log and a `legacy_recipe_loaded{flow=…}` metric so we can track migration
progress.

The migration target ordering is:

1. `covered_call_v1` — first explicit flow contract (this ADR's example);
2. all other recipes — one PR per recipe, each closing a checklist item
   in the Phase 4 tracking issue;
3. the shim is removed once `legacy_recipe_loaded` is zero for 7 days.

---

## D7 — Multi-flow Concurrency

Module: `hermes_quant/contracts/aggregator.py` (~150 LOC).

Two flows may legally generate `TradeIntent`s on the same symbol in the same
tick (e.g. a covered-call flow wants to *enter*, a vol-arb flow wants to
*hedge*). **First-wins is forbidden.** Instead, conflicting intents are
forwarded to the existing ADR-0023 committee aggregator, extended here with
a per-symbol scoring step:

```python
def aggregate(intents: list[TradeIntent]) -> TradeIntent | None:
    by_symbol = group_by_symbol(intents)
    final = []
    for symbol, group in by_symbol.items():
        if len(group) == 1:
            final.append(group[0]); continue
        scored = [(score(intent), intent) for intent in group]
        scored.sort(key=lambda x: x[0], reverse=True)
        winner = scored[0][1]
        audit_log.emit(AuditEvent(
            kind="flow.aggregator.resolved_conflict",
            symbol=symbol,
            candidates=[i.flow_name for _, i in scored],
            scores=[s for s, _ in scored],
            winner=winner.flow_name,
        ))
        final.append(winner)
    return final
```

The scoring function combines: (a) the screener's confidence from ADR-0002
`AnalystView`, (b) the flow's historical Sharpe over a configurable window,
(c) the headroom remaining in the flow's risk envelope. The exact weights
live in `aggregator.py` and are themselves subject to ADR-0027 immutability
(weights cannot be hot-patched at runtime). The point of D7 is that the
**rationale is logged** — every multi-flow conflict produces a single
`flow.aggregator.resolved_conflict` audit event with the scores, so the
operator can replay any decision.

---

## Test Plan

All tests live under `tests/contracts/` (~600 LOC across 8 named tests).

- **`test_flow_yaml_schema_validates_minimal_example`** — load the canonical
  YAML above, round-trip through `Flow` Pydantic model, assert no extra
  fields, assert `mode_allowed == [RESEARCH, PAPER]`.
- **`test_flow_envelope_cap_le_global_cap_or_raises`** — synthesize a flow
  with `max_net_vega_per_position: 200` while the global cap is `100`;
  assert `FlowEnvelopeViolatesGlobalCap` is raised by the validator (D2),
  *not* at runtime; assert the exception carries the field name and both
  values.
- **`test_flow_fsm_transition_emits_audit_event`** — drive the FSM through
  PENDING_ENTRY → ENTERED → PENDING_EXIT → EXITED; assert the audit log
  contains exactly 3 entries with the correct `flow_name`, `symbol`,
  `from_state`, `to_state`, monotonic `asof` timestamps, and a stable
  `compiled_hash`.
- **`test_flow_mode_allowed_skipped_at_runtime`** — instantiate a daemon
  in `mode='research'`; load a flow with `mode_allowed=[paper, live]`;
  run one tick; assert the screener was never called (mock with
  `assert_not_called()`); assert the `silenced_by_mode_gate` counter
  incremented by exactly 1 with the correct `flow` label.
- **`test_flow_compile_idempotent_caches_compiled_object`** — call
  `compile_flow(path)` twice; assert `is` identity on the returned
  `CompiledFlow`; `touch` the YAML to bump `mtime_ns`; call again; assert
  a *new* `CompiledFlow` is returned (cache invalidated).
- **`test_flow_aggregator_scores_conflicting_intents`** — two flows produce
  buy intents for AAPL with sizes 100 and 200; aggregator emits exactly
  one final intent; assert a `flow.aggregator.resolved_conflict` audit
  event was emitted with both candidate flow names and scores; assert the
  winner is the one with the higher score (not the one that arrived
  first).
- **`test_flow_backward_compat_shim_wraps_screener_only_recipe`** — load
  a legacy recipe with no `fsm` block; assert the loader returns a
  `CompiledFlow` whose FSM has the default 2-state shape; assert the
  `legacy_recipe_loaded{flow=...}` metric incremented; assert
  `mode_allowed == [RESEARCH]`.
- **`test_flow_validator_rejects_unknown_top_level_keys`** — parse a YAML
  containing a stray `notes: hello` top-level key; assert `ValidationError`
  is raised; assert the error message names the offending field.

---

## Implementation Map

```
hermes_quant/contracts/__init__.py                         ~5  LOC
hermes_quant/contracts/schema.py                           ~250 LOC
hermes_quant/contracts/validator.py                        ~180 LOC
hermes_quant/contracts/compiler.py                         ~200 LOC
hermes_quant/contracts/fsm.py                              ~120 LOC
hermes_quant/contracts/aggregator.py                       ~150 LOC
hermes_quant/recipes/flows/covered_call_v1.yaml            (first migration)
tests/contracts/test_*.py                                  ~600 LOC / 8 tests
```

Effort estimate: **1–2 weeks**.

---

## Cost

**$0.** The contract layer is a local, deterministic, pure-Python module:
YAML parsing, Pydantic validation, in-process FSM evaluation. There are no
LLM calls in the hot path — LLM work happens *upstream* in the methodology
screener (ADR-0030) and *downstream* in the analyst protocol (ADR-0002),
both of which are rate-limited and cached.

---

## Cross-references

- **ADR-0002** — analyst protocol; flows consume `AnalystView`s emitted by
  the screener.
- **ADR-0023** — committee aggregator; extended in D7 to score conflicting
  flow intents.
- **ADR-0027** — immutable risk caps; D2 validator enforces
  `flow_envelope ≤ global`.
- **ADR-0028** — data layer; `flow.execution.fill_assumption` references a
  registered `FillModel`.
- **ADR-0029** — paper-only mode; `mode_allowed: [live]` is rejected at
  type level until `LiveTradingApproval` lands.
- **ADR-0030** — methodology screener; flows reference methodologies via
  `$ref: methodology://name`.
- **ADR-0031** — governance plane; FSM transitions and aggregator decisions
  emit audit events into this plane.
- **AGENTS.md "Action space is discrete"** — a flow's risk envelope can
  only *narrow* the action space, never widen it.

---

## Consequences

**Positive.**

- One declarative artifact per strategy. The full lifecycle of an open
  position is visible in 30 lines of YAML.
- Static guarantees: no flow can exceed the global vega cap, reference a
  missing fill model, or sneak `live` mode in without a `LiveTradingApproval`.
- Deterministic replay: every transition and every aggregator decision
  produces an audit event with a `compiled_hash`, so a historical run can
  be reconstructed bit-for-bit.
- Multi-flow safety: the aggregator's scoring step makes flow conflicts a
  first-class observable phenomenon rather than a race.

**Negative / costs.**

- Migration friction: every existing recipe must be rewritten. The
  backward-compat shim absorbs most of this, but the long-tail cleanup
  is real.
- Schema rigidity: `extra="forbid"` will reject typos that were
  previously silently ignored. This is the intended trade-off but will
  produce noise during the first weeks of migration.
- A new module boundary (`hermes_quant/contracts/`) to maintain.

**Mitigations.**

- The shim and the `legacy_recipe_loaded` metric let us migrate
  incrementally without freezing development.
- The validator's error messages include YAML line numbers and field
  names, so typo-rejection is a 5-second fix in practice.
- The contracts module is pure and small (~900 LOC total); it is
  extensively tested (D-test plan above) and has zero runtime
  dependencies on network or LLM services.
