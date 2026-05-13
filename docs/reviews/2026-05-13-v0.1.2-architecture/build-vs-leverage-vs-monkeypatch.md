# hermes-quant v0.1.2+ — BUILD vs LEVERAGE vs MONKEYPATCH

**Date**: 2026-05-13
**Scope**: 19 v0.1.2 work items vs Hermes Agent core surface area
**Frame**: money-software discipline (ADR-0012 §"LLMs out of action path"); analysis-only, no new design

The v0.1.2 plan is dominated by trading-mechanics primitives that have no
analogue in Hermes Agent core (lookahead enforcement, portfolio
reconstruction, calibration gates, no-LLM signal pipeline). Hermes core's
durable systems — cron, kanban, delegation, sessions, memory, profiles,
credential pools — are general-purpose agent infrastructure that *bolts onto
the side of* the daemon, not into it. The action path stays code-first,
deterministic, replayable; Hermes-core surfaces stay on the operator-UX
side.

The classification below pins each item.

---

## Section 1 — BUILD (in hermes-quant code)

Items where Hermes-core has nothing equivalent and the plugin owns the
implementation.

### 1.1 Calibrator gate lift (entry+exit fill joining → `horizon_return` tag)
**Module**: `hermes_quant/daemon/settlement_loop.py` (`construct_realized_outcomes`, `dispatch_settlement`); `hermes_quant/calibrators.py` (Isotonic update path).
**Shape**: gate flag `calibration_quality: "slippage_only" | "horizon_return"` on each settlement record; lift requires (a) ADR-0011 portfolio rewrite landed and (b) exit-fill joined to entry signal via `signal_id`/`exec_id`. Today guarded by `confidence_correctness_pending=True` per Phase-8 P0-A.3.
**Why BUILD**: Hermes core has no concept of "calibrated probability". `BMAAggregator.update` is hermes-quant's own posterior, not an LLM token-level concern.

### 1.2 portfolio_loader rewrite (4 cases × buy/sell × long/short, 11-test fence)
**Module**: `hermes_quant/daemon/portfolio_loader.py`; tests at `tests/unit/daemon/test_portfolio_loader_reconstruction.py`.
**Shape**: dispatch on `(old_qty, signed_qty)` → cases (a)/(b)/(c)/(d) per ADR-0011 table; realized PnL formula `(fill - avg_old) * closed_qty * sign(old_qty)`; the 11 tests are the merge fence (gate-removal commit is separate per ADR-0011).
**Why BUILD**: Pure money-arithmetic. Hermes core doesn't track positions. SQLite WAL via `state.db::executions` is hermes-quant private state.

### 1.3 KronosAnalyst + KairosAnalyst (deterministic neural)
**Module**: `hermes_quant/analysts/kronos.py` (both classes) per AGENTS.md repo-layout note.
**Shape**: implements `Analyst` Protocol; `observe(MarketContext) -> AnalystView | None`; deterministic torch.inference_mode + fixed seeds + frozen weights.
**Why BUILD**: Domain-specific feature engineering. The `Analyst` Protocol is hermes-quant's contract (ADR-0002), not part of Hermes core.

### 1.4 Settlement journal — markdown writer + parser + retrieval helper
**Module**: new package `hermes_quant/journal/` — `models.py` (`SettlementEntry`, `Reflection`, `AnalystComponent` Pydantic), `writer.py` (`append_pending`, `resolve` with atomic-rename), `reader.py` (`get_recent_lessons(symbol, n_same, n_cross)`), `render.py` (private `_render(entry) -> str`).
**Shape**: per ADR-0010 — `~/.hermes/quant/journal.md`, HTML-comment delimiters, two-phase pending→resolved, no embeddings, atomic-rename on every mutation.
**Why BUILD**: Hermes core has MEMORY.md (auto-injected memory) but the *scope* is fundamentally different — the journal is a per-trade ledger keyed by `entry_id == signal_id`, the daemon writes it without ever reading it back (ADR-0010 §9 reproducibility constraint), and Phase-B resolution patches existing entries by id. MEMORY.md is free-form prose injected at LLM turn boundaries; it cannot do indexed two-phase patching. See §2.6 for "but we surface it via session_search anyway."

