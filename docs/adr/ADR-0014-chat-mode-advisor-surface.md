# ADR-0014: Chat-mode advisor surface

**Status:** Accepted (2026-05-13), implemented
**Supersedes:** none
**Amends:** none (extends ADR-0013)
**Cross-cuts:** ADR-0013 (dual surface), ADR-0010 (settlement journal — read path), ADR-0012 (LLMAnalyst deferred), ADR-0005 (data layer + as_of plumbing), ADR-0007 (plugin / CLI tree), ADR-0002 / ADR-0003 / ADR-0004 (analyst, BMA, risk gate)

---

## Context

ADR-0013 established that hermes-quant ships **two surfaces** in v0.1.x:
the **daemon surface** (long-running, emits signals, settles trades,
updates calibrators, writes the journal — requires broker + portfolio) and
the **advisor surface** (synchronous, in-process, read-only — answers
questions from live data + journal lessons; no daemon, no broker). This
ADR locks the advisor contract.

The user story is concrete:

> *"I'm using Hermes for general work. I install hermes-quant. I ask Hermes
> 'what does the system say about AAPL right now?' and get a structured
> analyst + aggregator + risk-gated answer with the journal's recent
> lessons on AAPL. No daemon. No broker. No setup beyond
> `pip install -e '.[yfinance]'`."*

The advisor is the **on-ramp** that makes hermes-quant viable for "anyone
using Hermes" — not just operators who have already configured freqtrade
and committed capital. Without it, the plugin appears inert to a curious
chat-mode user. It is also what operators reach for first when they want
to sanity-check the daemon's view without scraping `signals.jsonl`.

### Hard constraints inherited from project posture

Per `AGENTS.md` and ADR-0007:

- Tools are read-only views. The advisor is a tool. It MUST NOT actuate
  trades, write to the signal bus, or move capital.
- Calibrators update only on **realized outcomes** (ADR-0003 / ADR-0010).
  The advisor has no realized outcome to feed back. It MUST NOT update
  calibrators.
- The advisor MUST be safe to call from any Hermes session, including
  sessions running with `--yolo`. A `--yolo` user asking "look at AAPL"
  must get a snapshot, not a trade.

The advisor is therefore **strictly read-only** against both market data
and on-disk state. Its only side effect is the return value.

## Decision

### D1. Tool: `quant_recommend`

```python
quant_recommend(
    symbol: str,
    asset_class: str = "equity",     # "equity" | "crypto" | "fx"
    timeframe: str = "1d",           # any provider-supported timeframe
    lookback_bars: int = 200,
    include_lessons: bool = True,
    n_lessons_same: int = 3,
    n_lessons_cross: int = 2,
    as_of: str | None = None,        # ISO 8601; None == now
) -> dict
```

Returns a structured `dict` (not a JSON string — the Hermes tool dispatcher
JSON-encodes for the model). The shape is fixed; operators wiring up
notification consumers can rely on it:

```python
{
  "symbol":       "AAPL",
  "asset_class":  "equity",
  "timeframe":    "1d",
  "as_of":        "2026-05-13T20:00:00Z",  # bar timestamp, NOT wall clock
  "data_quality": {
      "bars_received":         200,
      "gaps":                  [],   # list of [start_iso, end_iso] missing windows
      "last_bar_age_minutes":  1432,
  },
  "analyst_views": [               # one per analyst that ran successfully
      {
          "analyst":                  "classical_ta",
          "direction":                "long",   # "long" | "short" | "flat"
          "confidence":               0.61,     # post-calibration, [0,1]
          "expected_signed_edge_bps": 12.3,
          "horizon_bars":             5,
          "metadata":                 {"rsi": 38.2, "ema_fast_above_slow": True},
      },
  ],
  "aggregated_signal": {           # BMAAggregator output; None if no analyst views
      "direction":                  "long",
      "confidence":                 0.58,
      "expected_signed_edge_bps":   11.8,
      "horizon_bars":               5,
  },
  "risk_gate": {                   # DefaultRiskGate output
      "pass":                 True,
      "gated_reason":         None,            # populated when pass=False
      "kelly_fraction":       0.0085,          # informational; see caveats
      "recommended_action":   "long_with_stop",
      # one of: "long_with_stop" | "short_with_stop" | "flat" | "gated"
  },
  "lessons": [                     # recent journal entries; [] if include_lessons=False
      {
          "when":              "2026-05-10T14:32:00Z",
          "symbol":            "AAPL",
          "scope":             "same",         # "same" | "cross"
          "analyst_consensus": "long",
          "realized_outcome":  "positive",     # "positive" | "negative" | "neutral" | "pending"
          "reflection":        "Earnings beat drove the rally...",
      },
  ],
  "caveats": [                     # always populated; ordered most → least relevant
      "This is a snapshot-in-time view, not a guaranteed forecast",
      "No portfolio risk context (single-symbol view)",
      "Calibration not updated from this read",
  ],
  "doctor": {
      "data_provider_alive":  True,
      "analyst_errors":       [],   # list of {"analyst": str, "error": str}
  },
}
```

