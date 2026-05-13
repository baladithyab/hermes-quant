# Changelog

All notable changes to hermes-quant.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
