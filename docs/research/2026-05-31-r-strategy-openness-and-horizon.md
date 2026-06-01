# Research Study — Strategy Openness, Multi-Leg Deliberation, and the Horizon Model

**Status:** synthesis study (recon + external research → architecture recommendation)
**Date:** 2026-05-31
**Author:** hermes-quant architect subagent
**Decisions that fell out:** [ADR-0082](../adr/ADR-0082-deterministic-structure-selection-layer.md) (structure-selection layer),
[ADR-0083](../adr/ADR-0083-defer-intraday-build-horizon-neutral-foundations.md) (defer intraday; build horizon-neutral foundations first).

> **The rail this whole study respects.** The deterministic gate is the FINAL authority
> (ADR-0004/ADR-0079 D-1; options_gate `from_gate_result` seam, `multileg.py:99-185`). LLM /
> committee / semantic / social are *evidence that can only silence, never authorize*. Nothing
> below puts an LLM, a debate, or a learned model on the money path. Every new capability is
> default-OFF, eval-gated, and reversible. This is money-software; silence-by-default is the
> safe failure mode.

---

## 0. The operator's three questions, answered up front

1. **Is the pipeline strategy-OPEN or fixed-play?** → **Fixed-play today; recommend OPENING the
   *play/eligibility* layer mechanically (registry-of-plays mirroring the existing
   analyst/aggregator entry-point discovery), and adding a *deterministic* structure-selection
   layer — NOT a free LLM chooser.** (§1, §4.1; ADR-0082.)
2. **Does multi-leg flow through bull/bear/judge + risk-committee, or only the gate?** → **Only
   the gate. The deliberation layer is 100% equity/direction-only; multi-leg structure is bolted
   on at the deterministic gate and never deliberated. Recommend: keep deliberation at coarse
   *intent* granularity (an optional `structure_intent` enum on `ResearchPlan`), feed a
   deterministic stance×IV-regime selection matrix, then the existing gate. The debate NEVER picks
   legs/Greeks/strikes.** (§2, §4.1; ADR-0082.)
3. **Interday-only or long-horizon-intraday?** → **Fundamentally interday (daily-bar anchored,
   fire-once-per-ET-day). Recommend DEFER intraday; build the two horizon-neutral foundations now
   (still-forming-bar fix, settlement v0.1.2 exit-fill join). Intraday is unmeasurable and
   economically unmotivated until those land.** (§3, §4.2; ADR-0083.)

---

## 1. Current state — STRATEGY SELECTION IS FIXED (hardcoded 5-play registry, no optimizer)

The play set is a frozen, code-level rule-profile registry; there is **no best-fit selector/argmax**.

- `playbook/profiles.py:235` — `PROFILES: dict[str, PlayProfile]` literally enumerates the 5 plays
  (`covered_call, csp, wheel, leaps, swing`); each is a module-level `@dataclass(frozen=True)`
  `PlayProfile` (`profiles.py:29`). A play **is** its rule profile (hard/soft/eviction/regime/bias) —
  no behavior, just data. `wheel` is mechanically `covered_call AND csp`, merged at import time
  (`profiles.py:138`).
- The 5 names are **re-hardcoded** as parallel hand-maintained lists: `PLAY_NAMES`
  (`watchlist_evolution.py:55`) and `score_all()`'s fixed 5-key return dict (`scorers.py:337-345`,
  verified: keys `covered_call/csp/...` are literal). Adding a play requires editing ≥4 places; there
  is **no plugin / entry-point / YAML path for plays**.
- **No cross-play winner-take-all.** `score_all(snapshot)` scores ALL 5 plays and returns the full
  per-play dict — it does NOT pick a winner (`scorers.py:337`). Selection is purely per-play
  eligibility (hard rules pass AND score ≥ 0.65 AND not evicted AND not regime-denied,
  `scorers.py:281`). A symbol can be active on multiple plays — "eligible-on-many," not "one best play."
- **Direction×play compatibility is a GATE, not a selector** (`direction_bias.py`): `play_bias` /
  `bias_allows_direction` / `compatible_plays` FILTER structurally-incoherent routes (the AXP-SHORT-
  via-CSP fix); they never rank. `bias` is fixed per profile (cc/csp/wheel/leaps = bullish, swing =
  agnostic; verified `profiles.py:77,110,156,175,208`).
- **The CONTRAST that matters for the recommendation:** ANALYSTS and AGGREGATORS already have an
  entry-point/YAML discovery escape hatch (`recipes.py:256-261,274-279`). **Plays have none.** The
  pluggable seam exists in the codebase — it just hasn't been extended to plays.

