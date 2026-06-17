# hermes-quant / cowork-quant — hierarchical architecture diagrams

**Status:** living reference (regenerate when a layer changes)
**Date:** 2026-06-17
**Scope:** the whole system, drawn top-down from the system context to component
level. Covers the *current* shipped pieces (hermes-quant package; cowork-quant
v0.1) and the *target* shape they are converging on — the shared `pdr-core` +
two-shell design ratified by ADR-0092.

> **The one property to hold in mind:** authority is concentrated at one
> immutable choke point per concern. **Decision** authority = the deterministic
> risk gate (ADR-0004). **State** authority = one append-only hash-chained
> ledger (ADR-0085). **Learning** authority = a human sign-off (the advisory
> plane proposes, never ships, ADR-0080). The LLM runs strictly *upstream* as
> evidence that can only subtract; it is never an order authority. Paper /
> advisory only — silence by default.

Diagrams are Mermaid; they render on GitHub and in most markdown viewers. Read
them in order — each level zooms into the box above it.

---

## What the project is, in three sentences

`hermes-quant` is a mature multi-analyst Perception → Decision → Reaction (PDR)
trading framework distributed as a **Hermes Agent** plugin (~250 Python modules,
92 ADRs, paper/backtest/HITL only). `cowork-quant` is its lean sibling that
rebuilds the same charter for **Claude Cowork**: Claude runs the analyst
committee *in-session* and a small deterministic Python package
(`scripts/quantcore/`) owns everything money-adjacent. ADR-0092 is the
convergence target: extract the money-bearing math into one host-agnostic
**`pdr-core`** and let both hosts be thin shells over it.

---

## Level 0 — System context

The whole system: one shared core, two host shells, the human, and read-only
data. Both shells speak the same contract to the core (`AnalystView` in,
`Proposal` out, `Fill` back). The core cannot tell which host — or which kind of
analyst — produced a view, and that is the point.

```mermaid
flowchart TB
    HUMAN(["Human operator<br/>approves · executes in own broker · confirms fills"])
    DATA[("Market data (read-only)<br/>yfinance · ccxt · CoinGecko · SEC EDGAR · broker MCP")]

    subgraph CORE["pdr-core — host-agnostic money spine · ADR-0092"]
        DEC["DECISION<br/>calibrated BMA → deterministic GATE (FINAL)<br/>quarter-Kelly · discrete ladder"]
        STATE["STATE<br/>one hash-chained ledger<br/>portfolio reconstructed · settle · calibrate"]
        GOV["GOVERNANCE<br/>manifest digest · verify_ledger<br/>kill-switch · eval gate · byte-identical replay"]
        DEC --- STATE --- GOV
    end

    subgraph HQ["hermes-quant shell — Hermes Agent plugin"]
        HQP["PERCEPTION<br/>Analyst Protocol classes<br/>data providers · MCP bridge"]
        HQR["REACTION<br/>cron / daemon ticks<br/>paper reactor · live STUBBED + INERT"]
    end

    subgraph CW["cowork-quant shell — Claude Cowork plugin"]
        CWP["PERCEPTION<br/>in-session committee<br/>skills + bull / bear / risk-skeptic subagents"]
        CWR["REACTION<br/>scheduled /watch turns<br/>HITL only · PreToolUse deny-hook"]
    end

    DATA --> HQP
    DATA --> CWP
    HQP -->|"AnalystView[]"| DEC
    CWP -->|"AnalystView[]"| DEC
    DEC -->|"authorized Proposal"| HQR
    DEC -->|"authorized Proposal"| CWR
    HQR -->|"Fill"| STATE
    CWR -->|"Fill"| STATE
    CWR -.->|"proposal card"| HUMAN
    HUMAN -.->|"approve + manual fill"| CWR
```

