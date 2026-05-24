# ADR-0031: Governance Plane Consolidation

**Status:** Proposed
**Date:** 2026-05-24
**Related:** ADR-0026 (retrospective amendment loop), ADR-0027 (options-aware risk gate), ADR-0028 (options data layer), ADR-0029 (multi-leg paper reactor), ADR-0030 (daily picker recipe), AGENTS.md (`Things to NEVER do`)

---

## 1. Context

hermes-quant has grown three runtime planes:

- **Perception** — analysts, semantic perception, recipe runtime (ADR-0021..0025).
- **Decision** — deliberative committee, aggregators, picker (ADR-0023, ADR-0030).
- **Reaction** — risk gate, paper reactor, HITL CLI for live orders (ADR-0027, ADR-0029, ADR-0015).

What it *does not* have is a **fourth, explicit plane for governance**. The artifacts that govern behavior — invariants, kill switches, human approvals, paper→live promotion, and the retro loop's mutation boundary — currently live in five different places:

1. The `Things to NEVER do` section of `AGENTS.md` (just patched in commit `53a864e` to add the covered-call net-greek rule, the `LiveTradingApproval` rule, and the `fetched_at > asof` rule).
2. ADR-0026 D5 retrospective amendment scope allowlist (just patched in commit `d095c24` to bind `code_change` to a file-path allowlist).
3. ADR-0027 immutable risk-gate invariants (`max_position_pct` cap, BPR buffer, no-naked rule, drawdown breakers, net-delta cap).
4. The HITL approval flow, which is currently scattered across `hermes_quant/proposals.py` and `hermes_quant/cli/`.
5. The promotion policy implicit in ADR-0029 D7 (paper→live gate; just patched with type-level `LiveTradingApproval` and the statistical criteria authoritative in ADR-0029 D7: **`N >= 100` settled paper outcomes** (trade-count, not calendar-day-driven), **lower-bound Sharpe `>= 1.0`** via 95% BCa bootstrap, **rolling 30-day max drawdown `<= 1%`**, **zero kill-switch triggers in trailing 14 days**, **zero immutable-rule breaches in rolling 30-day window**, **calibrators within `±5%` of realized**, AND **weekly retro `promotion_readiness: true` flag**).

This is fragile in three concrete ways:

1. **No single source of truth for invariants.** The "never widen the action space" rule lives in AGENTS.md prose. The `max_position_pct` cap lives in the risk gate code. The "never bypass the gate" rule lives in the aggregator's plumbing. There is no module you can import that returns the canonical invariant list, and no CI gate that asserts the retro loop's allowlist is disjoint from it.
2. **Implicit promotion.** The paper→shadow→live promotion criteria are described in ADR-0029 but not encoded as a runnable predicate. A future LLM-authored amendment could in principle approve live trading because nothing fails closed at the boundary.
3. **Ambiguous kill-switch semantics.** The halt flag is written to `state.json` from at least three call sites (data layer crash handler, risk-gate hard breach, manual CLI). There is no idempotent `fire()` and no atomicity contract.

This ADR proposes a fourth plane — **Governance** — that is *append-only*, contains *no new runtime decision-making*, and exists purely to consolidate what is already true. The goal is mechanical: surface the invariants in code, give the kill switch a single owner, give HITL a single contract, and encode the promotion policy as a predicate that fails closed.

**Posture restatement (non-negotiable).** This ADR does not change behavior. It does not loosen, widen, or relax any existing rule. The risk gate stays immutable per ADR-0027. The action space stays discrete per AGENTS.md. The retro loop's mutation surface gets *narrower*, not wider. If this ADR's tests fail to import the existing invariants, the build fails closed.

## 2. Decision

### D1. Module layout — the governance plane

A new package `hermes_quant/governance/` is introduced. It is the only home for cross-plane governance artifacts and is itself **excluded from the retro loop's `code_change` allowlist** (D6).

```
hermes_quant/governance/
  __init__.py
  audit_log.py        # append-only event log spanning all 4 planes
  kill_switch.py      # explicit module — currently scattered
  approvals.py        # refactor of HITL flow currently in proposals.py + cli/
  promotion.py        # NEW — encodes paper→shadow→live promotion policy
  invariants.py       # SINGLE SOURCE OF TRUTH for immutable invariants
```

Module signatures (sketch):

