# Backlog Resolution — Commit State & Triage (PHASE 1)

**Date:** 2026-06-13
**Branch:** `docs/rearchitecture-shared-pdr-core`
**Driver:** tiered deep-work mega-workflow over the open-seed backlog.

## Commit state at start of this run

Six commits landed in the prior session (this branch, not pushed):

| SHA | Commit |
|---|---|
| `ed790fe` | docs(adr): land ADR-0091 investigation paper trail |
| `dc697e5` | docs(adr): add ADR-0092 shared host-agnostic PDR core |
| `5a75bdd` | chore(seeds): file 12 rearchitecture seeds (ra00–ra11) |
| `18b0e5d` | docs(adr): resolve ADR-0091 to Option E (carry-forward fold) |
| `0a62e79` | docs(plan): scope Increment 0 (correctness core) |
| `3d12a57` | docs(arch): operator debrief — 3 architectures |

Working tree: `.gitignore` (pre-existing, not ours) + `cowork-quant/` (deliberately uncommitted — gets its own repo). Baseline: **4238 tests collected** (`pytest --collect-only`).

## Backlog triage — 35 open seeds → 3 classes

The defining distinction (money-software): an autonomous agent can write code behind a flag, but **cannot flip live `.env` flags or register crons in the Hermes `cron.db`** — those are operator actions on a live box. "Backlog to zero" therefore means: CODE items → implement-behind-flag + commit; OPERATOR items → produce the exact command + eval evidence, mark operator-blocked; DEFERRED items → re-deferred *with the recorded eval-gated justification* (which already exists in ADRs).

### CODE (18) — agent-actionable behind flags
`ra00`–`ra11` (the ADR-0092 rearchitecture epic + children), `pe04` (pre-existing e2e test failure), `b67a` (consumer-trend haircut, data-gated), `243d` (B48 react.live fallback), `5a63` (MCP manifest pins), `cap2` (playbook aggregate-cap positions), `pr3338` (stranded-PR clean-room). Plus re-bucketed: `8188` (mint kill_switch_clear CLI verb — code), `0fc0` (Alpaca account-toolset leak — manifest code), `2f01` (IC_DEDUP wiring — code), `8db9` (amend ADR-0062 — docs).

### OPERATOR (≈10) — produce command + evidence, cannot self-resolve
`ba90` (flip CATALYST_ONBOARDING), `8b01` (register profitability cron), `afa4` (flip GRAPH_MINING + register), `71ef` (deploy + register calibrator-drift cron), `6bb9` (promote PORTFOLIO_CAPS+SLIPPAGE default-ON), `58e9`/`e18b` (enable Alpaca MCP), `9048` (go-live + deploy-sync + cron-registry destale).

### DEFERRED (5) — re-deferred with existing justification
`335e` (settlement deferred-exit drain — keystone, but gated on the Increment-0 ledger work), `d9d8` (2-week ADR freeze — governance), `817b` (full-universe load test — v0.9+), `79f5` (Alpha Zoo / RL — DO_NOT_BUILD), `4d37` (ADR-0083 intraday — gated on measured edge).

## Execution shape

Tiered mega-workflow (Template B): Frame (Fable solo) → Discover (Opus dives + Sonnet sweeps) → Research (hyperresearch, conditional) → Plan (Fable solo) → Act↔Review bounded loop (Opus fleet + Sonnet reconcile + Opus review panel) → Ship (commit-only, on shipReady). A concurrent critique team runs in parallel to surface new backlog items. All money-path work ships default-OFF behind `HERMES_QUANT_*` flags; live flag-flips remain operator gates.
