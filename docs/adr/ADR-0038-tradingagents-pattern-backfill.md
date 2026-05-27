# ADR-0038 — TradingAgents Pattern Backfill (P3 / P5 / P6 / P8 / P11 / P12)

**Status**: proposed
**Date**: 2026-05-26
**Wave**: D (post-Wave-C wire-up of multi-timeframe + LLM committee)
**Supersedes**: nothing
**Amends**: nothing
**Cites**: `docs/research/04-tradingagents-comparison.md` §"Patterns I missed in v1 (added 2026-05-13 second pass)"

## Context

The 2026-05-13 deepwiki second pass on `TauricResearch/TradingAgents` enumerated
17 patterns worth evaluating. As of 2026-05-26 (HEAD `907b8c2`):

- **7 ported / shipped**: P1 look-ahead `as_of` (in `as_of_decision`), P2
  `safe_symbol_component` (utils path safety), P9a alpha-vs-benchmark return
  (settlement loop), P10 `get_recent_lessons` (journal reader), P13
  env-vars-for-paths-only (audited), P17 Pydantic→markdown render (in
  `journal/render.py:_render_entry`), and the original v1 P0 yf_retry().
- **4 NOT-APPLICABLE by design**: P4 `ConditionalLogic` (no DAG), P14 bulk
  indicator amortization (rolling pandas already amortizes), P15
  `invoke_structured_or_freetext` (Pydantic strict mode in committee turns),
  P16 `SignalProcessor` markdown extractor (we read typed fields).
- **4 deferred to v0.3.0 LLM-analyst surface**: P7 two-tier LLM split, P9b
  LLM reflection text, P14 (if tool surface added), P15 (paired with P7).
- **6 still missing and worth landing now**: P3, P5, P6, P8, P11, P12.

These six are all small, composable, and mutually independent. They earn
their place by hardening *daemon-internal* discipline (idempotency, schema,
diagnostics, CI hygiene, vendor routing) rather than adding new strategy
surface — the kind of work that prevents future regressions rather than
moving the trading frontier. Wave D backfills them.

## Decision

Land six narrowly-scoped patterns in a single wave, with one ADR-anchored
contract per pattern, no new dependencies, and complete test coverage.

### D.1 — P3: Per-symbol watermark store (resume idempotency)

**Module**: `hermes_quant/daemon/watermark.py` (new).

**Shape**:

```python
@dataclass(frozen=True, slots=True)
class Watermark:
    symbol: str
    last_processed_bar_ts: pd.Timestamp  # tz-naive UTC, exclusive upper bound
    indicator_snapshot_hash: str          # 16-hex-char prefix of sha256
    updated_at: pd.Timestamp              # monotonic clock anchor

class WatermarkStore:
    """SQLite-backed (symbol -> Watermark). Writes are atomic via WAL."""
    def __init__(self, path: Path | None = None) -> None: ...
    def get(self, symbol: str) -> Watermark | None: ...
    def set(self, wm: Watermark) -> None: ...
    def all_for_symbols(self, symbols: list[str]) -> dict[str, Watermark]: ...
```

**Storage**: single SQLite at `~/.hermes/quant/watermarks.db`
(profile-aware via `_resolve_profile_path`, same as `state.db`). Schema:

```sql
CREATE TABLE IF NOT EXISTS watermark (
    symbol TEXT PRIMARY KEY,
    last_processed_bar_ts TEXT NOT NULL,  -- ISO 8601, tz-naive UTC
    indicator_snapshot_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;
```

**Integration**: `tick_loop.run_one_tick` calls `wm = store.get(symbol)`
before calling `analyst.analyze(ctx)`. If `wm` exists AND
`ctx.bar_ts <= wm.last_processed_bar_ts`, skip emit. After successful gate
emit, write `Watermark(symbol, ctx.bar_ts, sha256(ctx)[:16], now())`.

**Invariant**: watermark write happens **after** `signal_bus.emit()` returns
(i.e., the bar_ts has been durably journaled). On crash mid-tick, we may
re-emit the same `(symbol, bar_ts)` once; downstream consumers (freqtrade,
journal) idempotency-key on `signal_id` already.

**NOT a SqliteSaver wrapper** — TradingAgents uses LangGraph's
`SqliteSaver` to checkpoint a multi-LLM DAG mid-execution. We don't have a
DAG. Our watermark is a flat key-value store; one row per symbol; no
mid-tick state to resume.

**Tests**: 12+ unit tests covering: empty store, set+get round-trip,
duplicate-symbol overwrite, multi-symbol batch read, profile isolation,
malformed timestamp recovery, bar_ts comparison logic, hash-mismatch
warning path.

