# Increment 1 (step 4) — Port DefaultRiskGate into pdr_core

**Date:** 2026-06-13
**Branch:** `docs/rearchitecture-shared-pdr-core`
**ADR:** ADR-0092 (shared host-agnostic pdr-core)
**Grounded in:** the Increment-1 deep-dive (`wf_a409942b`) port blueprint + the contract layer landed sv1/fl1/pg1.

## Commit state at start

Increment 0 complete (ADR-0091 Option E, both folds: i0a). Increment 1 contracts landed:
`pdr_core/contracts.py` (frozen TRIAD + purity gate), sv1/fl1/pg1 hardening (unified
schema sentinel, Fill validation, tightened FORBIDDEN list incl. governance/evidence/
state/pydantic). kelly is verified-pure; the purity gate now guards the boundary the
port will cross.

## What this increment does (ADDITIVE — live gate untouched)

Port `DefaultRiskGate` VERBATIM into `hermes_quant/pdr_core/gate.py`, wired into NOTHING.
The live `hermes_quant/risk/gate.py` stays bit-for-bit. Safety = a **parity grid** proving
the core gate == `DefaultRiskGate` over a fixture matrix covering every rule branch.

### Port sequence (mega-workflow, dependency-ordered)
1. **kelly git-mv** — `risk/kelly.py` → `pdr_core/kelly.py` (pure: only `__future__`+`math`) + a re-export shim at the old path so every importer still works. Parity grid on the moved functions.
2. **read-interfaces** — `pdr_core/gate_types.py`: frozen read-interfaces for MarketState/Portfolio/HaltState (NaN-fail-closed sentinels preserved) + **`GateDecision`** (the output type).
3. **gate body** — verbatim Rule0..Rule7 + RiskConfig/PROFILES (exact ADR-0004 numbers) + pure leaves. THREE behavior-preserving coupling edits: (a) inject a no-op `audit_sink` (replaces lazy `governance.audit_log` — also severs the transitive pydantic import); (b) `event_risk_enabled` from config (replaces the `os.environ` read); (c) lookahead check dropped to the shell (core imports no `evidence`).
4. **parity grid** — `tests/pdr_core/test_gate_parity.py`: core `GateDecision` → `protocol.Action` field-by-field identical to `DefaultRiskGate` across every branch.

### The riskiest coupling (resolved by design)
`protocol.Action` carries the durable-HALT verdict (`halt`/`halt_scope`/`halt_until`): Rule 1
(drawdown → halt, `halt_until=None` explicit-resume) and Rule 2 (daily-loss → halt,
`halt_until=_next_session_open`) are the gate-as-final-flatten+halt authority (ADR-0004).
`pdr_core.Proposal` has **no** halt fields — collapsing the gate output to a bare Proposal
would silently drop the halt verdict (money-safety regression). **Resolution:** the core gate
returns a frozen `GateDecision` carrying the full halt triple; the shell maps it to
`protocol.Action`. The parity grid asserts the halt triple field-by-field.

## Acceptance (orchestrator renders the ship verdict)
- Verbatim: rule sequence/arithmetic/numbers match DefaultRiskGate.
- Parity grid covers every rule branch AND asserts the full halt triple; passes.
- Purity gate (`test_contract_purity.py`) green — no governance/evidence/state/pydantic import.
- Live `risk/gate.py` byte-unchanged; kelly shim keeps all importers working.

## Not in this increment
Shell adoption (routing `advisor.recommend`/the reactors through the core gate) is a later
increment. Numeric calibrated BMA port is step 6 (separate). Settlement-FIFO normalizer
wiring (sf1/i0c), flag-transition rebuild guard (ft1), asset_class `us_option` divergence
(ac1) remain filed follow-ups.