The **structural substitution** between shells: hermes-quant's continuous
`daemon → signal bus → consumer` becomes cowork-quant's discrete
`scheduled turn → ledger queue → interactive session`. Because the charter is
already interday-only (ADR-0083), the discrete cadence costs almost nothing
theoretically — and it makes "no autonomous execution" a platform-enforced
invariant rather than a promise.

---

## Level 1 — The PDR paradigm and the canonical flow

Every firing path (live, autonomous, backtest) runs the same single pipeline.
Backtest only swaps a replay data provider; nothing else changes.

```mermaid
flowchart LR
    A["SCAN / SENSE<br/>build_perception_frame(symbol, asof)"] --> B["ANALYZE<br/>each source → AnalystView"]
    B --> C["LLM committee (optional, bounded)<br/>evidence — may only subtract"]
    C --> D["FUSE<br/>numeric calibrated BMA → CommitteeSignal"]
    D --> E["GATE<br/>deterministic · FINAL → sized / capped Action"]
    E --> F["PROPOSE"]
    F --> G["REACT<br/>HITL approve OR inert-stubbed execute"]
    G --> H["append Fill → ledger"]
    H --> I["SETTLE<br/>exit↔entry join → realized horizon return"]
    I --> J["REFLECT / calibrate"]
    J -.->|"updates per-analyst BMA weights"| D
    E -.->|"disagreement · low margin · stale → silence"| K(["no proposal"])
```

### The contract chain (the frozen boundary)

Decisions move through a chain of immutable typed objects — never prose, never a
shared mutable dict. On any validation failure the behavior is **ABSTAIN**, never
retry-into-compliance.

```mermaid
flowchart LR
    PF["PerceptionFrame"] --> AV["AnalystView[]"]
    AV --> CS["CommitteeSignal"]
    CS --> GD["GateDecision / Action"]
    GD --> PR["Proposal"]
    PR --> FL["Fill"]
    FL --> SR["settle record"]
    SR -.->|"per-analyst calibration"| AV
```

**Authority monotonicity** (the defining invariant): evidence can only *subtract*
upstream of the gate; the gate is the sole admit-and-size authority; nothing
downstream can re-introduce a silenced signal or size above the gate's envelope.

---

## Level 2 — Subsystem decomposition

### 2a. `pdr-core` — the money-bearing spine

The host-agnostic package (`hermes_quant/pdr_core/`, extraction in progress).
Pure stdlib + frozen dataclasses so it stays trivially movable to a standalone
repo; it imports no host or governance module.

```mermaid
flowchart TB
    subgraph CORE["pdr-core (hermes_quant/pdr_core/)"]
        CONTR["contracts.py<br/>AnalystView · Proposal · Fill (frozen, validated)"]
        AGG["aggregate.py<br/>calibration-weighted Beta-binomial BMA"]
        SNAP["portfolio_snapshot.py<br/>reconstructed PortfolioState (never stored)"]
        GT["gate_types.py<br/>GateDecision · Action · verdicts"]
        GATE["gate.py<br/>deterministic risk gate · FINAL authority"]
        KELLY["kelly.py<br/>quarter-Kelly → discrete ladder"]
    end
    CONTR --> AGG
    AGG -->|"CommitteeSignal"| GATE
    SNAP -->|"current book"| GATE
    GT --> GATE
    GATE --> KELLY
    KELLY -->|"Proposal on ladder"| CONTR
```

### 2b. hermes-quant shell — subsystems mapped to PDR planes

The large package (~250 modules) organized by the plane each directory serves.
Everything money-adjacent (gate, kelly, ledger, settlement) is what ADR-0092
pulls down into `pdr-core`; the rest stays shell.

