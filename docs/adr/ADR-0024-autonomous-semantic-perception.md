# ADR-0024: Autonomous semantic perception artifacts

- **Status:** Accepted
- **Date:** 2026-05-14
- **Related:** ADR-0021 PDR recipes, ADR-0022 semantic perception, ADR-0023 deliberative committee

## Context

Hermes can read and synthesize market context that quantitative analyzers cannot
see directly: news, filings, forum summaries, operator notes, prior decisions,
and cross-market narratives. To make that useful inside hermes-quant, semantic
perception must become autonomous without compromising money-software safety.

The unsafe design would call an LLM from inside each trading tick. That breaks
replayability, creates provider availability risk in the hot path, and makes
backtests impossible to reproduce.

## Decision

Autonomous semantic perception is an **artifact pipeline**, not a live-tick model
call.

1. Hermes cron/research jobs generate semantic packets and committee turns.
2. Packets/turns are written as hashed JSON files under `~/.hermes/quant/`.
3. PDR recipes consume those artifacts through `MarketContext.extras`.
4. Advisor/backtest/signal artifacts persist packet hashes and committee hashes.
5. Invalid/stale/missing artifacts degrade to abstain/flat.

The plugin therefore ships CLI surfaces for artifact write/validate/list and a
cron installer that creates a Hermes job prompt. The job prompt tells Hermes to
research sources and then call the CLI writer. Cron can be disabled or edited by
the operator using normal Hermes cron commands.

## Artifact paths

- Semantic packets:
  `~/.hermes/quant/semantic_packets/<asset-key>/<packet_hash>.json`
- Committee turns:
  `~/.hermes/quant/committee_turns/<asset-key>/<turns_hash>.json`

Both are canonical JSON with atomic rename writes.

## Safety rules

- Trading ticks never call models or the web directly.
- Cron-generated perception can be reviewed, validated, deleted, or replayed.
- Packet hashes and committee hashes are carried into advisor/backtest/signal
  artifacts so every decision can be traced to exact semantic inputs.
- Cron perception is opt-in; install defaults do not create jobs.
- Live trading remains blocked by existing reactor/risk-gate policies.

## Consequences

Positive:

- Hermes becomes an autonomous semantic sensor while preserving replayability.
- Anyone can install the plugin and customize sources/prompts without editing
  Python code.
- Backtests can compare quantitative-only and semantic-augmented recipes.

Negative / deferred:

- The first cron installer emits a prompt to Hermes rather than owning a full
  source-ingestion subsystem.
- Source adapters (RSS, X, filings, Telegram channels, etc.) remain future
  plugin/recipe extensions.
- Model mixtures remain artifact-driven; live provider orchestration should be
  introduced only with prompt/version/model hashing and replay tests.