**Naming-collision caution (carry into all docs):** three distinct "PROFILES" exist — playbook PLAY
profiles (`profiles.py:235`), RISK profiles (`risk/gate.py:264`, conservative/moderate/aggressive),
and recipe COMPONENTS. A structure-selection module must not collide; key it off `StrategyKind`
(`options/recipes.py:58`, the 3-value Literal) and keep it in `options/`.

---

## 2. Current state — MULTI-LEG IS GATED, NOT DELIBERATED (the two pipelines are physically disjoint)

The agentic deliberation + risk layer reasons ONLY about **direction/conviction on a single
underlying**. Options structure is gated structurally by a self-contained deterministic gate and is
**never deliberated**. No import edge, no shared contract field carries leg/Greek/structure into
deliberation (grep across `agents/`, `aggregators/`, `analysts/`, `advisor.py` for
`strategy_kind|OptionLeg|net_greeks|MultiLegProposal|...` is CLEAN).

- **Judge output contract** `ResearchPlan` (`research_debate/schemas.py:105`) carries ONLY:
  `recommendation: PortfolioRating` (5-tier SELL..BUY), `confidence`, `rationale`,
  `strategic_actions`, `horizon_emphasis: 1d|1w|1M`, `counterarguments`. **No leg/Greek/defined-risk/
  strategy_kind field anywhere.** (Verified: `structure_intent` does not exist in the codebase — it is
  greenfield.)
- **Risk committee** (`risk_committee/`) reasons only about single-underlying sizing/stops; its only
  output lever is a `silence_multiplier ∈ [0,1]` that can ONLY reduce `size_fraction`
  (`committee.py:110`). No leg/Greek reasoning.
- **The core contract is direction+magnitude only.** `AnalystView`/`AggregatedSignal`
  (`protocol.py:90/135`) carry `direction(-1|0|1)`, `magnitude`, `confidence`, `horizon` — no structure
  field. `AssetClass` at `protocol.py:48` explicitly states `'option' deferred to v0.2 (requires
  Greeks-aware sizer per ADR-0009 §P2-options)`. The type system was designed before options.
- **Structure is bolted on at the gate.** `options_gate.py:345` is a fully self-contained deterministic
  authority: 3-bucket classify (`covered_call/cash_secured_put/defined_risk` vs reject-as-naked,
  `:139-191`), max-loss/margin/Greek/BPR/pin-risk checks, deterministic `floor()` sizing, re-checks
  every cap at admitted lot count (`:622-712`). Default-OFF behind `HERMES_QUANT_OPTIONS_GATE=1`. It can
  ONLY reject(silence) or pass-through.
- **The unrepresentable-pass seam.** `MultiLegProposal` (`options/multileg.py:58`) is ONLY mintable via
  `from_gate_result` (`:99-162`); `risk_gate_pass=True` is **unrepresentable** by any other construction
  (`__post_init__` raises, ContextVar lock at `:52-55`). The producer `recipes.build_multi_leg_proposal`
  (`recipes.py:157`) builds legs deterministically via `_pick_by_target_delta` (`:125`, explicitly
  "no LLM") and imports NONE of the deliberation contracts.

**Consequence:** the bull/bear judge cannot say "sell a 30-delta covered call vs buy stock"; it can
only say BUY/OVERWEIGHT/HOLD on the underlying. There is no seam by which deliberation chooses or
critiques an option STRUCTURE.

---

## 3. Current state — HORIZON IS FUNDAMENTALLY INTERDAY (daily-bar anchored, fire-once-per-ET-day)

A partial multi-horizon fan-out exists but extends only UPWARD (1d→1w/1M/1Q), never to intraday.
Intraday plumbing exists in three places but is never wired into production crons, and several core
assumptions are hard-coded to a daily bar / per-day holding.

**What is intraday-READY (low-cost wins, no daily assumption):**
- `Timeframe` Literal includes intraday (`protocol.py:45`); `recommend()` intraday lookback math is
  complete (`advisor.py:62-69,792-798,1072-1081` — `recommend(symbol, timeframe="1h")` works end-to-end
  today). Three analysts (semantic/microstructure/classical_ta) are intraday-capable.
- The no-lookahead chain is timeframe-agnostic: `fetch_bars(as_of=)` (`protocol.py:505`), redundant asof
  filter (`advisor.py:848`), `lookahead_gate.py:113` (pure `available_at > asof`), decision-time vs
  bar-time split (`advisor.py:1059`, ADR-0068).