```mermaid
flowchart TB
    subgraph PERC["PERCEPTION"]
        D1["data/ — providers (yfinance · ccxt · alpaca · alphavantage), chain, mcp_bridge, vendor_routing"]
        P1["perception/ — frame · builder · convergence · saturation · velocity"]
        CAT["catalyst/ — ingest · classify · calendar · social · propagation"]
        REG["regime/ — detector · hmm · per_regime_weights · state_variables"]
        UNI["universe/ — alpaca_scanner · point_in_time"]
    end
    subgraph DECI["DECISION"]
        AN["analysts/ — classical_ta · fundamentals · kronos · microstructure · overnight_drift · semantic"]
        AGT["agents/ — research_debate (bull/bear) · risk_committee · trader · llm_caller · structured_output"]
        AGGS["aggregators/ — bma · deliberative · llm_committee"]
        RG["risk/ + admissibility/ + pdr_core/ — gate (FINAL) · kelly · options_gate · open_guard"]
    end
    subgraph REAC["REACTION"]
        RX["react/ — paper · live (gated) · multileg; backends: alpaca · deterministic"]
        DM["daemon/ — discovery · settlement_loop · signal_bus · halt_state · tick_lock"]
        OPT["options/ — greeks · pricing · multileg · structure_select · recipes"]
    end
    subgraph MEG["MEMORY · EVAL · GOVERNANCE (cross-cutting)"]
        GVN["governance/ — approvals · audit_log · kill_switch · invariants · promotion"]
        MEM["memory/ — reflector · retriever · decisions · weekly_retro · meta_retro"]
        EVx["eval/ + evaluation/ + backtest/ — promotion_gate · stockbench · cv · dsr · walk_forward · ablation"]
        EVI["evidence/ — store · schema · lookahead_gate"]
        LRN["learning/ + factors/ — posterior_refit · calibration · alpha_zoo · factor_oracle"]
    end
    PERC --> DECI --> REAC
    MEG -.->|"weights · gates · provenance"| DECI
    REAC -.->|"fills · outcomes"| MEG
```

### 2c. cowork-quant shell — plugin anatomy

Claude orchestrates LLM subagents in-session; the deterministic `quantcore`
package does all money math behind a JSON CLI; hooks make no-execution a platform
rule; state lives in the user's workspace folder.

```mermaid
flowchart TB
    subgraph PLUGIN["cowork-quant plugin (Claude Cowork)"]
        CMD["commands/<br/>/brief /scan /propose /settle /status<br/>/doctor /watch /retro /schedule /dashboard"]
        SK["skills/<br/>quant-core · analysts"]
        AGENTS["agents/<br/>bull-analyst · bear-analyst · risk-skeptic"]
        HOOKS["hooks/<br/>deny_execution.py · hooks.json (PreToolUse)"]
        ASSET["assets/<br/>dashboard_template.html (self-contained, no CDN)"]
        subgraph QC["scripts/quantcore/ — deterministic core (ported math)"]
            QGATE["gate.py · kelly.py · aggregate.py"]
            QLED["ledger.py · settle.py · verify_ledger.py"]
            QGOV["manifest.py · replay.py · mask.py · evalx.py"]
            QSUP["schemas.py · config.py · regime.py · hypotheses.py · calendar_events.py · exec_guard.py"]
        end
    end
    CLI["quantcore.cli — JSON in / JSON out<br/>gate · propose · decide · fill · mark · settle · status · verify · expire · resume"]
    STATE[("workspace/quant-state/<br/>ledger.jsonl · briefs · retros · dashboard.html")]

    CMD -->|"Claude orchestrates"| AGENTS
    CMD --> SK
    CMD -->|"shells out"| CLI
    AGENTS -->|"AnalystView JSON"| CLI
    CLI --> QC
    QC --> STATE
    HOOKS -.->|"deny any order / transfer tool"| CMD
```

---

## Level 3 — Component level

### 3a. The deterministic gate (decision authority)

The gate is the single choke point. It consumes the fused signal, the
reconstructed book, costs, and `asof`, then walks an ordered rule stack — each
rule can only *silence, halt, or shrink*, never amplify. Sizing is the last step
and always snaps to the discrete ladder. (Exact rule order and thresholds live in
`gate.py` / ADR-0004; the stack below is representative.)

