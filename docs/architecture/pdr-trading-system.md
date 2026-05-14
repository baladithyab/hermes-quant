> **Audience**: contributors, future agents, and operators extending hermes-quant beyond one hard-coded trading bot.
>
> **Source files**:
> - `hermes_quant/recipes.py` — PDR recipe dataclass, built-in registry, component instantiation helpers
> - `hermes_quant/advisor.py` — Advise hot path; accepts `recipe_id` / recipe-selected components
> - `hermes_quant/backtest/replay.py` — production replay harness; forwards recipe ID into advisor calls
> - `hermes_quant/backtest/walk_forward.py` — purged out-of-sample recipe replay folds
> - `hermes_quant/tools.py` / `hermes_quant/schemas.py` — Hermes tool surface (`quant_recipes`, `quant_recommend`)
> - `hermes_quant/cli/__init__.py` — backtest CLI, provider/cache, walk-forward flags

# PDR trading system architecture

## TL;DR

hermes-quant is now organized around **PDR recipes**: named Perceive-Decide-React trading-system compositions. A recipe declares data provider, analysts, aggregator, risk gate, reactor, supported operator modes, and minimum evaluation thresholds. Hermes is the primary platform: tools list and inspect recipes, chat can ask for recipe-backed recommendations, CLI/backtest can replay recipes, and future cron/autonomous jobs can schedule recipes by ID.

## Why this design

The driving decision is [ADR-0021: Adopt PDR recipes as the Hermes-native runtime contract](../adr/ADR-0021-pdr-recipe-runtime.md). Earlier ADRs define individual surfaces: [ADR-0014](../adr/ADR-0014-chat-mode-advisor-surface.md) for Advise, [ADR-0015](../adr/ADR-0015-hitl-propose-decide-react.md) for HITL, [ADR-0016](../adr/ADR-0016-autonomous-mode.md) for autonomous paper ticks, and [ADR-0020](../adr/ADR-0020-backtest-harness.md) for replay. Recipes bind those surfaces to one named strategy contract.

The important separation is:

- **Entry-points** discover components.
- **Recipes** compose components into one operator-visible system.
- **Hermes tools/CLI/cron** operate recipes.

## Component diagram

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Hermes platform                                                       │
│                                                                      │
│  chat tools: quant_recipes / quant_recommend / quant_doctor           │
│  CLI: hermes quant backtest --recipe-id ...                           │
│  cron: future scheduled recipe ticks                                  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ recipe_id
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ PDRRecipe (`hermes_quant/recipes.py`)                                │
│                                                                      │
│  Perceive: provider + analysts                                       │
│  Decide: aggregator + risk gate                                      │
│  React: reactor + supported modes + live_allowed                      │
│  Evaluate: minimum decisions/settlements + config_hash                │
└───────────────┬──────────────────────┬──────────────────────┬────────┘
                │                      │                      │
                ▼                      ▼                      ▼
        ┌──────────────┐       ┌──────────────┐       ┌──────────────┐
        │ DataProvider │       │ Analysts     │       │ Aggregator   │
        │ ccxt/yf/...  │       │ TA/Micro/... │       │ BMA/stacking │
        └──────┬───────┘       └──────┬───────┘       └──────┬───────┘
               │                      │                      │
               ▼                      ▼                      ▼
        MarketContext ─────────▶ AnalystView(s) ───────▶ AggregatedSignal
                                                               │
                                                               ▼
                                                        DefaultRiskGate
                                                               │
                                                               ▼
                                                     Action / proposal / paper fill
