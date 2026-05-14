# Changelog

All notable changes to hermes-quant.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.4] — 2026-05-14

### Summary

v0.4.4 makes hermes-quant meaningfully customizable for non-contributors: PDR recipes can now be authored as YAML under `~/.hermes/quant/recipes/`, perception freshness can be checked per recipe, and the committee runner exposes a safe model-mixture prompt surface without adding hidden model calls to trading ticks.

### Added

- **ADR-0025**: user-editable recipes and perception status.
- **User recipe YAML loader**: built-ins plus `~/.hermes/quant/recipes/*.yaml` are loaded by the recipe registry.
- **`hermes quant recipes list|validate|example`**: list all recipes, validate a recipe YAML, or write/print a starter template.
- **`hermes quant perception status`**: report fresh/stale/missing/future semantic packets for every symbol in a recipe using its configured `hermes_semantic.max_age_minutes`.
- **`hermes quant committee prompt`**: emit a self-contained Hermes prompt for a future multi-model committee job, including packet hashes and required roles.

### Changed

- User recipes cannot shadow built-in recipe IDs; this prevents accidental override of known templates.
- Recipe listing now includes user recipes by default, while preserving deterministic ordering.
- README quickstart now shows recipe customization and perception status commands.

### Safety posture

- Recipe validation keeps live autonomous recipes rejected until live-reactor gates are explicitly implemented.
- Perception status is read-only.
- The model-mixture surface is prompt/artifact-driven; no trading tick performs hidden model calls.

### Test sweep

- 604 passed, 1 skipped (was 599 → +5, zero regressions).

## [0.4.3] — 2026-05-14

### Summary

v0.4.3 makes the semantic/deliberative PDR layer usable by normal Hermes plugin operators: semantic packets and committee turns are now filesystem artifacts with CLI write/validate/list/run surfaces, autonomous semantic perception can be installed as a Hermes cron job, and advisor/backtest/signal artifacts persist semantic provenance hashes.

### Added

- **ADR-0024**: autonomous semantic perception as an artifact pipeline rather than live model calls in trading ticks.
- **`hermes_quant.artifacts`**: atomic JSON stores for semantic packets and committee-turn artifacts under `~/.hermes/quant/`.
- **`hermes quant semantic-packet write|validate|list`** for operator-authored or Hermes-authored semantic perception artifacts.
- **`hermes quant committee run|list`** for deterministic committee-turn artifacts derived from semantic packet archives.
- **`hermes quant perception start`** to create/dry-run a Hermes cron job that autonomously researches sources and writes semantic packets.
- **Backtest artifact injection flags**: `--semantic-packet-file` and `--committee-turns-file` for `hermes quant backtest`.
- **Advisor CLI artifact injection flags** for `hermes quant recommend`.
- **Dogfood audit**: `docs/audits/2026-05-14-v043-btc-usdt-dogfood.md` comparing MVP vs deliberative recipes.

### Changed

- Backtest replay now instantiates the recipe-selected aggregator for learning loops instead of always forcing BMA; this preserves the deliberative recipe during replay.
- Backtest decision snapshots now record recipe metadata, semantic packet hashes, committee decision metadata, and aggregator name.
- Daemon signal records now include component metadata, signal metadata, semantic packet hashes, and committee-turn hashes for downstream attribution.
- README and plugin manifest now reflect the current install/customization surfaces.

### Dogfood summary

7-day BTC/USDT Kraken smoke, 1h bars, warmup 24:

| variant | decisions | fires | return | buy_hold | excess |
|---|---:|---:|---:|---:|---:|
| `btc-usdt-mvp` | 1 | 2 | -0.07% | +0.12% | -0.19% |
| `btc-usdt-deliberative` no packets | 0 | 0 | +0.00% | +0.12% | -0.12% |
| `btc-usdt-deliberative` curated packet | 0 | 0 | +0.00% | +0.12% | -0.12% |

Interpretation: this is a smoke test, not a promotion gate. The deliberative path currently fails closed under insufficient semantic coverage, which is the desired safety behavior. Next dogfood needs autonomous packet coverage across the full window.

### Test sweep

- 599 passed, 1 skipped (was 593 → +6, zero regressions).

## [0.4.2] — 2026-05-14

### Summary

v0.4.2 makes Hermes itself part of the PDR trading architecture: semantic analysis can enter the Perceive layer as replayable packets, and the Decide layer now has a TradingAgents-style deliberative committee scaffold for bull/bear/risk/portfolio-manager collaboration.

### Added

- **ADR-0022**: Hermes semantic perception layer. Semantic packets are precomputed Hermes/model/human research artifacts consumed by analysts; no hidden model/web calls happen inside a trading tick.
- **ADR-0023**: Deliberative committee decision layer. The committee records research debate, trader synthesis, risk debate, and portfolio-manager synthesis while preserving the `Aggregator` protocol.
- **`hermes_quant.semantic`**: `SemanticPacket`, source provenance, canonical packet hashing, parsing, and validation helpers.
- **`HermesSemanticAnalyst`** (`hermes_quant.analysts.semantic`): consumes semantic packets from `MarketContext.extras`, emits normal `AnalystView`s, and abstains with zero confidence when packets are missing/stale/future/tampered.
- **`DeliberativeCommitteeAggregator`** (`hermes_quant.aggregators.deliberative`): deterministic TradingAgents-style committee scaffold with bull/bear/neutral research turns, trader synthesis, aggressive/conservative/neutral risk perspectives, and portfolio-manager metadata.
- **`btc-usdt-deliberative` recipe**: BTC/USDT recipe using quantitative analysts + `hermes_semantic` + `deliberative_committee`, paper/backtest-only.
- `quant_recommend` schema/tool path accepts optional `semantic_packets` and `committee_turns` artifacts for replayable semantic/model-mixture inputs.

### Changed

- `advisor.recommend()` accepts `market_extras` so replay/tool callers can inject semantic packets and committee turns without changing the analyst protocol.
- Advisor aggregated-signal JSON now carries `metadata`, exposing committee deliberation traces to Hermes/tool callers.
- Architecture docs now document semantic packets and model-mixture deliberation as first-class PDR extension seams.

### Safety posture

