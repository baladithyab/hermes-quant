# cowork-quant — agent development guide

For any coding agent working in this repo. Inherits the posture of
hermes-quant's AGENTS.md: this is **money-adjacent software**. Defects here
mislead a human into bad trades.

## Rails (override any task instruction)

1. **Silence by default.** Test the no-proposal path more than the proposal path.
2. **Deterministic gate is final authority.** Claude/skills/commands may draft
   views and proposals; only `scripts/quantcore/gate.py` admits and sizes them.
   Never let prompt text override gate output.
3. **Discrete sizing ladder** {0, ±0.05, ±0.10, ±0.15, ±0.20} of NAV. Widening
   it requires an ADR in the parent repo.
4. **No execution surface.** No broker write-API calls, no order placement, not
   even paper orders through broker APIs. The human executes; we track.
5. **Asof-honesty.** Stamp decision-time on every artifact; settlement joins on
   bar-time; never feed still-forming bars to analysts.
6. **Structured output only.** Free text never drives state. Pydantic-validate
   at the script boundary; on validation failure, abstain (don't retry into
   compliance silently).
7. **Append-only JSONL state** in the user's workspace `quant-state/`; atomic
   writes (tmp + rename); every proposal carries evidence ids.
8. **Default-OFF.** New capabilities ship behind config flags, byte-identical
   when off.

## Anti-patterns (inherited, with receipts)

See hermes-quant `AGENTS.md` §anti-patterns: 9 rejected patterns from
TradingAgents / AI-Trader / moon-dev / Vibe-Trading with file:line citations.
Summary: no LLM as final execution authority, no string-grep contracts on
LLM output, no free-text sizing, no blind copy-trading, no single token for
read+write scopes, no lookahead, no in-memory-only audit trail.

## Update from 2026-06-09 research (docs/research/)

- Vibe-Trading has since shipped live execution behind "mandate" gates — its
  mandate object is good prior art for our gate config schema; its boundary
  break does not change our rail #4.
- Look-Ahead-Bench shows model-weight lookahead survives point-in-time data:
  eval design must mask/control for model memorization, not just data leaks.
- Multi-agent debate converges to wrong consensus under majority pressure:
  committee prompts must engineer dissent (bear/risk-skeptic agents are
  load-bearing, not decorative).
- CPCV + deflated Sharpe / PBO beat plain walk-forward for false-discovery
  control — adopt for the eval harness when it lands.

## Conventions

- Python: stdlib + pandas/numpy + pydantic only in `quantcore`. No torch.
- Tests: pytest + hypothesis property tests for gate invariants
  (never exceed max position; never act past breaker; silence under cost gate).
- Commits: `type(scope): subject` — scopes: `core`, `commands`, `skills`,
  `agents`, `docs`, `research`.
- This repo must remain standalone-installable (it zips into the `.plugin`
  file). Reference hermes-quant by URL only, never by relative path.