### D.2 — P5: BarSnapshot Pydantic state model

**Module**: `hermes_quant/schemas/bar_snapshot.py` (new).

**Shape**:

```python
class BarSnapshot(BaseModel):
    """Per-bar pipeline state. Each pipeline stage populates its slot.

    Used as the JSONL row schema for tick_loop emit and as the in-memory
    state passed between analysts/aggregator/risk-gate.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    symbol: str
    bar_ts: datetime  # tz-naive UTC
    asof_decision: datetime  # when the gate decided

    # Slots populated in pipeline order. None = stage didn't run.
    ohlcv: OHLCVSlot | None = None
    indicators: IndicatorsSlot | None = None
    regime_label: str | None = None
    analyst_views: list[AnalystViewSlot] | None = None
    aggregated_signal: AggregatedSignalSlot | None = None
    risk_check: RiskCheckSlot | None = None
    final_decision: FinalDecisionSlot | None = None
    meta: MetaSlot
```

**Constraint — bit-identical legacy path**: today's JSONL writers emit
ad-hoc dicts. Wave D introduces `BarSnapshot` as the **internal** state
model with a `to_jsonl_row()` method that produces the existing dict shape
for backward compat. Emitting the new shape is **opt-in via
`HERMES_QUANT_SNAPSHOT_V2=1`** for one release cycle, then default-on in
v0.4.

**Adapters**: `BarSnapshot.from_market_context(ctx, views, signal, action)`
constructor builds the model from existing types; no breaking change to
`Analyst`/`Aggregator`/`RiskGate` Protocol signatures.

**Tests**: 15+ unit tests: schema round-trip, slot independence, frozen
guarantee, missing-meta rejection, JSONL parity with legacy dicts under
`HERMES_QUANT_SNAPSHOT_V2=0`, opt-in shape under `=1`.

### D.3 — P6: quant_doctor content-presence DaemonState mirror

**Module**: amend `hermes_quant/tools.py::quant_doctor` (existing tool).

**Shape**:

```python
class DaemonState(BaseModel):
    """Read-only mirror reconstructed from JSONL bus + halt registry.

    Populated by reading the last N rows of signals.jsonl per-symbol
    and inferring stage status from BarSnapshot slot presence.
    """
    per_symbol: dict[str, SymbolStatus]
    halts: list[HaltSummary]
    last_heartbeat_age_s: float
    journal_pending_count: int

class SymbolStatus(BaseModel):
    last_bar_ts: datetime | None
    stages_seen: list[Stage]  # Literal["ohlcv","indicators","analysts","aggregated","risk","final"]
    last_action_dir: int | None
    last_action_conf: float | None
```

**Inference rule**: for each symbol, walk last 10 JSONL rows; mark stage
`X` as "seen" iff `BarSnapshot.X is not None` in any row. Dedup events on
`(symbol, bar_ts)` via in-method `_seen_event_ids: set` (matches
TradingAgents `_processed_message_ids` pattern).

**Output**: `quant_doctor` returns the `DaemonState.model_dump()` dict,
which Hermes' tool layer renders. Content is opaque to the daemon; this is
read-only diagnostic surface only.

**Tests**: 8+ unit tests covering: empty bus, partial-stage progression,
multi-symbol independence, dedup correctness, stale-bar timeout, halt
mirror, journal pending count.

### D.4 — P8: Autouse dummy keys + structured-output mocks

**Module**: amend `tests/conftest.py` with a second autouse fixture.

**Shape**:

```python
@pytest.fixture(autouse=True)
def _autouse_dummy_third_party_keys(monkeypatch):
    """Inject placeholder env vars for every third-party SDK so CI never
    blocks on missing creds. Real tests that need real creds opt out by
    overriding via their own monkeypatch.setenv() calls.
    """
    placeholders = {
        # LLM providers
        "OPENROUTER_API_KEY": "test-placeholder",
        "ANTHROPIC_API_KEY": "test-placeholder",
        "OPENAI_API_KEY": "test-placeholder",
        "AWS_BEARER_TOKEN_BEDROCK": "test-placeholder",
        # Data providers
        "ALPACA_API_KEY": "test-placeholder",
        "ALPACA_SECRET_KEY": "test-placeholder",
        "ALPHAVANTAGE_API_KEY": "test-placeholder",
        # Exchanges (ccxt)
        "BINANCE_API_KEY": "test-placeholder",
        "BINANCE_SECRET": "test-placeholder",
        "COINBASE_API_KEY": "test-placeholder",
        "COINBASE_SECRET": "test-placeholder",
    }
    for key, val in placeholders.items():
        monkeypatch.setenv(key, val)
```

