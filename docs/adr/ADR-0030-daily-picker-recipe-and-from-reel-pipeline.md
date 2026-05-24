# ADR-0030: Daily Picker Recipe + From-Reel Methodology Pipeline

**Status:** Proposed
**Date:** 2026-05-24
**Related:** ADR-0015 (HITL CLI for orders), ADR-0021 (PDR recipe runtime), ADR-0023 (deliberative committee), ADR-0024 (autonomous semantic perception), ADR-0025 (user-editable recipes), ADR-0027 (options risk gate), ADR-0029 (multi-leg paper reactor)

---

## 1. Context

The user — the sole architect of hermes-quant — is feeding the system trading methodologies sourced from short-form social video (Instagram Reels, TikTok, YouTube Shorts) at a roughly weekly cadence. Each reel is the verbal exposition of a *methodology*: a small set of filter rules, sometimes with a scoring or sizing heuristic, that defines a watchlist or trade-candidate universe. The most recent example — socalminh's covered-call methodology — is a four-rule mid-cap monthly-premium screen sourced entirely from a talking-head reel with no on-screen UI to OCR.

Up until now these methodologies have been encoded as bespoke Python recipes inside `hermes_quant/recipes/`. That approach fails on three axes:

1. **Author barrier.** A methodology is a list of numeric thresholds — a screener, not a program. Hand-coding each one in Python means the architect is the only person (or LLM) who can author them, and authoring is slow.
2. **Replayability.** Methodology rules drift. A reel from May becomes obsolete in August when the author updates a threshold in a follow-up. With Python recipes there is no clean version/source-URL provenance trail. With YAML on disk pinned to a `methodology_version`, every recipe-instance can be replayed against historical data.
3. **Composability.** Two methodologies firing on the same name (e.g. socalminh's covered-call screen *and* a momentum swing screen) need to vote, and that vote needs to slot into ADR-0023's deliberative committee. A pile of one-off Python recipes does not give us a uniform `AnalystView` per methodology.

This ADR proposes (a) a methodology DSL — three-namespace YAML — that captures the rules; (b) a six-stage from-reel ingestion pipeline that turns a reel URL into a versioned methodology YAML; (c) a `daily-picker` recipe that loads the entire methodology library, runs it against the universe, and aggregates per ADR-0023; (d) cron evolution from the current interim daily brief to a methodology-driven one.

**Posture restatement (non-negotiable).** The DSL-loaded methodology is a *read-only plan*. No money flows through the picker. The picker emits `MultiLegProposal` objects into `proposal_store`; orders are decided by the human via the HITL CLI per ADR-0015. The action space remains discrete (proposal → accept/skip). Failed methodology load is silence-by-default — a malformed or missing YAML is skipped with a warning, never crashes the picker. Every methodology that lands in production has a versioned YAML on disk; from-reel-extracted methodologies additionally carry a `source_url` and `extraction_pipeline_version` in metadata.

## 2. Decision

### D1. Methodology DSL — three-namespace YAML

A methodology is a YAML document with three rule namespaces (`fundamentals`, `options_chain`, `event_flags`), a `compose` block, and a `metadata` block. Numeric rules are `{min, max}` intervals; categorical rules use `_in` suffix for set-membership; boolean rules use `reject_if_true` / `warn_if_true`. Custom fields are allowed in any namespace, provided their resolution is annotated in `metadata.denominator_resolutions` (R4 §3).

```yaml
# methodology/socalminh-covered-call.yaml
fundamentals:
  market_cap: { min: 2e9, max: 1e10 }

options_chain:
  dte: { min: 21, max: 36 }
  put_call_ratio: { min: 0.2, max: 0.7 }
  monthly_premium_yield_atm: { min: 0.10 }

event_flags: {}

compose:
  vote_weight: 1.0
  must_pass:
    - 'fundamentals.market_cap'
    - 'options_chain.dte'
    - 'options_chain.put_call_ratio'
    - 'options_chain.monthly_premium_yield_atm'
  warn_only: []

metadata:
  methodology_id: 'socalminh-covered-call-v1'
  source_url: 'https://www.instagram.com/reel/DYe45Cxi5ju/'
  extraction_date: '2026-05-23'
  extraction_pipeline_version: '1.0.0'
  rule_confidence:
    'fundamentals.market_cap': STATED
    'options_chain.dte': STATED
    'options_chain.put_call_ratio': STATED
    'options_chain.monthly_premium_yield_atm': INFERRED-NUMERIC
  denominator_resolutions:
    'options_chain.monthly_premium_yield_atm':
      resolution: 'atm_call_bid_div_spot_at_chosen_dte'
      alternatives_considered:
        - 'annualized_premium_div_12'
        - 'first_otm_call_bid_div_spot'
      author_review_date: '2026-05-23'
```

Compare with a hypothetical second methodology that also fires on mid-caps but adds an IV-rank floor and an earnings guard:

```yaml
# methodology/swingtrader-iv-momentum.yaml
fundamentals:
  market_cap: { min: 1e9, max: 5e10 }
  sector_in: ['tech', 'healthcare', 'consumer_discretionary']
  earnings_in_dte_window: false

options_chain:
  dte: { min: 30, max: 60 }
  iv_rank: { min: 30 }
  bid_ask_pct: { max: 0.05 }

event_flags:
  ex_dividend_within_dte: { reject_if_true: false }
  fed_meeting_in_window: { warn_if_true: true }

compose:
  vote_weight: 0.7
  must_pass:
    - 'fundamentals.market_cap'
    - 'options_chain.iv_rank'
    - 'options_chain.bid_ask_pct'
  warn_only:
    - 'event_flags.fed_meeting_in_window'

metadata:
  methodology_id: 'swingtrader-iv-momentum-v1'
  source_url: 'https://www.youtube.com/shorts/abc123'
  extraction_date: '2026-04-12'
  extraction_pipeline_version: '1.0.0'
  rule_confidence:
    'fundamentals.market_cap': STATED
    'options_chain.iv_rank': STATED
    'options_chain.bid_ask_pct': INFERRED
```

The DSL is intentionally narrow. Anything more exotic (regime-conditional thresholds, multi-symbol joins) is out of scope; see Open Questions.

### D2. Python types and ADR-0023 bridge

The DSL is loaded into two dataclasses:

```python
@dataclass(frozen=True)
class MethodologyRule:
    namespace: str           # 'fundamentals' | 'options_chain' | 'event_flags'
    field: str               # e.g. 'market_cap'
    bounds: Bounds           # {min, max} | {reject_if_true} | {sector_in: [...]}
    confidence: Confidence   # STATED | INFERRED | INFERRED-NUMERIC
    must_pass: bool
    warn_only: bool

@dataclass(frozen=True)
class Methodology:
    methodology_id: str
    rules: list[MethodologyRule]
    vote_weight: float
    metadata: MethodologyMetadata

    def evaluate(self, candidate: Candidate) -> AnalystView:
        ...
```

`Methodology.evaluate(candidate)` returns an `AnalystView` — the same struct ADR-0023's deliberative committee already consumes. The view carries `direction` (long-only for the covered-call case), `confidence` (= product of rule-pass confidences, dampened by any `INFERRED-NUMERIC` rules), `magnitude` (notional sizing hint, default = `vote_weight`), and `dissent_reasons` (the list of `warn_only` rules that fired). The committee aggregator (BMA + stacking, ADR-0023) sees an `AnalystView` per methodology and combines them with no methodology-aware logic — the abstraction is clean.

A `Methodology` is wrapped by a `PDRRecipe` (ADR-0021) for execution. The recipe exposes `daily-picker` as the recipe-name; the runtime loads every YAML in `methodology/` at recipe-load time, instantiates one `Methodology` per file, and registers them as analyst nodes inside the daily-picker recipe.

### D3. From-reel ingestion pipeline (R4 §6)

Six numbered stages. Stages 1–4 run automated; stage 5 is the architect's gate; stage 6 is filesystem-level.

1. **Acquire.** `yt-dlp` downloads the reel — video, audio, and platform metadata. Works across IG, TT, YT.
2. **Dual analysis.** Two branches run blind to each other:
   - *Traditional branch:* Whisper-base STT on audio, RapidOCR-ONNX over keyframes, targeted `vision_analyze` calls for ambiguous frames. Output: `traditional.md`.
   - *Multimodal branch:* Gemini-3.1-Pro-Multimodal sees the video directly. Output: `multimodal.md`.
3. **Diff.** A reviewer (human now, retro-loop subagent later) diffs the two extractions. Convergence on a rule = high-confidence; divergence = the rule is flagged for re-run with `whisper-large-v3` and a human read.
4. **DSL YAML draft.** The agreed methodology is converted into a `methodology/<handle>-<short-name>.yaml`. Each rule is annotated with `STATED` (verbatim numeric), `INFERRED` (paraphrased), or `INFERRED-NUMERIC` (numeric but ambiguous denominator/anchor).
5. **Architect review.** The architect reviews the YAML before merge. Critically, this is where methodology gaps get filled in with house defaults — liquidity floors (`avg_option_volume_30d`, `bid_ask_pct`), earnings-window guards, ex-dividend guards. The reel rarely covers these; the house always requires them.
6. **Recipe registration.** YAML lands in `methodology/`. The recipe registry auto-discovers on next reload. No code change required.

Failure modes the pipeline already absorbs:

- **F1 verbal-only, no UI shown.** The socalminh case. Workaround: rely on higher-tier ASR + accept the human fill-in.
- **F2 ambiguous denominator.** The 10%/month case. Workaround: store `denominator_resolutions` in metadata; the architect picks at registration.
- **F3 transcription brand-name corruption** (e.g. 'Parture' → likely 'Barchart'). Workaround: re-run with whisper-large; if still ambiguous, treat the tool name as `unknown` and implement filters provider-agnostic.
- **F4 heuristic with no numeric anchor** (e.g. 'sweet spot'). Workaround: codify implicit speaker defaults plus a YAML comment flagging heuristic origin.
- **F5 conflicting methodologies on the same name.** Resolved by D4 below.
- **F6 reel is 100% sponsorship/UI demo with no real methodology.** Reject at extraction. No YAML produced.
- **F7 author updates methodology in a follow-up reel.** Each YAML carries `methodology_version`; recipe registry pins each recipe-instance to a specific YAML version. Old version stays in tree for replay.
- **F8 stale extraction.** TTL on methodology metadata + a monthly re-acquire cron that pulls the latest reel from the author and diffs against the stored YAML.

### D4. Composability — methodology-level voting

When two methodologies fire on the same candidate, votes aggregate strictly via ADR-0023's deliberative committee — this ADR adds **no new aggregation rules**. The committee already does BMA + stacking on a vector of `AnalystView`s; methodologies are just additional analysts.

Three conflict-resolution rules apply at the proposal layer (post-aggregation):

- **Direction disagreement → silence-by-default.** If two methodologies vote opposite directions on the same name, the picker emits no proposal for that name. This is consistent with the silence-by-default posture.
- **Direction agreement, size disagreement → take min.** Conservative sizing wins.
- **Direction agreement, full agreement → emit proposal at aggregated size,** which the ADR-0027 risk gate then trims if it breaches caps.

The aggregated proposal is gated through ADR-0027 (options risk gate) and lands in `proposal_store` as a `MultiLegProposal`. Then ADR-0015's HITL CLI takes over.

### D5. Worked example — socalminh-covered-call as a drop-in

The full YAML is shown in D1 above. End-to-end behavior:

1. `pdrl run --recipe daily-picker --asof 2026-05-24` fires (cron — see D7).
2. `daily-picker` loads `methodology/socalminh-covered-call.yaml` (and every other YAML in the directory) into `Methodology` instances.
3. For each name in the universe (mid-cap subset, ~28 names):
   - Pull fundamentals snapshot → check `market_cap ∈ [2e9, 1e10]`.
   - Pull options chain snapshot at the chosen DTE band → check `dte ∈ [21, 36]`, `put_call_ratio ∈ [0.2, 0.7]`, and the ATM call bid / spot ratio ≥ 0.10.
4. Each rule's confidence multiplies into the methodology's overall `AnalystView.confidence`. The `INFERRED-NUMERIC` premium-yield rule dampens confidence by 0.85 (configurable).
5. The methodology emits one `AnalystView` per surviving candidate. The committee aggregates with whatever other methodologies fire. ADR-0027 gates. `MultiLegProposal` lands in `proposal_store`. Discord surface displays it. Human decides via HITL CLI.

The architect's pre-merge review for socalminh added (not visible in D1's minimal YAML) a house-default liquidity floor (`bid_ask_pct: { max: 0.05 }`) and an earnings guard (`fundamentals.earnings_in_dte_window: false`), since the reel did not cover them.

