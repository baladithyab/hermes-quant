# hermes-quant — architecture map (2026-05-30)

Code-explorer pass for the deep-work backlog loop. ~51.5k LoC of Python across 179
`.py` files, 35 top-level packages, 75 ADRs (numbered to 0076; 0040 and 0046 are
gaps). The README/AGENTS.md describe the *founding* v0.1 vision (3 analysts,
daemon→JSONL→freqtrade); the **actual** code at HEAD is a far more evolved
v0.4.4 advisor/committee/cron system. Where they conflict, trust the code — this
doc cites `file:line`.

> **Posture reminder (AGENTS.md):** money-software. Silence-by-default; hard
> deterministic rules over learned policy; everything replayable from disk; the
> LLM is NEVER the final execution authority. Every gap below should be read
> through "a defect subtracts from a real bank account."

---

## 1. The pipeline, end to end

There are **two operator surfaces** (ADR-0013) and several orchestration entry
points layered on the same core. The canonical synchronous decision function is
`recommend()` in `hermes_quant/advisor.py:631`.

### 1a. The core advisor (perceive → decide → gate), `advisor.py:631`

```
recommend(symbol, asset_class, timeframe, as_of, market_extras, ...)
  │
  ├─[recipe]  optional PDR recipe selection (recipes.py:get_recipe) drives
  │           asset_class/timeframe/analysts/aggregator/gate         advisor.py:691-711
  │
  ├─[1 FETCH] provider.fetch_bars(sym, tf, start, end, as_of=asof)   advisor.py:766
  │           default = YFinanceProvider (equity/etf only;            advisor.py:285
  │           crypto/fx NotImplemented in advisor — ccxt only in daemon)
  │
  ├─[2 LOOKAHEAD] bars filtered to timestamp <= as_of (defense-in-depth;
  │           provider also filters)                                 advisor.py:810-817
  │
  ├─[bar-align] drop_still_forming_bar() — drops today's unsettled    advisor.py:830
  │           daily bar mid-session (ADR-0069)        data/bar_alignment.py
  │
  ├─[regime] build_regime_extras(symbol, bars) merged into ctx.extras advisor.py:861
  │           (ADR-0063; never raises)             regime/extras_builder.py
  │
  ├─[3 ctx]   MarketContext(asset,timeframe,bars,last_close,asof,extras) advisor.py:871
  │
  ├─[4 ANALYSTS] _build_default_analysts() → loadout (§2)            advisor.py:337
  │           each analyst.analyze(ctx) -> AnalystView | None         advisor.py:931
  │           one bad analyst can't kill the loop (try/except per analyst)
  │
  ├─[5 AGGREGATE] BMAAggregator().aggregate(views, ctx)              advisor.py:972
  │           -> AggregatedSignal                       aggregators/bma.py:305
  │
  ├─[6 RISK]  DefaultRiskGate().gate(signal, market, portfolio, halt) advisor.py:991
  │           -> Action | None (None = silence)              risk/gate.py
  │           NOTE: advisor builds a SYNTHETIC FLAT portfolio          advisor.py:128
  │           + bootstrap MarketState — NOT real broker state          advisor.py:1046
  │
  └─ returns dict: {analyst_views, aggregated_signal, risk_gate,
       lessons, caveats, decision_price, bar_ts, decision_wall_clock} advisor.py:1010-1026
```

The advisor is **read-only, synchronous, deterministic** (ADR-0014). It never
writes state, never updates calibrators, never places an order. `risk_gate.kelly_fraction`
in the result is the size daemon-mode *would* target on a clean slate.

### 1b. The autonomous tick (adds gate → react), `autonomous.py:255`

`tick(dry_run, symbols)` is the "Decide → Gate → React" wrapper used by cron:

```
autonomous.tick()                                          autonomous.py:255
  ├─ mode gate: refuses unless quant.pdr.mode==autonomous   autonomous.py:284
  ├─ kill-switch: refuses if tripped (cum P&L < threshold)   autonomous.py:296
  ├─ for each watchlist entry:
  │    advisor_recommend(symbol, ...)                        autonomous.py:354
  │    silence_bias_gate(advisor_result, cfg, lessons)       autonomous.py:385
  │        4 dims: voices≥2, confidence≥0.65, urgency≥0.5,   gates/silence_bias.py:128
  │        salience (≤3 recent rejections). ALL must pass → FIRE.
  │    if FIRE:
  │       per-tick cap (max_per_tick_opens, default 1)        autonomous.py:402
  │       [ADR-0071] portfolio-caps clip (gated OFF, §4)      autonomous.py:427
  │       if not dry_run: _react() → PaperReactor.execute()   autonomous.py:464/508
  └─ returns TickResult{decisions, fires, silences, errors}
```