**Rule**: tests must not assert on the placeholder values directly; if a
test cares about credential validity, it explicitly sets a real (or
explicitly-fake) value via its own `monkeypatch.setenv()`.

**Tests**: 3 unit tests: fixture is autouse, override-by-test works,
no-leak across test boundaries.

### D.5 — P11: VENDOR_METHODS 2D dispatch table

**Module**: amend `hermes_quant/data/base.py` and add
`hermes_quant/data/vendor_routing.py`.

**Shape**:

```python
# vendor_routing.py
VENDOR_METHODS: dict[str, dict[str, Callable[..., pd.DataFrame]]] = {
    # method_name -> {vendor_name -> callable}
    "fetch_bars": {
        "yfinance": YFinanceProvider().fetch_bars,
        "ccxt": CcxtProvider().fetch_bars,
        # "alpaca": ... in v0.4
    },
    "fetch_latest": {
        "yfinance": YFinanceProvider().fetch_latest,
        "ccxt": CcxtProvider().fetch_latest,
    },
}

TOOLS_CATEGORIES: dict[str, list[str]] = {
    "core_ohlcv": ["fetch_bars", "fetch_latest"],
    # "fundamentals": [...] later
}

VENDOR_LIST: list[str] = ["yfinance", "ccxt"]


def route_to_vendor(method: str, vendor: str) -> Callable:
    """Resolve method × vendor; raise KeyError on unknown combination."""
```

**Tests**: 10+ unit tests including a `test_vendor_completeness.py`
asserting every vendor in `VENDOR_LIST` implements every method in its
category — the "static check that every vendor implements every method"
property TradingAgents codifies.

**Backwards compat**: `fetch_with_chain` keeps its 1D-providers signature;
new code paths use `route_to_vendor()`. No call-site changes in v0.4.

### D.6 — P12: Category + per-method vendor config

**Module**: extend `hermes_quant/config.py` (or wherever quant config
loader lives) with a two-level config reader.

**Shape**:

```python
class VendorConfig(BaseModel):
    """Two-level vendor selection: category default + per-method override.

    Example config.yaml:
        quant:
          data:
            vendors_by_category:
              core_ohlcv: yfinance
            vendor_overrides_by_method:
              fetch_latest: alpaca  # if you want intraday from alpaca
    """
    vendors_by_category: dict[str, str] = Field(default_factory=dict)
    vendor_overrides_by_method: dict[str, str] = Field(default_factory=dict)

    def resolve(self, method: str) -> str:
        """Return the configured vendor for method.

        Priority: per-method override > category default > raise.
        """
```

**Integration**: wire into `route_to_vendor` so callers pass `method` only;
the resolver reads `VendorConfig` from `~/.hermes/config.yaml::quant.data`.

**Tests**: 8+ unit tests covering: empty config raises, category-only
resolves, per-method override beats category, unknown method raises,
invalid vendor name raises early.

## Constraints (load-bearing)

1. **No new dependencies**. Everything lands on stdlib + already-vendored
   pandas / pydantic / sqlite3.

2. **No LLM in the action path** (ADR-0012). All six patterns are
   non-LLM infrastructure. Watermark, snapshot, doctor, conftest, vendor
   dispatch, vendor config — none introduce an LLM call.

3. **No Hermes-core monkeypatches** (ADR per `build-vs-leverage-vs-monkeypatch.md`
   §3.4). All work lives in plugin code.

4. **Bit-identical legacy paths** when env flags off. P5 BarSnapshot is
   opt-in via `HERMES_QUANT_SNAPSHOT_V2=1`; P3/P6/P8/P11/P12 layer on top
   of existing surfaces without changing default behavior.

5. **Profile-aware paths**. P3 `watermarks.db` resolves under
   `~/.hermes/profiles/<active>/quant/watermarks.db` when a profile is
   active, falling back to `~/.hermes/quant/watermarks.db`.

6. **Test budget**: ≥56 new unit tests across D.1–D.6 (12+15+8+3+10+8).
   Each pattern must have its own test file under
   `tests/unit/wave_d/test_*.py`.

## Rejected alternatives

- **Building all 17 patterns**. P4, P14 (now), P16 are NOT-APPLICABLE.
  P7, P9b, P14 (later), P15 belong to v0.3.0 LLM-analyst surface — not
  this wave.

- **LangGraph SqliteSaver checkpointer (P3 alternative shape)**. Adopting
  LangGraph means importing the orchestration spine TradingAgents uses; we
  rejected that in `04-tradingagents-comparison.md` §"What we deliberately
  do NOT steal." A flat per-symbol KV store gets the resume-idempotency
  property with zero new dependencies.

