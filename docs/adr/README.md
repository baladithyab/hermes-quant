# Architecture Decision Records

| # | Title | Status | Date |
|---|---|---|---|
| [ADR-0001](ADR-0001-sidecar-architecture.md) | Sidecar architecture — daemon decoupled from gateway |  |  |
| [ADR-0002](ADR-0002-analyst-protocol.md) | Analyst protocol contract |  |  |
| [ADR-0003](ADR-0003-aggregator.md) | Aggregator design — Bayesian baseline + logistic stacking, RL deferred |  |  |
| [ADR-0004](ADR-0004-risk-gate.md) | Risk gate — deterministic rules, silence-by-default |  |  |
| [ADR-0005](ADR-0005-data-layer.md) | Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities |  |  |
| [ADR-0006](ADR-0006-rl-aggregator-deferred.md) | RL aggregator deferred to v0.2 with concrete success criterion |  |  |
| [ADR-0007](ADR-0007-plugin-shape.md) | Plugin shape — Hermes plugin tools = read-only views; daemon owns the loop |  |  |
| [ADR-0008](ADR-0008-freqtrade-integration.md) | Freqtrade integration via signal bus (sidecar consumer) |  |  |
| [ADR-0009](ADR-0009-amendments-from-phase4-review.md) | Phase-4 cross-family review amendments to ADR-0001..0008 |  |  |
| [ADR-0010](ADR-0010-settlement-journal.md) | Settlement journal (markdown sidecar) |  |  |
| [ADR-0011](ADR-0011-portfolio-reconstruction-sign-convention.md) | Portfolio reconstruction sign convention |  |  |
| [ADR-0012](ADR-0012-llmanalyst-protocol-deferred.md) | LLMAnalyst protocol (deferred to v0.3.0) |  |  |
| [ADR-0013](ADR-0013-hermes-core-integration-stance.md) | Hermes-core integration stance + dual-surface architecture |  |  |
| [ADR-0014](ADR-0014-chat-mode-advisor-surface.md) | Chat-mode advisor surface |  |  |
| [ADR-0015](ADR-0015-hitl-propose-decide-react.md) | HITL propose-decide-react surface | "rejected"}`                              | |  |
| [ADR-0016](ADR-0016-autonomous-mode.md) | Autonomous mode (silence-bias gated paper-trading) |  |  |
| [ADR-0017](ADR-0017-ccxt-provider.md) | CcxtProvider for crypto OHLCV bars |  |  |
| [ADR-0018](ADR-0018-kronos-analyst.md) | KronosAnalyst — third voice, not the oracle |  |  |
| [ADR-0019](ADR-0019-evaluation-module.md) | `evaluation/` module promotion (CV + lookahead + DSR) |  |  |
| [ADR-0020](ADR-0020-backtest-harness.md) | Backtest harness — `hermes_quant.backtest` |  |  |
| [ADR-0021](ADR-0021-pdr-recipe-runtime.md) | Adopt PDR recipes as the Hermes-native runtime contract | accepted | 2026-05-14 |