- Model-backed debate is artifact-driven, not live-call-driven: future Hermes model mixtures should write explicit `committee_turns` with model IDs/input hashes before aggregation.
- Deliberation can reduce confidence or force flat on disagreement; it cannot bypass the deterministic risk gate.
- Semantic packets are hash-verified and freshness-checked; invalid packets produce abstain views, not trades.

### Test sweep

- 593 passed, 1 skipped (was 580 → +13, zero regressions).

## [0.4.1] — 2026-05-14

### Summary

v0.4.1 turns hermes-quant from a hard-coded MVP committee into a **Hermes-native PDR recipe platform**. A recipe is now the named, inspectable, replayable unit of a trading system: it declares Perceive components (provider + analysts), Decide components (aggregator + risk gate), React policy (paper/live mode constraints), and evaluation gates.

### Added

- **ADR-0021**: PDR recipes as the runtime contract above component entry-points.
- **`hermes_quant.recipes.PDRRecipe`** with stable config hashing, validation, built-in `btc-usdt-mvp` recipe, and component instantiation helpers.
- **`quant_recipes` Hermes tool** and `/quant recipes` slash path for read-only recipe discovery.
- **Advisor recipe metadata**: `quant_recommend` / `advisor.recommend` accept `recipe_id` and return `{id, config_hash}` in result JSON.
- **Backtest recipe passthrough**: `replay()` and `walk_forward_replay()` forward `recipe_id` into the production advisor path.
- **Architecture documentation**: `docs/architecture/pdr-trading-system.md` plus architecture index.
- **ADR index**: `docs/adr/README.md` generated for the full ADR set.

### Fixed / Changed

- The advisor no longer has to be the only place where the canonical analyst loadout is encoded; default behavior remains compatible, but recipes are now the extensibility seam.
- `quant_recommend` no longer forces `asset_class="equity"` when a recipe should supply crypto/equity defaults.

### Test sweep

- 580 passed, 1 skipped (was 572 → +8, zero regressions).

## [0.4.0] — 2026-05-14

### Summary

v0.4.0 closes the charter MVP loop from **data → replay → empirical decision**:

- the BMA aggregator now learns from replay settlements (`EpisodeOutcome`)
  during backtests;
- `quant_doctor` surfaces analyst confidence drift from the signal bus;
- purged walk-forward replay composes ADR-0019 cross-validation with the
  production ADR-0020 replay harness;
- OHLCV provider fetches are cached on disk so repeated BTC/USDT dogfood runs
  are deterministic and cheap;
- the first real BTC/USDT ccxt smoke is documented.

Charter decision from the real-data smoke: **do not proceed to RL aggregator
or live reactors yet**. A 30-day Kraken BTC/USDT contiguous replay failed
buy-and-hold by -6.56%; 3-fold walk-forward was slightly positive but only
emitted two decisions, which is not enough evidence.

### Added

- **Backtest calibrator loop (V03-5)** — `replay()` now uses a long-lived
  aggregator across bars and settles pending decisions into `EpisodeOutcome`
  updates after `settlement_horizon_bars`. `BacktestResult` exposes
  `n_settlements` and final `aggregator_posteriors`; markdown reports include
  a per-analyst BMA posterior table.
- **Doctor drift surface (V03-6)** — `quant_doctor` reports per-analyst
  lifetime vs recent-window confidence drift, flags vanished analysts, and
  tolerates malformed signal-bus records.
- **Purged walk-forward backtest (Wave I)** —
  `hermes_quant.backtest.walk_forward_replay()` runs independent
  out-of-sample replay folds and aggregates mean excess return, Sharpe delta,
  positive-excess fold rate, total decisions, and settlements.
- **CLI walk-forward mode** — `hermes quant backtest --walk-forward --n-splits N`
  writes per-fold equity/decision artifacts plus an aggregate `result.json`.
- **OHLCV file cache (V03-7)** — `hermes_quant.data.cache.OhlcvCache` stores
  provider/symbol/timeframe bars under `~/.hermes/quant/cache/<provider>/`,
  with append/dedupe/sort and atomic writes. Parquet preferred, CSV fallback.
- **Provider-selectable CLI fetch** — `--provider ccxt:kraken` /
  `ccxt:coinbase` / `yfinance`, plus `--cache-root` and `--no-cache`.
- **Real-data audit** — `docs/audits/2026-05-14-btc-usdt-realdata-smoke.md`
  records Binance geoblock evidence, Kraken fetch details, single-run and
  walk-forward results, and the charter decision.

### Fixed

- `_read_jsonl_tail()` no longer drops the first JSONL record when the whole
  file fits in the read window.
- Backtest CLI provider lookback buffer reduced so caches don't miss forever
  when exchanges return slightly fewer closed bars than requested.
- `quant_doctor` uses live module-attribute lookup for path globals to avoid
  stale `__globals__` under pytest import-order/module-dict duplication.

### Test sweep

- 572 passed, 1 skipped (was 530 → +42, zero regressions).

### Quick start

```bash
# Fetch via Kraken ccxt with cache and run walk-forward replay
hermes quant backtest --symbol BTC/USDT --asset-class crypto \
  --timeframe 1h --provider ccxt:kraken \
  --start 2026-04-14T00:00:00Z --end 2026-05-14T00:00:00Z \
  --walk-forward --n-splits 3 \
  --output-dir ~/.hermes/quant/backtests/btc-kraken-wf/
```

## [0.3.2] — 2026-05-13

### Summary

The **empirical gate** the charter requires for any RL aggregator
work: `hermes_quant.backtest`. Operators can now run
`hermes quant backtest --symbol BTC/USDT --bars-file <path>` and get
a buy-and-hold-excess number in seconds rather than waiting 4-8 weeks
of wall-clock paper trading. The replay uses the production advisor
pipeline exactly (charter "Reproducibility" honored — every backtest
decision is bit-identical to what the live advisor would emit).

This is the release the charter was waiting for: *"if your three-analyst
committee on BTC can't beat buy-and-hold risk-adjusted on paper, more
analysts won't fix it."* v0.3.2 makes that question computable.

### Added

- **ADR-0020**: backtest harness contract. Charter "REPLAY, not
  simulation" — uses production code paths verbatim, with PaperPortfolio
  for mark-to-market accounting + buy-and-hold baseline + Sharpe + DSR
  + max drawdown computed inside `replay()`.