```python
# hermes_quant/governance/audit_log.py
class GovernanceEvent(BaseModel):
    event_id: str           # UUIDv7
    schema_version: int     # bumped on any field-add
    asof: datetime          # UTC, end-to-end
    event_type: Literal[
        "proposal_emitted", "gate_approval", "gate_rejection",
        "fill", "kill_switch_fired",
        "promotion_event", "retro_amendment_applied",
    ]
    plane: Literal["perception", "decision", "reaction", "governance"]
    payload: dict[str, Any]

def append(event: GovernanceEvent) -> None: ...   # fsync; never updates
def read(since: datetime | None = None) -> Iterator[GovernanceEvent]: ...
```

```python
# hermes_quant/governance/kill_switch.py
def fire(reason: str, source: str) -> None:
    """Idempotent. Writes state.json halt flag via atomic-rename. Appends one
    governance event per *first* call; subsequent calls are no-op + warn."""
```

```python
# hermes_quant/governance/promotion.py
# Mirrors ADR-0029 D7's LiveTradingApproval validator inputs verbatim.
# This module is the read-only EVALUATOR; ADR-0029's LiveTradingApproval
# constructor is the only place thresholds live (single source of truth).
class PromotionDecision(BaseModel):
    promoted: bool
    blocked_by: list[str]                     # human-readable reasons
    paper_outcomes_count: int                 # ADR-0029: must be >= 100
    rolling_30d_realized_sharpe: float        # point estimate
    sharpe_95ci_lower: float                  # ADR-0029: must be >= 1.0 (BCa bootstrap)
    rolling_30d_max_drawdown_pct: float       # ADR-0029: must be <= 0.01
    no_killswitch_in_trailing_14d: bool       # ADR-0029: must be True
    immutable_breaches_in_window: int         # ADR-0029: must == 0
    calibrator_drift_max: float               # ADR-0029: must be <= 0.05 (3-of-3 calibrators)
    weekly_retro_promotion_readiness: bool    # ADR-0026: meta-retro flag must be True
```

### D2. Append-only audit log

`audit_log.jsonl` lives at `~/.hermes/quant/governance/audit_log.jsonl`. It is opened in append mode only. The `audit_log` module exposes `append(event)` and `read(since=...)`; no `update`, no `delete`, no `truncate`. Every event row carries `schema_version: int`; reads with mismatched version raise `AuditLogSchemaMismatch` instead of silently coercing.

The seven event types in D1 cover every cross-plane transition we currently care about. New event types require a `schema_version` bump and an ADR amendment — they are not in the retro loop's allowlist.

**Source:** AGENTS.md "All times are UTC end-to-end" + atomic-rename pattern.

### D3. Kill switch — single module, idempotent fire

Today `state.json["halt"] = True` is written from at least three places. D3 collapses all of them onto `governance.kill_switch.fire(reason, source)`. The function:

1. Acquires a process-local mutex.
2. Reads current `state.json`. If `halt == True` already, logs a `WARNING` ("kill switch already fired by `<prior_source>`"), returns without writing or appending.
3. Otherwise: writes `state.json` via the atomic-rename pattern (`state.json.tmp` → `state.json`), then appends one `kill_switch_fired` governance event with `(reason, source, prior_state)`.

This gives us the contract `test_kill_switch_fire_is_idempotent`: two consecutive `fire()` calls produce exactly one log entry.

**Source:** AGENTS.md "Cross-process state" → atomic-rename pattern.

### D4. Approvals — single HITL contract

The current HITL flow is scattered: proposal acceptance lives in `proposals.py`, CLI confirmation prompts live in `cli/lifecycle.py` and `cli/backtest.py`, and live-broker construction lives wherever ADR-0029's `LiveTradingApproval` is instantiated. D4 collapses these onto one module:

```python
# hermes_quant/governance/approvals.py
class HumanApprovalToken(BaseModel):
    token_id: str
    granted_by: str          # CLI user / Discord ID
    granted_at: datetime
    scope: Literal["proposal", "amendment", "promotion", "retro_code_change"]
    target_ref: str          # proposal_id / amendment_id / etc.

def require_human_token(scope, target_ref) -> HumanApprovalToken:
    """Raises NoApprovalError unless the user has explicitly granted a token
    for this exact (scope, target_ref) pair. There is no auto-approval
    path, including from the CLI, including from the daemon."""
```

Money never goes through tools (per AGENTS.md). Money goes through `require_human_token` at one of four named scopes. The `approvals` module is the only writer of the token store.