**Field-population rules for v0.1.2:**

| Field                                  | v0.1.2 status                                                              |
|----------------------------------------|----------------------------------------------------------------------------|
| `symbol`, `asset_class`, `timeframe`   | always populated (echo of input)                                           |
| `as_of`                                | always populated; bar timestamp (`MarketContext.asof`), never wall clock   |
| `data_quality.gaps`                    | populated; empty list when contiguous                                      |
| `analyst_views`                        | populated with deterministic analysts only (ADR-0012 defers LLM analysts)  |
| `aggregated_signal`                    | `None` iff zero analyst views ran successfully                             |
| `risk_gate.kelly_fraction`             | populated, but **informational only** (no portfolio context)               |
| `risk_gate.recommended_action`         | always populated; `"gated"` when `pass=False`                              |
| `lessons`                              | `[]` when `include_lessons=False` or journal is empty / absent             |
| `caveats`                              | always populated (length ≥ 3)                                              |
| `doctor.analyst_errors`                | populated; empty list when all analysts ran cleanly                        |

Reserved (not present in v0.1.2): `portfolio_context` (needs ADR-0011),
`llm_views` (gated by ADR-0012, v0.3.0), `multi_horizon`.

### D2. Implementation seam

New module **`hermes_quant/advisor.py`**:

```python
def recommend(
    symbol: str,
    *,
    asset_class: str = "equity",
    timeframe: str = "1d",
    lookback_bars: int = 200,
    include_lessons: bool = True,
    n_lessons_same: int = 3,
    n_lessons_cross: int = 2,
    as_of: str | None = None,
    config: AdvisorConfig | None = None,
) -> dict: ...
```

- **Synchronous** (`def`, not `async def`). Hermes tool handlers are
  invoked synchronously by the dispatcher; an `async` advisor would force
  every caller (CLI, slash command, Discord) to manage an event loop.
- One module, one function, one return shape. The CLI subcommand, the
  Hermes tool handler, and the slash command all wrap this single
  function. No surface duplicates analyst-loading or risk-gate logic.
- `AdvisorConfig` loads lazily from the same TOML/YAML the daemon reads
  — same analyst list, same calibrator coefficients, same risk-gate
  thresholds. The advisor and daemon must not diverge.

The Hermes tool handler in `hermes_quant/tools.py` is a thin wrapper:

```python
def quant_recommend(args: dict, ctx) -> dict:
    return advisor.recommend(**_validate_args(args))
```

The CLI subcommand in `hermes_quant/cli/recommend.py` is also a thin
wrapper that adds rich rendering by default and `--json` for piping.

### D3. Constraints (binding for any v0.1.2 PR)