### 1.5 `as_of` parameter on `DataProvider` Protocol (lookahead enforcement)
**Module**: `hermes_quant/data/base.py` (`DataProvider` Protocol signature), `hermes_quant/data/yfinance_provider.py`, future `ccxt_provider.py`.
**Shape**: add `as_of: pd.Timestamp | None = None` to `fetch_bars`; leaf-level `df = df[df["timestamp"] <= as_of]` BEFORE return (per TradingAgents §1 / pattern #1 from research/04). Threads through `tick_loop.run_one_tick`.
**Why BUILD**: Lookahead-bias is a backtesting/replay concept; Hermes has no equivalent. The leaf-level enforcement is the load-bearing invariant.

### 1.6 `safe_symbol_component` (path safety)
**Module**: new `hermes_quant/utils/symbol_safety.py`.
**Shape**: whitelist regex, ≤32 chars, rejects `.`/`..`/whitespace/null bytes (TradingAgents pattern #2). Routes called from cache paths, JSONL filenames, log paths.
**Why BUILD**: Hermes core has no per-asset filesystem-path threading. The `BTC/USDT` slash + path-traversal class is hermes-quant-specific.

### 1.7 `test_no_lookahead.py` CI gate
**Module**: `tests/test_no_lookahead.py` running `shuffle_timestamps_test()` against every shipped analyst+aggregator (per AGENTS.md "No look-ahead bias" §).
**Why BUILD**: Domain test. Pairs with §1.5 — the `as_of` plumb is what makes the gate enforceable.

### 1.8 Monotonic heartbeat + halt-mirror staleness fallback
**Module**: `hermes_quant/daemon/heartbeat.py` (switch wall-clock → `time.monotonic()`), `hermes_quant/daemon/halt_state.py` (compare mirror mtime vs SQLite mtime, re-read SQLite on staleness; per Phase-8 P1-ε).
**Why BUILD**: Daemon-internal liveness contract. Hermes' own dead-man-switch is for the gateway, on a different SQLite file.

### 1.9 `trading_calendars` for `halt_until`
**Module**: `hermes_quant/risk/gate.py::_next_session_open` (replaces v0.1.1's `now+24h` band-aid per Phase-8 P1-δ).
**Why BUILD**: Specific to financial markets; Hermes has no sense of "session open."

### 1.10 yfinance OHLCV file cache (parquet, per-symbol)
**Module**: `hermes_quant/data/yfinance_provider.py` (cache layer), `hermes_quant/data/cache.py` (cache key derivation `{cache_dir}/{symbol}-{interval}-{start}-{end}.parquet` per TradingAgents pattern §P2; routes through `safe_symbol_component`).
**Why BUILD**: Provider-specific caching of OHLCV. Hermes' `enable_cache` toggle on `mcp_camoufox_browse` is browser caching, not market data.

### 1.11 ccxt provider + StackingAggregator
**Module**: `hermes_quant/data/ccxt_provider.py`; `hermes_quant/aggregators/stacking.py`.
**Why BUILD**: Pure trading domain. ADR-0005 / ADR-0003 define the contracts.

### 1.12 Per-symbol SQLite watermark store (resume idempotency)
**Module**: new `hermes_quant/daemon/watermark.py` (or column on `state.db::analyst_views`); records `(symbol, last_processed_bar_ts, indicator_snapshot_hash)` per TradingAgents pattern #3.
**Why BUILD**: Daemon-internal idempotency for the tick loop. Distinct from Hermes' kanban watermark — this is per-bar, not per-task. (See §2.4 for the kanban contrast.)

### 1.13 `BarSnapshot` Pydantic merge-friendly state model
**Module**: new `hermes_quant/state.py` or `hermes_quant/schemas.py` (extend existing `schemas.py`).
**Shape**: TypedDict-like Pydantic with named slots (`ohlcv`, `indicators`, `regime_label`, `signal_proposal`, `risk_check`, `final_decision`, `meta`); each pipeline stage returns its slot, daemon merges (per TradingAgents pattern #5).
**Why BUILD**: Replaces ad-hoc dicts in JSONL writers. Domain-specific state layout.

### 1.14 `quant_doctor` content-presence DaemonState mirror
**Module**: `hermes_quant/tools.py::quant_doctor` (today read-only ad-hoc; rewrite to derive status from `BarSnapshot` slot presence per TradingAgents pattern #6).
**Why BUILD**: Tool implementation — registered via `ctx.register_tool(name="quant_doctor")` already; only the body changes.

### 1.15 Autouse dummy-keys conftest
**Module**: `tests/conftest.py` autouse fixture setting placeholder env vars for CCXT exchanges, OpenRouter, Telegram, etc. (TradingAgents pattern #8).
**Why BUILD**: Plugin-local CI hygiene. Hermes core has its own conftest but doesn't know about ccxt/exchange creds.

### 1.16 Alpha-vs-benchmark return at settlement
**Module**: `hermes_quant/daemon/settlement_loop.py::construct_realized_outcomes` (compute `alpha_return = raw_return − benchmark_return`; benchmark = `BTC/USDT` for crypto, `SPY` for US equities); `journal/models.py::SettlementEntry.alpha_return` already in ADR-0010 schema.
**Why BUILD**: Domain math. Trivial once §1.4 schema lands.

### 1.17 `get_recent_lessons` retrieval (no embeddings)
**Module**: `hermes_quant/journal/reader.py::get_recent_lessons(symbol, n_same=5, n_cross=3)`.
**Shape**: parse journal on `<!-- ENTRY_END -->`, sort by `asof_decision`, return last `n_same` entries for `symbol` + last `n_cross` reflection-only entries from any other symbol (TradingAgents pattern #10; ADR-0010 §7).
**Why BUILD**: Bound to ADR-0010's schema. Hermes' `session_search` toolset searches conversation history, not a structured-Pydantic file. (See §2.6 for the indexing complement.)

### 1.18 `VENDOR_METHODS` 2D dispatch + per-method override config
**Module**: `hermes_quant/data/base.py` extending `fetch_with_chain` to a `(method, vendor)` 2D table; `~/.hermes/config.yaml::quant.data.vendor_methods` override per ADR-0005.
**Why BUILD**: Provider-routing matrix is hermes-quant-private. Hermes' provider system is for LLM models, not market-data vendors.

### 1.19 Env-vars for paths only, not behavior
**Modules**: audit pass on every `os.getenv("HERMES_QUANT_*")` site; behavioral toggles must come from `~/.hermes/config.yaml::quant.*` instead.
**Why BUILD**: Plugin-local convention. Hermes' own config-vs-env discipline (`hermes config set` for behavior, `.env` for credentials) is the reference, but the cleanup is in the plugin's source.

---

## Section 2 — LEVERAGE (use Hermes-core as-is)

Items / facets where Hermes-core machinery is already the right answer.
Adopting it shrinks plugin LOC and gives operators uniform UX.

### 2.1 Daemon scheduling — KEEP systemd, do NOT use Hermes cron

**Hermes provides**: `cron/jobs.py` + `cron/scheduler.py` with schedules
`'30m'`, `'every 2h'`, `'0 9 * * *'`, ISO timestamps; per-job `script`,
`no_agent`, `context_from`, `workdir`; 3-min hard interrupt; `.tick.lock`
prevents duplicate ticks across processes.

**hermes-quant currently uses**: ADR-0007 §canonical-CLI mandates
`hermes quant start` writes a systemd unit + enables + starts.

**Recommendation**: **KEEP systemd. Reject Hermes-cron substitution.**

| Trade-off | systemd (current) | Hermes cron |
|---|---|---|
| Liveness contract | hard — systemd restarts on crash | best-effort — scheduler is itself a Hermes process |
| Lock semantics | `DaemonLock` flock + PID file owned by daemon | `.tick.lock` owned by Hermes scheduler; concurrent Hermes restarts could double-tick |
| Tick interval | continuous loop (60s sleep, in-process state) | discrete invocation; `BMAAggregator` posteriors would need re-hydration each fire |
| Heartbeat | monotonic in-process | each cron fire is a fresh process — heartbeat-as-liveness becomes meaningless |
| Hard 3-min interrupt | n/a | a slow yfinance fetch on a busy 1m-bar ticker would silently truncate the tick |
| `--max-concurrent-children` parity | the daemon IS one process | cron's `kanban.dispatch_in_gateway` interaction is a second concurrency surface |

The 3-minute hard-interrupt is the dealbreaker. A money-software tick that
gets killed mid-`add_halt` SQLite transaction is exactly the silent-corruption
class ADR-0011's `NotImplementedError` gate exists to prevent. systemd's
"the daemon owns its own lifecycle" model matches the discipline.

**However**: Hermes cron IS the right answer for the *operator-facing*
support tasks that should run alongside the daemon — see §2.2.

### 2.2 Operator-facing scheduled tasks → Hermes cron

These are good fits for `hermes cron create`:
- **Daily journal digest**: `hermes cron create '0 22 * * *' --prompt "Summarize today's hermes-quant journal entries from ~/.hermes/quant/journal.md"`. Reads `journal.md` (read-only — passes ADR-0010 §9), produces a Discord/email message via cron's delivery framing. No daemon coupling.
- **Weekly drift audit**: `hermes cron create 'every monday 9am' --skills hermes-quant --prompt "Run /quant doctor and report any drift > 10pp"`. Pure read-side; uses already-registered `quant_doctor` tool.
- **Settlement-journal rotation pre-warning**: cron job that counts journal entries and warns if approaching `journal_max_entries`.

**Hook**: zero plumbing — these are just `hermes cron create` invocations a v0.1.2 README/skill section recommends.

### 2.3 Daemon process supervision — `register_cli_command` already correct

ADR-0007 + `hermes_quant/__init__.py::register()` already do the right
thing: `ctx.register_cli_command(name="quant", setup_fn=quant_cli.setup_argparse, handler_fn=quant_cli.dispatch)`.
The CLI subcommand tree (`hermes quant start|stop|restart|status|doctor`)
is the supported control plane. No change needed.

### 2.4 Backtest replay / training tasks → Hermes kanban (defer to v0.2)

**Hermes provides**: SQLite kanban at `~/.hermes/kanban.db`; worker toolset
`kanban_show, kanban_complete, kanban_block, kanban_heartbeat,
kanban_comment, kanban_create, kanban_link`; gateway-resident dispatcher
spawns workers per assigned profile; auto-blocks after ~5 spawn failures.

**v0.1.2 fit**: weak. v0.1.2's "backtest replay" is not yet a multi-task
parallel work queue — it's a single replay-from-`signals.jsonl` against a
freqtrade backtester. One process, one symbol-set, one timeframe.

**v0.2 fit**: strong. When hermes-quant adds:
- multi-symbol parallel parameter sweeps (sweep RSI window across BTC/ETH/SOL),
- RL training episodes (each episode = one kanban task),
- nightly cross-family aggregator A/B (each ensemble candidate = one task),

then kanban is the durable durable work-queue. Each task carries
`(symbol, start_ts, end_ts, aggregator_config_hash)`; workers spawn
under hermes-quant profile; reclaim-after-5-fails matches the
"silence-by-default" discipline.

**Recommendation**: **defer to v0.2** when the workload shape exists.
v0.1.2 is too early — putting a single-replay job on the board adds
ceremony without payoff. File `docs/v0.2/kanban-backtest-shape.md` as the
forward-looking doc.

### 2.5 Parallel analysts via `delegate_task` — REJECT

**Why considered**: `delegate_task(tasks=[...])` runs children in parallel
up to `delegation.max_concurrent_children`. Could spawn one subagent per
analyst.

**Why REJECT**:
1. `delegate_task` is LLM-mediated. Per ADR-0012 the action path is
   **zero LLM invocations**. Putting an LLM in front of Kronos's
   `torch.inference_mode()` adds a non-deterministic, prompt-injectable
   layer in front of a deterministic neural — strictly worse.
2. The analysts are deterministic Python functions taking ~10ms each.
   Async + `concurrent.futures.ThreadPoolExecutor` (in-process) is the
   right primitive. We don't need subagent overhead.
3. `delegate_task` children are non-durable (parent interrupt = child
   cancelled per SKILL.md §Delegation). Tick-loop interruption mid-analyst
   would lose the partial AnalystView. In-process loops are interrupt-safe
   via the daemon's existing `_SHUTDOWN` flag.

**Use `delegate_task` for**: cross-family aggregator review during a v0.2
parameter-sweep night, when each child is a *full* tick-loop replay over
historical bars and the synchronous-then-summary semantics fit. NOT for
the per-tick analyst pool.

### 2.6 Settlement journal indexing → `session_search` toolset

**Hermes provides**: `session_search` toolset searches past conversations.

**hermes-quant adjacent need**: ADR-0010 §7 explicitly forbids embeddings;
`get_recent_lessons` is a flat tail-N. But operators DO want "find me last
3 LONGs on BTC where the BMA confidence was >0.7" — that's a real query.

**Recommendation**: **build §1.17 unchanged. Do NOT plumb the journal
through Hermes' session_search.**

Reasons:
- `session_search` indexes Hermes session SQLite; the journal is its own
  file, off-tree.
- Indexing the journal would create the exact ChromaDB-class regression
  ADR-0010 explicitly avoids (TradingAgents removed `FinancialSituationMemory`
  for this reason).
- The flat tail-N retrieval suffices for v0.3.0 LLM-analyst RAG.

If a v0.3.0 LLM analyst later wants richer queries, ADR-0010 §rationale
describes the upgrade path (derived index next to the journal, NOT inside
it). Don't build that today.

### 2.7 Trading accounts as Hermes profiles — STRONG fit

**Hermes provides**: `~/.hermes/profiles/<name>/` with isolated
`config.yaml`, `sessions/`, `skills/`, `memory/`. CLI: `hermes profile
create paper-binance --clone-from main`; `hermes profile use paper-binance`.

**hermes-quant fit**: each trading account is naturally a profile.
- `paper-binance` profile → `quant.data.exchange: binance`, `quant.daemon.account: paper-binance`, `~/.hermes/profiles/paper-binance/quant/state.db` isolation.
- `live-alpaca` profile → live API keys in profile's `.env`, separate `state.db`, separate `signals.jsonl`, separate `journal.md`.
- `backtest-2024-q4` profile → frozen historical config, clone-and-replay.

The `DaemonLock(account_id=args.account)` already partitions on account;
`hermes-quant-daemon --account paper-binance` running under
`hermes --profile paper-binance` is the natural decomposition.

**Recommendation**: **adopt as a documented pattern in v0.1.2 README.**
Plugin code change: zero — `register_cli_command`'s handler already sees
the active profile via Hermes' profile-aware path resolution. The win is
operator UX: "spin up a new paper account" becomes
`hermes profile create paper-binance --clone-from main && hermes --profile paper-binance quant start`.

ADR-0007's `--use-profile` flag aliasing already avoids the global
`--profile` collision per plugin-authoring.md `★ GOTCHA: --profile collides`.

### 2.8 Credential pools for data-provider keys

**Hermes provides**: `hermes auth add` rotates across multiple API keys
per provider when one is exhausted.

**hermes-quant fit**: yfinance is keyless, but **ccxt** (binance, coinbase),
**alpaca**, future **polygon.io**/**twelvedata** all rate-limit and benefit
from rotation. `~/.hermes/auth.json` already handles the pooling logic.

**Recommendation**: when §1.11 ccxt provider lands, **use Hermes'
`auth.json` pool reader** rather than rolling a per-provider rotation.
Concretely: `hermes_quant.data.ccxt_provider.CcxtProvider.__init__` pulls
keys via Hermes' `agent/credential_pool.py` API (or its public entry
point). Caveat: confirm the pool API is import-stable for plugins; if it
is internal-only, build a one-pass cache reader against `auth.json`'s
documented schema.

### 2.9 Webhooks for broker callbacks — POTENTIAL v0.2 fit

**Hermes provides**: `gateway/webhooks` with `/webhooks/<name>` routing,
`hermes webhook subscribe` CLI.

**Adjacent need**: brokers / exchanges that POST fill-confirm callbacks
(coinbase WebSocket-fill, alpaca trade events) → today the daemon polls
`executions.jsonl` (written by freqtrade). A push-driven path could shave
100-1000ms off `last_settled_record_count` recomputation.

**Recommendation**: **defer to v0.2.** v0.1.2's freqtrade-side polling is
correct and doesn't need this. When alpaca-direct mode lands (no
freqtrade), webhooks become attractive: `hermes webhook subscribe
alpaca-fills` routing JSON onto `executions.jsonl` via a small
write-through handler.

### 2.10 Secret redaction on tool output → leave OFF

**Hermes provides**: `security.redact_secrets` (off by default; toggling
needs Hermes restart per SKILL.md §Secret-redaction).

**hermes-quant impact**: low. `quant_status`, `quant_show_signals`,
`quant_show_views`, `quant_doctor` all return structured data, no API
keys or secrets. Turning redaction on globally would add latency/false-
positive risk (redacting decimal numbers that look like keys) without
benefit.

**Recommendation**: **don't recommend secret-redaction in the README.**
If an operator turns it on for unrelated reasons, the plugin tools tolerate
it (no key strings in output); document in §troubleshooting only.

### 2.11 `MEMORY.md` / `USER.md` auto-injection — NOT the journal

**Hermes provides**: `~/.hermes/MEMORY.md` + `USER.md` injected into
every LLM turn.

**Settlement journal scope contrast** (per task brief): the settlement
journal is a per-trade ledger (entry_id-keyed, two-phase, atomic-rename,
schema-validated Pydantic). MEMORY.md is operator-prose memory injected
at LLM turn boundaries.

**Recommendation**: **separate concerns; don't merge.** The daemon must
not depend on MEMORY.md (ADR-0010 §9 reproducibility). However, a v0.3.0
LLM-analyst could legitimately pull `get_recent_lessons` output into its
own prompt context — that's the documented seam. If an operator wants the
journal's last few entries surfaced into chat memory, document the
one-liner cron job that appends a digest to MEMORY.md (per §2.2).

### 2.12 Filesystem checkpoints (`/rollback`) — NOT for trading state

**Hermes provides**: filesystem checkpoints, `/rollback` slash.

**hermes-quant impact**: dangerous if naively used. Rolling back the
daemon's working tree mid-trade would not roll back `state.db` (SQLite
WAL is not in the checkpoint), `signals.jsonl` (append-only), or broker
state. The system would silently desync.

**Recommendation**: **document in AGENTS.md "do NOT enable
`--checkpoints` when running `hermes quant start`."** Tag in a v0.1.2
quant_doctor warning: if `~/.hermes/checkpoints/` is non-empty, the
daemon emits a warning at startup.

---

## Section 3 — MONKEYPATCH (install-time Hermes-core changes)

Per `references/plugin-as-monkeypatch.md` template (`apply()` / `revert()`
/ `is_applied()` triple, marker on target module, no raise from
`apply()`).

**Surveyed**: every Hermes-core surface listed in the task brief
(PluginContext methods, hooks, toolsets, cron, kanban, delegation, session
store, memory, MCP, redaction, checkpoints, webhooks, profiles,
credential pools).

**Findings**: zero monkeypatch candidates emerge from the v0.1.2 plan as
written. Justification per surface:

### 3.1 Why no patches are required for the v0.1.2 set

The 19 v0.1.2 items split into:
- **In-plugin pure code** (1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11, 1.12, 1.13, 1.14, 1.15, 1.16, 1.17, 1.18, 1.19) — 18 items; nothing in Hermes-core to alter.
- **Settlement journal** (1.4) — net-new file under `~/.hermes/quant/`, parallel to (not inside) Hermes session SQLite.

Every Hermes-core surface the plugin touches it touches *via the public
plugin contract*: `register_tool`, `register_command`, `register_cli_command`,
`register_hook(pre_gateway_dispatch)`, `register_skill`. None of these need
behavior modification.

The two adjacent surfaces where a patch *might* tempt:
- **`cron/jobs.py`**: if hermes-quant wanted to run the daemon as a cron job
  with a >3-min tick interval, the 3-min hard-interrupt would have to be
  bypassed. **Don't.** The right answer is "don't use cron for the daemon"
  (§2.1). A patch to bypass the interrupt would weaken Hermes core's
  liveness guarantee for every other cron user — wrong scope per
  `plugin-as-monkeypatch.md` §"When NOT to reach for monkeypatch."
- **`hermes_state.SessionDB`**: the daemon does NOT touch session state
  (per AGENTS.md §"No editing of Hermes core SQLite tables"). No patch
  needed.

### 3.2 When a future patch WOULD be the right shape

Concrete examples for v0.2+ (not in v0.1.2 scope, listed for forward
visibility):

- **If** Hermes' `kanban.dispatch_in_gateway` lacks per-task model
  overrides for hermes-quant backtest workers, that's the route-fidelity
  class — `mc_bootstrap/route_fidelity.py` is the prior art. A monkeypatch
  plugin (`hermes-quant-bootstrap`) with one patch (`route_fidelity_for_quant`)
  would be appropriate.
- **If** Hermes' `pre_gateway_dispatch` hook ordering ever gets reshuffled
  upstream and the deferred `/quant` install (`discord_slash.py`) breaks,
  the fix is a patch on the dispatch ordering, not a fork of the gateway.

Both are speculative. No concrete defect in v0.1.2 forces either.

### 3.3 When monkeypatch would be wrong (and what to do instead)

Per `plugin-as-monkeypatch.md` §"When NOT to reach for monkeypatch":

| Hypothetical change | Why monkeypatch is wrong | Right answer |
|---|---|---|
| Add a new hook (e.g. `pre_signal_emit`) | Schema change to `PluginContext` interface | Upstream PR to hermes-agent |
| New SQLite columns on Hermes session table | Data-model schema change | Upstream PR or fork |
| Replace `cron/jobs.py` 3-min interrupt with configurable timeout | Touches >50 LOC; behavior-critical for every cron user | Upstream PR |
| Change `~/.hermes/MEMORY.md` injection cadence | Actively-evolving code; high refactor risk | Upstream PR or accept upstream behavior |
| Make `register_cli_command` accept async setup_fn | Interface change | Upstream PR |

The pattern is: **plugin-local additions go in plugin code, plugin-local
behavior tweaks of stable functions go in monkeypatch plugins, anything
that changes Hermes-core's contract goes upstream.**

### 3.4 Recommended footing

Carry **zero monkeypatches** into v0.1.2. Document this section in
`docs/adr/ADR-0013-hermes-core-integration-stance.md` (proposed status,
v0.1.2 target) so the next contributor doesn't unconsciously reach for
the pattern. If a real defect emerges during v0.1.2 dogfooding, add the
patch at that point per the prior-art template (`hermes-mission-control/
plugins/mission-control-bootstrap/mc_bootstrap/`).

---

## Section 4 — Open questions / decisions to lock in pre-v0.1.2

### 4.1 Should the systemd unit live under a Hermes profile root?

**Question**: ADR-0007 says `hermes quant start` writes a systemd unit;
unclear if the unit's `ExecStart=hermes-quant-daemon --account X` should
also pass `--profile <hermes-profile>` to align with §2.7's "trading
account = Hermes profile" recommendation.

**Recommended default**: **yes, pass `--profile` via the
unit's `Environment=HERMES_PROFILE=<name>`.** The daemon then resolves
`~/.hermes/profiles/<name>/config.yaml::quant.*` and writes
`~/.hermes/profiles/<name>/quant/{state.db, signals.jsonl, journal.md}`.

**Rationale**: makes account-isolation a first-class invariant, matches
how Hermes profiles already isolate other state, prevents cross-account
ledger contamination on multi-account hosts.

**Would change recommendation if**: profile-aware path resolution turns
out to be slow or fragile (>50ms init) — in that case, isolate via
explicit `HERMES_QUANT_HOME=/path/to/per-account/dir` env var instead
(§1.19 says env-vars-for-paths is fine; this is a path).

### 4.2 Where does the journal sit when a profile is active?

**Question**: `~/.hermes/quant/journal.md` (global) vs
`~/.hermes/profiles/<name>/quant/journal.md` (profile-local).

**Recommended default**: **profile-local.** ADR-0010 says
`HERMES_QUANT_JOURNAL_PATH` is the override; default to the profile-aware
path when a profile is active, fall back to `~/.hermes/quant/journal.md`
otherwise.

**Rationale**: a `live-alpaca` journal merging with a `paper-binance`
journal is a category error. The atomic-rename writer would then race
across two daemon instances writing the same file (each daemon thinks
it's the only writer). Profile isolation cleanly prevents this.

**Would change recommendation if**: operators want a unified cross-
account view. Solution: §2.2 cron job that reads all profiles' journals
and renders a digest into a single read-only file.

### 4.3 Does v0.1.2 need a `quant.daemon.tick_interval` config or stay CLI-flag-only?

**Question**: today `--tick-interval 60` is CLI-only. Migrating to
`~/.hermes/config.yaml::quant.daemon.tick_interval` aligns with §1.19's
"behavior in config, not env."

**Recommended default**: **add config support, keep CLI flag as override**
for v0.1.2. Order: `--tick-interval` > `quant.daemon.tick_interval` >
default (60).

**Rationale**: ADR-0007 anticipates this ("v0.1.1 hardcoded; v0.1.2 reads
config"). The dual-path (CLI override > config > default) is what every
other Hermes plugin does; consistency with operator muscle memory.

**Would change recommendation if**: there's a use case for changing the
tick interval at runtime without daemon restart. Then we'd need a
`quant_set_tick_interval` tool — but that's an actuator-side concern
forbidden by the AGENTS.md "Money never goes through tools" rule. Verdict:
config is read at daemon start, not hot-reloaded.

### 4.4 Should `quant_doctor` warnings about Hermes-core misuse fail or pass?

**Question**: §2.12 recommends `quant_doctor` warns if filesystem
checkpoints are enabled. Should it WARN (yellow) or FAIL (red)?

**Recommended default**: **WARN, not FAIL.** Operators may have
checkpoints on for unrelated work in the same Hermes session — failing
the doctor would create a class of "false red" reports that erode
operator trust.

**Rationale**: money-software discipline says fail loud on real defects
(NotImplementedError on portfolio flips). Hermes-core configuration
collisions are operator-environment issues, not daemon defects. The
silence-by-default principle applies: when the daemon can't tell whether
checkpoints affect it, surface the info, don't crash.

**Would change recommendation if**: a real corruption is observed when
checkpoints + daemon are both active. Then upgrade to FAIL on the same
condition that produced the corruption (e.g. checkpoint window straddles
a `state.db` write).

### 4.5 Does the journal `get_recent_lessons` output get registered as a Hermes tool?

**Question**: §1.17 builds `get_recent_lessons`. Should hermes-quant
register it as `quant_show_lessons` (read-only tool, §AGENTS.md compliant)
so chat-mode operators can ask "show me last 3 BTC reflections" without
shelling out to a CLI?

**Recommended default**: **yes, register as `quant_show_lessons` in
v0.1.2.** Read-only, structured-dict return, fits the existing tool
roster.

**Rationale**: the cost is one `register_tool` call + one schema entry +
one handler that wraps `journal.reader.get_recent_lessons`. The benefit
is operators can use the journal during a debugging session without
context-switching to a terminal. ADR-0010 §6 explicitly supports the
forward-direction "journal → LLM-analyst RAG" path.

**Would change recommendation if**: tool-output budget per turn becomes
an issue (each entry can be ~1-2KB). Then default to `n_same=3, n_cross=2`
for the tool surface but keep the library defaults at `n_same=5, n_cross=3`.

### 4.6 Does v0.1.2 introduce a `hermes-quant-bootstrap` monkeypatch plugin shell?

**Question**: §3.4 recommends zero patches for v0.1.2. Should a
no-op-by-default `hermes-quant-bootstrap` plugin be scaffolded *now* so
that future patches have a home?

**Recommended default**: **no, defer until first concrete patch is
needed.** Empty plugins age poorly; the first patch is also when the
plugin gets the right shape.

**Rationale**: per `plugin-as-monkeypatch.md` §prior-art, even
mission-control-bootstrap is prior art *because* it has two real patches.
Scaffolding before there's a patch creates a maintenance object with no
testable surface and invites premature patching.

**Would change recommendation if**: dogfooding v0.1.2 surfaces ≥2
concrete defects in Hermes-core that genuinely need patching. Then build
the multi-patch shell from the start, copying the `mc_bootstrap`
template directly.

---

## Summary

| Section | Item count | Net effect on v0.1.2 plan |
|---|---|---|
| BUILD | 19 / 19 v0.1.2 work items | Plan stands as-is; in-plugin scope |
| LEVERAGE | 5 strong adoptions (CLI seams already wired, profiles, credential pools, cron for ops tasks, MEMORY.md scope-separated), 4 explicit rejections (cron for daemon, delegate_task for analysts, session_search for journal, checkpoints for daemon), 3 deferrals (kanban → v0.2, webhooks → v0.2, MCP server → unused) | Adds documentation tasks (profile pattern, cron ops jobs, ADR-0013 stance) but no LOC |
| MONKEYPATCH | 0 patches | File ADR-0013 to lock the no-patches stance for v0.1.2 |
| Open questions | 6 | All have recommended defaults; none gate v0.1.2 if accepted |

The v0.1.2 work split is overwhelmingly trading-domain primitives that
have no Hermes-core analogue. The discipline win is in §2 — adopting
profiles for trading accounts, recommending Hermes cron for digest/audit
ops tasks, and explicitly rejecting `cron-for-daemon` /
`delegate_task-for-analysts` / `session_search-for-journal` so the
boundary stays clean. The plan as currently scoped does not require any
Hermes-core monkeypatches; `mission-control-bootstrap` remains the
template if a defect emerges during dogfooding.