`_react()` (`autonomous.py:508`) synthesizes a `Proposal` stand-in and calls
`PaperReactor().execute()` — **paper only**; live is type-system-gated (§4). It
bypasses the HITL pending-state; the execution record IS the audit trail.

### 1c. Live cron entry points (`ops/scripts/`)

The real "running system" is cron scripts, not a long-lived daemon:

- **`quant-autonomous-tick.py`** — wraps `autonomous.tick()`; every 30 min in
  market hours. `--dry-run` default; `--armed` for real paper fires. Halt
  fail-closed + per-symbol-per-day idempotency. Monkey-patches the mode reader
  so config.yaml stays clean (`quant-autonomous-tick.py:247`).
- **`quant-playbook-tick.py`** — the richest path. Daily `0 6 * * 1-5`. 5-play
  playbook (covered_call/csp/wheel/leaps/swing), but **only swing+leaps are
  wired** (`EQUITY_PLAYS`); option plays are filtered pending the multi-leg
  reactor. Optionally runs the **deliberative LLM committee** when
  `HERMES_QUANT_DELIBERATIVE=1` (`quant-playbook-tick.py:446,591,712`).
- **`quant-daily-interim.py`** — the **catalyst-aware** picker. This is the ONLY
  live path that loads semantic packets into `market_extras`
  (`quant-daily-interim.py:127-141` via `load_packets_for`). Read-only brief
  unless `HERMES_QUANT_AUTONOMY=paper`.
- **`quant-hourly-tick.py`** — market-hours health/perf/conditions check.
- **`quant-universe-scan.py`** — daily Alpaca premarket universe scan.
- **`quant-watchlist-evolve.py`** — per-play evolving-watchlist tick.
- **`quant-playbook-weekly.py` / `-quarterly.py`** — ADR-0035 rebalance/review.
- **`quant-bootstrap-calibrator.py`** — fit IsotonicCalibrator from Alpaca bars.
- Catalyst crons: **`quant-catalyst-ingest.py`** (GN-RSS → packets),
  **`quant-catalyst-eval-gate.py`** (D74.7 gate), **`quant-catalyst-coverage.py`**
  (graph↔universe coverage watchdog), **`quant-catalyst-profitability.py`**,
  **`quant-catalyst-socialarb-eval.py` / `-labels.py`** (ADR-0076).
- One-offs/research: `quant-amzn-weight-oos.py`, `quant-wave3-candidates.py`.

### Key data contracts (`hermes_quant/protocol.py`)

`MarketContext` (bars+last_close+asof+extras) → `AnalystView`
(direction∈{-1,0,1}, magnitude, confidence, confidence_raw, horizon) →
`AggregatedSignal` → `Action` (signed target_position_pct from the discrete
ladder {0,±0.05,±0.10,±0.15,±0.20}). Cross-process durable state lives under
`~/.hermes/quant/`: `signals.jsonl`, `ticks.db`, `state.json`, `autonomous-tick.jsonl`,
`catalyst/packets.jsonl`, `catalyst/propagation-log.jsonl`.

---

## 2. The analyst loadout

Built by `_build_default_analysts()` (`advisor.py:337`; an inline duplicate at
`advisor.py:884` for the legacy default path — **see gap G1**). Entry points are
declared in `pyproject.toml:94-98`.

| Analyst | Module | In default loadout? | Gating |
|---|---|---|---|
| `ClassicalTAAnalyst` | `analysts/classical_ta.py` | **Yes, always** | none |
| `MicrostructureLite` | `analysts/microstructure.py` | Yes (ImportError-soft) | none |
| `KronosAnalyst` | `analysts/kronos.py` | Yes if `kronos` extra installed; abstains (conf=0) otherwise | optional dep (torch/HF) |
| `FundamentalsAnalyst` | `analysts/fundamentals.py` | **No — OFF** | `HERMES_QUANT_FUNDAMENTALS_ENABLED=1` (default `0`), ADR-0064 |
| `HermesSemanticAnalyst` | `analysts/semantic.py` | **No — OFF** | `HERMES_QUANT_SEMANTIC_ENABLED=1` (default `0`), ADR-0074 |

