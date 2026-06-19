# Three Architectures — Operator Debrief

**Date:** 2026-06-12
**Companion:** ADR-0092, `docs/design/2026-06-12-shared-pdr-core-architecture.md`
**Sources:** two adversarial assessment workflows (81 agents), a current-structure map of cowork-quant, ADR-0091 resolution.

This is the plain-language briefing on where the two systems are today and where the new architecture takes them. All three share one charter: **Perception → Decision → Reaction**, deterministic risk gate as final authority, silence-by-default, no-look-ahead, paper-only.

---

## A. hermes-quant today — the Hermes-agent plugin

**Scale:** ~82K production LoC across 255 modules, 328 test files, 90 ADRs. Grew ~4× in ~2 weeks.

**What it is:** a long-lived, capability-rich trading framework for the **Hermes agent** host. Python analyst *classes* (classical TA, microstructure, Kronos foundation-model, semantic/catalyst, fundamentals) feed a regime-aware Bayesian Model Averaging aggregator, through a deterministic risk gate, into reactors that write a paper book. Driven by cron scripts (universe scan, watchlist evolve, autonomous tick, playbook tick, hourly tick, EOD interim) plus an MCP tool surface.

**The crown jewels (verified clean):** `advisor.recommend` (the Perception→Decision engine), the analyst Protocol, the numeric calibration-weighted BMA math, and `risk/gate.py` (the deterministic gate — imports only `protocol`+`kelly`, a genuinely clean leaf). These are well-tested, low-coupling, and worth keeping.

**Where it hurts (the connective tissue — scored 3-4/10 coherence):**
- **Two incompatible spines.** The *documented* spine (`daemon → signals.jsonl → freqtrade`) is vestigial — no deployed cron runs it. The *live* spine is cron scripts that each re-glue Perception and Reaction by hand. The docs describe a system that doesn't run.
- **Dual-ledger divergence (live).** Two reconstructors read the same `executions.jsonl` with incompatible semantics (cumulative-delta vs latest-target). Re-affirm +0.20 twice → 0.40 in one book, 0.20 in the other. The gate sizes against one; the concurrent-position rail polices the other. (This is ADR-0091, now resolved to Option E but not yet implemented.)
- **2 of 4 fire-paths bypass the cap seam.** `autonomous.py:884` hardcodes `PaperReactor()`; `playbook-tick` POSTs raw to Alpaca through hand-rolled rails. The "final authority" cap is path-dependent — the mechanism behind the 41.6×-gross incident.
- **Orphaned instruments.** The paper→live promotion gate has zero callers; the entire LLM committee layer fires on no live path; three eval gates are wired to nothing (~76KB).
- **No single source of truth for "what is ON."** Runtime enablement lives off-repo in `~/.hermes/.env`; three flag inventories disagree; production scripts exist in three drifting copies.

**Two refuted scares (intellectual honesty):** a "22-module circular-dependency blob" and "58% deferred-import camouflage" were both measurement artifacts — the real runtime import graph is a clean DAG. The leaves are not the problem.

## B. cowork-quant today — the Claude Cowork plugin

**Scale:** ~3.9K core LoC across 19 `quantcore` modules, 212 tests green, v0.2.0. ~20× smaller than hermes.

**What it is:** a lean, from-scratch rebuild of the *same charter* for the **Claude Cowork** host. The split is the whole idea: **Claude runs the analyst committee in-session** (skills + bull/bear/risk-skeptic subagents emit Pydantic-validated `AnalystView` JSON), and **a small deterministic Python core (`scripts/quantcore/`) does everything that touches money** — BMA aggregation, the 8-rule gate, ¼-Kelly sizing, the ledger, settlement, calibration, eval. The human executes trades in their own broker. Discrete scheduled `/watch` turns replace the daemon.

**Command surface:** `/scan` (views only), `/propose` (gate + AskUserQuestion approval), `/settle` (fills + calibration), `/brief`, `/watch` (unattended, queue-only), `/retro`, `/schedule`, `/dashboard`, `/doctor`.