### D6. `daily-picker` recipe

`daily-picker` is a `PDRRecipe` (ADR-0021) whose body is:

1. **Load methodology library.** Glob `methodology/*.yaml`, parse, validate against schema, instantiate one `Methodology` per file. Failed loads → log + skip (silence-by-default).
2. **Load universe.** Pull the configured universe (currently ~28 mid-cap + 10 large-cap, expanding under ADR-0024).
3. **Fan out.** For each `(methodology, name)` pair, call `methodology.evaluate(candidate)`. Drop names where no methodology fires.
4. **Aggregate.** Hand the vector of `AnalystView`s to ADR-0023's committee aggregator.
5. **Risk-gate.** Each aggregated candidate goes through ADR-0027.
6. **Emit.** Surviving candidates become `MultiLegProposal`s in `proposal_store`. Markdown summary goes to Discord.

### D7. Cron evolution

The current interim cron at `~/.hermes/scripts/quant-daily-interim.py` fires pre-market (8:30 AM ET, M-F), loads the universe, calls the equity directional advisor, persists JSON to `~/.hermes/quant/daily-briefs/<timestamp>-interim.json`, and sends Discord-ready markdown.

Under this ADR, the pre-market cron becomes a one-line shell wrapper:

```bash
pdrl run --recipe daily-picker --asof "$(date +%Y-%m-%d)"
```