- **Replacing JSONL writers with BarSnapshot in the same wave**.
  Bit-identical legacy paths are a cardinal Wave-D constraint; flipping
  the JSONL emit shape is a v0.4 task (with its own ADR).

- **Reading JSONL via a streaming parser in `quant_doctor`**. The flat
  read-last-N approach is sufficient (latest 10 rows per symbol) and
  avoids state-machine bugs for a read-only diagnostic.

## Reproducibility / settlement-journal impact

None. Watermark is daemon-internal idempotency; it does not appear in
`signals.jsonl` or `journal.md`. BarSnapshot's adapter ensures
`signals.jsonl` shape is unchanged unless `HERMES_QUANT_SNAPSHOT_V2=1`.
`quant_doctor` is read-only.

## Acceptance gates

- All 56+ new tests pass on the touched surface.
- Full suite delta: net 0 new failures vs. HEAD `907b8c2`. Pre-existing
  pollution (23 kronos + 1 deterministic_aggregator) remains documented;
  this wave does not chase it.
- `ruff` baseline holds: 94 pre-existing errors, no new ones.
- ADR-0035 reconciliation: watermark store enables future weekly-rebalance
  resume mid-failure. ADR-0036 reconciliation: BarSnapshot gives
  `horizons_present` a typed home (currently a journal tag).

## References

- `docs/research/04-tradingagents-comparison.md` §"Patterns I missed in
  v1" — the source enumeration.
- `docs/reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md`
  §3.4 — no-monkeypatches stance.
- ADR-0010 (settlement journal), ADR-0035 (cadence), ADR-0036
  (multi-timeframe), ADR-0037 (LLM committee) — adjacent contracts.

## Deviations from contract (post-Wave-D corrections — 2026-05-26)

The implementation that landed at commit `77b809a` differs from the
contract above on three points. They were caught by the Sonnet 4.6
cross-family review and folded back into the ADR rather than chased into
the v0.4 follow-up wave, per the principle that the ADR should describe
what shipped, not what was intended.

### Correction 1 — `tool_schemas.py` rename

**Original §D.2 implication**: the new `hermes_quant/schemas/` package
sits alongside the existing `hermes_quant/schemas.py` legacy file (which
holds JSON Schema dicts for tool registration).

**What landed**: a Python namespace collision — `hermes_quant/schemas.py`
and `hermes_quant/schemas/` cannot coexist in the same parent (the
package wins, the file is silently shadowed). Tool registration in
`hermes_quant/__init__.py:register()` would have failed at plugin load
because `schemas.QUANT_STATUS` no longer resolved.

**Resolution**: legacy `hermes_quant/schemas.py` was renamed to
`hermes_quant/tool_schemas.py` post-commit; `hermes_quant/__init__.py`
imports it as `from . import tool_schemas as schemas` to preserve the
local alias `schemas.QUANT_STATUS` etc. for tool registration. The new
Pydantic package retains the canonical `hermes_quant.schemas` namespace.

### Correction 2 — `fetch_latest` dropped from `core_ohlcv`

**Original §D.5**: `VENDOR_METHODS` lists both `fetch_bars` and
`fetch_latest` under the `core_ohlcv` category.

**What landed**: only `fetch_bars`. `CcxtProvider.fetch_latest` does not
exist in the current tree (only `YFinanceProvider` implements it), and
adding it was outside Track A's edit surface (which forbade modifications
to `hermes_quant/data/ccxt_provider.py`).

**Resolution**: documented inline in `vendor_routing.py` source comment
("Deviation from ADR-0038 §D.5"). Adding `fetch_latest` to
`CcxtProvider` is a v0.4 follow-up wave; the dispatch table will pick it
up automatically once the method exists, and the
`test_vendor_completeness` test will start enforcing it as a static
guarantee.

### Correction 3 — VendorConfig YAML auto-loader and `route_to_vendor`
wire-up deferred

**Original §D.6**: "wire into `route_to_vendor` so callers pass `method`
only; the resolver reads `VendorConfig` from
`~/.hermes/config.yaml::quant.data`."

**What landed in 77b809a**: `VendorConfig` was shipped as a model +
`.resolve()` only. No YAML auto-loader; no integration into
`route_to_vendor`.