**What it does *better* than hermes (by construction):**
- **One ledger, one fold.** A hash-chained append-only JSONL; `PortfolioState` is *reconstructed* by one `Ledger.portfolio()` fold, never stored. The dual-ledger divergence is **structurally impossible** here (hermes spreads position truth across 26 files).
- **Fixture-leak impossible.** Storage location is injected (`StateConfig/state_dir`); a test process literally cannot reach live storage.
- **Validated fill seam.** `cli fill` refuses double-fills, off-ladder sizes, direction flips, and any size *above* the approved target. (hermes only rejects non-finite / >1.0.)
- **Governance hermes lacks:** `manifest.py` (SHA-256 of gate-config + CODE stamped as the first ledger event), `verify_ledger.py` (cross-module consistency — every fill traces to an approved proposal), `replay.py` (byte-identical), and a `PreToolUse` deny-hook that *platform-enforces* no-execution. The port itself surfaced three latent hermes P0 money bugs.

**What it does *not* have yet (per `PARITY.md`):** the numeric calibrated BMA (uses in-session committee rules; numeric deferred), Kronos/FoundationModelAnalyst, options (Greeks gate, multi-leg), HMM regime, the evidence-snapshot store, pre-trade admissibility. And a permanent, deliberate scope contraction: **no execution surface ever** (stricter than hermes), no continuous-presence features (microstructure, real-time social, intraday). It is also still an untracked working dir — not git-init'd, its 212-test count not yet reproduced in CI.

## C. The new architecture — one shared `pdr-core`, two shells (ADR-0092)

**The insight:** hermes and cowork are **parallel plugins for two different agent hosts**, same charter, different integration semantics. The fragmentation comes from each independently re-deriving — and re-breaking — the money-bearing core. The fix is to stop doing that twice.

**Extract one host-agnostic `pdr-core`** that owns 100% of money-adjacent state + arithmetic + the gate + the contracts:
- **State:** cowork's hash-chained single-fold ledger, promoted (kills the dual-ledger + fixture-leak bug class for *both* hosts).
- **Decision:** the gate ported verbatim from hermes (already a clean leaf) + hermes's numeric calibrated BMA (the charter's interpretability story needs it; cowork only spec'd it).
- **Settlement + calibration, governance** (manifest, verify_ledger, replay, eval/promotion gate, kill-switch), all in the core.

**hermes and cowork become thin shells** that do only host-native work:
- **hermes shell:** Python analyst classes, data providers, MCP, cron/daemon cadence, reactor backends (paper now; gated-live stubbed + inert behind a never-enabled flag — the B48 question stays deferred).
- **cowork shell:** in-session subagent committee, scheduled `/watch` turns, `/commands`, the deny-hook.

**The seam that makes it work:** the charter's `AnalystView` contract. The same uniform schema that makes the aggregator blind to *which analyst* produced a view also makes the core blind to *which host* produced it. A Hermes Python analyst and a Cowork subagent both emit `AnalystView` — the core cannot tell them apart, and that is the point. The litmus test: **touches money-state or money-arithmetic → core; how a host produces views or routes a proposal → shell.**

**The flow (identical live / autonomous / backtest):**
`SCAN → SENSE → build_perception_frame (once) → ANALYZE → [optional LLM committee, evidence-only] → FUSE (BMA) → GATE (final) → propose → [shell: HITL or inert-stub] → append Fill → settle → reflect.`

### How the three map

| Concern | hermes today | cowork today | new `pdr-core` |
|---|---|---|---|
| Host | Hermes agent | Claude Cowork | host-agnostic core + 2 shells |
| Money-state | 26 files, 2 divergent folds | 1 hash-chained ledger, 1 fold | cowork's ledger, promoted |
| Aggregator | numeric calibrated BMA | in-session committee rules | hermes's numeric BMA (canonical) |
| Risk gate | clean leaf, but bypassed by 2/4 paths | 8-rule, single chokepoint | gate ported verbatim, one node on every path |
| Analysts | Python classes | in-session subagents | both, via one `AnalystView` contract |
| Execution | paper + latent gated-live seed | none ever (HITL only) | shell decision; core emits authorized Proposal only |
| Governance | promotion gate orphaned | manifest+verify+replay+deny-hook | cowork's governance, promoted |
| Scale | ~82K LoC / 255 modules | ~3.9K core / 19 modules | core small; shells thin; leaves ported incrementally |

### Migration (strangler-fig, ledger-first)
Increment 0 (correctness core, ADR-0091 Option E) → freeze contracts + port leaves → hermes shell adopts core → cowork shell adopts core (git-init + CI) → orchestration spine + deploy lineage → cross-store atomicity → **run the charter's never-run proof** (3-analyst beats buy-and-hold risk-adjusted on paper). Each step default-OFF, eval-gated, no dark window. Details in the plan.