Kronos also ships `KairosAnalyst` (BTC fine-tune) per AGENTS.md but it is not in
the default loadout. The semantic analyst is a packet consumer only — it never
calls a model/web inside `analyze()`; it reads `ctx.extras["semantic_packets"]`,
re-validates them against decision time, and emits a PEER `AnalystView`
(`analysts/semantic.py:75,147`). BMA's `require_ensemble` guard means a lone
semantic view cannot fire a trade (§3).

Abstain handling: views with `confidence < 0.10` (`ABSTAIN_THRESHOLD`,
`bma.py:128`) are dropped before aggregation, so an abstaining Kronos doesn't
count as a "voice" toward the silence-bias `min_analysts_emitted` gate.

### Every `HERMES_QUANT_*` feature flag (grep-verified)

Library flags (`hermes_quant/`), all default OFF/conservative unless noted:

| Flag | Default | Effect / ADR |
|---|---|---|
| `HERMES_QUANT_FUNDAMENTALS_ENABLED` | `0` | add FundamentalsAnalyst (ADR-0064) |
| `HERMES_QUANT_SEMANTIC_ENABLED` | `0` | add HermesSemanticAnalyst (catalyst, ADR-0074) |
| `HERMES_QUANT_PORTFOLIO_CAPS` | unset(=off) | portfolio-aware Kelly clip in tick (ADR-0071) `autonomous.py:335` |
| `HERMES_QUANT_TRADER_LLM` | `0` | LLM TraderNode v0.2 (ADR-0054) `agents/trader.py:478` |
| `HERMES_QUANT_RISK_COMMITTEE_LLM` | (off) | LLM 3-way risk committee (ADR-0056) |
| `HERMES_QUANT_RESEARCH_DEBATE` | `0` | bull/bear debate stage (ADR-0065/66) `llm_committee.py:977` |
| `HERMES_QUANT_RESEARCH_DEBATE_ROUNDS` | — | debate round count |
| `HERMES_QUANT_RISK_ROUNDS` | — | risk-committee round count |
| `HERMES_QUANT_REFLECTOR_LLM` | `0` | LLM-wired reflection (ADR-0057) `memory/reflector.py:554` |
| `HERMES_QUANT_REFLECTION` | `0` | enable paper-reflection hook `react/paper.py:205` |
| `HERMES_QUANT_MEMORY_INJECT` | `0` | inject memory into committee `llm_committee.py:296` |
| `HERMES_QUANT_REGIME_HMM` | (off) | HMM regime classifier v0.2 (ADR-0058) `regime/detector.py:101` |
| `HERMES_QUANT_ANALYSTS_USE_REGIME` | — | regime-aware confidence multiplier |
| `HERMES_QUANT_HORIZONS` | — | multi-timeframe fan-out opt-in (ADR-0036, "Wave C", deferred) |
| `HERMES_QUANT_IC_DEDUP_THRESHOLD` | `0.99` | IC dedup gate cutoff `factors/ic_dedup.py:44` |
| `HERMES_QUANT_OPEN_GUARD` | — | intraday open-guard dedup (ADR-0072) |
| `HERMES_QUANT_PAPER_SLIPPAGE_MODEL` | `v0.1` | paper fill model (ADR-0070) `react/paper.py:100` |
| `HERMES_QUANT_PAPER_INITIAL_CASH` | — | paper starting NAV |
| `HERMES_QUANT_WATERMARK_ENABLED` | — | tick watermark dedup |
| `HERMES_QUANT_SNAPSHOT_V2` | `0` | bar-snapshot schema v2 `schemas/bar_snapshot.py:385` |
| `HERMES_QUANT_KNOWLEDGE_CUTOFF` | "" | stockbench knowledge cutoff `eval/stockbench.py:47` |
| `HERMES_QUANT_EVIDENCE_DIR` / `_JOURNAL_PATH` / `_ALPHA_ZOO_DIR` | — | path overrides |
| `HERMES_QUANT_PREWARM_WORKERS` | — | prewarm parallelism `playbook/scorers.py:692` |