```mermaid
flowchart TB
    IN["CommitteeSignal + PortfolioState + MarketCosts + asof"] --> R0
    R0["stale / still-forming bar → ABSTAIN"] --> R1
    R1["disagreement / low margin → SILENCE"] --> R2
    R2["cost gate: edge < costs → SILENCE"] --> R3
    R3["event blackout (calendar proximity) → SILENCE"] --> R4
    R4["circuit breakers / drawdown → FLATTEN + HALT"] --> R5
    R5["per-position & portfolio caps (Rule 6.5)"] --> R6
    R6["quarter-Kelly sizing on calibrated edge"] --> R7
    R7["snap to discrete ladder<br/>0 / +/-.05 / +/-.10 / +/-.15 / +/-.20"] --> OUT
    OUT(["Proposal (sized, capped) — OR silence"])
```

### 3b. The contract triad (frozen dataclasses)

The exact seam between shell and core (`pdr_core/contracts.py`). Construction
*rejects* off-ladder sizes, bool/str/NaN injections, and lookahead — the
anti-leverage-gambling and no-lookahead invariants are arithmetic, enforced at
the boundary. (`CommitteeSignal` fields are representative of the fused output.)

```mermaid
classDiagram
    class AnalystView {
      str analyst
      str asset
      str asset_class
      int direction
      float magnitude
      float confidence
      float confidence_raw
      str horizon
      ts asof_decision
      ts bar_ts
      tuple evidence_ids
    }
    class CommitteeSignal {
      int direction
      float magnitude
      float confidence
      float agreement
      int effective_n
      list dissent
      bool event_risk
      str regime
    }
    class Proposal {
      str symbol
      str asset_class
      float target_position_pct
      str gate_reason
      ts asof
    }
    class Fill {
      str proposal_id
      str asset
      float fill_price
      float fill_size_pct
      ts asof_execution
      str schema_version
    }
    AnalystView "1..*" --> CommitteeSignal : aggregate BMA
    CommitteeSignal --> Proposal : gate + kelly
    Proposal --> Fill : shell executes HITL
    Fill --> AnalystView : settle then calibrate
```

Key field semantics: `direction` is in {-1, 0, +1}; `confidence` is a *calibrated*
P(direction correct); `bar_ts <= asof_decision` is the no-lookahead anchor;
`target_position_pct` must sit on the ladder; `fill_size_pct` is the **absolute**
post-fill target (Option E, ADR-0091) — the fold derives the traded delta once.

### 3c. Governance & honesty plane (v0.2 controls)

State integrity and "honest numbers, not just honest machinery." Verification
runs on every load; honesty controls gate any number used in a config decision.

```mermaid
flowchart TB
    subgraph ENTRY["every turn / CLI entry / SessionStart"]
        VL["verify_ledger — recompute hash chain head→tail<br/>any break → HALT + ABSTAIN (R11)"]
        MAN["manifest digest — SHA-256(config + code)<br/>written as first ledger event"]
    end
    subgraph HONEST["honesty controls"]
        MASK["mask.py — leakage-masked eval<br/>alias tickers + relative dates (R9)"]
        ATTR["attribution — alpha after market/style<br/>selection alpha = headline metric (R10)"]
        CAL["calibration — Brier-skill + ECE-with-CI<br/>negative-skill analyst → BMA weight ~0"]
    end
    subgraph CHECK["correctness & robustness checks"]
        EVAL["eval gate — CPCV + deflated Sharpe + PBO"]
        REPLAY["replay — byte-identical decision from stored inputs"]
        DRILL["adversarial drills — TradeTrap perturbations"]
        SHADOW["shadow ledger — counterfactual what-if book"]
    end
    HOOK["PreToolUse deny-hook — blocks any order/transfer tool (R12)"]
    KS["kill-switch — OUTSIDE and ABOVE the gate"]
    HOOK -.-> KS
    MAN --> EVAL
    VL --> REPLAY
```

