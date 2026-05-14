---
status: accepted
date: 2026-05-14
amends:
  - ADR-0014
  - ADR-0015
  - ADR-0016
  - ADR-0020
---

# ADR-0021: Adopt PDR recipes as the Hermes-native runtime contract

## Context and Problem Statement

v0.4.0 proved the MVP loop: ccxt/yfinance data can be replayed through the
production advisor, BMA posteriors can learn from settlements, and Hermes can
surface the charter decision. The remaining architecture problem is
**evolvability**: the core runtime still contains hard-coded loadouts in places
where future trading systems need named, inspectable, replayable compositions.

A PDR trading system should be expressed as a named **recipe** that declares its
Perceive, Decide, and React parts. Hermes then becomes the main operator
platform: chat/tools inspect recipes; CLI/backtest/autonomous run recipes;
cron schedules recipe ticks; documentation explains recipe guarantees.

Relationship to prior ADRs:

- **ADR-0014 (advisor surface)**: preserved. The one-shot advisor remains the
  Advise surface, but its loadout is selected by a recipe rather than being
  hard-coded inside `advisor.py`.
- **ADR-0015 (HITL PDR)**: preserved. Propose/approve/reject stays the HITL
  operator surface; the proposal should record `recipe_id` for replay.
- **ADR-0016 (autonomous mode)**: preserved. Autonomous mode remains paper-only
  by default and silence-biased; watchlist entries should select a recipe.
- **ADR-0020 (backtest harness)**: amended. Backtests remain production replay,
  but the production pipeline under replay is a recipe-selected pipeline.

## Decision Drivers

- Hermes must be the primary operator platform, not just a thin wrapper around a
  single hard-coded bot.
- Future analysts/aggregators/reactors should plug in without editing the
  advisor hot path every time.
- Every signal must remain replayable from disk: recipe ID + recipe config hash
  becomes part of the reproducibility boundary.
- The system must preserve money-software defaults: silence by default, hard risk
  rules, no LLM tool can place live trades.
- Recipes need to be legible to humans and future agents from Hermes chat, CLI,
  docs, and artifacts.

## Considered Options

- Keep hard-coded advisor loadout
- Plugin entry-points only
- PDR recipe runtime contract

## Decision Outcome

Chosen option: **PDR recipe runtime contract**, because it makes the trading
system an operator-visible unit while preserving existing protocol/entry-point
extension seams.

### Consequences

- **Positive**: Backtests, HITL proposals, autonomous ticks, and doctor output can
  all refer to the same named recipe.
- **Positive**: New strategies are expressed as data/config plus registered
  components, not branches through `advisor.py`.
- **Positive**: Recipe config hashes create an explicit reproducibility boundary.
- **Negative**: Another abstraction exists; recipe validation must be strict or
  invalid recipes become a new failure mode.
- **Negative**: Existing hard-coded defaults must remain compatible until all
  surfaces accept `recipe_id`.
- **Neutral**: Entry-points remain the component discovery mechanism; recipes are
  the composition mechanism above entry-points.

## Pros and Cons of the Options

### Keep hard-coded advisor loadout

- Good, because it is simple and already works for the current MVP committee.
- Good, because no config parser or validation layer is needed.
- Bad, because each new strategy requires code edits in hot-path runtime files.
- Bad, because Hermes cannot list, inspect, schedule, or backtest named systems.
- Bad, because replay artifacts cannot state which strategy composition produced
  a decision beyond indirect code-version inference.

### Plugin entry-points only

- Good, because the repo already has entry-point discovery for analysts,
  aggregators, and data providers.
- Good, because third-party packages can publish components independently.
- Bad, because entry-points list parts, not compositions. They do not answer:
  which analysts belong together, which gate applies, which provider/exchange is
  intended, or whether the recipe is safe for autonomous mode.
- Bad, because operators need a named system, not a bag of importable classes.

### PDR recipe runtime contract

- Good, because it keeps components discoverable while making the composition
  explicit and inspectable.
- Good, because Hermes tools can expose recipe registry, validation, status, and
  backtest actions without adding new trading side effects.
- Good, because recipes can carry policy: supported modes, paper-only/live-ready,
  minimum decision counts, data provider preferences, and evaluation gates.
- Bad, because recipe schemas and migrations must be maintained.
- Bad, because component name drift can break recipes unless validation catches it
  early.

## Acceptance gate

- [x] Define a recipe dataclass/schema with stable fields for Perceive, Decide,
  React, evaluation policy, supported modes, and reproducibility hash.
- [x] Ship a default recipe equivalent to the current BTC/USDT MVP committee so
  existing behavior remains available.
- [x] Add tests proving recipe hashing is stable and invalid recipes are rejected.
- [x] Wire at least one production/evaluation surface to accept a recipe rather
  than a hard-coded loadout.
- [x] Document the architecture in `docs/architecture/pdr-trading-system.md`.

## More Information

- Architecture reference: `docs/architecture/pdr-trading-system.md`
- Charter: `docs/charter/2026-05-13-hermes-quant-charter.md`
- Real-data smoke: `docs/audits/2026-05-14-btc-usdt-realdata-smoke.md`