1. **No state mutation.** `recommend()` must not write to `state.json`,
   `signals.jsonl`, `ticks.db`, the journal, or any calibrator. Read-only
   against the journal via `journal/reader.py::get_recent_lessons`
   (ADR-0010 Pattern #10). The journal **write** path stays daemon-side.
   The test fence (D7) enforces this with a snapshot-then-recommend-then-
   snapshot file-hash check.
2. **Synchronous.** One network call per data provider per invocation.
   Returns within 10s on the default lookback (200 bars at `1d`). No
   retries beyond whatever backoff the data provider implements
   internally. If the provider raises after its own retries, the advisor
   degrades to the empty-bars path (constraint 4) — it does not retry.
3. **Deterministic given inputs.** `(symbol, as_of, timeframe,
   lookback_bars, calibrator_snapshot)` uniquely determines the output.
   The advisor reads calibrator coefficients **once at start of the call**
   and uses that snapshot for the whole call. Two calls with the same
   inputs against the same calibrator snapshot produce byte-identical
   results, modulo `data_quality.last_bar_age_minutes` (wall-clock-derived
   and excluded from determinism guarantees — see the golden-file test at
   D7).
4. **Safe under no-data scenarios.** Empty bars or `DataQualityError` from
   the provider must not raise out of `recommend()`. Instead:
   ```python
   {
       ...,
       "aggregated_signal": None,
       "risk_gate": {
           "pass": False,
           "gated_reason": "no_data",
           "kelly_fraction": 0.0,
           "recommended_action": "gated",
       },
       "caveats": [..., "Insufficient data for recommendation"],
       "doctor": {"data_provider_alive": False, "analyst_errors": []},
   }
   ```
   Same shape applies when the provider returns < 2 valid bars after
   boundary validation (per `AGENTS.md` data-validation rule).
5. **`as_of`-aware.** Optional ISO 8601 timestamp; defaults to wall-clock
   "now". When specified, the data provider filters bars to `<= as_of`
   per the ADR-0005 lookahead-enforcement amendment. Enables backtest-
   mode queries and is what the deterministic golden-file test at D7
   exercises.
6. **No LLM in the analyst chain.** Per ADR-0012, the v0.1.2 advisor only
   invokes deterministic analysts (`ClassicalTAAnalyst`, future
   `KronosAnalyst` / `KairosAnalyst`). `LLMAnalyst` integration is
   deferred to v0.3.0 and lands behind a config flag at that time.
7. **Single-symbol only in v0.1.2.** Multi-symbol portfolio
   recommendations require the ADR-0011 portfolio_loader rewrite to land
   first. v0.1.2 rejects list-of-symbols input with `ValueError` rather
   than silently looping (looping would mask the missing portfolio-
   context contract).

### D4. CLI surface

```
hermes quant recommend <SYMBOL>
    [--asset-class equity]
    [--timeframe 1d]
    [--lookback 200]
    [--no-lessons]
    [--n-lessons-same 3]
    [--n-lessons-cross 2]
    [--as-of 2026-05-12T16:00:00]
    [--json]
```

Default output is rich-formatted, matching `hermes quant status`: header
panel (symbol / as_of / data quality), table of analyst views, aggregated
signal, risk-gate decision, collapsed lessons section. `--json` returns
the raw dict for piping. Fits under the existing `hermes quant` tree per
ADR-0007 — no new top-level command, no `--profile` collision.

### D5. Slash command and Discord

`/quant recommend <SYMBOL>` — same arguments as the CLI, returns rich-
formatted text in chat (in `hermes_quant/slash.py`). Discord wiring goes
through the existing
`discord_slash.py::install_quant_slash_on_pre_dispatch` hook per ADR-0013
§D2 — no new private-attribute reads. Subcommand: `/quant recommend AAPL`.

### D6. Test fence

`tests/integration/test_advisor_e2e.py` — REQUIRED for v0.1.2 release.
Each bullet is one named test:

- `test_recommend_known_symbol_returns_valid_shape`: recommend against a
  fixture symbol with bundled bars; assert the return dict matches the
  Pydantic schema for the contract above.
- `test_recommend_empty_bars_returns_gated`: monkey-patch the data
  provider to return empty; assert no exception, `pass=False`,
  `gated_reason="no_data"`, `recommended_action="gated"`.
- `test_recommend_as_of_deterministic_golden`: recommend with
  `as_of=<fixed past timestamp>` against a fixed bar fixture; compare
  result (minus `last_bar_age_minutes`) byte-for-byte with a checked-in
  golden JSON file.
- `test_recommend_does_not_mutate_state`: snapshot file hashes of
  `~/.hermes/quant/{state.json, signals.jsonl, ticks.db, journal/}`,
  call `recommend()`, re-snapshot, assert all hashes unchanged.
- `test_recommend_does_not_call_calibrator_update`: mock every
  `IsotonicCalibrator.update`, `ColdStartShrinkage.update`, and BMA
  weight update; assert zero calls during `recommend()`.
- `test_recommend_no_lessons_skips_journal_io`: call with
  `include_lessons=False`; assert `lessons == []` AND that
  `journal.reader.get_recent_lessons` is never invoked.

### D7. Tool registration

In `hermes_quant/__init__.py::register(ctx)`:

```python
ctx.register_tool(
    name="quant_recommend",
    toolset="quant",
    schema=schemas.QUANT_RECOMMEND,
    handler=quant_tools.quant_recommend,
)

# Per architecture review §4.5 — direct journal access is independently
# useful and shares the same read path as the advisor's lessons block.
ctx.register_tool(
    name="quant_show_lessons",
    toolset="quant",
    schema=schemas.QUANT_SHOW_LESSONS,
    handler=quant_tools.quant_show_lessons,
)
```

Both tools register under the existing `quant` toolset; no plugin-manifest
change beyond the entry already declared by ADR-0007.

## Consequences

### Positive

- **Zero-config bootstrapping.** Install plugin →
  `pip install -e '.[yfinance]'` → `/quant recommend AAPL` works. No
  daemon, no broker, no portfolio. The on-ramp that converts curious
  Hermes users into hermes-quant users without freqtrade setup first.
- **Shared analyst surface.** Each analyst is exercised by both the
  advisor and the daemon. The advisor's E2E tests cross-check daemon
  behaviour: divergence between "advisor recommends X" and "daemon would
  emit signal Y" is a regression worth investigating.
- **Operator sanity-check tool.** `hermes quant recommend AAPL` answers
  "what is the daemon thinking about AAPL right now?" without parsing
  `signals.jsonl`. Daemon and advisor views should match for any symbol
  the daemon is currently watching, modulo bar-arrival timing.
- **Deterministic, replayable.** `as_of` plus calibrator snapshotting
  make the advisor a usable harness for "what would we have said?"
  investigations, complementing the ADR-0005 daemon-side replay path.

### Negative

- **Tool output is large** (~3–5 KB per call with lessons). Manageable
  but counts against the model's context budget; three calls in one
  session consume meaningful tokens. `include_lessons=False` exists for
  callers that already have journal context.
- **"Snapshot-in-time" misinterpretation risk.** A user reading
  `recommended_action: "long_with_stop"` may treat it as a buy
  recommendation. Mitigated by the mandatory `caveats` field and an
  explicit README "advisor is not advice" disclaimer; residual risk is a
  function of the surface itself.
- **`kelly_fraction` is informational.** Without portfolio context, the
  risk gate uses default position-budget assumptions; the reported Kelly
  fraction is what the **daemon** would size to under its configured
  portfolio. The advisor caller should not act on it as a sizing
  recommendation.
- **Single-symbol limitation in v0.1.2.** The natural follow-up
  ("show me long candidates from my watchlist") is post-portfolio-
  rewrite work; v0.1.2 explicitly rejects multi-symbol input rather than
  half-implementing it.
- **Provider rate limits become a chat-mode failure mode.** Spamming
  `/quant recommend AAPL` will hit yfinance limits faster than a polling
  daemon would. The `doctor` field surfaces this
  (`data_provider_alive: False`); request coalescing is a v0.2 concern.

## Cross-references

- **ADR-0013** — dual-surface decision; this ADR fills in the advisor
  contract that ADR-0013 deferred.
- **ADR-0010** — settlement journal; `get_recent_lessons` is the only
  journal interaction the advisor is permitted.
- **ADR-0012** — LLMAnalyst protocol deferred to v0.3.0; locks v0.1.2
  advisor as deterministic-only.
- **ADR-0005** (lookahead-enforcement amendment) — `as_of` parameter
  depends on the data-layer plumbing established there.
- **ADR-0007** — plugin shape and CLI tree; `recommend` subcommand fits
  under existing `hermes quant` group with no new top-level command.
- **ADR-0002 / ADR-0003 / ADR-0004** — analyst, aggregator, risk-gate
  components the advisor wires together. The advisor adds no new logic;
  it is a synchronous orchestrator.
