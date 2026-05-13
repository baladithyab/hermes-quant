# ADR coverage audit — hermes-quant v0.1.1 + v0.1.2 prep

**Date**: 2026-05-13 (post-v0.1.1 ship)
**Scope**: 13 audit items across SHIPPED-in-v0.1.1 and PLANNED-for-v0.1.2 / v0.3.0
**Existing ADRs**: ADR-0001..0009 in `docs/adr/`
**Inputs**: `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md`, source under
`hermes_quant/`, `docs/research/04-tradingagents-comparison.md`, `CHANGELOG.md`

## Why this audit exists

After shipping v0.1.1 the user asked: "did you ADRify everything you planned
on setting up with hermes-quant?" The honest answer was **no**. This audit
finds the gaps and pins them down before any v0.1.2 implementation work
begins, so v0.1.2 lands against fixed contracts instead of inventing them
during coding.

## Gap report

| # | Item | Verdict | Citation / rationale |
|---|------|---------|----------------------|
| 1 | P0-A.3 calibration_quality gating (`slippage_only` vs `horizon_return`; `dispatch_settlement` skips slippage_only) — **SHIPPED v0.1.1** | **AMEND ADR-0003** (also touch ADR-0009 §P1-10) | ADR-0003 / ADR-0009 §P1-10 introduce `EpisodeOutcome` and assume `realized_returns[horizon]` is the directional horizon return. The settlement loop now persists a different quantity (per-fill **slippage**) under the same `realized_return` field name, gated by a `_calibration_quality` metadata tag. This is a contract change that's currently only described in a docstring (`settlement_loop.py:21-48`) and the Phase-8 synthesis. Amend ADR-0003 with: (a) `_calibration_quality ∈ {slippage_only, horizon_return}` semantics, (b) the dispatch-skip rule, (c) the v0.1.2 transition path. |
| 2 | P0-B edge-sign alignment guard in `DefaultRiskGate` (silence when `edge * direction <= 0`) — **SHIPPED v0.1.1** | **AMEND ADR-0004** (sequence list) + cite in ADR-0009 §P0-1/P0-5 | ADR-0004 §Decision lists six rules; ADR-0009 §P0-5 promotes circuit breakers ahead of flatness. Neither acknowledges the cold-start-shrinkage failure mode where a positive `direction=+1` signal with shrunk confidence yields a negative `expected_signed_edge`, which the old `abs(edge) < threshold` gate would let through and Kelly would size in the OPPOSITE direction. Fix at `risk/gate.py:229-231`. Add "Rule 5a — edge-sign alignment guard" with rationale rooted in calibrated-probability shrinkage from ADR-0002 / ADR-0009 §P0-2. |
| 3 | P0-C tick loop installs durable halt when gate emits `Action(halt=True)` — **SHIPPED v0.1.1** | **AMEND ADR-0009 §P0-D** (durable halt ordering) + cross-link from ADR-0001 | ADR-0009 §P0-4 / §P0-D specifies "halt FIRST, then any other action" but was previously interpreted at `cmd_emergency_stop` only. Phase-8 P0-C extended that to the **tick loop**: when drawdown / daily-loss circuit breakers return `Action(halt=True, halt_scope=...)`, `tick_loop.run_one_tick` MUST `halt_state.add_halt(...)` BEFORE `emit_signal_record(...)`. Implementation at `tick_loop.py:231-252`. Amend §P0-D to say "applies anywhere a halt action originates — `cmd_emergency_stop` AND tick-loop circuit-breaker emit." Add idempotency note (existing-active-halt → swallow `ValueError`). |
| 4 | P1-α `NotImplementedError` gating on `portfolio_loader`; v0.1.2 rewrite (4 cases × buy/sell × long/short) — **PLANNED v0.1.2** | **NEW ADR-0011 — Portfolio reconstruction sign convention** | Current code at `portfolio_loader.py:105-137` raises `NotImplementedError` on partial close or flip with case enumeration in docstring; no ADR captures the v0.1.2 contract. Scope: define four canonical cases — (a) same-direction open/add → averaged cost basis, (b) opposite with `\|new\| < \|old\|` → partial close at avg_old, (c) full close at exactly zero, (d) opposite with `\|new\| > \|old\|` → close + reopen at fill (direction flip). Pin realized-PnL sign convention so a profitable long-to-short flip never registers as loss. Mandate 8+ unit tests covering buy/sell × long/short × partial/full/flip. |
| 5 | Phase-9e exponential-backoff retry (`_retry_with_backoff` in `YFinanceProvider`) — **SHIPPED v0.1.1** | **AMEND ADR-0005** (error taxonomy section) | ADR-0005 §Open questions line 153 names the error taxonomy and says rate-limit responses should "back off + fall back to next provider in chain." New code at `yfinance_provider.py:47-94` adds a per-provider retry layer (3 attempts, 2s base, factor 2 → 2s/4s) BEFORE chain fallback, adapted from TradingAgents `yf_retry`. Amend ADR-0005 to specify: per-provider retry happens first; chain fallback only after retries exhausted. Cite TradingAgents provenance. |
| 6 | Settlement journal at `~/.hermes/quant/journal.md` (pending→resolved markdown, atomic-rename, no embeddings) — **PLANNED v0.1.2** | **NEW ADR-0010 — Settlement journal (markdown sidecar to JSONL bus)** | Already telegraphed in `docs/research/04-tradingagents-comparison.md`. Scope: define the markdown sidecar at `~/.hermes/quant/journal.md` as an operator-UX channel COMPLEMENTARY to the canonical JSONL bus — append-only with `<!-- ENTRY_END -->` delimiters, two-phase pending→resolved entries, atomic-rename writes, NO embeddings / vector store. Pin contract: JSONL is wire-format truth; journal.md is observability only — never read by daemon for decisions. |
| 7 | Calibration-quality lifecycle: when v0.1.2 lands entry+exit fill joining, `slippage_only` flips to `horizon_return` and Beta posteriors start updating — **PLANNED v0.1.2** | **AMEND ADR-0003** (same amendment as #1) + new section in ADR-0011 | This is the *transition* half of the schema change in #1. "Lifting the gate" unblocks BMA posterior evolution and stacking data. Phase-8 synthesis §P0-A.3 mitigation and `settlement_loop.py:35-48` document the intent. Roll into the ADR-0003 amendment from #1 — describe entry-record persistence, exit-fill join logic, FIFO vs avg-cost choice for partial-exit attribution, test fence asserting "no `slippage_only` outcomes reach `analyst.update`." Cross-cuts ADR-0011 because exit-fill joining piggybacks on the new portfolio loader's case enumeration. |
| 8 | Monotonic-clock heartbeat (Phase-8 P1-β) — **PLANNED v0.1.2** | **SKIP** (mechanical) — but add one-line note to ADR-0008 | Switching elapsed-time math to `time.monotonic()` while keeping wall-clock for log/audit `asof` is a one-file mechanical change; no semantic contract shift. Document as a one-line "implementation note: use monotonic clock for elapsed math" in ADR-0008's heartbeat section. |
| 9 | Halt mirror staleness fallback (Phase-8 P1-ε) — **PLANNED v0.1.2** | **AMEND ADR-0008** (signal bus / halt mirror contract) | ADR-0008 establishes the JSON mirror at `~/.hermes/quant/halt_state.json` as the freqtrade fast-path. Current `_write_mirror` runs AFTER SQLite commit, opening a crash-window. Amend to specify the consumer-side fallback contract: if `mirror.mtime < sqlite.mtime`, strategy SHALL fall back to read-only SQLite open. Spell out mtime comparison precisely. |
| 10 | `trading_calendars` for proper session boundaries in `_next_session_open` (Phase-8 P1-δ) — **PLANNED v0.1.2** | **AMEND ADR-0004** (circuit-breaker halt-until semantics) | Current `gate.py:292-312` returns `now + 24h` for non-UTC tz as a coarse v0.1.1 fix. v0.1.2 adopts `trading_calendars` for session-aware boundaries. Crosses an ADR boundary because: (a) adds runtime dependency, (b) changes daily-loss `halt_until` semantics, (c) touches all multi-asset-class behavior. Amend ADR-0004 §Rule 2 with calendar-derived `halt_until` formula and `now + 24h` fallback if calendar lookup fails. |
| 11 | `LLMAnalyst` Protocol with `PortfolioDecision`-style structured output — **PLANNED v0.3.0** | **NEW ADR-0012 — LLMAnalyst protocol (deferred to v0.3.0)** | Scope: extend ADR-0002 `Analyst` Protocol with structured-output variant — `LLMAnalyst` emits Pydantic-validated 5-tier rating mechanically mapped to `(direction ∈ {-1,0,+1}, confidence ∈ [0,1])`. Pin principle: **LLM stays out of the action path** — its output flows through the same calibrator + risk gate as any other analyst. Status = "proposed, deferred to v0.3.0" so design is fixed before implementation. |
| 12 | Dual-tier LLM config (`quant.llm.deep` / `quant.llm.quick`) — **PLANNED v0.3.0** | **Roll into ADR-0012** (same as #11) | Self-contained sub-section. Specify two named tiers (deep = claude-opus-class for daily/weekly synthesis; quick = haiku-class for per-tick gating), config schema under `quant.llm.{deep,quick}.{model, max_tokens, timeout_s}`, and routing rule (analysts declare their tier; misuse like calling `deep` on every 1m tick is a config error surfaced by `quant doctor`). |
| 13 | `tests/test_no_lookahead.py` CI gate — **PLANNED v0.1.2** | **SKIP** (test-infra) — but **AMEND ADR-0006** to make the gate non-negotiable | Mechanical test: import every shipped analyst + aggregator, run `shuffle_timestamps_test()`, fail CI if any beats chance. ADR-0006 §Graduation criteria treats lookahead-freedom as a hard precondition; that promise has been unfenced for two minor releases. Amend ADR-0006: "**`tests/test_no_lookahead.py` MUST exist and run on every PR; an analyst that fails the gate cannot be released, regardless of backtest Sharpe.**" |

## Recommended ADR work order for v0.1.2 prep

Order by (a) what unblocks the most other v0.1.2 work, (b) what closes the
largest open contract gap surfaced by Phase-8, (c) what's cheapest to write.

1. **AMEND ADR-0003 + ADR-0009 §P1-10 — calibration_quality lifecycle** (items #1, #7)
   — load-bearing across `settlement_loop`, `bma`, stacking; only documented
   in a docstring today. Nails down the exit-fill-join contract that
   ADR-0011 and the v0.1.2 calibrator-update flip both depend on. **One
   amendment, two items closed.**

2. **NEW ADR-0011 — Portfolio reconstruction sign convention** (item #4)
   — current `NotImplementedError` is a known time bomb. Four-case
   enumeration is small; test fence concrete. Settlement loop's exit-fill
   join, equity/drawdown, cooldown all sit downstream.

3. **AMEND ADR-0004 — risk gate** (items #2, #10) — bundle edge-sign
   alignment guard (P0-B, shipped) and `trading_calendars` daily-loss
   `halt_until` change (P1-δ, planned). Coherent "v0.1.1 → v0.1.2 risk-gate
   evolution" diff.

4. **AMEND ADR-0009 §P0-D — durable halt ordering** (item #3) — tiny but
   important. Extend "halt FIRST" rule from `cmd_emergency_stop` to
   tick-loop circuit-breaker emission. Codify idempotency-on-existing-halt
   swallow.

5. **NEW ADR-0010 — Settlement journal (markdown sidecar)** (item #6) —
   write before v0.1.2 implementation lands so the "JSONL = truth, journal
   = UX, no embeddings" boundary is fixed up front and TradingAgents'
   design isn't accidentally cargo-culted further.

6. **AMEND ADR-0005 — data-layer error taxonomy** (item #5) — paragraph
   specifying per-provider retry-before-fallback layering with 3-attempt /
   2s-base / factor-2 defaults. Cite TradingAgents `yf_retry` provenance.

7. **AMEND ADR-0008 — halt mirror staleness fallback** (item #9) — specify
   mtime-based fallback to direct SQLite read for freqtrade strategy. Pair
   with v0.1.2 implementation in `halt_state.py`.

8. **AMEND ADR-0006 — lookahead test gate** (item #13) — one-line addition
   mandating `tests/test_no_lookahead.py` as release blocker. Ideally land
   the test in the same PR.

9. **NEW ADR-0012 — LLMAnalyst protocol (deferred to v0.3.0, status=proposed)**
   (items #11, #12) — write as "deferred / proposed" now so v0.3.0
   implementation has a fixed target. Lowest-priority slot — but writing
   it during v0.1.2 prep avoids the trap of designing it concurrently with
   implementation.

**No ADR work for monotonic-clock heartbeat (item #8)** — implement
directly in `heartbeat.py`, document with one-line note in ADR-0008.

## TradingAgents-mining additions (P0 surprises)

The second-pass TradingAgents mining surfaced TWO P0 gaps NOT covered above:

A. **Look-ahead-bias filtering at the data leaf** — every `data_provider.fetch_*`
   needs an `as_of: pd.Timestamp` parameter that drops rows past it at the
   leaf. Pairs with the `tests/test_no_lookahead.py` gate (item #13). Land
   together. **AMEND ADR-0005** and **AMEND ADR-0002** (DataProvider
   Protocol gets the new param).

B. **`safe_symbol_component()` path-traversal guard** — every cache/JSONL/log
   path that interpolates a `pair`/`symbol` must run it through a whitelist
   regex first. Crypto pairs like `BTC/USDT` already need slash sanitization.
   **NEW ADR-0013 — Symbol path safety (or fold into ADR-0005 amendment)**.

These are additional v0.1.2 work, not in the Phase-8 synthesis but caught
by the second-pass mining.

## Total ADR work

- **3 NEW ADRs**: 0010 (settlement journal), 0011 (portfolio reconstruction),
  0012 (LLMAnalyst, deferred). Possibly +0013 (symbol path safety) if not
  folded into 0005.
- **5 AMENDMENTS**: 0003 (calibration_quality), 0004 (edge-sign + sessions),
  0005 (retry layer + as_of param), 0006 (lookahead gate), 0008 (mirror
  staleness).
- **1 cross-ADR ordering note**: 0009 §P0-D extended.
- Items #1+#7, #2+#10, #11+#12 collapse into single documents → **8 actual
  documents to write**.

## Provenance

- This audit: `docs/reviews/2026-05-13-v0.1.1-phase8/adr-coverage-audit.md`
- TradingAgents pattern mining (round 2):
  `docs/research/04-tradingagents-comparison.md` §"Patterns I missed in v1"
- v0.1.1 ship synthesis: `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md`
- All Phase-8 reviewer outputs: `/tmp/hq-phase8/{claude,gemini,deepseek}.json`