### 3d. cowork-quant command → CLI verb surface

How a user-facing slash command maps to the deterministic CLI. Commands are
prompt-orchestration; the CLI is the only thing that touches state.

| Command | quantcore CLI verb(s) | What it does |
|---|---|---|
| `/brief` | `status` | Daily PDR brief: regime, watchlist deltas, open book, event proximity |
| `/scan` | (committee only) | Run analysts on a ticker/watchlist → `AnalystView`s, no proposal |
| `/propose <ticker>` | `gate` → `propose` → `decide` | Full PDR turn: committee → gate → sized proposal → HITL approval |
| `/settle` | `mark`, `settle`, `fill` | Settle horizon-expired positions; record fills; update calibration |
| `/status` | `status`, `verify` | Show book, NAV, pending proposals, halts, calibration |
| `/watch` | `settle` → `mark` → `gate` → `propose` (+`expire`) | Unattended turn: queue gate-approved proposals only — **no approvals, no fills** |
| `/retro` | `settle` + reports | Weekly/quarterly retrospective; advisory-only config proposals |
| `/doctor` | `verify` | Environment + data-path + ledger-integrity health check |
| `/schedule` | — | Set up the autonomous cadence (Cowork scheduled tasks) |
| `/dashboard` | `status` | Render static (or live-artifact) HTML view of state |
| (human-only) | `resume` | Clear a circuit-breaker halt — requires explicit human confirmation |

### 3e. The rails (the bounds nothing in the loop may cross)

The eight v0.1 rails, plus four v0.2 additions promoted by the June-2026
literature. These are invariants, not preferences.

| # | Rail |
|---|---|
| 1 | Silence by default — disagreement or stale data → no proposal |
| 2 | Hard rules over LLM judgment — the gate's output is final |
| 3 | Discrete sizing ladder, enforced at config, gate, proposal *and* fill seams |
| 4 | No order execution, ever (cowork-quant; stricter than hermes-quant) |
| 5 | Asof-honesty — decision-time stamped; no still-forming bars |
| 6 | Structured output only — Pydantic/dataclass-validated before the ledger |
| 7 | Append-only hash-chained ledgers with integrity verification |
| 8 | New capabilities default-OFF until measured (risk-tightening may default ON) |
| R9 | Honesty over history — forward-only is the gold standard; replay is a masked diagnostic |
| R10 | Attribution before applause — credit *selection alpha*, not raw return |
| R11 | Verify state on every load — a hash-chain break halts and abstains |
| R12 | Platform-enforced no-execution — PreToolUse deny-hook, not just intent |

---

## Current state vs target (where the boxes really are today)

- **hermes-quant** — shipped and mature (v0.6.4 alpha): analysts, aggregators,
  deterministic gate, daemon/cron, paper reactors, governance, memory, eval,
  backtest, options, catalyst. Paper/HITL only; live execution stubbed + inert.
- **cowork-quant** — v0.1 shipped (168 tests): committee → gate → hash-chained
  ledger → HITL → dashboard. v0.2 is **designed, not yet coded** (Waves 4–7:
  ledger verifier, leakage-masked eval, manifest+replay, platform rails, then
  committee-v2 weighting, screener, attribution, then drills/evidence-store, then
  flag-gated options / foundation-model analyst / shadow ledger / quarterly retro).
- **pdr-core** — the convergence target (ADR-0092). `hermes_quant/pdr_core/`
  holds the frozen contract triad and gate/kelly/aggregate leaves; the migration
  that makes it the *sole* money spine for both shells is in progress.

So Levels 0–1 describe the **target** unified shape; Level 2b is **today's**
hermes-quant; Level 2c is **today's** cowork-quant v0.1 with the v0.2 components
(screener, mask, manifest, replay, shadow, attribution) drawn where they will sit.
```

