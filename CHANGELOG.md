# Changelog

All notable changes to hermes-quant.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] — 2026-05-13

### Added — first executable vertical slice

This is the **first runnable release**. v0.1.0 was scaffold-only (8 ADRs +
Protocol contracts); v0.1.1 makes the daemon actually run with a working
slice from `tick_loop` → analysts → BMA aggregator → risk gate → JSONL bus
→ freqtrade consumer strategy.

**Implementation (14 commits in three waves + Phase-9 Phase-8-driven fixes):**

Wave 1 — risk + signal-bus primitives:
- `risk/kelly.py` — Kelly numerator using `(2p-1)·m` exact log form, NOT
  buggy `p·m`. Single source of truth for cost gate AND Kelly sizer.
  Synthesis-v2 §P0-A.
- `daemon/signal_bus.py` — JSONL bus with `fcntl.flock(LOCK_EX)` atomicity.
  Multi-process concurrency tested with 4 concurrent writers + exact line-
  count assertion (no corruption). Synthesis-v2 §P0-B.
- `daemon/halt_state.py` — SQLite halt registry with WILDCARD `'*'` sentinels
  NOT NULL, WITHOUT ROWID, PK including halt_epoch. Atomic-rename JSON
  mirror for fast strategy reads. Synthesis-v2 §P1-β.
- `daemon/heartbeat.py` — Bootstrap-grace dead-man-switch. Distinguishes
  bootstrap-active (alive) from bootstrap-expired no-heartbeat (dead).
  Synthesis-v2 §P0-C.
- `daemon/slippage.py` — Side-aware adverse-bps estimator. Buys use
  `(fill-decision)/decision`, sells use `(decision-fill)/decision`; only
  positive (adverse) values persist. Synthesis-v2 §P1-ζ.

Wave 2 — analysts + aggregators + risk gate impl:
- `data/{base,yfinance_provider}.py` — DataProvider Protocol + yfinance
  with validation gates + chain fallback. **`yf_retry()` exponential
  backoff** added in Phase-9e (TradingAgents-inspired): 3 attempts × {2s,
  4s} budget, transparent recovery from 429s.
- `analysts/classical_ta.py` — RSI(14) + EMA(20)/EMA(50)/SMA(200) +
  Donchian(20) + range expansion ensemble. Constant-price RSI returns 50.0
  not 100.0.
- `calibrators.py` — Identity, ColdStart shrinkage, IsotonicRegression.
- `aggregators/bma.py` — Beta-binomial posterior weights with agreement
  bonus, no-decay (decay deferred to v0.2 per Phase-8 P2 finding).
- `risk/gate.py::DefaultRiskGate` — 8-rule sequence: halt → drawdown →
  daily-loss → flat/zero-confidence → cooldown → cost-gate → Kelly-sizing
  → min-trade-size. Phase-8 P0-B alignment guard added.

Wave 3 — daemon orchestration + freqtrade consumer:
- `daemon/{lock,discovery,portfolio_loader,tick_loop,settlement_loop,main}.py`
- `cli/halts.py` — `halt`, `resume`, `emergency-stop` with synthesis-v2
  §P0-D ordering: durable SQLite halt FIRST, then bus signal emit, then
  broker-cancel intent.
- `consumers/freqtrade/quant_consumer_strategy.py` — IStrategy
  implementation reading signals.jsonl, writing executions.jsonl with
  matching flock protocol.

**Phase-8 cross-family review fixes (the Phase-9 commits):**
- 3 reviewers via curl-bypass: claude-4.7-opus / gemini-3.1-pro-preview /
  deepseek-v4-pro. Cost: $0.71 / 4 minutes wall-clock. Verdict: 1 MERGE,
  2 BLOCK on overlapping calibration-loop concerns.
