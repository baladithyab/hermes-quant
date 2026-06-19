# Inc-struct liveness findings (ra05 / ra07 / ra08) — 2026-06-13

Branch: docs/rearchitecture-shared-pdr-core. Method: grep+read liveness trace against
actual source; live cron registry `~/.hermes/cron/jobs.json`; live env `~/.hermes/.env` +
armed wrapper `~/.hermes/scripts/quant-playbook-tick-armed.sh`. Daemon-vestigial lesson
applied: "referenced" != "live-firing". Full trace: `research/temp/inc-struct/prove.md`;
disposition plan: `research/temp/inc-struct/plan.md`.

These three seeds were tagged "orphaned instrument" but the liveness trace proves NONE is
dead orphan code. Each is recorded here as DOCUMENT-ONLY so future readers don't re-litigate
or wrongly quarantine.

## ra07 — governance.promotion.evaluate() (promotion.py:257)
STAGED fail-closed paper->live money gate. ZERO non-test callers. weekly_retro.py is the
PRODUCER of the `weekly_retro_promotion_readiness` signal evaluate() consumes (not a caller);
react/live.py is the THRESHOLD PROVIDER (not a caller). No registered cron calls it. The
operator promotion scripts use the OTHER gate (eval.promotion_orchestrator). Unblocks on B48
/ B01 (a LIVE reactor that does not yet exist — LiveBroker.submit_mleg_order raises
NotImplementedError). Do NOT quarantine (staged, producer is a live cron); do NOT wire to a
cron (no live reactor to consume the decision).

## ra08 — LLM committee (llm_committee.run_llm_committee / agents/research_debate)
DEFAULT-OFF, shadow-only eval-gated rollout. `run_llm_committee` is reachable at runtime
ONLY from the enabled `quant-playbook-tick-daily` cron via `_run_committee_safe`
(quant-playbook-tick.py), gated on `HERMES_QUANT_DELIBERATIVE=1`. `run_research_debate` is
reachable ONLY inside run_llm_committee, gated on `HERMES_QUANT_RESEARCH_DEBATE=1`. The live
`.env` and armed wrapper carry NEITHER flag. Even when enabled it is shadow/journal-only —
the BMA risk gate stays the final authority (DELIBERATIVE_PROMOTE does not override the
gate). Do NOT quarantine (default-OFF rollout, not dead); do NOT flip the flag (no live .env
flips; no eval evidence the committee beats the BMA fallback).

## ra05 — AnalystView contracts + EvidenceRecord
Three "competing" AnalystView definitions are one live + one confined-future-core + one
serialization view:
- `protocol.AnalystView` (protocol.py:108) — the LIVE emit contract; all six analysts emit
  it; BMA consumes it.
- `pdr_core.contracts.AnalystView` (contracts.py:93) — ADR-0092 host-agnostic core
  destination; confined entirely inside `hermes_quant/pdr_core/` (only pdr_core/aggregate.py
  imports it). OUT OF SCOPE to edit.
- `schemas/bar_snapshot.AnalystViewSlot` — a frozen serialization view, not an emit contract.
Collapsing them touches the pdr_core core seam + protocol money types — its own
parity-tested increment (same class as ra06).

EvidenceRecord (evidence/schema.py:50): reachable-on-demand (form4 adapter constructs
FilingEvidence; EvidenceStore + risk/gate.py ADR-0033 D5 lookahead seam exist) but NOT
wired to a live perception source. The live gate (advisor.py:1148 DefaultRiskGate();
recipes.py:286 with empty risk_gate_config) passes NO evidence_store, so the lookahead check
is skipped by default. The no-lookahead-evidence enforcement is DORMANT in production. Wiring
an evidence_store would flip the final-authority gate from skipped->active (behavioral, not
additive) — defer to a parity-tested increment with eval coverage.

## Out of scope (confirmed)
ra06 — collapse the PortfolioState classes (state/portfolio_state.py:218 +
risk/portfolio_normalize.py:101). Touches live state.db money-state; own parity-tested
increment. Not attempted.
