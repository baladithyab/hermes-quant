# Research: HKUDS/Vibe-Trading — PDR-relevant architecture & multi-source fusion

Date: 2026-05-30
Source: deepwiki `HKUDS/Vibe-Trading` (5 grounded queries). Cross-checked against the
hermes-quant PDR north-star (Perception→Decision→Reaction) and rails (silence-by-default,
deterministic risk gate is final authority, require_ensemble, asof honesty).

> Caveat for hermes-quant: Vibe-Trading is an **LLM-agent research/backtesting** system. The
> LLM is the *executor* of analysis (ReAct loop drives tool calls), which is the OPPOSITE of
> hermes-quant's "LLM=evidence, execution=deterministic" rail. Adopt its **data-grounding and
> citation discipline** and its **artifact/hypothesis ledger contracts** — do NOT adopt its
> agent-loop-as-decision-maker topology. It explicitly does NOT place/execute live trades.

---

## 1. Pipeline stages (data → action)

Five named stages: **Plan → Ground → Execute → Validate → Deliver** (no live ACT stage).

| Stage | Consumes | Emits |
|---|---|---|
| **Plan** | user NL prompt | structured plan: selected skills, tools, data sources, swarm preset (`AgentLoop` + `ToolRegistry`) |
| **Ground** | plan + extracted symbols | `data_map` of OHLCV DataFrames per symbol; data-router fallback chain picks provider (`_fetch_auto`) |
| **Execute** | `data_map` + strategy logic | equity series, trade records, positions. `SignalEngine` makes signals → `BaseEngine.run_backtest` does bar-by-bar |
| **Validate** | equity + trades | metrics (`calc_metrics`), benchmark, Monte-Carlo / Bootstrap / Walk-Forward, a `run_card.json/md` (config + strategy hash + data-source provenance + validation) |
| **Deliver** | metrics + run card + artifacts | HTML/PDF reports, CSV/JSON/PNG artifacts, exportable strategy code (TradingView/TDX/MT5) |

PDR mapping for hermes-quant: Plan+Ground ≈ **Perception (SCANNING+ANALYSIS)**; Execute+Validate ≈
**Decision (DELIBERATION+RISKING)**; Deliver ≈ a reporting fold of **Reaction**. Vibe-Trading has
no equivalent of the hermes-quant deterministic risk gate / reactor — that is hermes-quant's own.

## 2. Data Grounding Block + Citation HARD RULE (the part hermes-quant wants)

**Grounding Block** = a structural pre-reasoning splice. Before the LLM worker reasons, the system:
(1) scans `user_vars` for data-source-suffixed symbols (`NVDA.US`, `700.HK`); (2) fetches last
`DEFAULT_WINDOW_DAYS` (=30) of OHLCV via `backtest.loaders.registry.resolve_loader`
(`fetch_grounding_data`); (3) renders a compact markdown block spliced into the worker prompt by
`build_worker_prompt`, placed **before** the "Execution Rules" so the worker sees real prices when
planning its first tool call. Purpose: kill price hallucination from training data.

**Data Citation Discipline (HARD RULE)** — applied *unconditionally to every agent*, including
synthesis/aggregator agents that hold no data tools. Every specific number (price/%/volume) MUST
trace to one of: (a) a tool-call result **in the current run**, (b) the Ground-Truth block, or
(c) Upstream Context that itself sourced from (a)/(b). Citing from memory/training is forbidden;
an unbacked number must be re-fetched or omitted+qualified.

**Verification location & enforcement** — happens in **Validate**, enforced by `GoalStore` via the
`_verification_status` static method. When `append_evidence` runs, evidence is marked **verified**
only if it has a local artifact path with a **matching SHA256 hash** OR a valid `run_id`. A bare
`tool_call_id` is NOT sufficient. **Uncited/unverified ⇒ goal cannot complete**: criterion audit
fails with "every criterion needs verified run/artifact evidence"; goal flips to `blocked` /
`insufficient_evidence`. This is the cite-or-die gate hermes-quant should mirror.

## 3. Multi-modality fusion