### D5. Promotion policy — encoded as a predicate

ADR-0029 D7 (patched 2026-05-24) defines the paper→live gate as a `LiveTradingApproval` Pydantic model whose `__init__` enforces every threshold. ADR-0029 is the **single source of truth** for the numerical thresholds; D5 here is the read-only EVALUATOR that gathers the metrics from the audit log + run cards + paper-vs-shadow drift report and produces the `PromotionDecision` object that `LiveTradingApproval`'s constructor will refuse to accept if any field violates ADR-0029's bounds.

`promotion.evaluate()` blocks promotion if any field on `PromotionDecision` would fail the `LiveTradingApproval` validator (see schema above). The exact thresholds are NOT duplicated here on purpose — duplication is exactly the failure mode this ADR is consolidating against. If ADR-0029 amends a threshold (e.g., `N >= 100` becomes `N >= 150`), only ADR-0029 changes; this module's evaluator picks up the new bound automatically because it imports `LiveTradingApproval` and tests construction.

`LiveTradingApproval` cannot be instantiated unless `promotion.evaluate()` produced a `PromotionDecision` whose every field would pass the validator. The CLI `hermes quant live promote --confirm` is the only call site and the only place `LiveBroker(approval=...)` is constructed.

**Source:** ADR-0029 D7 (just-patched); AGENTS.md "Never instantiate LiveBroker without a LiveTradingApproval object."

### D6. Invariants — single source of truth

`hermes_quant/governance/invariants.py` exports the immutable-rule list as runtime-checkable predicates. These are *not* defaults. They are constants the rest of the system imports.

```python
# hermes_quant/governance/invariants.py
ACTION_SPACE: frozenset[float] = frozenset({0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20})

IMMUTABLE_INVARIANTS: tuple[str, ...] = (
    "max_position_pct",         # ADR-0027
    "bpr_buffer",               # ADR-0027
    "no_naked_short_options",   # ADR-0027
    "drawdown_circuit_breaker", # ADR-0027
    "net_delta_cap",            # ADR-0027
    "action_space_discrete",    # AGENTS.md NEVER #1
    "rl_cannot_bypass_gate",    # AGENTS.md NEVER #2
    "no_live_during_research",  # AGENTS.md NEVER #3
    "covered_call_net_greeks",  # AGENTS.md NEVER (ADR-0027 patch)
    "live_requires_approval",   # AGENTS.md NEVER (ADR-0029 patch)
    "no_future_fetched_at",     # AGENTS.md NEVER (ADR-0028 patch)
)

def assert_disjoint_from(allowlist: Iterable[str]) -> None:
    overlap = set(IMMUTABLE_INVARIANTS) & set(allowlist)
    if overlap:
        raise InvariantAllowlistOverlap(overlap)
```

CI runs `assert_disjoint_from(retro.code_change_allowlist)` at startup. This is the formal binding between AGENTS.md prose, ADR-0027 immutables, and ADR-0026's just-patched allowlist.

### D7. Retro loop boundary — governance is block-listed

ADR-0026 D5 was just patched to bind `code_change` to a file-path allowlist:

```yaml
# methodology/retro-allowlist.yaml
code_change_allowlist:
  - 'hermes_quant/risk/**'
  - 'hermes_quant/proposals.py'
  - 'methodology/*.yaml'
code_change_blocklist:
  - 'hermes_quant/governance/**'   # this ADR's new module — never retro-amendable
  - 'docs/adr/**'                  # ADRs go through human editing per ADR-0026 D5
```

A retro proposal whose `scope_type=code_change` and `scope_target` resolves under `hermes_quant/governance/**` is rejected at the synthesizer stage (before HITL even sees it) **and** at the HITL boundary (`approvals.require_human_token` refuses to mint a token for `scope="retro_code_change"` whose target is governance-pathed). Defense-in-depth — both checks must independently fail closed.

## 3. Test plan

The following pytest tests are specified now; implementation lands with the module. Fixtures live under `tests/governance/fixtures/`.