**What is hard-coded INTERDAY (the blockers):**
- **(a) Cron cadence:** every production entry point hardcodes `"1d"`
  (`quant-daily-interim.py:835`, `quant-autonomous-tick.py:121`, `quant-playbook-tick.py:516`
  `_ALLOWED_HORIZONS=("1d","1w","1M","1Q")` drops 1h/15m). The hourly tick is READ-ONLY monitoring +
  an optional propose/fire that just delegates to the 1d playbook (`quant-hourly-tick.py:445/467`).
- **(b) `open_guard.py` — per-ET-day, same-daily-bar dedup (ADR-0072):** keys idempotency on
  `(symbol, sign(direction))` per ET CALENDAR DAY (`:122,154,165,179`). Structurally INCOMPATIBLE with
  intraday cadence; `allow_intraday_add=True` (`:217`) is the only per-pick escape.
- **(c) Per-day journal idempotency:** `fired_today` (`autonomous-tick.py:156`),
  `fired_today_pairs` (`playbook-tick.py:162`) gate on `date_et == today`.
- **(d) Risk gate per-day budgets:** daily-loss breaker vs `daily_open_equity` (`protocol.py:281`),
  `_next_session_open` is daily-granular (`gate.py:548`). **Kelly + cost gate are calibrated to PER-DAY
  vol** (`_BOOTSTRAP_VOL_BY_ASSET_CLASS`, `advisor.py:86`, "~1.2% per-day stdev"; `kelly.py:131`,
  `gate.py:491`). At 1h the per-period stdev is ~2.5x smaller → Kelly `f*=edge/σ²` balloons AND the
  fixed `cost_multiple × round_trip_cost` threshold (`gate.py:460`) silences almost everything. The
  cost/vol model has **no horizon scaling**.
- **(e) `options_gate.py` per-day Greek budgets:** `theta_budget_pct_nav_per_day=0.02` (`:74`),
  `min_dte_for_new_entry=7` (`:79`).
- **(f) Still-forming-bar discipline is daily-ONLY:** `drop_still_forming_bar` early-returns
  `non_daily_timeframe` for anything except `"1d"` (`bar_alignment.py:117`, verified). An intraday read
  mid-bar would silently use a PARTIAL bar's close as `last_close` — reintroducing the exact
  replay-equality / calibration-drift bug ADR-0069 fixed for daily. `advisor.py:868` calls this
  unconditionally.
- **(g) Settlement cannot measure a multi-hour hold:** `settlement_loop.py` is a v0.1.1 shell — per-fill
  SLIPPAGE only, NO exit-fill joining, calibrator updates GATED off (`_calibration_quality="slippage_only"`,
  `:40-48,77-78`, verified). The `horizon` field already defaults to `"1h"` (`:168`) — plumbing ready,
  exit-time math not.

---

## 4. External findings → architecture recommendation

### 4.1 Strategy-openness (the play registry AND structure into deliberation)

**Convergent external signal (4 named repos + production specs + industry):** the field overwhelmingly
separates "LLM proposes / deterministic layer decides," and structure-selection — *when it exists at
all* — is a small **RULE TABLE keyed on stance × IV/vol-regime**, NOT free LLM choice.

- **TradingAgents (Tauric)** == hermes deliberation TODAY: bull/bear + risk committee + PM are
  DIRECTION-only, 5-tier `PortfolioRating`, no strategy registry, no options. **So the recon verdict
  "deliberation is direction-only" is not a hermes gap — it is the field's default.** No surveyed repo
  with a debate makes the debate pick option structure under a deterministic constraint.
- **ai-hedge-fund (virattt):** "pluggable strategy layer" is an ANALYST registry (`ANALYST_CONFIG`) —
  perception is pluggable, NOT the trade structure. `compute_allowed_actions()` deterministically
  enumerates legal actions+max-quantities BEFORE the LLM; the LLM may only PICK from the pre-validated
  set. This is hermes' "deliberation proposes, gate is final authority" applied to action+size.
- **Vibe-Trading (HKUDS):** the ONLY repo that reasons about option STRUCTURE — a skill-library registry
  + `analyze_options` (BSM+Greeks). BUT the agent picks structure FREELY via ReAct; deepwiki confirmed
  **NO deterministic gate/validator** over the choice. **This is precisely the model hermes' rails
  forbid — the cautionary case, not the template.**