A second cron at 3:30 PM ET refreshes the watchlist (universe membership churns based on liquidity + market-cap drift). The interim Python script is preserved for one release as a regression baseline; once `daily-picker` matches its output on a 5-day fixture window, the interim is removed.

## 3. Consequences

**Positive.**
- Non-coder methodology contributors can author a YAML and submit it. The architect's bar drops from "Python recipe" to "YAML review."
- Methodologies are replayable. Every proposal carries a `methodology_id@version` provenance; backtests can pin to a specific YAML version.
- Versioned and auditable. The from-reel pipeline produces a paper trail (source URL, extraction pipeline version, confidence annotations) attached to every methodology.
- New-methodology onboarding is fast. The socalminh example took roughly an afternoon end-to-end; the limiting factor was the architect's review of the gaps, not coding.

**Negative.**
- DSL coverage gaps. Methodologies that need filters the DSL doesn't express (regime-conditional, multi-symbol joins) cannot be encoded without a DSL extension. Workaround: fall back to a Python `Methodology` subclass that overrides `evaluate`.
- YAML drift from real broker fields. Broker option-chain field naming is not stable across Polygon / Tradier / IBKR. The DSL field names are canonical; the loader maps to broker-specific names. New broker = mapping table update.
- Verbal-only methodologies still need human fill-in for liquidity / earnings / dividends. Stage 5 of the pipeline remains a manual gate.

