# ADR-0025: User-editable recipes and perception status

- **Status:** Accepted
- **Date:** 2026-05-14
- **Related:** ADR-0021 PDR recipes, ADR-0024 autonomous semantic perception

## Context

v0.4.1 introduced built-in PDR recipes, and v0.4.3 introduced semantic packet
artifacts. That made the architecture extensible for contributors, but normal
operators still had to edit Python to customize a recipe.

For hermes-quant to be a well-built Hermes plugin that anyone can install and
customize, the composition layer must be editable from `~/.hermes/quant/` and
observable from the CLI.

## Decision

Add user-editable recipe YAML files under:

```text
~/.hermes/quant/recipes/*.yaml
```

The runtime recipe registry loads built-ins plus user recipes. User recipes may
not shadow built-in IDs. Operators can validate recipes before use:

```bash
hermes quant recipes example --output ~/.hermes/quant/recipes/my.yaml
hermes quant recipes validate ~/.hermes/quant/recipes/my.yaml
hermes quant recipes list
```

Add a perception status surface:

```bash
hermes quant perception status --recipe-id btc-usdt-deliberative
```

This reports whether each symbol in the recipe has a fresh semantic packet based
on the recipe's `hermes_semantic.max_age_minutes` configuration.

## Safety rules

- User recipes must pass the same `PDRRecipe.validate()` rules as built-ins.
- User recipes cannot shadow built-in IDs, preventing accidental override of
  known-safe templates.
- `live_allowed=True` with autonomous mode remains rejected until live-reactor
  gates are explicitly implemented.
- Perception status is read-only.

## Consequences

Positive:

- Users can customize analysts, aggregators, timeframes, providers, and semantic
  freshness without code changes.
- Operators get immediate feedback about stale/missing perception artifacts.
- The plugin remains installable with no prompts for credentials.

Negative / deferred:

- Recipe YAML validates shape but does not yet fully preflight every third-party
  entry point at list time.
- Perception status reports packet freshness, not packet quality.
- A future UI/dashboard could make recipe editing safer than hand-written YAML.