Modalities are **separate skills/agents** (77-skill library across 8 categories: data-source,
technical, factor/quant, fundamental, macro/commodity, crypto/DeFi, options/FI, strategy). They are
composed by the **Swarm = multi-agent DAG** (`SwarmRuntime._execute_run`): tasks are sorted into
topological layers, each layer runs in parallel, and **`SwarmTask.input_from` passes
`upstream_summaries` as the next agent's context** — that is the fusion step (summary-passing, not a
numeric weighted aggregator like hermes-quant's BMA). Social/sentiment enters as a preset team
(`social_alpha_team`: Twitter/Reddit/public-opinion) and is bound by the same Citation HARD RULE.
Notably there is **no hermes-quant-style weighted/peer aggregation with a haircut and
require_ensemble** — fusion is qualitative prompt-chaining, so the rigor lives entirely in citation,
not in the combiner.

## 4. Hypothesis Registry / Shadow Account / Alpha Zoo → PDR stage

- **Hypothesis Registry** (Decision/Perception-bridge ledger): stores `title, thesis, universe,
  data_sources, skills, status, invalidation_notes`. Lifecycle `exploring → testing →
  validated|rejected → monitoring`. API `create/update/link_backtest/search_hypotheses`; CLI
  `list/show/invalidate`. **`link_backtest` ties a hypothesis to its evidence run** — durable
  proposal→validation/rejection audit trail. (A "Security Scanner" attaches `security_warnings` and
  does deterministic OHLCV feature eval on shadow/strategy content.)
- **Shadow Account** (Reaction-diagnostics, paper/behavioral): parse broker CSV → profile behavior
  (holding period, win rate, disposition effect, chasing, overtrading, anchoring) → extract 3-5
  if-then rules → `run_shadow_backtest` (delta-PnL vs realized) → HTML/PDF report + **today's
  matching signals** (symbols matching the shadow's entry cadence). Tools: `analyze_trade_journal,
  extract_shadow_strategy, run_shadow_backtest, render_shadow_report, scan_shadow_signals`.
- **Alpha Zoo / Factor Research** (Perception/Analysis): 452 pre-built alphas (`qlib158` 154,
  `alpha101` 101, `gtja191` 191, `academic` 6), each with formula-LaTeX, theme, universe, warmup,
  required columns + a **`decay_horizon`**. Bench → cross-sectional IC/IR + alive/reversed/**dead**
  categorization; compose via `ZooSignalEngine.from_zoo(...)`.

## 5. Trend detection / data convergence / information-edge timing

- **Trend/momentum**: present only as *factors* (e.g. `academic_carhart_mom` = 12m−1m return; several
  GTJA momentum factors) and Shadow features (`momentum`, `price_above_ma`, plus "chasing-momentum"
  bias detection). **No trend-VELOCITY / week-over-week acceleration primitive** (the Camillo DETECT
  signal hermes-quant lacks — confirmed missing here too).
- **Data convergence / cross-source**: it fetches across markets with a fallback chain, and the
  Citation rule forces provenance, but there is **NO cross-source corroboration *requirement* before
  a signal is accepted**. Convergence-as-require_ensemble-at-perception is hermes-quant-original.
- **Edge decay / saturation**: only implicit, via per-factor **`decay_horizon`** (signal-relevance
  lifespan) + PIT data for lookahead honesty. **No model of "Wall Street catches up" / investor
  saturation / information-parity exit.** The Camillo EXIT-on-parity concept is absent.

---

## TL;DR — what hermes-quant should ADOPT (≤180 words)

- **Pre-reasoning Grounding Block + Citation HARD RULE.** Splice fetched ground-truth (with `asof`)
  into every analyst/LLM prompt BEFORE reasoning, and forbid any number not traceable to a
  current-run tool call, the ground-truth block, or cited upstream. This operationalizes
  hermes-quant's lookahead-honesty rail at the prompt layer and starves hallucinated prices.
- **Cite-or-die verification gate (`GoalStore._verification_status`).** Evidence counts only with a
  matching **SHA256 artifact hash or valid `run_id`** — a tool-call-id alone is not enough; uncited
  criteria block completion. Mirror this as a deterministic admissibility check feeding the risk gate
  (evidence that can't cite → silenced, never sized).
- **Hypothesis Registry with `link_backtest` + status lifecycle** (exploring→testing→validated/
  rejected→monitoring, with `invalidation_notes`). A durable proposal→evidence→verdict ledger that
  fits hermes-quant's eval-gating-before-live-influence discipline.
- **Do NOT adopt** the LLM-agent-as-decision-maker / qualitative summary-passing fusion (violates
  the deterministic-authority rail), and note Vibe-Trading also LACKS trend-velocity, cross-source
  convergence-as-requirement, and information-parity-exit — so those remain hermes-quant-original.