**Neutral.**
- More YAMLs to maintain. Each is small (~30–60 lines) and versioned, so the maintenance burden is bounded. The monthly re-acquire job (F8) catches stale extractions.

## 4. Alternatives Considered

**Pure Python recipes per methodology.** The status quo before this ADR. Rejected: barrier to non-coder authors; iteration speed too slow; no clean versioning across methodology rule changes; provenance scattered across docstrings instead of structured metadata.

**LLM-at-runtime methodology interpreter.** A generic recipe that takes the methodology *as English text* and an LLM evaluates rules per-candidate. Rejected on two grounds. (1) Non-deterministic — the same candidate on the same day could pass or fail across runs. (2) Blows the no-LLM-in-decision-path posture: ADR-0023's committee is deterministic numeric aggregation; injecting an LLM at the rule-evaluation layer would force ADR-0023 to either ignore the methodology's confidence (lose information) or trust it (trust an LLM's self-reported confidence, which is not calibrated).

**Single mega-recipe with branches.** One Python recipe with `if methodology == 'socalminh': ...; elif methodology == 'swing': ...`. Rejected: no per-methodology versioning, poor composability (branches don't naturally produce independent `AnalystView`s for the committee), and the file grows monotonically.

## 5. Open Questions

- **Time-varying rules.** A methodology may say "IV rank ≥ 30 in normal regime, ≥ 50 when VIX > 25." The current DSL has no regime-conditional bounds. Defer to a future ADR once a methodology in the wild actually requires it; until then, encode the conservative branch.
- **Methodology-default vs house-default conflicts.** If a methodology specifies `bid_ask_pct: { max: 0.10 }` but the house default is `0.05`, who wins? Default rule: house wins (tighter). Future option: a per-methodology `override_house_defaults: ['bid_ask_pct']` flag, gated by architect approval at registration.
- **Confidence calibration.** `INFERRED-NUMERIC` currently dampens by 0.85. Is that right? Calibrate against backtests once we have ≥ 5 methodologies live.

## 6. Implementation Sketch

File layout:

```
hermes_quant/methodology/
    schema.py        # pydantic schema for the YAML
    loader.py        # glob + parse + validate, returns list[Methodology]
    evaluator.py     # MethodologyRule.evaluate, Methodology.evaluate → AnalystView
    confidence.py    # confidence dampening table
hermes_quant/recipes/
    daily_picker.py  # PDRRecipe wrapping the loader + committee
methodology/
    socalminh-covered-call.yaml
    swingtrader-iv-momentum.yaml
    ...
scripts/
    ingest_reel.py   # the 6-stage from-reel pipeline driver
```

Hook points: `daily_picker.py` imports `loader.load_all()` at recipe-load and registers each `Methodology` with the existing ADR-0023 `CommitteeAggregator`. The aggregator's output flows through the existing ADR-0027 gate and into `proposal_store`. No changes to ADR-0015's HITL CLI.

## 7. Test Plan

- **Schema validation.** Every YAML in `methodology/` must validate against `schema.py`. CI fails if any YAML is malformed. Add `tests/test_methodology_schema.py::test_all_methodologies_validate`.
- **Evaluator unit tests.** Apply the socalminh covered-call YAML to a hand-built mock universe with three known-good names and three known-bad (one fails market-cap, one fails DTE, one fails put-call). Assert exactly the three good names emit an `AnalystView`. Repeat for the swingtrader YAML.
- **Integration test.** Load every YAML in `methodology/`, run `daily-picker` against a fixture universe of ~30 names with frozen options-chain snapshots, and assert that the output `MultiLegProposal` set matches a golden file. Update the golden file deliberately when methodologies change.
- **From-reel pipeline regression.** Replay the socalminh extraction end-to-end (cached reel + cached Whisper output + cached Gemini output) and assert the produced YAML matches the committed `methodology/socalminh-covered-call.yaml` byte-for-byte. Catches regressions in the extractor.
- **Conflict resolution.** Synthesize two methodologies that disagree on direction for the same name; assert no proposal emits. Synthesize two that agree on direction but disagree on size; assert size = min.


---

## Amendment 2026-05-24 -- D1 add 4th DSL namespace `risk_liquidity`

**Source**: `docs/reviews/2026-05-24-synthesis-adrs-0026-0030.md` P0-8
**Reviewer**: Grok-4.3
**Status**: Adopted

### What changed

D1 (this ADR, line 25 onward) defines three rule namespaces: `fundamentals`, `options_chain`, `event_flags`. Liquidity-flavored gates (bid-ask spread caps, minimum open interest, minimum daily volume, IV-rank bands) cross-cut these namespaces -- bid/ask is a chain property but the rule's purpose is a quality floor, not a methodology-specific filter. Forcing them into `options_chain` confuses the namespace abstraction (chain rules should be about strike/DTE/etc., not quality floors). Forcing them into `event_flags` is a category error. Folding them into `risk` (ADR-0027's gate) would drift the immutable risk-gate spec -- the risk gate's invariants are deliberately small and stable; per-methodology liquidity preferences shouldn't move that surface.

Add a 4th namespace: `risk_liquidity`. Semantics: "the always-on quality floor every methodology must clear, regardless of what the methodology specifies." A methodology MAY override the global default for any `risk_liquidity` rule (e.g., a wheel methodology may demand `open_interest_min: 500` while the global default is `100`), but it CANNOT loosen below the daemon-published global floor (the floor is set in the `daily-picker` recipe config and is itself immutable per ADR-0027 / ADR-0026).

Initial primitive set (the resolver registry -- implementation wave wires each):

- `bid_ask_spread_max` -- maximum allowed bid-ask spread, expressed as either an absolute dollar amount or a percentage of mid (`{max: 0.05}` for $0.05 abs; `{max_pct_of_mid: 0.10}` for 10%-of-mid). Resolver computes per contract from the chain's quote at evaluation time.
- `open_interest_min` -- minimum open interest per contract leg. Resolver reads `OptionContract.open_interest` from the chain.
- `daily_volume_min` -- minimum 30-day average daily volume on the underlying (equity bars). Resolver reads from the standard yfinance/alpaca bar trail.
- `iv_rank_band` -- IV rank window `{min: 30, max: 70}` (percentile of trailing-1y IV). Resolver computes rolling-1y IV percentile from chain's IV history (when ADR-0028 D7 parquet is available) or falls back to chain's current IV vs. the underlying's HV30 (degraded but present).

YAML shape:

```yaml
risk_liquidity:
  bid_ask_spread_max:
    max_pct_of_mid: 0.10
  open_interest_min:
    min: 100
  daily_volume_min:
    min: 1_000_000
  iv_rank_band:
    min: 25
    max: 75
```

Composition (this ADR D4 `compose` block) is unchanged in spirit; `risk_liquidity` rules participate in the AND-aggregation at the methodology level and the silence-by-default disposition is honored (any single `risk_liquidity` rule that flags an asset silences the methodology's vote on that asset). The methodology library's aggregation across methodologies (committee, ADR-0023) treats `risk_liquidity` failures the same as any other rule failure -- they suppress the methodology's vote, which then suppresses the asset's eligibility under that methodology only. Other methodologies' votes on the same asset are independent.

The schema validator (`hermes_quant/methodology/schema.py`) gains `risk_liquidity` as a fourth top-level key, accepted as a dict with keys drawn from the registry above. Unknown keys in `risk_liquidity` are rejected at YAML-load time with `MethodologySchemaError("unknown risk_liquidity primitive: ...")`. Custom (registry-extending) primitives must be added in a NEW ADR amendment, not by methodology authors directly -- this keeps the registry small and audit-friendly.

The global floor (the `daily-picker` recipe-level defaults that no methodology can loosen) is configured at recipe load time:

```yaml
# hermes_quant/recipes/daily_picker.yaml
risk_liquidity_floor:
  bid_ask_spread_max:
    max_pct_of_mid: 0.20
  open_interest_min:
    min: 50
  daily_volume_min:
    min: 250_000
  iv_rank_band:
    min: 0
    max: 100
```

A methodology's `risk_liquidity` is merged with the floor at `Methodology.evaluate` time: per-rule, the STRICTER of (methodology, floor) wins. A methodology setting `open_interest_min.min: 25` is silently coerced to `50` (the floor wins because it's stricter); a methodology setting `open_interest_min.min: 500` keeps `500` (methodology wins because it's stricter than the floor). The coercion is logged but not loud -- the operator's intent is presumed to be "at least this strict," and the floor is informational.

### Why

The synthesis (P0-8) flagged this as inevitable namespace creep within weeks of first reel ingestion: bid/ask, OI, volume, IV-rank are universal liquidity floors that every methodology will want some version of, and forcing every YAML author to copy-paste a "boilerplate liquidity block" into `options_chain` would breed inconsistencies. Promoting them to a first-class namespace `risk_liquidity` keeps the abstraction clean, lets a small number of resolvers handle the rules consistently, and crucially keeps the `risk` namespace (the immutable risk-gate spec from ADR-0027) invariant under methodology authoring pressure. The merge-with-floor rule preserves the "global floor cannot be loosened" property without making methodologies hard to author.

### Affected sections of this ADR

- D1 "Methodology DSL" (lines 25-46) -- adds `risk_liquidity` as the fourth namespace; example YAML blocks updated in subsequent amendments / revisions.
- D4 composition arithmetic -- `risk_liquidity` rules participate in AND-aggregation per methodology; silence-by-default preserved.
- Section 7 "Test Plan" -- additions:
  - Schema validation: every `risk_liquidity` block in shipping YAMLs validates against the registry.
  - Floor merge: a methodology with looser-than-floor rules is coerced to floor; a methodology with stricter-than-floor rules keeps its values.
  - Rejection of unknown primitives: a YAML with `risk_liquidity.foo_bar: ...` fails load with `MethodologySchemaError`.
- Implementation map -- `hermes_quant/methodology/resolvers/risk_liquidity.py` (new file) houses the four initial resolvers; the loader registers them.