Cron-only flags (`ops/scripts/`): `HERMES_QUANT_DELIBERATIVE` (+ `_RISK`,
`_PROMOTE`, `_QUICK_MODEL`, `_DEEP_MODEL`), `HERMES_QUANT_AUTONOMY`,
`HERMES_QUANT_AUTONOMOUS(_ARMED)`, `HERMES_QUANT_PLAYBOOK_DRY_RUN`,
`HERMES_QUANT_PLAYBOOK_TICK_MOCK`, `HERMES_QUANT_LOG_LEVEL`.

**The headline fact:** nearly every advanced surface (LLM committee, trader,
risk committee, reflector, semantic/catalyst, fundamentals, HMM regime, portfolio
caps, research debate) is **default-OFF behind an env flag**. The shipped live
default is the 3-analyst numerical ensemble → BMA → deterministic gate →
silence-bias → paper. This is deliberate (silence-by-default), but it also means
much of the 51k LoC is dormant in the default path.

---

## 3. The catalyst / semantic subsystem (ADR-0074/0075/0076)

`hermes_quant/catalyst/` (1,598 LoC) is "Catalyst Sense": a producer pipeline
parallel to the universe scan that turns news/social into `SemanticPacket`s the
existing `HermesSemanticAnalyst` consumes. Five stages:

```
[1 INGEST]            [2 CLASSIFY]        [3 CORRELATE]        [4 SYNTHESIZE]     [5 EMIT]
ingest.py            classify.py         propagation.py       synthesize.py      synthesize.py
GN-RSS (stdlib       keyword/regex       entity→sector→       per-symbol packet  append-only JSONL
urllib+xml, no       severity+polarity   symbol "butterfly"   asof=PUB TIME      packets.jsonl
paid API)            cascade             curated signed graph confidence=linkage  +propagation-log
social.py: Reddit                        (noisy-OR × agreement) magnitude=severity (learned-graph corpus)
+ Google Trends
   │                                           │                     │                  │
   └──── CatalystItem(title, published_at=PUB TIME) ────────────────┘                  ▼
                                                                      load_packets_for(sym, asof)
   DETERMINISTIC NUMERICAL ──► BMA AGGREGATOR ◄── PROBABILISTIC SEMANTIC ◄── market_extras["semantic_packets"]
   (ClassicalTA+Micro+Kronos)        │            (HermesSemanticAnalyst, PEER view)
                                      ▼
                       risk gate (0004) + silence-bias + caps (0071) + open-guard (0072)
```