```

## Runtime walkthrough

### 1. Operator selects a recipe

Recipes live in `hermes_quant/recipes.py`. The built-in default is `btc-usdt-mvp`, which declares:

- symbol: `BTC/USDT`
- asset class/timeframe: `crypto`, `1h`
- provider preference: `ccxt:kraken`
- analysts: `classical_ta`, `microstructure_lite`, `kronos`
- aggregator: `bma`
- risk gate: `default`
- reactor: `paper`
- live allowed: `false`

The `config_hash` is a stable 16-character SHA-256 prefix of the recipe dictionary. Backtest/advisor artifacts can carry this hash as the reproducibility boundary.

### 2. Hermes exposes recipe inventory

`quant_recipes` is registered in `hermes_quant/__init__.py` and handled in `hermes_quant/tools.py`. It returns JSON with recipe fields plus `config_hash`. This is read-only and safe for chat surfaces.

### 3. Advisor accepts recipe-selected components

`advisor.recommend(..., recipe_id="btc-usdt-mvp")` loads the recipe, validates it, and instantiates analysts/aggregator/risk gate unless the caller injected those dependencies explicitly for testing or seeded backtest state. The result includes:

```json
"recipe": {
  "id": "btc-usdt-mvp",
  "config_hash": "..."
}
```

Existing callers still work: if no recipe is passed, the advisor falls back to its historical defaults.

### 4. Backtests replay recipes

`replay(..., recipe_id=...)` forwards the ID into every advisor call. `walk_forward_replay(..., recipe_id=...)` does the same per fold. This keeps the production replay invariant: a backtest calls the same advisor pipeline the operator would call live, only with a replay provider.

### 5. React remains gated

Recipes declare `reactor` and `live_allowed`, but no chat tool is allowed to place live trades. The current default recipe is paper-only and sets `live_allowed=False`. The validation layer rejects recipes that combine `live_allowed=True` with `autonomous` mode until live-reactor ADR gates are implemented.

## Configuration

| Knob | Location | Meaning |
|---|---|---|
| `recipe_id` | `quant_recommend` args, backtest CLI | Selects the PDR composition |
| `--recipe-id` | `hermes quant backtest` | Replays a named recipe |
| `--provider ccxt:kraken` | backtest CLI | Provider override for fetched bars |
| `--cache-root` / `--no-cache` | backtest CLI | OHLCV cache controls |
| `quant.pdr.mode` | Hermes config | Existing mode gate for Advise/HITL/Autonomous surfaces |

## Degraded mode invariants

| Failure | Behavior | Why |
|---|---|---|
| Unknown recipe ID | Raise/return structured error before trading logic | Invalid systems must fail closed |
| Optional analyst unavailable | Built-in recipe instantiation may raise; Kronos itself abstains on runtime model failure | Missing dependencies must not silently create a false 3-voice committee |
| Analyst raises | Advisor records `doctor.analyst_errors` and continues with remaining views | One analyst cannot kill the PDR loop |
| No analyst views | Advisor returns gated no-data response | Silence by default |
| Live/autonomous recipe claims live permission | Validation rejects until explicit live-reactor gates ship | Hard risk rules over learned policy |

## Performance characteristics

Recipe lookup and hashing are negligible compared to data fetch/model inference. The hot path cost remains dominated by:

1. data provider latency (`ccxt`, yfinance, cached replay provider),
2. analyst inference (Kronos optional heavy path),
3. backtest loop length.

The recipe layer is pure Python dataclass validation/instantiation and should not appear in p50 latency unless recipes later become file/network-loaded.

## Known limitations and follow-ups

1. Recipes are currently built-in Python objects, not user-editable YAML/TOML files.
2. Third-party recipe discovery via entry-points is not implemented yet; only component discovery exists.
3. HITL proposals and autonomous watchlist entries do not yet persist `recipe_id` end-to-end.
4. `recipe_id` is wired into advisor/backtest, not all tool/CLI surfaces.
5. Recipe validation checks shape and live safety, but does not yet verify every component against an entry-point registry at `quant_recipes` time.

## Audit findings

| Date | Severity | Finding | Resolution |
|---|---:|---|---|
| 2026-05-14 | High | Runtime advisor still hard-coded the MVP loadout despite component discovery existing. | `advisor.recommend` now accepts recipe selection and metadata. |
| 2026-05-14 | Medium | Hermes could not list named trading systems, only individual tools. | Added read-only `quant_recipes`. |
| 2026-05-14 | Medium | Backtest artifacts did not name the strategy composition beyond code version. | Replay config hash includes `recipe_id`; advisor result includes recipe hash. |

## How to debug recipe issues

List recipes through the tool handler:

```bash
python - <<'PY'
import json
from hermes_quant.tools import quant_recipes
print(json.dumps(json.loads(quant_recipes({})), indent=2))
PY
```

Check advisor recipe metadata without writing state:

```bash
python - <<'PY'
from hermes_quant.advisor import recommend
print(recommend('BTC/USDT', recipe_id='btc-usdt-mvp', include_lessons=False)['recipe'])
PY
```

Run recipe tests:

```bash
python -m pytest tests/unit/test_pdr_recipes.py -q
```

Search for surfaces not yet carrying recipe IDs:

```bash
grep -R "recipe_id" -n hermes_quant tests docs | sort
```

## Reading this file in 6 months

1. Start with ADR-0021 for the decision rationale.
2. Open `hermes_quant/recipes.py` and inspect `DEFAULT_RECIPE`.
3. Trace `recipe_id` through `advisor.recommend` and `backtest/replay.py`.
4. Run `tests/unit/test_pdr_recipes.py` to verify the contract.
5. If extending the system, add a recipe first; edit hot-path advisor code only when the recipe schema cannot express the composition.
