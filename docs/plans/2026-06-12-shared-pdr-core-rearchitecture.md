# Migration Plan — Shared PDR Core + Two Integration Shells

**Status:** plan (ratified by ADR-0092, proposed)
**Date:** 2026-06-12
**Companion:** `docs/adr/ADR-0092-shared-pdr-core-two-integration-shells.md`, `docs/design/2026-06-12-shared-pdr-core-architecture.md`
**Blocked on:** ADR-0091 resolution (ledger fold semantics) before Increment 0.

This plan is **strangler-fig with the spine-keep-leaves discipline**: every money-touching step ships flag-OFF==legacy bit-for-bit, the old code stays importable for one shadow-compare cycle, and the live paper system never goes dark. The order is **non-negotiable** — it is driven by the verified blast-radius dependency (everything downstream reads the ledger, so the ledger goes first).

---

## Precondition P0 — operational safety (operator's call; recorded, not actioned)

The assessment **reproduced** that the live book is currently untrustworthy: dual-ledger divergence is live, 2-of-4 fire-paths bypass the cap seam, and an armed `playbook-tick` cron is POSTing real paper orders through hand-rolled rails.

**Recommendation:** make the armed crons observe-only until Increment 0 lands. One-line, fully reversible, removes the money risk without slowing the rearchitecture. The operator runs the crons — this plan records the recommendation; it does not flip anything.

**Precondition P1 — cheap and blocking, protects every approach:** extend the `conftest` autouse isolation to `executions.jsonl` / `QUANT_HOME` / `artifacts` (today it isolates `state.db` + governance/evidence/kill-switch, but NOT `executions.jsonl`/`QUANT_HOME` — the real gap). This is the guard that makes the fixture-leak class impossible during migration. Do this first, regardless of which increment follows.

---

## Increment 0 — the correctness core (MUST be first)

**Goal:** stand up `pdr-core` from cowork's `quantcore` spine; land the single-reconstructor hash-chained ledger.

- Promote cowork `ledger.py` → `pdr-core`: one append-only hash-chained JSONL, `PortfolioState` reconstructed via one `Ledger.portfolio()` fold, `state_dir` injected (no default path to live storage).
- **Consume ADR-0091's resolution** for the fold semantics. Do NOT re-derive the producer fix — two cross-family review rounds already killed Option A and found P0s in Option B; Option C (version-discriminated carry-forward) is the surviving candidate. Treat any historical repair as a **new derived artifact** (`executions.v2.jsonl`), leaving the append-only original immutable — an append-only money log cannot be un-appended.
- Signed-notional `equity_total` (shorts subtract, not `abs()`-add) — **isolate this as its own gated change with its own parity assertion**; it independently changes the NAV the gate sizes against.
- **External truth oracle:** reconcile the reconstructed NAV against the **Alpaca paper broker statement**, not just the old `state.db` (self-comparison from a shared-origin source cannot catch a shared-origin error).

**Eval gate:** new ADR-0091 acceptance tests green; reconstructed NAV matches the broker statement within tolerance; `verify_ledger` cross-module consistency passes (every fill traces to a prior APPROVED proposal).

**Kill criterion:** if no single fold semantic can reconcile against the broker statement without per-reactor special-casing, the rot is in the leaves (reactors emit irreconcilable units) — escalate. Early signal: Increment 0 alone blows past ~3 weeks.

## Increment 1 — freeze contracts + port the proven leaves into core

**Goal:** the frozen typed boundary + the leaves cowork only spec'd.

- Freeze `AnalystView / PerceptionFrame / CommitteeSignal / Proposal / Fill` as frozen models in `pdr-core`.
- Port hermes's **numeric calibration-weighted BMA** (the canonical aggregator — charter interpretability depends on it; cowork's in-session rules demote to a shell evidence producer). Specify cold-start calibration bootstrap.
- Port the **gate verbatim** (already a clean leaf), the **settlement/horizon math** + cowork's raw-move-vs-realized-P&L correctness fix (R1-01), and the **CPCV/DSR/PBO eval harness**.
- Promote `EvidenceRecord` to THE typed perception datapoint; introduce a `perception_sources` entry-point group to kill the if/try flag-gated builder branches.

**Eval gate:** ported BMA reproduces hermes's current vote on a frozen replay set; the no-lookahead shuffle-timestamp CI gate passes on the core.

## Increment 2 — hermes shell adopts the core