**How fusion works (D74.1):** semantic enters BMA as one more `AnalystView`,
weighted by track-record reliability like any analyst. A high-confidence semantic
stance that *disagrees* with the numerical analysts *reduces* aggregate
confidence rather than overriding. BMA's `require_ensemble` + `n_distinct_analysts
>= 2` guard (`bma.py:498-519`) means **semantic alone cannot fire** — it needs a
numerical corroborator or it's silenced as single-source.

**Lookahead discipline (D74.4, the load-bearing rule):** every `CatalystItem` and
packet carries `asof = publication time`, never wall-clock-now
(`synthesize.py:112`, `social.py:23`). `load_packets_for()` re-runs
`validate_semantic_packet` against the query `asof`
(`synthesize.py:207`); the analyst re-validates again against *decision* time
(`semantic.py:164` — uses `ctx.extras["decision_asof"]` when present so a packet
published today isn't rejected against yesterday's daily-bar close, falling back
to `ctx.asof` for backtests). Defense-in-depth, three validation points.

**The propagation graph / butterfly engine (D74.2, `propagation.py`):** an
operator-curated YAML (built-in seed `_BUILTIN_GRAPH:71`) of signed, weighted
edges keyed by canonical source entity. `effect_sign` is defined for a *negative*
catalyst on the source; a positive catalyst flips it (`propagate():353-356`).
Confidence = noisy-OR linkage × directional-agreement (`propagation.py:377-379`),
so near-cancelling opposing edges don't emit high confidence.
`confidence ← linkage score`, `magnitude ← classify severity` — never conflated
(D74.3). Covered sectors: space/launch, semis/supply-chain, aero/airlines, EV,
banks, and a consumer-trend/social-arb class. **The OPEC/energy edge was removed**
(`propagation.py:96-103`) because the severity classifier extracts polarity, not
supply direction, so it mis-signed XOM/CVX — the sign-consistency eval caught it.

**Social arbitrage (ADR-0076, `social.py` + the `brand_self` relation class):**
Reddit + Google Trends producers emit the same `CatalystItem` shape. The
consumer-trend class only cleared the D74.7 gate at 0.60 hit-rate on n=5, so its
packet confidence is haircut to 0.5 (`CONSUMER_TREND_CONFIDENCE_HAIRCUT`,
`synthesize.py:53-66`) — it enters BMA as a deliberately *weak* peer view.
`profitability.py` is the live-feedback loop: it joins the append-only
propagation log against realized forward returns by relation class
(`MIN_SAMPLE=20`, `MIN_HIT_RATE=0.6`) and recommends raising/lowering the haircut.

**The eval gate (D74.7, `eval.py`):** three axes that must all pass before
semantic influences live decisions — (1) negative control (benign headlines → 0
packets), (2) directional precision vs real forward returns ≥0.6 hit-rate, (3)
sign-consistency (every sector's edge propagates the defensible stance under a
known polarity, market-data-free). `eval_gate()` (`eval.py:197`) is the combined
runner, driven by `quant-catalyst-eval-gate.py`.

**Wiring status:** ingest/classify/correlate/synthesize/eval are all built. The
analyst is wired into `_build_default_analysts()` behind
`HERMES_QUANT_SEMANTIC_ENABLED`. The packet→advisor coupling is live in exactly
ONE script — `quant-daily-interim.py:127-141`. **The autonomous tick and playbook
tick do NOT load packets into `market_extras`** (gap G3).

---

## 4. Scaffolded-but-headless / gated-OFF modules

Ordered by how close they are to the live path.

| Module | State | What it would take to activate |
|---|---|---|
| **Semantic / Catalyst Sense** (`catalyst/`, `analysts/semantic.py`) | Fully built; OFF by env flag; wired into only one cron | Pass D74.7 eval gate on a real labeled set → flip `HERMES_QUANT_SEMANTIC_ENABLED=1` → wire `load_packets_for` into `autonomous.tick`/playbook-tick (G3). |
| **Multi-leg options reactor** (ADR-0029) | **Does NOT exist.** `react/live.py` is an inert type-gated stub; `options/` has only `greeks.py` + `pricing/gbs.py` — no `occ.py`, no `MultiLegReactor`. PaperReactor is equity-only. | Build `options/occ.py` (OCC-21 format/parse), the multi-leg `Proposal` shape, atomic HITL approval, broker `mleg` routing, next-day NTA settlement reconciliation. Until then covered_call/csp/wheel plays are filtered out of playbook-tick (`EQUITY_PLAYS`). |
| **PMCC shadow tracker** (`shadow/pmcc.py`) | Built as a *counterfactual* for the missing reactor — marks a 2-leg PMCC to Black-Scholes model. Writes nothing to executions; pure shadow. | Activates implicitly once the multi-leg reactor lands (it's the validation harness). |
| **Shadow account** (`shadow/account.py`, `rules.py`, `runner.py`, ADR-0049) | Built; 5 counterfactual rules, isolated SQLite per rule, read-only vs audit log. Invoked from `quant-daily-interim.py`. | Largely active in the interim cron; verify it's scheduled in production cron and surfaced in the daily report. |
| **LLM deliberative committee** (`aggregators/deliberative.py`, `llm_committee.py`, `agents/`) | Built; OFF. Only runs when `HERMES_QUANT_DELIBERATIVE=1` in playbook-tick. The default `run_committee_from_packets` is a *deterministic* synthesis (`committee_runner.py:130`), not an LLM. | Flip `HERMES_QUANT_DELIBERATIVE=1` (+ model env vars); needs an LLM caller (`agents/llm_caller.py`) configured. |
| **LLM Trader / Risk-committee / Reflector v0.2** (ADR-0054/0056/0057) | Built; OFF behind `HERMES_QUANT_TRADER_LLM` / `_RISK_COMMITTEE_LLM` / `_REFLECTOR_LLM`. | Configure LLM caller + flip flags; gated on the rollout playbook (ADR-0062). |
| **Live trading** (`react/live.py`, `governance/promotion.py`) | Type-system-gated: `LiveBroker.submit_mleg_order` doesn't exist without a `LiveTradingApproval`, which can only be constructed by passing every ADR-0029-D7 threshold (≥100 paper outcomes, Sharpe 95%CI lower ≥1.0, ≤1% DD, no kill-switch 14d, 0 immutable breaches, human promoter). | Accumulate the paper track record + run the promotion orchestrator (`eval/promotion_orchestrator.py`). Intentionally hard. |
| **HMM regime classifier v0.2** (`regime/hmm.py`, ADR-0058) | Built; OFF behind `HERMES_QUANT_REGIME_HMM`. Default regime path is the deterministic detector. | Flip flag; verify calibration. |
| **Portfolio-aware Kelly caps** (ADR-0071, `risk/portfolio_normalize.py`) | Built; wired into `autonomous.tick` but OFF (`HERMES_QUANT_PORTFOLIO_CAPS` unset). | Flip env var after reviewing one tick log. |
| **Multi-timeframe fan-out** (`recommend_multi_horizon`, ADR-0036) | Built and BMA supports cross-horizon weighting, but `recommend()` (the live path) is still single-horizon; `HERMES_QUANT_HORIZONS` wire-up is "Wave C, deferred" (`advisor.py:656`). | Wire the env var into the daily tick. |
| **Stacking / RL aggregator** (ADR-0003/0006) | Stacking entry point declared (`pyproject.toml:102`) but `aggregators/stacking.py` is **absent from disk** — only `bma.py`, `deliberative.py`, `llm_committee.py` exist. RL deferred to graduation criteria. | Implement stacking; RL gated on DSR p<0.05 + ≥12 walk-forward folds + shuffle test (ADR-0006). |

---

## 5. Test posture

- **153 test files, ~2,323 `test_` functions** under `tests/` (mirrors the package
  layout: `agents/`, `analysts/`, `backtest/`, `catalyst` cases under `unit/`,
  `factors/`, `governance/`, `regime/`, `shadow/`, `evidence/`, `grounding/`,
  `cron/`, `scripts/`, etc.).
- **Run:** `pytest tests/ -q` (or `-n auto` for parallel via pytest-xdist).
  `asyncio_mode = "auto"`, `DeprecationWarning` filtered
  (`pyproject.toml:152-159`).
- **No-lookahead CI gate** (`tests/test_no_lookahead.py`, ADR-0006 release
  blocker): 4 invariants — (1) analyst view at T identical with/without future
  bars present, (2) provider honors `as_of`, (3) advisor deterministic under
  `as_of` replay, (4) `shuffle_timestamps_test` from `evaluation/lookahead.py`
  proves analysts use temporal structure, not bar-position. Currently
  parametrized over ClassicalTA + MicrostructureLite only (Kronos/semantic not
  in this gate — see gap G2). There's a second lookahead layer in
  `evidence/lookahead_gate.py` + `factors/lookahead_sentinel.py` (ADR-0050/0051)
  for the alpha-zoo / factor library.
- **Integration tests gate live providers:** `tests/integration/` (alpaca paper,
  yfinance) skipped by default; markers `requires_network` and `timeout(N)`
  declared. Run explicitly before release.
- **Known-flaky quarantine:** there is **no centralized quarantine file**.
  Flakiness is handled ad hoc — `test_no_lookahead.py:339` deliberately asserts
  only structural fields (not pass/fail) because the small-`n_shuffles` p-value is
  flaky; a handful of tests (`test_bma_abstain_filter.py`, `test_kronos_analyst.py`,
  `test_prewarm_snapshot_cache.py`, `test_fundamentals_ablation.py`,
  `test_backtest_replay.py`) mention flaky/skip handling inline. This is a gap
  (G4): no `pytest.ini` marker for quarantine, no nightly re-run of quarantined
  tests.
- Property-based testing with `hypothesis` is prescribed in AGENTS.md for the risk
  gate invariants but is not a declared dev dependency in `pyproject.toml:69-76`
  (only pytest stack + ruff + mypy) — verify it's actually used (gap G5).

---

## 6. Biggest architectural gaps (from the code, not the docs)

- **G1 — Duplicated analyst-loadout logic.** `_build_default_analysts()`
  (`advisor.py:337`) and the inline default in `recommend()` (`advisor.py:884-928`)
  construct the loadout twice with copy-pasted env-flag checks. They can drift; a
  new analyst or flag must be added in both places. Consolidate to one call site.

- **G2 — The no-lookahead CI gate doesn't cover all shipped analysts.** AGENTS.md
  claims the shuffle test runs "against every shipped analyst and aggregator," but
  `test_no_lookahead.py` only parametrizes ClassicalTA + MicrostructureLite. Kronos,
  Fundamentals, and the Semantic analyst are not in the gate. The semantic analyst
  is the highest-lookahead-risk surface (it ingests dated news) and its honesty
  rests entirely on `validate_semantic_packet` + the decision-time logic
  (`semantic.py:164`) — that decision-time branch is not exercised by the
  release-blocker gate.

- **G3 — Catalyst packets reach only one of three live decision paths.** The
  semantic analyst is enabled globally by one env flag, but only
  `quant-daily-interim.py` actually loads packets into `market_extras`. If an
  operator flips `HERMES_QUANT_SEMANTIC_ENABLED=1` expecting the autonomous tick
  or playbook tick to react to catalysts, the analyst silently abstains
  (`no_semantic_packets`) because those paths never populate
  `ctx.extras["semantic_packets"]`. The flag and the wiring are decoupled.

- **G4 — No flaky-test quarantine system.** For money-software with a
  release-blocking lookahead gate, there's no marker/CI mechanism to quarantine
  and separately re-run flaky tests; flakiness is suppressed by weakening
  assertions (`test_no_lookahead.py:339`), which erodes the very invariant the
  test exists to protect.

- **G5 — Declared-but-absent components.** `aggregators/stacking.py` is referenced
  by an entry point (`pyproject.toml:102`) and ADR-0003 but **does not exist on
  disk**; importing the `stacking` aggregator entry point would fail. Similarly
  `hypothesis` is prescribed but not a dependency. Entry-point/ADR claims have
  drifted from the filesystem.

- **G6 — Synthetic-portfolio risk evaluation in the advisor.** The advisor gates
  against a *synthetic flat 100k portfolio* (`advisor.py:128,987`), so its
  `kelly_fraction` ignores real concentration/exposure. The autonomous tick layers
  ADR-0071 portfolio caps on top — but those are OFF by default
  (`HERMES_QUANT_PORTFOLIO_CAPS` unset). In the default config, position sizing is
  effectively portfolio-blind, which for real capital is the riskiest single gap.

- **G7 — Edge-sign is hand-curated and unproven at scale.** The propagation graph's
  `effect_sign` is the self-described "highest-risk modeling choice"
  (`propagation.py:60`). The OPEC removal shows the risk is real and already bit
  the system. The learned-graph corpus (`propagation-log.jsonl`) is being
  accumulated but no learned model consumes it yet — the moat is logged, not built.

- **G8 — README/AGENTS.md are ~70 ADRs stale.** They describe a daemon→freqtrade
  v0.1 system; the live system is cron-script-driven advisor/committee v0.4.4 with
  no long-running daemon in the default path (`daemon/main.py` exists but the cron
  scripts are what run). An onboarding agent reading only the top-level docs would
  build a wrong mental model. The ADRs are the source of truth; the prose docs lag.

---

### Quick file index for the backlog loop

- Core decision: `hermes_quant/advisor.py:631` (`recommend`), `:446`
  (`recommend_multi_horizon`)
- Orchestration: `hermes_quant/autonomous.py:255` (`tick`), `:508` (`_react`)
- Aggregation: `hermes_quant/aggregators/bma.py:305` (`aggregate`), `:498`
  (single-source guard)
- Gates: `hermes_quant/gates/silence_bias.py:128`, `hermes_quant/risk/gate.py`
- Catalyst: `catalyst/{ingest,classify,propagation,synthesize,eval,profitability,social}.py`
- Headless: `react/live.py`, `shadow/pmcc.py`, `options/` (no occ/reactor)
- Cron: `ops/scripts/quant-*.py`
- Lookahead gate: `tests/test_no_lookahead.py`
