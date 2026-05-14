# ADR-0023: Deliberative committee decision layer

- **Status:** Accepted
- **Date:** 2026-05-13
- **Related:** ADR-0003 aggregators, ADR-0016 silence-biased autonomous mode, ADR-0021 PDR recipe runtime, TradingAgents architecture research

## Context

The `TradingAgents` project demonstrates a useful collaboration pattern:

1. specialist analysts produce independent reports,
2. bull/bear researchers debate the investment case,
3. a trader turns the debate into a concrete proposal,
4. aggressive/conservative/neutral risk agents debate risk,
5. a portfolio manager synthesizes the final decision,
6. the state object accumulates every report and debate turn for audit and
   future reflection,
7. quick models handle specialist/debate turns while deep models handle manager
   synthesis.

hermes-quant currently has independent quantitative analysts and a BMA
aggregator. That is robust and replayable, but it does not yet model structured
disagreement or multi-model deliberation. The user's target is a Hermes-native
PDR trading platform that can reach similar collaboration quality while keeping
money-software safety invariants.

## Decision

Introduce a **Deliberative Committee Aggregator** as a decision-layer adapter.
It preserves the `Aggregator` protocol but records a richer deliberation trace in
`AggregatedSignal.metadata`.

The committee stages are:

1. **Specialist ingest** — receives normal `AnalystView` objects from quantitative
   and semantic perception analysts.
2. **Research debate** — computes bull, bear, and neutral cases from the view
   distribution.
3. **Trader synthesis** — converts research debate into a provisional signal.
4. **Risk debate** — records aggressive, conservative, and neutral risk
   perspectives.
5. **Portfolio-manager synthesis** — emits the final `AggregatedSignal`, or
   delegates to the baseline BMA output when deliberation does not improve
   certainty.

The first implementation is deterministic and local. It does **not** call LLMs.
It creates the state shape, metadata trace, and extension seam for future
model-backed committee turns.

## Model-mixture contract

Future model-backed deliberation must use an explicit `ModelVote`/`CommitteeTurn`
artifact contract rather than hidden network calls inside `aggregate()`.

A model turn records:

- role (`bull_researcher`, `bear_researcher`, `risk_conservative`,
  `portfolio_manager`, ...),
- model id and provider,
- prompt/input hash,
- structured stance/direction/confidence/magnitude,
- rationale,
- source packet hashes or analyst view hashes,
- timestamp and reproducibility metadata.

The aggregator may consume supplied model turns from `MarketContext.extras`, or
from a replay artifact, but the deterministic fallback must work without them.

## Safety rules

- The committee cannot bypass the deterministic risk gate.
- Disagreement increases silence pressure; it never increases size by itself.
- Missing model turns degrade to deterministic fallback, not to live calls.
- All deliberation metadata must be JSON-serializable and bounded in size.
- A committee output with too few analyst voices or high disagreement must be
  flat/low-confidence.

## Consequences

Positive:

- We get TradingAgents-style collaboration and debate as a first-class decision
  primitive while preserving Hermes replayability.
- The output remains compatible with existing advisor, backtest, doctor, and
  signal-bus flows.
- Hermes can later run actual multi-model debate jobs upstream and inject the
  resulting turns as artifacts.

Negative / deferred:

- Initial committee reasoning is heuristic, not truly model-deliberative.
- Model execution, prompt versioning, and model-vote packet writing are separate
  follow-up work.
- Calibration must track committee outputs separately from BMA.

## Implementation notes

- Add `hermes_quant/aggregators/deliberative.py`.
- Add recipe support for `aggregator="deliberative_committee"`.
- Add a built-in `btc-usdt-deliberative` recipe that includes the semantic
  analyst and deliberative aggregator, but keeps `live_allowed=False`.
- Add tests proving silence on missing/contradictory views and deterministic
  metadata shape.
