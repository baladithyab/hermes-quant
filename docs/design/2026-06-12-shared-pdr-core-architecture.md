# Shared PDR Core + Two Integration Shells — Target Architecture

**Status:** design (ratified by ADR-0092, proposed)
**Date:** 2026-06-12
**Companion:** `docs/adr/ADR-0092-shared-pdr-core-two-integration-shells.md` (the decision), `docs/plans/2026-06-12-shared-pdr-core-rearchitecture.md` (the migration)
**Grounded in:** two adversarial assessment workflows (2026-06-12, 81 agents), the charter, `cowork-quant/docs/PARITY.md`, ADR-0079 (PDR model), ADR-0091 (open ledger decision this design consumes).

---

## 1. One paragraph

A single PDR-native trading system whose money-bearing spine is one small, host-agnostic Python package — `pdr-core` — that owns 100% of money-adjacent state and arithmetic. The LLM runs entirely upstream as evidence. Two thin plugins integrate the core into two different agent hosts: **hermes-quant** (Hermes agent: Python analyst classes, crons/daemon, MCP) and **cowork-quant** (Claude Cowork: in-session subagent committee, scheduled `/watch` turns). Both produce the same uniform `AnalystView` contract and feed `Fill`s back; the core returns one authorized, sized, capped `Proposal`. The core is blind to both the *modality* of a signal and the *host* that produced it.

## 2. The shape

```
                  ┌──────────────────────────────────────────────────┐
   AnalystView[]  │   pdr-core   (shared · host-agnostic ·            │
   (one uniform   │              owns ALL money-adjacent state)       │
    contract, ───▶│   DECISION:  aggregate → numeric calibrated BMA   │──▶ authorized
    any host,     │              GATE (deterministic, ADR-0004, FINAL)│    Proposal
    any modality) │              kelly · ladder {0,±.05,±.10,±.15,±.20}│   (sized, capped)
                  │   ──────────────────────────────────────────────  │
   Fill        ──▶│   STATE:     ONE hash-chained ledger              │
   (host writes   │              ONE reconstruct (Ledger.portfolio())  │
    back) ────────│              settlement · calibration             │
                  │   GOVERNANCE: manifest(SHA-256 config+CODE)        │
                  │              verify_ledger · kill-switch · eval gate│
                  │              replay (byte-identical)               │
                  └───────▲──────────────────────────────────▲─────────┘
                          │ AnalystView[] in / Proposal out   │
          ┌───────────────┴────────┐            ┌─────────────┴───────────────┐
          │  hermes-quant           │            │  cowork-quant                │
          │  (Hermes agent plugin)  │            │  (Claude Cowork plugin)      │
          │                         │            │                              │
          │  PERCEPTION:            │            │  PERCEPTION:                 │
          │   Analyst Protocol      │            │   in-session committee       │
          │   classes (TA, micro,   │            │   (skills/ + bull/bear/      │
          │   Kronos, semantic,     │            │    risk-skeptic subagents)   │
          │   fundamentals)         │            │   → AnalystView JSON         │
          │   data providers, MCP   │            │                              │
          │  REACTION:              │            │  REACTION:                   │
          │   cron/daemon ticks     │            │   scheduled /watch turns     │
          │   reactor backends:     │            │   HITL only · deny-hook      │
          │    paper now;           │            │   no execution ever (rail#4) │
          │    gated-live STUBBED   │            │   /commands                  │
          │    + INERT (B48 defer)  │            │                              │
          └─────────────────────────┘            └──────────────────────────────┘
```

## 3. The five core responsibilities (what moves into `pdr-core`)

| Concern | Source of the clean version | What it kills |
|---|---|---|
| **State** — one append-only hash-chained JSONL ledger; `PortfolioState` always *reconstructed*, never stored | cowork `ledger.py` (promoted) | dual-ledger divergence (one fold, not 26 files); fixture-leak (`state_dir` injected) |
| **Gate** — deterministic risk gate, FINAL authority, ported verbatim | hermes `risk/gate.py` (already a clean leaf: imports only `protocol`+`kelly`) | nothing — it's correct; it just needs to be the *only* gate node on every path |
| **Aggregate** — numeric calibration-weighted Beta-binomial BMA | hermes `bma.py` math (the charter's interpretability story needs the numeric calibrated aggregator; cowork only spec'd it) | the "which spine's aggregator?" ambiguity (open question Q5) |
| **Settlement + calibration** — exit↔entry fill join → realized horizon return → per-analyst calibration | hermes settlement math + cowork's `settle.py` raw-move-vs-realized-P&L correctness fix (R1-01) | the short-calibration inversion; the BUILT-BUT-NOT-WIRED settlement seed (335e) |
| **Governance** — manifest (SHA-256 of config+CODE stamped as first ledger event), `verify_ledger` cross-module consistency, kill-switch OUTSIDE+ABOVE the gate, eval/promotion gate, replay | cowork `manifest.py`/`verify_ledger.py`/`replay.py` (hermes lacks these) | unprovable promotion logs; orphaned promotion gate (zero callers today) |

