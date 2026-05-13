# Changelog

All notable changes to hermes-quant.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