- `test_audit_log_is_append_only_no_truncate` — schema test; attempting `update()` or `truncate()` on the log raises `AppendOnlyViolation`.
- `test_audit_log_event_schema_is_versioned` — every event row carries `schema_version`; reading a row whose version exceeds the current reader raises `AuditLogSchemaMismatch`.
- `test_kill_switch_fire_is_idempotent` — two consecutive `fire()` calls produce exactly one `kill_switch_fired` log entry; second call is a no-op + warn.
- `test_kill_switch_fire_drains_state_json_atomically` — assert `state.json` is written via `state.json.tmp` → `os.replace()`; no torn-write window observable mid-fire (test simulates SIGKILL between tmp write and rename).
- `test_promotion_gate_blocks_when_outcomes_below_adr29_threshold` — feed audit log with 99 settled paper outcomes; `PromotionDecision.promoted == False` and the resulting `LiveTradingApproval(...)` call raises `ValidationError("paper_outcomes_count must be >= 100")`. The test imports the threshold from ADR-0029's validator, not from a hardcoded number, so an ADR-0029 amendment automatically retunes the test.
- `test_promotion_gate_blocks_when_calibrator_drift_gt_5pct` — synthesize a calibrator predicting 0.70 against realized 0.76 (drift = 6%); assert `promoted == False`.
- `test_promotion_gate_blocks_when_immutable_breach_count_nonzero` — inject one `gate_rejection` event with `reason="net_delta_cap"` in window; assert `promoted == False` even if all other criteria pass.
- `test_invariants_list_disjoint_from_retro_allowlist` — property test importing both `governance.invariants.IMMUTABLE_INVARIANTS` and the retro allowlist; assert `set(...) & set(...) == set()`.
- `test_approvals_require_explicit_human_token` — call any governance-gated path without a token; `NoApprovalError` raised. No auto-approval path exists, including from CLI in `--yes` mode.
- `test_governance_module_blocked_from_retro_code_change` — synthesize a retro proposal targeting `hermes_quant/governance/audit_log.py`; assert rejection at the synthesizer stage (no `amendment_id` minted) AND at the HITL boundary (`require_human_token` refuses).

## 4. Implementation map

| Target file                                      | Type        | Est. LOC |
| :----------------------------------------------- | :---------- | :------- |
| `hermes_quant/governance/__init__.py`            | new         | ~10      |
| `hermes_quant/governance/audit_log.py`           | new         | ~150     |
| `hermes_quant/governance/kill_switch.py`         | new         | ~80      |
| `hermes_quant/governance/approvals.py`           | refactor    | ~120     |
| `hermes_quant/governance/promotion.py`           | new         | ~180     |
| `hermes_quant/governance/invariants.py`          | new         | ~60      |
| `tests/governance/test_audit_log.py`             | new         | ~120     |
| `tests/governance/test_kill_switch.py`           | new         | ~80      |
| `tests/governance/test_promotion.py`             | new         | ~150     |
| `tests/governance/test_invariants.py`            | new         | ~60      |
| `methodology/retro-allowlist.yaml`               | patch (D7)  | ~5       |

No runtime-behavior changes outside the module boundary. Estimated 2–4 days of work.

## 5. Cost

**$0.** This is a pure refactor + new local module. There are no LLM calls in any hot path. The audit log, kill switch, approvals, promotion predicate, and invariants are all deterministic local code. The retro loop continues to use whatever inference budget ADR-0026 already allocated; this ADR does not add to it.

## 6. Things this ADR does NOT do

- Does **not** introduce any new runtime decision-making behavior. The picker, aggregator, and risk gate are byte-for-byte unchanged.
- Does **not** modify the risk gate. ADR-0027 immutables remain immutable; this ADR only *reads* them through `invariants.IMMUTABLE_INVARIANTS`.
- Does **not** modify the analyst protocol. ADR-0002 is untouched.
- Does **not** enable live trading. The paper→live gate remains closed; ADR-0029 D7 still gates `LiveBroker.submit_mleg_order`. This ADR only encodes the existing criteria as a predicate.
- Does **not** widen the action space. `ACTION_SPACE` in `invariants.py` is the *current* discrete set per AGENTS.md.
- Does **not** introduce new external dependencies. `pydantic` is already in the dep tree.
- Does **not** auto-approve anything. There is no path through `approvals.require_human_token` that bypasses an explicit human grant — including from the daemon, including from `--yes`, including from a retro proposal.
- Does **not** allow the retro loop to amend `hermes_quant/governance/**`. That path is block-listed in D7 and tested in `test_governance_module_blocked_from_retro_code_change`.

If a future ADR wants to relax any of the above, it must do so explicitly and in human-edited prose — the retro loop cannot reach this file.