## 4. The contracts (the frozen boundary)

A chain of frozen typed objects, never prose, never a shared mutable god-dict. Validated at every host/core boundary; on validation failure the behavior is **ABSTAIN** (silence-by-default), never retry-into-compliance.

```
PerceptionFrame ─▶ AnalystView[] ─▶ CommitteeSignal ─▶ GateDecision/Action ─▶ Proposal ─▶ Fill ─▶ settle record
```

**`AnalystView` — the host-blind, modality-blind seam** (the charter's "uniform output schema so the aggregator doesn't care which analyst is which"):

```
analyst         source id (a Python class name OR a Claude subagent role)
asset, asset_class
direction       {-1, 0, +1}
magnitude       [0,1]  expected |return| over horizon
confidence      [0,1]  CALIBRATED P(direction correct)
confidence_raw  [0,1]  pre-calibration (feeds the calibrator)
horizon
asof_decision   decision/publication time
bar_ts          last CLOSED bar — the no-lookahead anchor
rationale
evidence_ids    tuple of EvidenceRecord ids, threaded view → committee → settlement → audit
```

The *floor* is virattt/ai-hedge-fund's `{signal, confidence, reasoning}` — identical for LLM personas and pure-Python analysts through one registry. We keep the richer calibrated contract; their 3-field shape is the minimum a source may emit.

**Authority monotonicity (the defining invariant):** evidence can only *subtract* upstream of the gate; the gate is the sole admit+size authority; nothing downstream of the gate can re-introduce a silenced signal or pick above the envelope. The LLM committee runs strictly upstream as BMA evidence — it can silence (confidence → 0) but never amplify, and there is no post-gate LLM selector.

## 5. What stays in each shell (and why it must NOT move to core)

**hermes-quant shell:** the Analyst Protocol *classes* (TA/microstructure/Kronos/semantic/fundamentals — these are leaves the core consumes via the contract), the data-provider layer + MCP wiring, the cron/daemon firing cadence, and the reactor backends. The gated-live-execution seam stays here, **stubbed and inert behind a never-enabled flag** (B48 deferred — the core never knows whether a host auto-executes).

**cowork-quant shell:** the in-session committee (skills + bull/bear/risk-skeptic subagents that emit `AnalystView` JSON), the scheduled `/watch` turn cadence, the `/commands`, and the `PreToolUse` deny-hook that platform-enforces no-execution.

The litmus test for "core vs shell": **if it touches money-state or money-arithmetic, it is core; if it is how a particular host produces views or routes an authorized proposal, it is shell.** A Hermes Python analyst and a Cowork subagent both emit `AnalystView` — the core cannot tell them apart, and that is the point.

## 6. The single flow (identical live / autonomous / backtest)

```
SCAN → SENSE → build_perception_frame(symbol, asof)  [ONCE]
     → ANALYZE (each source → AnalystView)
     → [optional bounded LLM committee — evidence that may only subtract]
     → FUSE (numeric calibrated BMA → CommitteeSignal)
     → GATE (deterministic, FINAL → sized/capped Action)
     → propose → [shell: HITL approve OR inert-stubbed execute] → append Fill to ledger
     → settle → reflect
```

Backtest swaps in a replay provider; **nothing else changes** (the production-replay invariant). One orchestration entry point — `build_perception_frame → recommend → gate → react` — that every firing path must call, killing the "wired on 1 of N paths" bug class.

## 7. Open questions this design forces (resolved in the plan / by the operator)

1. **Ledger fold semantics** — deferred to ADR-0091's open decision (A/B/**C**). The core consumes the resolution; it does not pre-empt it.
2. **Live execution** — operator chose *defer*. Core emits authorized Proposals; hermes shell stubs an inert execution seam; cowork shell is HITL-only. Decide after the correctness core is trustworthy.
3. **BTC-first vs equities** — the charter mandates the BTC/USDT proof first; the system's center of gravity has migrated to equities. The proof gate (Increment 6) must pick one.
4. **Numeric BMA vs in-session committee aggregation** — the core's canonical aggregator is the numeric calibrated BMA (charter interpretability depends on it); cowork's in-session rules become a *shell* evidence producer, not the aggregator. Cold-start calibration bootstrap must be specified.
5. **Two `require_ensemble` layers must not be merged** — cross-SOURCE convergence at perception ("is the trend real?") and cross-ANALYST agreement at decision (BMA n_distinct ≥ 2) are complementary; a lean rewrite is the most likely place to accidentally collapse them.
6. **Packaging/versioning** — monorepo with `pdr-core` as a path dependency, or separate repo published and pinned? (The plan recommends starting in-repo and extracting once contracts are frozen.)