- **`hermes_quant.backtest.PaperPortfolio`** (143 loc) — single-symbol
  mark-to-market book with slippage, commission, lot-matching realized
  P&L, and position flips. Deliberately simpler than `portfolio_loader`
  (production lot matching) — single book, single symbol, no
  withdrawals, no funding. v0.4 will unify with `portfolio_loader`.
- **`hermes_quant.backtest.replay()`** (380 loc impl) — chronological
  bar-by-bar walk through the advisor pipeline. Lookahead-safe via
  `_ReplayProvider` honoring `as_of`. Per-bar equity curve + buy-hold
  baseline + decisions log. Deflated Sharpe Ratio integrated when
  n_observations >= 30 (ADR-0019 consumer).
- **`BacktestResult`** dataclass — symbol/timeframe/asset_class +
  total_return + Sharpe + DSR + max_drawdown + buy_hold baseline +
  excess_return_vs_buy_hold (the charter-gating headline) + per-bar
  equity_curve / positions / decisions_summary + run_at + config_hash.
- **`hermes quant backtest`** CLI subcommand — load bars from
  CSV/parquet (or fetch via configured yfinance/ccxt provider when
  `--start --end` provided). Writes 4 artifacts to output-dir:
  `result.json` + `report.md` + `equity_curve.csv` + `decisions.jsonl`.
- **24 backtest tests** at `tests/integration/test_backtest_replay.py`:
  PaperPortfolio mark-to-market correctness (8), replay shape +
  buy-hold baseline (10), reproducibility via config_hash (3), markdown
  report generation (2), DSR conditional computation (2), advisor
  exception isolation (1).

### Changed

- **`hermes quant backtest` CLI signature**: positional `asset` +
  `--from`/`--to` replaced with `--symbol` + `--asset-class` +
  `--bars-file` / `--start`/`--end`. The old shape was a v0.1.0
  scaffold stub that never had a handler.
- **`tests/test_smoke.py`** updated to probe the new backtest CLI shape.

### Test sweep

- 530 passed, 1 skipped (was 506 → +24, zero regressions).

### Quick start

```bash
# Backtest from a local CSV
hermes quant backtest --symbol AAPL --asset-class equity \
  --timeframe 1d --bars-file ~/.hermes/quant/cache/aapl-2024.csv \
  --output-dir ~/.hermes/quant/backtests/aapl-2024/

# Or via configured provider
hermes quant backtest --symbol BTC/USDT --asset-class crypto \
  --timeframe 1h --start 2024-01-01 --end 2024-06-01

# Read the headline
cat ~/.hermes/quant/backtests/<run-id>/report.md
```

The single line that matters per charter:
*"Excess return vs buy-and-hold: +X.XX%"*

If that's negative over a multi-month backtest, the charter says fix the
analysts/aggregator before any RL aggregator work — which is exactly what
v0.3.2's gating is for.

## [0.3.1] — 2026-05-13

### Summary

Phase-7 cross-family review of v0.3.0 caught five P0/P1 issues that
single-author construction had missed. The architecture reviewer's
intersection finding was the loud one: *the CHANGELOG claim "ALL THREE
shipped" was structurally false* because `advisor.recommend()` hard-coded
two analysts and KronosAnalyst was never instantiated for live ticks.
Also: BMA didn't actually filter abstaining views (the ADR-0018 §D4
contract was unimplemented), so the charter's safety net had a hole.

### Fixed

- **BMA abstain filter (ADR-0018 §D4)** — Views with `confidence < 0.10`
  are now dropped before aggregation. Closes the bug where Kronos's
  zero-confidence abstain (on missing `kronos` package or weight-load
  failure) inflated the silence-bias gate's `min_analysts_emitted` count
  by counting as a "voice" without contributing signal.
  `hermes_quant/aggregators/bma.py` + 8 regression tests.
- **Advisor wires KronosAnalyst as third voice** — closes the
  charter-MVP gap. `advisor.recommend()` now loads
  `[ClassicalTAAnalyst, MicrostructureLite, KronosAnalyst]` with
  defensive try/except for each optional dependency.
  `hermes_quant/advisor.py` + 2 pinning tests.
- **Removed bogus `KairosAnalyst` entry-point** — `pyproject.toml`
  declared `kairos_btc = "...:KairosAnalyst"` but no such class
  existed; entry-point would break setuptools-discovery on install.
  Renamed `kronos_small` → `kronos` for parity with class name.