**Resolution (closed in commit AFTER 87c945a)**:
`VendorConfig.from_yaml(path: Path | None = None)` and a module-level
`get_vendor_config()` helper now load `quant.data` from
`~/.hermes/config.yaml` (profile-aware via
`hermes_quant.watchlist.get_config_path`). Validation fires at load
time so config typos surface immediately, and unrelated `quant.data.*`
sibling keys are filtered out before validation so they don't trip
`extra=forbid`. Eight new tests in
`tests/unit/wave_d/test_vendor_config.py` cover the loader path
(missing file, empty file, no `quant.data` section, typed loads,
validation failure, sibling-key filtering, module helper). The
`route_to_vendor` wire-up is still deferred — once a second vendor
joins `VENDOR_LIST` (post DataProvider Protocol unification, see
Correction 2), `route_to_vendor(method)` will become a one-liner that
delegates to `get_vendor_config().resolve(method)`.

### Acceptance gate — re-checked after corrections

- All 81 wave_d tests still pass after rename.
- Both namespaces (`hermes_quant.tool_schemas` for JSON Schema dicts,
  `hermes_quant.schemas` for Pydantic models) resolve cleanly.
- Plugin registration in `hermes_quant/__init__.py:register()` still
  references all four `QUANT_*` schemas via the local
  `schemas` alias.

## Post-merge codex review (2026-05-26 — tests + concurrency lens)

A follow-up codex review with `--lens tests --lens concurrency` against
HEAD `87c945a` surfaced three P2 correctness findings, all real. Fixed
in the immediate follow-up commit:

### P2-1 — Watermark composite-key collision (`tick_loop.py:269,387` /
`watermark.py`)

Root cause: the watermark `(get|set)` keyed on `task.asset` only. Under
ADR-0036 multi-timeframe horizons (e.g. `1d` + `1w` for the same
symbol) the first-emitted timeframe's `bar_ts` would shadow the
second's in the watermark short-circuit, silently killing the second
horizon's emit until its `bar_ts` caught up.

Fix: new schema `watermark_v2` with composite PK
`(symbol, exchange, timeframe)`. Old `watermark` table left in place as
a forensic tombstone (no fallback reads — a missing v2 row simply
re-processes the bar, safe per §D.1's "signal_id is the canonical
idempotency key" invariant). `Watermark` dataclass gains `exchange` and
`timeframe` fields; `WatermarkStore.get` takes 3 keys; the batch
helper renamed `all_for_symbols` → `all_for_keys` taking a list of
`(symbol, exchange, timeframe)` tuples. Equity tasks with `exchange=None`
coerce to `""` at the call site (sentinel; round-trips cleanly and
does not collide with a real exchange string). New tests cover the
two-timeframes and two-exchanges no-collision invariants.

Production impact: zero — `HERMES_QUANT_WATERMARK_ENABLED` is unset in
all live crons (verified via `cronjob list` 2026-05-26), so the
watermark module never ran in production. The bug was latent; the fix
ships clean.

### P2-2 — `quant_doctor` daemon_state mutated proposal state
(`tools.py:1260`)

Root cause: the `daemon_state` block called
`hermes_quant.proposals.get_default_store().list_pending(limit=1000)`,
which on a fresh signal-bus implicitly creates
`~/.hermes/quant/proposals.{jsonl,db}` AND triggers an expiration
sweep that writes `expired` records. A read-only diagnostic tool
should not mutate operator state.

Fix: switched to a bounded JSONL tail probe via
`_read_jsonl_tail(PROPOSAL_BUS_PATH, 5000)` counting `status=="pending"`
rows. No `ProposalStore` construction, no expiration sweep. If the
JSONL doesn't exist, returns 0 silently.

### P2-3 — `BarSnapshot.bar_ts` defaulted to `ctx.asof` not last bar
(`schemas/bar_snapshot.py:237`)

Root cause: when callers omitted `bar_ts`, the snapshot defaulted to
`ctx.asof` (the decision/tick wall-clock time, which can be AFTER the
last closed bar). Replaying the same market bar at a different tick
time produced a different `bar_ts` each replay, breaking V2 per-bar
dedup and replay diagnostics.

Fix: `bar_ts` now defaults to the last timestamp in `ctx.bars`
(normalized to tz-naive UTC), matching the watermark/dedup identity
established in §D.1. `asof_decision` continues to default to
`ctx.asof`. Defensive fallback to `ctx.asof` if `ctx.bars` is empty or
the `timestamp` column is missing, preserving legacy behaviour at the
edges.

### Acceptance after follow-up

- Wave-D test count: 89 → 94 (+5: two no-collision tests, v2
  composite-PK schema check, v1 tombstone presence check, empty-
  exchange sentinel).
- All 94 pass; no signal_bus / protocol parity regressions in
  84-test broader slice.
- Ruff clean on all touched files; pre-existing 4 N806 in `tools.py`
  baseline unchanged.