- All P0/P1 verified against ground truth (Hard Rule #14): zero false
  positives.
- **P0-A.1 + P0-A.2** — daemon persists `decision_price` on bus record;
  freqtrade strategy reads it from cache. Eliminated `decision_price=
  fill_price` artifact that would zero out every realized_return.
- **P0-A.3 + P0-A.4** — settlement loop tags outcomes with
  `_calibration_quality = "slippage_only"`; dispatch_settlement skips
  analyst.update / aggregator.update for tagged outcomes. v0.1.2 will
  lift the gate when entry+exit fill joining lands. Prevents Beta
  posteriors from being corrupted by single-fill slippage data
  masquerading as horizon return.
- **P0-B** — risk gate edge-sign alignment guard. Negatively-edged signal
  in requested direction → silenced (was: passed cost gate, then Kelly
  sizer flipped the sign and emitted opposite-direction action).
- **P0-C** — tick loop installs durable halt to SQLite when gate emits
  `Action(halt=True)`. Was: halt only announced on bus, lost on restart
  + not visible to other assets in scope.
- **P1-α** — portfolio_loader gates direction-flip + partial-close paths
  with `NotImplementedError` until v0.1.2 rewrite. Was: silently corrupted
  equity / drawdown via wrong-sign realized PnL.
- **P1-γ** — strategy oversized-exec drops at ERROR with full
  signal_id/exec_id audit trail (was: silent WARNING).
- **P1-δ** — `_next_session_open` non-UTC tz returns `now + 24h` not
  `next-UTC-day midnight` (was: equity halt at 14:00 ET would auto-clear
  at 19:00 ET same day).

**Documentation:**
- `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md` — full Phase-8
  cross-family review with decision matrix, file:line citations,
  hallucination check.
- `docs/research/04-tradingagents-comparison.md` — TauricResearch/
  TradingAgents comparison: steal list (yf_retry ✓ shipped, settlement
  journal v0.1.2, PortfolioDecision schema v0.3.0) and explicit leave
  list (LangGraph orchestrator, bull/bear LLM debate, LLMs in action
  path — incompatible with money-software discipline).
- `docs/plans/v0.1.1-wave-plan.md` — three-wave implementation plan.

### Test coverage

273 unit + integration tests passing, 1 skipped (sklearn-bound
`requires_network` mark). Coverage of every Phase-8 finding includes a
named regression test.

### Known v0.1.1 limitations

- Calibrator updates GATED OFF until v0.1.2 (Phase-8 P0-A.3). Per-fill
  slippage formula is not horizon return; entry+exit fill joining lands
  in v0.1.2.
- Portfolio reconstruction GATES OFF partial closes + direction flips
  with `NotImplementedError` until v0.1.2 rewrite (Phase-8 P1-α).
- Heartbeat uses wall-clock time; vulnerable to NTP backstep / VM
  resume (Phase-8 P1-β, deferred to v0.1.2 monotonic-clock fix).
- HaltState mirror has small staleness window between SQLite commit and
  atomic-rename (Phase-8 P1-ε; freqtrade reads SQLite directly via
  HaltStateSQLite as workaround).
- v0.1.1 settles fills + computes slippage but does NOT yet update Beta
  posteriors (per P0-A.3 gate). Posteriors stay at uniform priors.

### Roadmap (updated 2026-05-13)

- **v0.1.2** — KronosAnalyst + KairosAnalyst with bootstrap calibration
  + Phase-8 follow-ups: entry+exit fill joining → lift calibrator gate;
  portfolio_loader rewrite with explicit case handling + 8 unit tests;
  monotonic-clock heartbeat; halt mirror staleness fallback;
  `~/.hermes/quant/journal.md` settlement journal (TradingAgents-inspired
  pending→resolved markdown pattern with atomic-rename, no embeddings);
  OHLCV file cache with parquet for backtest replay;
  `tests/test_no_lookahead.py` CI gate (was promised in AGENTS.md, never
  landed in v0.1.1); `trading_calendars` for proper session boundaries.
- **v0.1.3** — MicrostructureLite + ccxt provider + StackingAggregator
- **v0.2.0** — Alpaca equities + NautilusTrader v0.2 consumer + RL aggregator
  (graduation-gated per ADR-0006)
- **v0.3.0** — options support + news-LLM analyst + audit-logging.
  `LLMAnalyst` Protocol with `PortfolioDecision`-style structured output
  (TradingAgents-inspired 5-tier rating → discrete direction/confidence
  mapping). Dual-tier LLM config (`quant.llm.deep` / `quant.llm.quick`)
  per TradingAgents pattern.

## [0.1.0] — 2026-05-13

### Added — architecture & scaffold

This is a **scaffold release**. It ships the architecture (ADRs + cross-family
review) + the plugin surface (tools, slash command, CLI subcommand tree) + the
core protocol contracts. **The daemon is not yet implemented**; v0.1.1 will
ship the executable signal loop.

**Architecture (1715 lines of ADRs):**
- ADR-0001 sidecar architecture (daemon decoupled from gateway)
- ADR-0002 analyst protocol contract
- ADR-0003 aggregator design — Bayesian + stacking; RL deferred to v0.2
- ADR-0004 risk gate — deterministic, silence-by-default, ¼-Kelly with
  discrete action steps
- ADR-0005 data layer — yfinance / ccxt / alpaca with provider chains
- ADR-0006 RL aggregator deferred to v0.2 with concrete graduation criteria
- ADR-0007 plugin shape — tools = read-only views; CLI = control plane
- ADR-0008 freqtrade integration via signal bus (JSONL contract)
- ADR-0009 Phase-4 cross-family review amendments — 16 P0/P1 fixes

**Cross-family review:**
- Phase-4 v1: 3/3 reviewers BLOCKed. 28 distinct findings across red-team,
  architecture, and quant-correctness lenses. 5 strong-intersection P0s.
- Phase-4 v2: 1/3 LIFT (Gemini), 2/3 MAINTAIN (GPT-5.5, DeepSeek) on
  Kelly numerator math + JSONL atomicity. v2 fixes documented in
  `docs/reviews/2026-05-12-adr-bundle/synthesis-v2.md` and will land in
  v0.1.1 implementation.
- All cross-family routing verified via direct OpenRouter curl bypass —
  the Hermes `delegate_task` route-fidelity bug forced a curl-bypass
  dispatcher (per parallel-critique skill).

**Research (554 lines):**
- DeepSeek V4 Pro on RL for algorithmic trading SOTA + pitfalls
- Gemini 3.1 Pro Preview on framework integration — surfaced the sidecar
  architecture insight that LLM-based analysts break vectorized backtesters
- Plugin architecture & Kronos wrapping (orchestrator-authored fallback;
  Kimi K2.6 dispatch failed with 8579 reasoning tokens / empty content)

**Plugin scaffold:**
- `plugin.yaml` manifest with `optional_env` for ALPACA_API_KEY,
  BINANCE_API_KEY, HF_TOKEN, MLFLOW_TRACKING_URI
- `pyproject.toml` with optional extras: `[yfinance]`, `[ccxt]`, `[alpaca]`,
  `[kronos]`, `[stacking]`, `[backtest]`, `[mlflow]`, `[all]`
- Entry points for analysts, aggregators, data providers
- Console scripts: `hermes-quant-daemon`, `hermes-quant-trainer`

**Plugin surface (working in v0.1.0 — read-only):**
- Tools: `quant_status`, `quant_show_signals`, `quant_show_views`, `quant_doctor`
- Slash: `/quant {status|signals N|views ASSET|doctor}`
- CLI: full canonical surface (per ADR-0009 §P1-11). Status / signals /
  show-views / doctor / config-show are wired; lifecycle / backtest /
  freqtrade subcommands print "v0.1.0 scaffold — coming in v0.1.1" notice.
- Discord `/quant` deferred slash command via `pre_gateway_dispatch` hook
  (per references/plugin-authoring.md)
- Skill registration: `~/.hermes/plugins/hermes-quant/hermes_quant/skills/hermes-quant/SKILL.md`

**Protocol contracts (`hermes_quant/protocol.py`):**
- `MarketContext`, `AnalystView`, `AggregatedSignal`, `Action`
- `Portfolio` (mark-to-market, sourced from broker reality per ADR-0009 §P0-3)
- `MarketState`, `Position`, `HaltState`, `HaltRecord`
- `RealizedOutcome` + `EpisodeOutcome` (cross-sectional, per ADR-0009 §P1-10)
- `Calibrator` Protocol (isotonic + cold-start shrinkage)
- `Analyst`, `StatefulAnalyst`, `Aggregator`, `RiskGate`, `DataProvider` Protocols
- `confidence_raw` field for calibrator training (per ADR-0009 §P0-2)

### Known v0.1.0 limitations

- Daemon process is a placeholder. `hermes quant start` prints a notice.
- No analysts implemented yet. The discovery infrastructure is in place
  (entry-points), but no concrete classes ship in v0.1.0.
- No risk gate implementation yet — the algorithm is fully specified in
  ADR-0004 + ADR-0009 §P0-5 + v2 review fixes (docs/reviews/.../synthesis-v2.md),
  but the Python module is empty.
- Phase-4 v2 review caught 5 code-level fixes that must land in v0.1.1
  before the daemon goes live (see synthesis-v2.md):
  - Kelly numerator: `(2p-1)·m`, NOT `p·m`
  - JSONL atomicity via `flock()`, NOT PIPE_BUF citation
  - Heartbeat bootstrap hole + emergency-stop durability
  - SQLite halt PK NULL safety
  - Slippage adverse-bps side-aware computation

### Roadmap

- **v0.1.1** — daemon + ClassicalTAAnalyst + BMAAggregator + RiskGate
  + yfinance provider + freqtrade strategy (working paper-trade vertical slice)
- **v0.1.2** — KronosAnalyst + KairosAnalyst with bootstrap calibration
- **v0.1.3** — MicrostructureLite + ccxt provider + StackingAggregator
- **v0.2.0** — Alpaca equities + NautilusTrader v0.2 consumer + RL aggregator
  (graduation-gated per ADR-0006)
- **v0.3.0** — options support + news-LLM analyst + audit-logging