- Route `advisor.recommend` through the core's aggregate+gate.
- Retire hermes's 3 duplicate `PortfolioState` classes; point the concurrent-position rail at the core's one reader.
- **Unify all fire-paths through ONE reaction seam** (`select_reactor`): fix `autonomous.py:884`; replace `playbook-tick`'s raw-Alpaca POST. Add a test asserting all four firing layers inherit the cap by construction (today 2 of 4 bypass).
- Keep the 4238-test corpus as the leaf spec; **flip the green-but-broken assertions** (they currently encode the ADR-0091 inflation + ADR-0087 bypass as GREEN). Enumerate WHICH tests assert broken behavior — if >30% need rewriting (not just flipping), the corpus was a spec of the tangle, not the leaves → escalate.

**Eval gate:** `test_frame_replay` equivalence stays green throughout; cap-inheritance test passes for all four layers; default flag-OFF reactor stays bit-for-bit legacy `PaperReactor`.

**Canary:** `playbook-tick` is an armed cron — canary one symbol for a session before full cutover.

## Increment 3 — cowork shell adopts the core

- `git init` + publish cowork-quant so its 212-test claim is reproducible in CI (it could not be reproduced in the assessment env).
- cowork depends on `pdr-core`; gains the ported numeric BMA / options / eval contracts as they land.
- cowork's in-session committee becomes a shell `AnalystView` producer over the shared contract.

**Eval gate:** cowork's 212 tests reproduce green in CI against `pdr-core`.

## Increment 4 — orchestration spine + deploy lineage (hermes)

- Delete the vestigial daemon main-loop cluster (`main`/`tick_loop`/`heartbeat`/`lock`/`watermark` — verified 0-1 external importers). **Keep** the shared daemon utilities (`signal_bus`/`halt_state`/`settlement_loop.join_exit_fills` [the kill-switch P&L basis]/`discovery`/`portfolio_loader`/`slippage` — verified 2-7 importers each). Do NOT over-broadly `delete daemon/`.
- Collapse the 5 reimplemented run-loops to one in-package orchestrator; the three live crons become thin shims.
- Deployed artifact becomes a thin shim importing the versioned in-package module; delete the `scripts/` vs `ops/scripts/` vs `~/.hermes/scripts/` three-way drift; retire the deploy-drift watchdog after one clean deploy cycle. One source of truth for "what is ON."

**Eval gate:** full test suite green; kill-switch + three cron tests green; AGENTS.md/README updated to the live spine (not the dead freqtrade path).

## Increment 5 — concurrency / atomicity (the blind spot all proposals missed)

- The `claim → broker → append executions → update state.db` write is **not atomic across stores** (ADR-0078; traced to the 880% blown-up book). Collapsing to one reconstructor does NOT make the write atomic — two crons can still interleave on the same symbol.
- Specify a single-writer invariant / lock / transactional ordering for the ledger append. This is orthogonal to the dual-ledger-semantics fix and survives every earlier increment if not addressed.

**Eval gate:** a concurrency test that interleaves two firing paths on one symbol cannot corrupt the fold.

## Increment 6 — run the charter's never-run empirical proof

On the unified, trustworthy core: **does a 3-analyst committee beat buy-and-hold risk-adjusted on paper over weeks?** This — not a parity table — is the charter's stated architectural success criterion, and it has been bypassed the entire time. Decide BTC-first (charter-faithful) vs equities (current instruments) before running (open question Q3).

---

## Cost & reversibility

- Front-loaded: Increment 0 (ledger + broker reconciliation) is the expensive, highest-stakes step.
- No long dark window — each step is flag-gated or test-gated; the default-OFF path stays bit-for-bit legacy. Two brief cron-pause windows: the `playbook-tick` reaction cutover (Increment 2, one-session canary) and the deployed-shim cutover (Increment 4, one deploy cycle).
- The decision core and gate are **ported, not rewritten** — they never go dark.
- Schedule is gated by trading-calendar days + operator availability (off-box `.env`/`armed.sh` + deployed-shim round-trips), not engineer-weeks alone.

## Earliest-firing kill criteria (escalate to a deeper rewrite if ANY trip)

1. No single ledger fold reconciles against the broker without per-reactor special-casing → rot is in the leaves.
2. `test_frame_replay` cannot be made green after the perception-producer unification → the "one producer" contract is unachievable without rewriting perception.
3. The orchestrator extraction requires changing `DefaultRiskGate` or BMA call signatures → the "keep the leaves" thesis is false.
4. >30% of the 4238-test corpus needs *rewriting* (not just assertion-flipping) → tests were specs of the tangle, not the leaves.
5. Increment 0 alone blows past ~3 weeks, or broker reconciliation keeps failing → ledger is more entangled with the leaves than the evidence suggests.

## Findings → seeds

File all derived findings as seeds (`.seeds/file_seed.py --title … --type bug|task|epic --priority 1|2|3`), not GitHub issues, per repo convention. Suggested epics: `pdr-core extraction`, `one-reaction-seam`, `cross-store atomicity`, `deploy-shim lineage`.