- **Production structure-selection = deterministic stance × IV-regime MATRIX** (independent convergence:
  ROT-TECH Stage-8, Iyer regime analyzer, VolatilityBox, jenova, OptionAlpha): rows = stance
  {bullish/bearish/mixed}, cols = IV-regime gate (e.g. ATM IV > 0.50 / IV-rank thresholds). LLM supplies
  only thesis/stance/confidence; the structure is chosen by the matrix; max-loss computed
  deterministically. Volatility-FIRST: IV-regime decides sell-vs-buy premium BEFORE direction picks the
  family.
- **The strongest prior-art for hermes' exact rail:** thesis-agent ("LLM reasoning, math decides";
  LLM weighted ~15%, validated identical to math-only); RakshaQuant (Regime→Strategy router→deterministic
  gates with VETO); Shannon/Dnalyaw ("risk engine physically incapable of being bypassed by a rogue LLM —
  not policy, architecture; LLM outputs are just columns in a feature matrix"). This mirrors hermes'
  `from_gate_result` unrepresentable-pass seam exactly.

**RECOMMENDATION — YES, on a NARROW, two-part, default-OFF, eval-gated basis:**

**Part A (lower risk, do first) — make the PLAY layer registry-open, mechanically:**
1. **Derive `score_all()`/`PLAY_NAMES`/wrappers from `PROFILES`** (single source of truth). Kills the
   3-4 hand-maintained parallel lists. Pure refactor, zero behavior change, no rail touched.
2. **Add a YAML/entry-point PLAY loader** mirroring the existing analyst/aggregator discovery
   (`recipes.py:256-279`). New plays load default-OFF; a play with no `bias` is treated incompatible
   (keep the default-bullish silences-SHORT behavior). Strategy-OPEN at the perception/eligibility layer,
   **without a cross-play optimizer** (the field does NOT converge on a winner-take-all selector — keep
   "eligible-on-many").

**Part B — add a DETERMINISTIC STRUCTURE-SELECTION layer between deliberation and the gate; deliberation
only PROPOSES a coarse intent, never the legs:**
1. **Additive contract change:** an OPTIONAL `structure_intent` enum on `ResearchPlan`
   (`{none, defined_risk_credit, defined_risk_debit, premium_capture, long_premium}`) + a `defined_risk`
   flag — coarse INTENT, not legs/Greeks/strikes. Absent → today's equity path (silence-by-default
   preserved; Pydantic optional, default `none`). The bull/bear/judge can ARGUE "thesis is range-bound,
   prefer premium capture" — legitimate qualitative reasoning the debate is good at.
2. **A deterministic structure-selection table** (new module `options/structure_select.py`) maps
   `(direction from AggregatedSignal, structure_intent, IV-rank/regime) → StrategyKind`, exactly like
   ROT-TECH Stage-8 / Iyer. Codified knowledge, no LLM, no optimization. Selects ONLY among
   gate-admissible buckets (`covered_call/cash_secured_put/defined_risk`) — never naked. Out-of-table or
   non-defined-risk → return `none` → silence.