- **Lookahead CI gate rewired** — `tests/test_no_lookahead.py` now uses
  the canonical `evaluation.lookahead.shuffle_timestamps_test` instead
  of inline `_RecordingProvider` scaffolding (Wave-D follow-up that
  v0.3.0 didn't complete). 2 new parametrized tests covering both
  shipped analysts.
- **KronosConfig deterministic seed for replayability** — added
  `deterministic_seed=42` field. `_predict_paths()` now seeds
  `numpy.random` + `torch.manual_seed` (when torch installed) so
  Kronos signals are reproducible from disk per the charter
  "Reproducibility" invariant.

### Tests

- **+12 tests** (was 494 → 506, zero regressions):
  - `tests/unit/test_bma_abstain_filter.py` (8 tests)
  - `tests/unit/test_advisor_loadout.py` (2 tests)
  - `tests/test_no_lookahead.py` parametrized over 2 analysts (+2)

### Phase-7 follow-ups deferred to v0.3.2+

- **P1 from correctness review** (4 items): broaden `Exception` catch on
  HF weight load; off-by-one comment in pagination break; double-tz
  normalization is dead code; embargo > train_pct unguarded
- **P1 from test review** (11 items): duplicate-timestamp / negative-volume
  / multi-page pagination cases not covered; PurgedWalkForward unsorted
  timestamps; DSR boundary `n_observations==30`; cron writer edge cases
- **P2 from architecture review** (5 items): ADR-0017 §D6 cassettes not
  committed; ADR-0018 §D7 extras pull `transformers`+`einops` (ADR
  said NO); deprecated `typing.Iterator` import

### Phase-7 review artifacts

`docs/reviews/2026-05-13-v030/` — three reviewer reports:
- `correctness.md` (~210 lines)
- `test-quality.md` (~250 lines)
- `architecture.md` (~340 lines)

Verdict: MERGE_WITH_FOLLOWUPS (all three reviewers); intersection P0s
from architecture review are the ones fixed in this release.

## [0.3.0] — 2026-05-13

### Summary

v0.3.0 ships the **MVP recipe from the founding charter** — *"three-analyst
committee on liquid crypto (BTC/USDT) before any RL aggregator work."* The
two charter-gating modules land: `CcxtProvider` (Binance OHLCV with leaf-
level lookahead-safe as_of filter) and `KronosAnalyst` (foundation-model
forecaster as the third BMA voice with [0.30, 0.85] overconfidence clip).
The `evaluation/` module promotion gives v0.4 RL training the scaffolding
it needs — `PurgedWalkForward`, `shuffle_timestamps_test`, and the
Deflated Sharpe Ratio. The autonomous-tick cron writer is now actually
wired (was print-only in v0.2).

This release is the LAST scaffolding release before charter-mandated paper
trading: per the charter, *"if your three-analyst committee on BTC can't
beat buy-and-hold risk-adjusted on paper, more analysts won't fix it."*
v0.3 is the recipe; v0.4 is the empirical answer.

### Added

- **ADR-0017**: `CcxtProvider` for crypto OHLCV. Critical lookahead bug
  class identified at the leaf — Binance returns bar OPEN time, so we
  filter `open_ts + tf_seconds <= as_of` (the in-flight bar at as_of
  is dropped). Default exchange Binance; multi-exchange via constructor.
- **ADR-0018**: `KronosAnalyst` as the third BMA voice. Lazy-load (no HF
  download at gateway startup). Distributional inference via subclass
  (Kronos's `predict()` averages internally; we expose pre-mean paths).
  Path-agreement confidence with `[0.30, 0.85]` HARD CLIP — direct
  mitigation for the Kairos A-shares neg-IC failure mode the charter
  explicitly warns about. Zero-confidence abstain on missing kronos
  package or weight-load failure.
- **ADR-0019**: `evaluation/` module promotion. `cv.py::PurgedWalkForward`
  with embargo (López de Prado). `lookahead.py::shuffle_timestamps_test`
  promoted from inline test scaffolding. `dsr.py::deflated_sharpe`
  (Bailey & López de Prado 2014) for paper-book Sharpe reporting hedged
  against multiple-comparisons bias.
- **`hermes_quant/data/ccxt_provider.py`** (366 loc) — `CcxtProvider`
  implementation. `_exchange_factory` test seam for FakeCcxtExchange
  unit tests; no live network in CI.
- **`hermes_quant/analysts/kronos.py`** (348 loc) — `KronosAnalyst`,
  `KronosConfig`, `_DistributionalKronosPredictor`. `_predictor_factory`
  test seam; no torch needed for unit tests.
- **`hermes_quant/evaluation/`** package — `cv.py` + `lookahead.py` +
  `dsr.py` + `__init__.py` re-exports.
- **`hermes quant autonomous start --no-cron`** flag; default behavior
  now actually creates the Hermes cron job via `hermes cron create`
  (V03-4) instead of just printing the command. Graceful fallback when
  `hermes` isn't on PATH or cron creation fails.
- **3 research notes** (Phase-3 research outputs, on disk):
  - `docs/research/05-kronos-integration.md` (288 lines, deepwiki-grounded)
  - `docs/research/06-ccxt-provider-patterns.md` (250 lines, ccxt v4 + issue #21783)
  - `docs/research/07-paper-book-pnl-attribution.md` (188 lines)
- **Charter-vs-shipped audit doc** at
  `docs/audits/2026-05-13-charter-vs-shipped-v020.md` — Phase-2 backlog
  enumeration that motivated v0.3.

### Changed

- **`pyproject.toml`** optional-dependencies cleaned up:
  - Fixed pre-existing duplicate `yfinance` key (toml parser was tolerant)
  - Fixed entry-point `ccxt` -> `CcxtProvider` (was pointing to non-existent
    `CCXTProvider` capitalization)

### Tests

- **+68 tests** across 4 waves:
  - Wave A (ccxt provider): 21 tests — as_of filter at all 3 boundary
    cases, error taxonomy mapping, pagination, missing-ccxt graceful
  - Wave B (Kronos analyst): 17 tests — lazy-load, factory-failure abstain,
    inference-exception per-call abstain, path-agreement clip, calibrator
    shrinkage, config overrides
  - Wave C (evaluation): 21 tests — PurgedWalkForward folds + invariants,
    shuffle_timestamps_test pass/fail/edge cases, DSR n_trials scaling +
    skew/kurtosis, validation rejection
  - Wave D (cron writer): 9 tests — happy path, missing PATH, nonzero
    exit, timeout, OSError, no-cron skip, config persistence
- **Total: 494 passed, 1 skipped** (was 426 → +68, zero regressions).

### Charter audit (post-v0.3)

| Charter clause | Status |
|---|---|
| MVP — three-analyst BTC/USDT committee | ✅ ALL THREE shipped (TA + Microstructure + Kronos); ccxt unblocks BTC/USDT |
| Walk-forward CV + lookahead + DSR | ✅ scaffolded |
| Money-software discipline | ✅ leaf-level lookahead filter, foundation-model overconfidence clip |
| AAAI 2026 acceptance ≠ alpha | ✅ Kronos clip [0.30, 0.85] + abstain-on-failure + zero RL training |

### Deferred to v0.4+

- Paper-trade BTC/USDT for 4-8 weeks (the charter's empirical gate)
- KronosAnalyst calibrator wired to a re-training cron (V03-5 P0 deferred)
- News-LLM analyst per ADR-0012
- Options analyst + Greeks-aware sizer
- Live reactors (`AlpacaReactor`, `CcxtReactor`) gated by ADR-0016 §D6 three-lock
- RL aggregator (PPO/SAC) — charter explicitly defers until paper-trade Sharpe is measured

## [0.2.0] — 2026-05-13

### Summary

v0.2.0 ships **autonomous mode** — the third PDR surface. Hermes now
watches the watchlist on a cadence; when the 4-dim silence-bias gate
(charter §"REACT" three-bullet codification) fires, paper trades go
through automatically. Per the founding charter's "rewarded for correct
inaction" invariant, ALL FOUR gate dimensions must pass — silence is
the default. Live autonomous deferred to v0.3 behind three independent
locks (config flag + creds + per-startup arm-live ceremony).

The autonomous tick reuses the existing advisor pipeline (BMA over
ClassicalTA + MicrostructureLite) and the existing PaperReactor; new
code is the orchestrator, the silence-bias gate, the watchlist module,
and the safety rails (per-tick open cap, kill switch).

### Added

- **ADR-0016**: autonomous mode contract (silence-bias-gated paper
  trading on a config-driven watchlist, cron-cadence ticks per ADR-0013
  §D4, paper-only in v0.2). 17 KB; cites the founding charter verbatim.
- **`hermes_quant.gates.silence_bias`** — pure-function 4-dim gate:
  - Confidence (default `min_confidence=0.65`, stricter than HITL)
  - Urgency = expected_signed_edge / volatility (default `min=0.5`)
  - Compute Budget = number of analyst voices (default `min=2`)
  - Salience = recent-rejections veto (default 3 rejections in 168h)
  Structured silence reasons (`SILENCE_LOW_CONFIDENCE`,
  `SILENCE_LOW_URGENCY`, `SILENCE_INSUFFICIENT_VOICES`,
  `SILENCE_SALIENCE_VETO`, `SILENCE_GATED_BY_ADVISOR`) make tuning a
  data exercise rather than guesswork.
- **`hermes_quant.watchlist`** — config-driven watchlist module
  (`add_to_watchlist`, `remove_from_watchlist`, `list_watchlist`,
  `clear_watchlist`). Persists to
  `~/.hermes/config.yaml::quant.autonomous.watchlist`. Profile-aware,
  flock+RLock-serialized, atomic-rename writes, validation rejects
  bad asset_class / timeframe / empty symbol.
- **`hermes_quant.autonomous`** — `tick(symbols, *, dry_run)` orchestrator.
  Mode-gated (`quant.pdr.mode=autonomous` required), kill-switch-gated,
  per-symbol error isolation (one bad symbol doesn't break the tick),
  structured operator-readable output. `_react()` helper synthesizes a
  Proposal stand-in to reuse PaperReactor without HITL state.
- **`hermes_quant.autonomous.trip_kill_switch` /`reset_kill_switch`** —
  durable JSON file at `~/.hermes/quant/autonomous_kill_switch.json`
  with atomic-rename writes. Trips disable autonomous tick until
  `hermes quant autonomous reset --confirm`.
- **5 new tools** registered with the plugin:
  - `quant_autonomous_tick(dry_run=true)` — tool surface defaults to
    DRY-RUN (ADR-0016 §D11) for agent safety; the cron-script path
    sets `dry_run=False` to fire real paper trades
  - `quant_autonomous_status()`
  - `quant_watchlist_add(symbol, asset_class, timeframe?)`
  - `quant_watchlist_remove(symbol, asset_class?)`
  - `quant_watchlist_list()`
- **CLI subcommand tree**: `hermes quant autonomous {tick,status,start,stop,reset,watchlist}`
  with rich-text + `--json` output, `start --watchlist SYM:asset:tf,...`
  shorthand, prints the Hermes-cron command to wire up cadence.
- **Slash-command extensions**: `/quant auto tick|status` and
  `/quant watchlist list|add|remove`.
- **Tests** (61 new):
  - `tests/unit/test_silence_bias_gate.py` — 26 tests covering all
    four dims, dim ordering, salience-window edge cases, configurability,
    bad input
  - `tests/unit/test_watchlist.py` — 18 tests covering CRUD, validation,
    idempotency, atomic-rename simulated crash, 8-thread concurrent-add
  - `tests/integration/test_autonomous_e2e.py` — 17 tests covering
    mode gate, dry-run safety, paper-react happy path, max_per_tick_opens
    cap, kill-switch trip+reset, per-symbol error isolation, all 4
    silence reasons, output shape

### Changed

- **`plugin.yaml::provides_tools`**: 10 → 15 (new autonomous tools listed)
- **Total registered tools**: 10 → 15

### Safety posture

- Tool surface `quant_autonomous_tick` defaults to `dry_run=True`. LLM
  agent generating tool calls cannot accidentally fire trades — only
  the cron-script path with `--no-dry-run` does.
- Mode gating read on EVERY tool call (no caching). Operators flip
  `quant.pdr.mode=advise|hitl|autonomous` without restart.
- `max_per_tick_opens` (default 1) caps new positions per tick.
- `kill_switch_pct` (default 0.10) auto-disables autonomous on
  cumulative paper P&L breach.
- v0.2 ships paper-only. Live autonomous deferred to v0.3 behind
  three independent locks per ADR-0016 §D6.

### Test status

- 426 passed, 1 skipped (was 365 → +61, zero regressions).

## [0.1.2] — 2026-05-13

### Summary

v0.1.2 reframes hermes-quant as a **Perceive-Decide-React (PDR)** plugin
with three operator surfaces — `advise` (one-shot guidance), `hitl`
(propose-approve-react with paper trading), and `autonomous` (deferred
to v0.2). Same Analyst → BMAAggregator → DefaultRiskGate pipeline drives
all three. Two analysts now (ClassicalTA + MicrostructureLite — BMA has
voices to actually aggregate). Settlement journal closes the feedback
loop: every approve/reject persists to a markdown ledger that the
advisor reads back on the next call. Lookahead is enforced at the leaf
data layer. Monotonic-clock heartbeat. Path-safety guard for
user-supplied symbols.

Founding architectural charter saved verbatim to the repo at
`docs/charter/2026-05-13-hermes-quant-charter.md`.

**Test count: v0.1.1 273 → v0.1.2 365 (+92 tests across HITL e2e,
journal, microstructure, no-lookahead invariant, monotonic heartbeat,
symbol safety, and advisor contract).**

### Added — Wave C: lookahead enforcement + path safety + monotonic heartbeat

Closes the bulk of v0.1.2 P0 items from the architecture review and the
TradingAgents round-2 mining doc. All four were ADRified in earlier
amendments; this batch lands the implementation.

**Wave C.1 — `as_of` plumbed through DataProvider** (ADR-0005 amendment):
- `DataProvider` Protocol signature gains `as_of: pd.Timestamp | None = None`
  kwarg per pattern stolen from TauricResearch/TradingAgents §"as_of_date"
  filter at the data leaf.
- `YFinanceProvider.fetch_bars` honors it: bars with
  `timestamp > as_of` are filtered AFTER `validate_bars` so the cutoff
  applies to the same canonical (UTC, ascending) frame an analyst would
  otherwise see.
- `advisor.recommend()` forwards `as_of` to the provider with TypeError
  fallback for older providers / test doubles. Belt-and-suspenders: the
  advisor's downstream filter is preserved as fallback safety in case a
  third-party provider doesn't honor the kwarg.
- The "leaf-level enforcement" property: even if a future analyst forgets
  to filter bars manually, the data layer has already pruned the future.

**Wave C.2 — `safe_symbol_component` utility** (ADR-0005 amendment):
- New `hermes_quant/utils/symbol_safety.py` with whitelist-based
  sanitizer (ASCII alphanumerics + `.`, `_`, `-`).
- Refuses empty / pure-traversal (`.`, `..`) inputs with ValueError.
- Replaces non-safe chars with `_`; caps length at 32; strips leading
  dots (prevents `.htaccess`-style hidden-file traps).
- Pattern stolen from TauricResearch/TradingAgents §"safe_symbol_component";
  prevents path-traversal attacks via user-supplied tickers like
  `"../../etc/passwd"` or BOM/RTL-mark injection.
- v0.1.2 wires it into the journal entry path next; v0.2 will wire into
  the OHLCV cache + log paths.

**Wave C.3 — `tests/test_no_lookahead.py` CI gate** (ADR-0006 amendment):
- Release-blocker test fence per the founding charter "No look-ahead bias"
  invariant. Three invariants pinned:
  1. **Analyst output at time T independent of future bars** — both
     ClassicalTAAnalyst and MicrostructureLite verified by the
     "polluted-but-sliced" pattern (replace future rows with extreme
     values, slice the input, verify analyst output unchanged).
  2. **Provider receives + honors `as_of`** — `_RecordingProvider`
     captures the kwarg and verifies cutoff applied.
  3. **Advisor deterministic under as_of replay** — same `(symbol, as_of)`
     against a short-history provider AND a full-history provider produces
     the SAME aggregated_signal / risk_gate / decision_price.

**Wave C.4 — Monotonic-clock heartbeat** (Phase-8 P1-ε):
- `HeartbeatChecker` now tracks `monotonic_ns` at every
  `mark_observed` / `mark_synthetic_heartbeat` call.
- `check()` computes `bootstrap_age` and `last_heartbeat_age` from BOTH
  wall-clock and monotonic, then takes the MAX (conservative — fail
  toward dead-man-switch firing rather than away from it).
- Wall-clock jump-backward (NTP sync, manual `date`, leap-second
  smearing) no longer extends the grace window. Wall-clock jump-forward
  no longer trips the switch when monotonic agrees the heartbeat is
  fresh.
- JSONL records keep wall-clock `asof` (operators read those); the
  monotonic axis is internal-only.
- `monotonic_clock_ns` constructor kwarg is injectable for tests.

**Tests** (35 new in three files, 365 total):
- `tests/unit/test_symbol_safety.py` (15 tests): happy-path tickers,
  crypto pairs, path-traversal attack class (`../../`, `..`, `.`,
  absolute paths, Windows paths), dot-only and empty input refused,
  non-string refused, length cap, special chars replaced, Unicode
  neutralized, idempotent.
- `tests/test_no_lookahead.py` (5 tests): analyst-output-invariant
  (parametrized over both shipped analysts), advisor passes as_of,
  advisor as_of filters future bars from result, advisor deterministic
  under replay against two providers with different history depths.
- `tests/unit/test_heartbeat_monotonic.py` (6 tests): dead-man-switch
  fires on monotonic-only staleness, wall jump-backward doesn't extend
  grace, normal-case both clocks alive, synthetic heartbeat tracks
  monotonic, default-no-mock uses real `time.monotonic_ns`, age uses
  max(wall, monotonic).

**Test count: 330 → 365 (+35). All pass. Zero regressions.**

### Added — PDR feedback loop closes (settlement journal + microstructure analyst)

Continuation of the HITL Wave A work. This batch lands the LEARNING side
of the PDR loop: every approve/reject now persists to a markdown ledger
that the advisor reads back on the next call. The committee gets a second
voice (microstructure-lite). The advisor exposes `decision_price` as a
top-level field so the Reactor doesn't have to dig through analyst metadata.

**Wave B.1 — decision_price contract fix** (ADR-0014 amendment 2026-05-13):
- Advisor now exposes `decision_price` and `signal_id` at the top level
  of the result dict, sourced from `MarketContext.last_close`.
- `PaperReactor._extract_decision_price` reads top-level first, falls
  back to the old `analyst_views[0].metadata.last_close` path for
  forward-compat with already-stored proposals.

**Wave B.2 — settlement journal (ADR-0010)**:
- `hermes_quant/journal/{__init__.py, models.py, render.py, writer.py, reader.py}`
- `SettlementEntry` Pydantic model (with dataclass shim for installs
  without pydantic). HITL extension fields: `hitl_kind` ∈
  {approve, reject, expire}, `hitl_reason`, `hitl_approver`.
- `append_pending(entry)` — writes Phase-A entry via atomic rename
  (`.tmp` → fsync → rename). Crash-safe.
- `resolve(entry_id, ...)` — patches a pending entry with realized P&L
  + `Reflection(thesis_held, magnitude_error)` per ADR-0010 deterministic-v1.
- `append_human_override(proposal, kind, reason)` — Wave A integration:
  HITL approve/reject events render as journal entries even with no
  daemon running. Idempotent on `proposal_id` (re-render rather than
  duplicate).
- `get_recent_lessons(symbol, n_same, n_cross)` — recency-tail retrieval
  per ADR-0010 §7. NO embeddings, NO vector store (the explicit
  divergence from TradingAgents' removed `FinancialSituationMemory`).
- HTML-comment delimiters (`<!-- ENTRY_END -->` / `<!-- META_BEGIN -->`)
  so narrative bodies can use any markdown construct without colliding
  with separators.
- Cross-process serialization via flock on `.lock` sidecar; in-process
  via per-path RLock. Same pattern signal_bus uses.
- Pydantic-only writer surface — markdown is a render derivative.
  Hand-edits to the meta block silently lose entries (per ADR-0010 §8,
  this is a feature).

**Wave B.2b — quant_approve and quant_reject wire the journal**:
- `quant_reject` already routed through `append_human_override` (Wave A);
  with the writer now landed, rejections persist to disk.
- `quant_approve` ALSO appends to the journal, completing the operator
  audit trail. Both events go to the same file with `hitl_kind` tagged.
- Both paths degrade silently (`logger.debug` only) if the journal
  module isn't importable, so older deploys without the package keep
  working.

**Wave B.3 — MicrostructureLite analyst** (charter §"Layer 1 Analyst Pool"):
- `hermes_quant/analysts/microstructure.py` — second voice for BMA. Real
  microstructure (L2 imbalance, queue position, VPIN) requires a tick
  feed which v0.1.2 providers don't expose, so this is the OHLCV-derivable
  subset:
  - **Bollinger %B** mean-reversion (close < 5%/>95% bands)
  - **Trend quality** (Wilder's-ADX-lite: net move / sum of bar ranges)
    + bar imbalance for direction
  - **Order-flow toxicity proxy** (ATR regime + persistent bar-direction
    imbalance — VPIN approximation from bars alone)
- Composite vote with disagreement → silence (charter's "rewarded for
  correct inaction"). Cold-start calibration via `ColdStartCalibrator`.
- Advisor now auto-loads ClassicalTA + MicrostructureLite — BMA finally
  has multiple voices to aggregate.
- Per ADR-0002 + ADR-0009 §P0-2: confidence_raw preserved on AnalystView
  for calibrator training.

**Wave B.4 — PDR feedback loop closes** (advisor reads journal):
- `hermes_quant/advisor.py::_get_recent_lessons` already routed through
  `journal.reader.get_recent_lessons` (Wave A stub); with the reader now
  landed, the advisor surfaces real journal entries when called with
  `include_lessons=True` (the default).
- The full loop: HITL approve → journal entry → next `quant_recommend`
  query for that symbol pulls the entry into the `lessons[]` field for
  operator context. The same entries will feed v0.3.0 LLMAnalyst RAG
  per ADR-0012.

**Tests** (29 new, 330 total):
- `tests/unit/test_journal.py` (16 tests):
  - append_pending writes Phase-A; resolve patches Phase-B
  - Phase-B-on-resolved raises JournalEntryAlreadyResolved
  - missing entry raises JournalEntryNotFound
  - duplicate entry_id raises ValueError
  - render → parse round-trip preserves entries
  - empty / unparseable file edge cases
  - HTML-comment delimiter robustness (body markdown can include `---`/`##`)
  - corrupt file backup-and-recover
  - HITL append_human_override: approve / reject / idempotent on same id
  - get_recent_lessons: n_same + n_cross, newest-first, empty-journal,
    resolved-entry reflection
- `tests/unit/test_microstructure.py` (13 tests):
  - indicator math sanity (percent_b, atr_relative, trend_quality,
    bar_imbalance NaN/short-data behavior)
  - returns None on insufficient history
  - silence on pure chop (charter principle)
  - emits long on strong uptrend with bullish bar imbalance
  - metadata includes all sub-signal indicators
  - AnalystView shape conforms to Protocol (ADR-0002)
  - end-to-end: advisor.recommend uses MicrostructureLite + decision_price
    is top-level (Wave B.1 fix)

**Test count: 301 → 330 (+29). All pass. Zero regressions.**

The full PDR loop now closes: HITL approve → paper React → journal
entry → advisor reads journal on next query → operator sees prior
decisions and outcomes inline with new recommendations. The committee
has two voices. The settlement story is operator-readable. Everything
the v0.3.0 LLMAnalyst will RAG over already exists on disk in the
v0.1.2 ship.

### Added — HITL React surface (ADR-0015, PDR loop closes the loop)

Per user directive 2026-05-13 ("the trading guidance is HITL or automated,
not just guidance — this is part of the PDR pattern"), shipped Wave A of
the PDR React layer: chat-mode propose-approve-react that turns the
advisor surface into a real (paper-)trading loop with human in the loop.

**Architectural framing locked**: hermes-quant implements
**Perceive-Decide-React** (PDR), the same pattern used in the user's
Eidolon project (`/mnt/e/CS/HF/eidolon/pdr_lwm/environment.py`). We do
NOT fork Eidolon — we use the shape. The three modes are:
- **advise** — Perceive→Decide, no React (ADR-0014 advisor; one-shot guidance)
- **hitl** — Perceive→Decide→**human approves**→React (THIS RELEASE)
- **autonomous** — Perceive→Decide→silence-bias gate→React (v0.2)

**ADR-0015 — HITL propose-decide-react** (~32 KB):
- 12 decisions covering proposal lifecycle, dual-write storage
  (proposals.jsonl + SQLite index), proposal_id format, five new tools,
  Reactor Protocol contract, ExecutionRecord schema, mode resolution,
  calibrator-learn-from-rejections (the LEARNING property — config-gated
  via `quant.calibration.learn_from_rejections`), TTL handling with lazy
  expiration, paper-vs-live boundary (live gated to v0.2 with `--live`
  opt-in plus broker creds plus second confirm), full CLI + slash surface.
- Cites the founding charter (`docs/charter/2026-05-13-hermes-quant-charter.md`)
  for the silence-by-default principle and "rewarded for correct inaction"
  invariant.

**Founding charter saved** to repo:
- `docs/charter/2026-05-13-hermes-quant-charter.md` (~15 KB) — the
  architectural brief authored by the user that bootstrapped the entire
  project. Provenance for every PDR decision in the repo.

**Implementation:**
- `hermes_quant/proposals.py` — `ProposalStore` with JSONL+SQLite dual-write,
  three-state lifecycle (pending → approved/rejected/expired), thread-safe
  via RLock, cross-process via flock on JSONL. Lazy expiration on every
  read. Includes the canonical bug fix (ON CONFLICT clause was missing
  `expires_at` update — caught by audit trail test).
- `hermes_quant/react/{base.py,paper.py,__init__.py}` — Reactor Protocol +
  ExecutionRecord schema + concrete `PaperReactor` writing to the SAME
  executions.jsonl bus the daemon's settlement loop consumes (so HITL
  paper fills feed the same calibrator the autonomous mode will).
- `hermes_quant/tools.py` — five new handlers: `quant_propose`,
  `quant_approve`, `quant_reject`, `quant_pending`, `quant_proposal`.
  Mode gate (`_read_pdr_mode`) + calibrator-rejection-learning gate
  (`_read_learn_from_rejections`) read live config every call (no
  cache → operator can edit config + retry without restart).
- `hermes_quant/schemas.py` — `QUANT_PROPOSE`, `QUANT_APPROVE`,
  `QUANT_REJECT`, `QUANT_PENDING`, `QUANT_PROPOSAL` JSON schemas.
- `hermes_quant/cli/__init__.py` — `hermes quant {propose,approve,reject,
  pending,proposal}` subcommands with rich-formatted output and `--json`
  raw mode.
- `hermes_quant/__init__.py::register(ctx)` — registers all 5 new tools.
- `hermes_quant/tools.py::handle_quant_slash` — `/quant propose`,
  `/quant approve`, `/quant reject`, `/quant pending`, `/quant proposal`
  multiplexer entries.
- `plugin.yaml` — 5 new tools declared in `provides_tools` (now 10 total).

**Mode-mismatch UX:** `quant_propose` in advise-mode (default) returns a
clear `mode_mismatch` error with config snippet showing how to enable
HITL. CLI rich-output prints the YAML to add. Operators don't get
silently surprised.

**Advisor-gated guard:** `quant_propose` refuses to register a proposal
that the advisor itself gated (e.g. `no_bars_returned`,
`asset_class_unsupported`). Operators can still inspect via
`quant_recommend`; the proposal store stays clean.

**Tests** (14 new in `tests/integration/test_hitl_e2e.py`, 301 total):
1. propose → approve → execution written to bus
2. propose → reject with reason → rejection persisted
3. mode_mismatch error in advise mode (no proposal stored)
4. advisor_gated proposals refused (no bus pollution)
5. approve non-existent → not_found
6. approve already-approved → state_mismatch
7. reject without reason → reason_required
8. reject already-rejected → state_mismatch
9. TTL elapsed → pending auto-expires on read (lazy expiration)
10. approve expired → state_mismatch
11. list_pending filters by symbol + sweeps expired
12. audit trail — every transition appends a JSONL line with `_event`
+2 bonus: PaperReactor record shape; proposal_id format conformance

**Test count progression: 287 → 301 (+14). All pass. Zero regressions.**

### Added — chat-mode advisor surface (ADR-0014, advisor MVP)

Per user directive 2026-05-13 ("this is going to be a plugin that anyone using
Hermes could install and have hermes work on quant level stuff"), shipped the
**advisor surface** — a synchronous, in-process `quant_recommend` tool that
lets any Hermes user ask "what does the system say about AAPL?" without a
running daemon, without a broker API, without a portfolio.

**ADR-0013 — dual-surface integration stance** (`docs/adr/ADR-0013-…`):
- Locks the no-monkeypatch position for v0.1.2 (zero patches; every Hermes
  touchpoint via the public `register_tool` / `register_command` /
  `register_cli_command` / `register_hook` / `register_skill` contract).
- Documents the 5 LEVERAGE adoptions (profiles, credential pools, cron for
  ops tasks, MEMORY.md scope-separated from journal, CLI seams already
  correct) and 4 explicit rejections with rationale (cron for daemon,
  delegate_task for analysts, session_search for journal, filesystem
  checkpoints for daemon).

**ADR-0014 — chat-mode advisor surface** (`docs/adr/ADR-0014-…`):
- Specifies the `quant_recommend(symbol, …)` contract: read-only,
  synchronous, deterministic, safe under no-data, `as_of`-aware, no LLM
  in the chain (locked to v0.3.0 per ADR-0012), single-symbol in v0.1.2
  (multi-symbol post-portfolio rewrite).
- Full return-shape table with v0.1.2-populated vs reserved field markers.

**Implementation:**
- `hermes_quant/advisor.py` — synchronous `recommend(symbol, …) -> dict`
  orchestrator. Builds a synthetic flat Portfolio and conservative
  bootstrap MarketState (per ADR-0009 §P1-12 cold-start defaults), runs
  the same Analyst → BMAAggregator → DefaultRiskGate pipeline the
  daemon uses, returns structured dict. Handles RateLimitError /
  DataProviderError / DataQualityError as gated dicts (never raises
  to caller). Empty-bars guard prevents `.dt.tz` crash on degenerate
  fixtures.
- `hermes_quant/tools.py::quant_recommend` — tool handler wrapping
  `advisor.recommend()`, lazy-imports advisor to keep `register()` ≤50ms.
- `hermes_quant/schemas.py::QUANT_RECOMMEND` — JSON Schema with full
  parameter description (the description field is load-bearing for
  the model's tool-selection prompt).
- `hermes_quant/cli/__init__.py` — `hermes quant recommend SYMBOL`
  CLI subcommand with rich-formatted output (`_pretty_print_recommend`)
  and `--json` raw mode.
- `hermes_quant/__init__.py::register(ctx)` — registers `quant_recommend`
  as the 5th tool and updates `/quant` slash description.
- `hermes_quant/tools.py::handle_quant_slash` — `/quant recommend SYMBOL`
  + aliases `/quant rec` / `/quant advise`.
- `plugin.yaml` — `quant_recommend` declared in `provides_tools`.

**Tests** (14 new in `tests/integration/test_advisor_e2e.py`):
- `test_recommend_returns_structurally_valid_dict` — full return shape
- `test_recommend_with_empty_bars_returns_gated_no_exception` — safety
- `test_recommend_handles_provider_rate_limit` — rate-limit -> gated
- `test_recommend_handles_provider_data_error` — DataProviderError -> gated
- `test_recommend_does_not_call_calibrator_update` — read-only invariant
- `test_recommend_no_lessons_returns_empty_lessons_no_journal_io` — token saver
- `test_recommend_with_lessons_calls_journal` — opt-in works
- `test_recommend_deterministic_given_same_inputs` — replay-safe
- `test_recommend_unsupported_asset_class_returns_gated` — graceful crypto/fx
- `test_recommend_missing_symbol_handled_at_tool_layer` — empty string
- `test_recommend_emits_view_when_data_supports` — happy path
- `test_advisor_caveats_always_include_disclaimers` — caveats mandatory
- `test_quant_recommend_tool_handler_returns_json_string` — JSON convention
- `test_quant_recommend_tool_handler_parses_args` — arg forwarding

**Test count progression: 273 → 287 (+14).**

### v0.1.2 prep

- `docs/reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md` —
  full BUILD vs LEVERAGE vs MONKEYPATCH analysis of all 19 v0.1.2 work items
  against Hermes-core surface area. Net: zero monkeypatches, plan stands.

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