3. **The selected `StrategyKind` feeds the EXISTING producer** `recipes.build_multi_leg_proposal` →
   `options_gate` → `from_gate_result` mint. The gate stays the SOLE authority on legs/Greeks/max-loss/
   BPR/pin-risk/sizing; the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}` and kill-switch are
   untouched. `structure_intent` is advisory input to a deterministic selector, **never a money-path lever**.
4. **Risk committee:** do NOT add leg/Greek reasoning to personas. Keep their only lever the
   `silence_multiplier ∈ [0,1]`. Structural risk lives entirely in `options_gate`.

**Guardrails (all required):** new default-OFF flag; eval-gate the matrix on labeled IV-regime episodes
with **as-of-honest IV-rank** (no future IV in the 52-week window — same discipline as
`bar_alignment`/`lookahead_gate`); absent/no-match/non-defined-risk → SILENCE; the deliberation→selector→
gate edge is the ONLY new contract; `from_gate_result` seam stays intact.

### 4.2 Horizon (long-horizon intraday: BUILD-behind-flag / DEFER / DO_NOT)

**RECOMMENDATION — DEFER (build the two horizon-neutral foundations now; DEFER intraday).** Grounded in:

- **The edge is naturally multi-day; intraday targets alpha the system cannot reach.** hermes' edge is
  catalyst + social-arb. The *intraday* slice of that alpha decays in MINUTES (Context Analytics: a 15-min
  lag erases most intraday sentiment alpha; tweet half-life ~80 min). hermes' fastest cadence is the
  hourly tick (read-only, delegates to 1d) — it systematically enters AFTER the intraday edge has decayed.
  Meanwhile the *durable* catalyst alpha is interday (PEAD/post-announcement drift over days 2-75). The
  daily system is already aimed at the right horizon.
- **A flat-by-close intraday mode would forfeit the overnight risk premium the daily system already
  harvests** (Concretum: long CAGR 7.3%→13.4%, Sharpe 0.76→0.97; shorts bleed). The bullish-biased,
  multi-day, fire-once system structurally captures this.
- **PDT is a NON-ISSUE** — it applies only to live margin accounts (never paper) and is being eliminated
  effective 2026-06-04. Do not let it drive design.
- **The rollout is unmeasurable until settlement v0.1.2 lands.** The eval gate can only see slippage, not
  horizon return (`settlement_loop.py:40-48`) — an eval-gated intraday rollout can never legitimately pass.
- **Retail intraday base rates are dismal** (97% of >300-day day-traders lose net). Burden of proof is
  high; silence-by-default argues against a high-turnover surface with no demonstrated edge.

**PHASE 0 (DO NOW — horizon-neutral, no rail impact, ship regardless of intraday):**
1. **Fix `drop_still_forming_bar`** (`bar_alignment.py:117`) to apply a per-timeframe bar-boundary/
   session-close cutoff for intraday TFs instead of early-returning `non_daily_timeframe`. Closes a
   no-lookahead honesty hole that bites the instant ANY 1h/15m read happens. Pure correctness.
2. **Land settlement v0.1.2 exit-fill join + horizon-return math** (`settlement_loop.py:29-48`),
   un-gating calibrator updates once direction-over-horizon is computed. **This is the measurement
   instrument every eval gate depends on, daily OR intraday.** Without it, no horizon claim is verifiable.

**PHASE 1 (ONLY IF a deferred intraday mode is later justified by a demonstrated, measurable edge —
default-OFF flag, eval-gated):**
1. **Bar-time idempotency:** re-key `open_guard.py` (ADR-0072) and `fired_today`/`fired_today_pairs` on
   `(symbol, direction, bar_ts)` / a holding-period token, NOT ET date.
2. **Horizon-scale the SIZING INPUTS (never the ladder):** make `_BOOTSTRAP_VOL_BY_ASSET_CLASS`
   per-timeframe or apply a √(horizon) scaler in Kelly (`kelly.py:131`) and a horizon-aware cost-gate
   threshold (`gate.py:460`). The `{0,±0.05..±0.20}` ladder stays immutable as the post-scale ceiling.
3. **Intraday session model** for the daily-loss breaker + `_next_session_open` (`gate.py:548`): scope
   daily-loss to the trading day but define intraday resume; keep per-day max-loss as a hard breaker.
4. **Fix Kronos horizon labeling** (`kronos.py:107` `horizon_label`, `:747` `freq="B"`) to derive horizon
   + y-timestamp freq from `ctx.timeframe`.
5. **Stand up a true intraday cron** (the hourly tick is the natural host but today only delegates to 1d).

**DESIGN STANCE for Phase 1:** a long-horizon intraday mode should hold ACROSS the close when the daily
thesis is long (harvest the overnight premium) — a "finer-entry, multi-session-hold" variant of the daily
strategy, NOT a flat-by-EOD day-trading strategy. Aligned with the catalyst/social-arb multi-day edge.

---

## 5. Rails-preservation summary (what NONE of this touches)

- The deterministic risk gate + `options_gate` stay the FINAL authority. No LLM/debate/learned model on
  the money path.
- The discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}` is immutable; only the Kelly/cost INPUTS that
  feed it would ever be horizon-scaled (Phase 1 only).
- The kill-switch (`hermes quant halt '*'`) is downstream of every flag.
- `from_gate_result` / `risk_gate_pass`-unrepresentable seam stays intact.
- Every new capability: default-OFF, off-state byte-identical, eval-gated, operator promotes.

---

## 6. Sources

External research is captured in full in the orchestrator's research bundle (DEEPWIKI:
TradingAgents, ai-hedge-fund, AI-Trader, Vibe-Trading; EXA: ROT-TECH Stage-8, thesis-agent,
RakshaQuant, Iyer regime analyzer, kevmyung swing-trading-agent, heygotrade; TAVILY: VolatilityBox,
jenova, Concretum overnight premium, Context Analytics sentiment decay, arXiv 2512.00280 retail
horizon, arXiv 2302.09654 tweet half-life, NautilusTrader bar-close discipline, square-root-of-time
vol scaling). Repo recon cites are inlined per claim above (`file:line`), all verified against the
working tree 2026-05-31.
