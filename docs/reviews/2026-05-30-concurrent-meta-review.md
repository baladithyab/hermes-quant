# Concurrent Meta-Review — Deduplicated NEW Backlog

**Date:** 2026-05-30
**Scope:** Synthesis of 31 findings from 5 concurrent audit lenses (integration-gaps, pdr-impl-drift, operational-readiness, test-eval-coverage, fresh-eyes-vision) over the 20-commit deep-work loop `e4ecad5..HEAD`.
**Posture:** Money-software, paper-only, PDR-architected (ADR-0079). Everything new is DEFAULT-OFF behind `HERMES_QUANT_*` flags.
**Mandate:** Find what the per-wave reviewers + Codex MISSED — gaps, regressions, missing edge cases, incomplete implementations, missing backlog items. Read-only.

**Lens legend:** `INT` integration-gaps · `DRIFT` pdr-impl-drift · `OPS` operational-readiness · `TEST` test-eval-coverage · `VIS` fresh-eyes-vision.

**Backlog of record:** `docs/research/2026-05-30-backlog-consolidated.md` (51 items B01–B51, committed at `e7abf50` — **BEFORE** the ADR-0079 capstone `1b40153`, so it predates the whole PDR-1..4 wave structure). `NEW` = not tracked there; `KNOWN` = tracked but with a gap/status-drift this review records.

---

## P0 — must precede any live enablement

### M01 · Session-edited trading scripts are shipped-to-repo but NOT LIVE; no deploy step exists
**Lenses:** OPS · (root cause behind several others)
**Where:** `~/.hermes/scripts/quant-autonomous-tick.py` etc. — verified: `diff -q` shows **all four** session-edited scripts (`quant-autonomous-tick.py`, `quant-daily-interim.py`, `quant-playbook-tick.py`, `quant-watchlist-evolve.py`) **DIFFER** from `ops/scripts/`. Deployed `quant-autonomous-tick.py` has **0 hits** for `DIRECTION_BIAS` / `CATALYST_ONBOARDING` / `ADMISSIBILITY`. No `.hermes/scripts`/copy/deploy steps in `docs/operations/ROLLOUT.md` or ADR-0062.
**Why it matters:** Live Hermes crons exec the DEPLOYED stale copies. The direction-bias gate (B04, `73d7ed4`), catalyst PDR wiring (`2e84f28`), and ADR-0075 onboarding seam (`3776a35`) are absent from production. **The AXP-SHORT-via-CSP bug the loop "fixed" is still firing live.** This is the root cause that makes every shipped fix non-live.
**Status:** **KNOWN-but-understated.** B51 scopes only ONE file (`quant-daily-interim.py`) as P2/S. The other 3 session-edited scripts have no deploy tracking, and the *anti-drift mechanism* (M02) is not tracked at all.

### M02 · No anti-drift mechanism: nothing verifies deployed scripts match repo (Issue #23 closed one file, left the class open)
**Lenses:** OPS
**Where:** Repo-wide — no test/CI/checksum comparing `ops/scripts/` ↔ `~/.hermes/scripts/`. Six deployed scripts differ from repo today.
**Why it matters:** For money-software where the deployed copy decides real (paper) capital, recurrence of drift is unguarded. This is the single biggest operational gap and should be an explicit deliverable of the in-flight ops-docs workflow (one-way sync + checksum manifest + CI drift check).
**Status:** **NEW** as a distinct item (B51 records only the desire for a generic mechanism for one file; the anti-drift *test/CI* is untracked).

### M03 · B01 multi-leg reactor was NOT built this session — multi-strategy plays still cannot fire (the single biggest vision gap)
**Lenses:** VIS · INT (confirmatory)
**Where:** `ops/scripts/quant-playbook-tick.py:73` `EQUITY_PLAYS={"swing","leaps"}` (verified — covered_call/csp/wheel filtered out at :147); `hermes_quant/react/multileg.py:66` raises `MultiLegReactorDisabled`; `hermes_quant/risk/options_gate.py:375` raises `OptionsGateDisabled` — both unwired into any proposal path (verified).
**Why it matters:** The vision is a MULTI-strategy engine (CC/CSP/wheel/LEAPS). This session shipped OCC-21 parse + collateral-secured gate + inert reactor scaffold, all default-OFF and routed nowhere. 22-of-25 universe signals (the SHORT/options majority) still cannot be expressed. Every other built-but-off feature (catalyst, admissibility, saturation) only pays off once the system can ACT on these plays. **B01 must be the next loop's headline.**
**Status:** **KNOWN** (B01, P0, blocked on B02/B03 which DID ship `6b1c05d`). Status drift: B02/B03 should move `proposed → built-default-OFF (gated)`.

### M04 · B04 direction-bias fix would ship DORMANT even after redeploy — armed wrapper never sets the flag
**Lenses:** OPS
**Where:** `~/.hermes/scripts/quant-autonomous-tick-armed.sh` — verified: sets `REFLECTION=1`, `PORTFOLIO_CAPS=1`, `PAPER_SLIPPAGE_MODEL=v0.2` but **NOT** `HERMES_QUANT_DIRECTION_BIAS_GATE`. The fix at `ops/scripts/quant-autonomous-tick.py:323` gates behind that flag (default-0).
**Why it matters:** Even after redeploying the new script, the direction-compatibility screen stays OFF. A redeploy alone gives a false sense the SHORT-via-bullish-play bug is fixed. The deploy runbook must couple script-copy WITH the wrapper flag flip — a two-part dependency tracked nowhere.
**Status:** **NEW.**

---

## P1 — blocks safe enablement of a specific feature

### M05 · Admissibility (ADR-0077) gates ONLY the autonomous-tick decision seam — daily-interim brief/HITL/auto-approve path and PaperReactor itself are NOT admissibility-aware
**Lenses:** INT (primary) + DRIFT (ordering) — **same gap, two lenses, high confidence**
**Where:** `hermes_quant/autonomous.py:522` (only live wiring); `ops/scripts/quant-daily-interim.py` (verified **0** refs to `select_oracle`/`oracle.verdict`/`admissibility`); `hermes_quant/react/paper.py` (verified **0** admissibility refs); `hermes_quant/tools.py` `quant_approve → PaperReactor.execute()`.
**Why it matters:** The risk gate can emit a negative `target_size` for shorts. Those flow into the daily-interim brief (renders `🔴 SHORT`) and, with `HERMES_QUANT_AUTONOMY=paper`, auto-fire via `quant_approve → PaperReactor` — none of which runs `select_oracle()`. When the operator flips `HERMES_QUANT_ADMISSIBILITY=1` (workflow A's goal), an **inadmissible short still executes on paper** through this seam. PDR §1.1 calls admissibility a "REACTION-layer fidelity gate" yet it is wired at the Decision seam in ONE path and the reactor (the actual Reaction layer) is unaware of it.
**Status:** **NEW** (B04 is the unrelated direction-bias gate; no item tracks the reactor/brief admissibility seam).

### M06 · Live admissibility seam runs POST-gate, contradicting ADR-0077 D77.4 and the just-corrected ADR-0079 "ADMIT before GATE" ordering
**Lenses:** DRIFT
**Where:** `hermes_quant/autonomous.py:522,559` — verified: admissibility reads `kelly_fraction` → `effective_size` and runs **after** FUSE → GATE → silence-bias → portfolio-caps. ADR-0077:152 + `pdr-unified-architecture.md:110-111` say ADMIT is an UPSTREAM pre-gate precondition; Codex Facet-5 just corrected the ADR diagram to `SATURATE→FUSE→ADMIT→GATE`. Code does the opposite.
**Why it matters:** Currently masked (flag OFF + REJECT-only silences), but it is a documented-architecture-vs-code contradiction. Admissibility cannot see the pre-gate signal it was specified to gate. PDR-1 must untangle this.
**Status:** **NEW.**

### M07 · ADR-0077's 38-short premise is unasserted by any test/eval AND the live wiring fails-closed on EVERY short (no account context plumbed)
**Lenses:** TEST
**Where:** `hermes_quant/autonomous.py:551` — verified: builds `AdmissibilityContext(current_ask=price)` with `account_equity`/`available_bp` NOT plumbed (comment at :549 calls it a documented gap → live oracle returns `MISSING_ACCOUNT_CONTEXT`). `tests/unit/test_admissibility_restate.py` uses a 4-short synthetic db, not real `state.db`.
**Why it matters:** ADR-0077's entire justification ("38 synthetic shorts are untradeable live") is unvalidated. Worse, when `HERMES_QUANT_ADMISSIBILITY=1` the live oracle silences EVERY short — fail-closed but useless. No test proves an ETB short survives the live wiring. **Account-context plumbing is the blocker before the premise can even be measured** and is tracked nowhere.
**Status:** **NEW.**

### M08 · Flag-flip ordering (fix Codex #11 unit bug BEFORE flipping `HERMES_QUANT_ADMISSIBILITY`) lives only in a review artifact
**Lenses:** OPS
**Where:** `docs/operations/ROLLOUT.md` (no `effective_size`/#11/admissibility-flip ordering); ADR-0077 (no "do-not-enable-until-#11" warning); only in `docs/reviews/.../synthesis.md`. The bug: `autonomous.py:534-541` converts NAV-fraction → share-qty; with #11 unfixed every short rejects as `FRACTIONAL_SHORT`.
**Why it matters:** An operator following existing rollout docs has no gate stopping a premature flip that silences ALL shorts. The feature-enablement runbook (task #13, doesn't exist) must carry this as a hard precondition.
**Status:** **NEW.**

### M09 · DEPLOYED armed wrappers already hard-set `PORTFOLIO_CAPS=1` and `SLIPPAGE=v0.2` — B12 is silently ON in production
**Lenses:** OPS
**Where:** `~/.hermes/scripts/quant-autonomous-tick-armed.sh` lines 42,48 (verified `PORTFOLIO_CAPS=1`, `PAPER_SLIPPAGE_MODEL=v0.2`); `quant-playbook-tick-armed.sh`.
**Why it matters:** Codex synthesis asserts "no new flag is hard-set to 1 anywhere in repo/deploy (grep-verified)" — but that grep covered the REPO only. The DEPLOYED wrappers (outside git) already enable B12's two promotions in live armed crons. **B12 is not "gated/pending" — it is silently ON.** Repo/deploy posture has diverged with nothing reconciling it.
**Status:** **KNOWN-but-wrong-status.** B12 marked `gated`; it is actually live in deploy. Needs reconciliation + a repo↔deploy posture audit (overlaps M02).

### M10 · Two new crons (catalyst-profitability, calibrator-drift) are neither deployed nor registered
**Lenses:** OPS
**Where:** Verified: both MISSING from `~/.hermes/scripts/`; 0 entries in `~/.hermes/cron/jobs.json`. Exist only as `ops/scripts/quant-catalyst-profitability.py`, `quant-calibrator-drift.py`.
**Why it matters:** B06 requires profitability wired into a firing cron to clear `MIN_SAMPLE=20` brand_self propagations before B07 can raise the consumer-trend haircut; B11 requires calibrator-drift weekly. **The B06/B07/B11 feedback loops are dead-on-arrival.** Backlog tracks the capabilities but NOT the deploy+register step.
**Status:** **NEW** (deploy+register step). B06/B11 capabilities are KNOWN.

### M11 · calibrator-drift cron violates the `no_agent` silence contract — will spam the operator every run
**Lenses:** OPS
**Where:** `ops/scripts/quant-calibrator-drift.py:136,138,148-156` — verified: unconditional `print()` of "collecting pairs", full DRIFT RESULT JSON, and auto_refit line on every run, vs `scheduler.py:1234` ("empty stdout → silent run, no delivery"). Siblings `quant-catalyst-coverage.py`/`-profitability.py` correctly return empty stdout when nothing changed.
**Why it matters:** As a weekly `no_agent` watchdog (B11), it cries wolf every Monday with zero drift, training the operator to ignore it. Needs a state-baseline/transition gate (alert only when `should_alert` flips) BEFORE registration.
**Status:** **NEW.**

### M12 · ADR-0075 onboarding eval-gate axis is named-but-unbuilt — the gate blocking `HERMES_QUANT_CATALYST_ONBOARDING` does not exist; scanning seam is already live-wired
**Lenses:** TEST · VIS (sequencing)
**Where:** ADR-0075:120-124 (eval-gate precondition) vs `ops/scripts/quant-catalyst-eval-gate.py:100-146` (never calls `catalyst_admissions()`, never simulates a fillable order, LUNR only a precision symbol). Scanning seam already live at `quant-watchlist-evolve.py:267-298`.
**Why it matters:** ADR-0075 keeps the flag OFF until an eval proves the LUNR/Blue-Origin case yields (1) admission, (2) correct direction, (3) fillable simulated order. That axis is unbuilt; unit tests use INJECTED packets + stubbed `tradeable()`. **Only the flag separates the scanning seam from firing**, with no real end-to-end gate.
**Status:** **NEW** (B05 tracks the feature, not its blocking eval gate).

### M13 · PerceptionFrame + PDR-1..4 are 100% aspirational AND absent from the backlog of record
**Lenses:** DRIFT · VIS · INT — **three lenses, one structural gap, highest confidence**
**Where:** Verified: **0 code hits** for `PerceptionFrame`/`TrendVelocity`/`SaturationScore`/`ConvergenceValidator` in `hermes_quant/`; no `perception/` package. Backlog verified: **0 PDR-1..4 references** (committed `e7abf50`, before capstone `1b40153`). Defined only in `docs/design/pdr-unified-architecture.md:277-388` + ADR-0079:189-192.
**Why it matters:** ADR-0079's central carrier object exists only on paper. The C2-2 fix wired semantic packets into all 3 live paths via **three non-uniform seams** (daily-interim inline `market_extras=`; autonomous monkey-patches `advisor_recommend`; playbook inline at a different call site) — exactly the GAP-D "three disjoint side-channels" PDR-1 makes structurally impossible. Because the backlog predates the capstone, the four PDR primitives the session's OWN capstone declares the next-wave critical path are invisible to the next loop's backlog-audit phase. **This is why `backlog_complete=false`.**
**Status:** **NEW** — fold PDR-1 (PerceptionFrame, first), PDR-2 (TrendVelocity), PDR-3 (ConvergenceValidator), PDR-4 (SaturationScore) into the backlog with their dependency order and eval gates.

### M14 · The self-improving loop did NOT advance toward the per-trade → weekly → monthly cadence the vision names
**Lenses:** VIS
**Where:** `hermes_quant/memory/reflector.py:554` (reflect gated OFF behind `HERMES_QUANT_REFLECTION`/`_REFLECTOR_LLM`); `quant-playbook-weekly.py` is a REBALANCE cron, not pattern-mining; no monthly meta-retro cron; B10 learned-graph mining still open.
**Why it matters:** The session added more EVIDENCE producers (social, catalyst, PMCC shadow) but did not advance the feedback loop that turns evidence into improved policy. The weekly pattern-mining retro + monthly meta-retro do not exist and are not in the backlog as distinct items.
**Status:** **PARTLY-NEW.** B10 (learned-graph mining, "the moat") KNOWN; the weekly-pattern-retro and monthly-meta-retro crons are NEW.

### M15 · No enablement-order runbook exists — the next-loop critical path to a live-fidelity firing system is captured nowhere as an executable sequence
**Lenses:** VIS · OPS
**Where:** Verified: `docs/ops/` does not exist (only `docs/operations/`); tasks #13 (feature-enablement runbook) + #14 (cron registry) pending. Order scattered across `synthesis.md` + `pdr-unified-architecture.md:380`.
**Why it matters:** Everything is default-OFF, so the operator's real blocker is KNOWING THE ORDER to flip flags safely. This sequence is the load-bearing artifact for the whole next loop and lives in no single doc.
**Status:** **NEW** (deliverable of in-flight workflow B; see Enablement Path below).

### M16 · Social-arb eval (B09) + AMZN-OOS harnesses read/write `/tmp` and pull live yfinance — non-reproducible, not in CI, no fixtures
**Lenses:** TEST
**Where:** `ops/scripts/quant-catalyst-socialarb-eval.py:80` (reads `/tmp/phase0_labels.json`), `quant-catalyst-socialarb-labels.py:67` (writes `/tmp`), `quant-amzn-weight-oos.py:14` (live yfinance).
**Why it matters:** B07/B09 gate enablement on these evals, but labels live in an ephemeral `/tmp` file (lost on reboot), returns shift every run, nothing in `tests/fixtures` or CI. The knife-edge `3/5=0.60` result cannot be re-verified deterministically — violates AGENTS.md testing discipline (deterministic, fixture-backed, no network).
**Status:** **NEW** (promote the labeled set into a versioned fixture as the B07/B09 prerequisite).

---

## P2 — correctness/coverage holes to close before the relevant flip

### M17 · Semantic packet injection is NOT in `autonomous.tick()` itself — only in the cron's monkey-patch; the `quant_autonomous_tick` TOOL path never injects packets even with the flag ON
**Lenses:** INT · DRIFT
**Where:** `ops/scripts/quant-autonomous-tick.py:329-347` injects `market_extras` via the wrapper; `hermes_quant/autonomous.py:355` calls bare `advisor.recommend` with no `market_extras`; `tools.py:729` calls `tick(dry_run=…)` with NO `advisor_recommend` override.
**Why it matters:** On the tool surface, `HERMES_QUANT_SEMANTIC_ENABLED=1` yields `no_semantic_packets` (silent abstain) — the same G3 decoupling C2-2 claimed to close, still present on the tool path. The wiring docstring's "every path" claim holds for the 3 cron entry points but not the in-process `tick()` API. PDR-1 (M13) closes this structurally.
**Status:** **NEW.**

### M18 · decisions_render + schema_render were BUILT this session (Wave C `4f27d19`) but are ORPHANED — backlog still marks them "open" (unbuilt), masking the half-wired state
**Lenses:** INT
**Where:** Verified: `hermes_quant/memory/decisions_render.py` only re-exported by `memory/__init__.py:24`; `hermes_quant/agents/schema_render.py` has ZERO non-test callers (grep for `render_decisions_md`/`render_schema`/`render_trader_proposal` returns only the definitions + the `__init__` re-export). `cli/status.py` reads the raw `decisions.jsonl` path constant, never calls a renderer.
**Why it matters:** B16 (markdown render over `decisions.jsonl`) + B18 (per-schema `render_X` helpers) are listed `open` but were actually implemented and are dead-ends — nothing in any cron/tool/CLI/brief/committee invokes them. Operators reading the backlog think these are TODO when they are orphaned modules awaiting a call site.
**Status:** **KNOWN-but-wrong-status.** B16/B18 should move `open → built-but-unwired`; add a wire-up item.

### M19 · Profitability verdict is tested at hit_rate 0.7, never at the `MIN_HIT_RATE=0.60` boundary or the `MARGINAL_HOLD` band — the exact knife-edge B07 turns on
**Lenses:** TEST
**Where:** `tests/unit/test_catalyst_integration.py:92-98` (tests 0.7 PROFITABLE / 0.4 UNPROFITABLE); `hermes_quant/catalyst/profitability.py:55-62` (three bands, boundary at 0.60, `MARGINAL_HOLD` 0.5≤hr<0.6 with positive return).
**Why it matters:** B07 raises the haircut only when brand_self clears the 0.60 bar — the untested boundary is precisely the live decision threshold (hr=0.60 PROFITABLE vs hr=0.59 MARGINAL_HOLD). Money-software: the action-vs-hold boundary should be the most-tested line, not skipped.
**Status:** **NEW.**

### M20 · Autonomous-tick wiring regression test reconstructs the wrapper inline instead of importing the shipped `_direction_screened_recommend`
**Lenses:** TEST
**Where:** `tests/unit/test_catalyst_wiring.py:137-176` — hand-copies the wrapper logic into the test body; daily-interim + playbook siblings load the real script via `_load_script()`.
**Why it matters:** This is the GAP-D regression guard, but it cannot catch a regression in the real script — removing the wiring call from the actual autonomous-tick wrapper still passes. The one path the direction-bias fix (B04) also touches is the one whose live wiring is not exercised.
**Status:** **NEW.**

### M21 · No-lookahead release-blocker gate covers only SemanticAnalyst packet consumption, not the perception-producing wiring/onboarding path end-to-end
**Lenses:** TEST · DRIFT
**Where:** `tests/test_no_lookahead.py:75-79,282-286` parametrizes only ClassicalTA + MicrostructureLite (2 of 5 analysts); Invariant-5 (:465-538) covers SemanticAnalyst at analyst level only. `wiring.semantic_market_extras()` defaulting `decision_asof=datetime.now()` (`wiring.py:45`) and `catalyst_admissions()` selecting at `asof=now` (`onboarding.py:84`) are NOT in the release-blocker gate.
**Why it matters:** The code PRODUCING the `extras` dict is ungated; there is no SCAN→onboard→recommend integration assertion that an onboarded symbol can't pull a wider window than its admitting packet justified. PDR-1's eval gate (ADR-0079:189, "frame path byte-identical + no-lookahead gate green") is unsatisfiable until this gate is extended — a hidden precondition.
**Status:** **NEW** (extend the no-lookahead gate as a PDR-1 precondition).

### M22 · Live-broker integration tests (Alpaca admissibility, options chain) are permanently skip-gated and likely never executed — the only place real broker fidelity is checked
**Lenses:** TEST
**Where:** `tests/integration/test_admissibility_alpaca_live.py:19-23`; `tests/integration/test_options_chain_live.py:18-25` (double-gated: `importorskip` alpaca + env flag + paper creds; skipped in CI and dev `.venv`).
**Why it matters:** Per synthesis.md:57-60, the admissibility predicate's correctness against real `easy_to_borrow`/`shortable_shares` is unverified by any reviewer. The 10 Codex post-go-live findings all assume an API response shape these tests are the only check of. Running them with recorded output must be a mandatory pre-flip step (tracked nowhere).
**Status:** **NEW** (add as a pre-flip gate in the enablement runbook M15).

### M23 · Social-arb producer stack got ahead of its consumer — PMCC shadow + social producers built before the reactor/onboarding that make them actionable
**Lenses:** VIS
**Where:** `hermes_quant/shadow/pmcc.py` (counterfactual for the not-yet-existing B01 reactor); `hermes_quant/catalyst/social.py` (Reddit/Trends producers, only GN queries deployed per B08); ADR-0075 onboarding still proposed (B05) → 4/5 consumer-trend targets out-of-universe.
**Why it matters:** NOT wasted work (PMCC shadow is the validation harness that activates when B01 lands; social is the perception side of the Camillo method) — but the build order front-loaded perception over the reaction/onboarding capability that monetizes it. Sequencing note, not drift to remove.
**Status:** **KNOWN-context** (B05/B08 tracked); recorded as a sequencing observation, no new item.

---

## P3 — record-only (correctly deferred; track as the named gate)

### M24 · Saturation property tests ("can only subtract" + "never touches non-semantic views") are promised for PDR-4 but absent — correctly deferred, not tracked as the gate for `HERMES_QUANT_SATURATION`
**Lenses:** TEST · DRIFT
**Where:** `pdr-unified-architecture.md:244-247,388`; verified 0 saturation hits in `tests/`. The shipped `test_authority_boundary_never_amplifies` shows the pattern is achievable.
**Why it matters:** Consistent with the default-OFF rail (PDR-4 is future, flag doesn't exist). Track these two property tests as the named eval gate that MUST exist before `HERMES_QUANT_SATURATION` is introduced.
**Status:** **NEW** (attach to PDR-4 under M13).

### M25 · options_gate + multileg reactor confirmed unwired with a documented scaffold→wired path — no gap, recorded for completeness
**Lenses:** INT
**Where:** `hermes_quant/risk/options_gate.py:375` / `hermes_quant/react/multileg.py:66` (verified inert); `docs/plans/wave-b2-options.md:55-58,433-504`; backlog B01/B02/B03/B48.
**Why it matters:** Verified-correct: path scaffold→wired is documented in the plan + docstrings + backlog. No action. Subsumed by M03 (build B01).
**Status:** **KNOWN.** No new item.

---

## Status reconciliation the backlog needs (post-session drift)

Verified against `docs/research/2026-05-30-backlog-consolidated.md`:

| Item | Backlog status | Actual | Commit |
|---|---|---|---|
| B04 direction-bias gate | `open` | shipped default-OFF | `73d7ed4` |
| B47 proposals `_reconcile_index` | `open` | shipped fail-loud | `0aebd88` |
| B06 profitability cron | `gated` | now change-detecting watchdog (still not deployed — M10) | `2e84f28`/`e4ecad5` |
| B02 / B03 options foundation | `proposed` | built default-OFF (gated) | `6b1c05d` |
| B16 / B18 render layer | `open` (implies unbuilt) | built-but-UNWIRED (M18) | `4f27d19` |
| ADR-0077 admissibility | absent | shipped default-OFF (new gated item) | `ee75811` |
| PDR-1..4 primitives | absent | designed, unbuilt (M13) | ADR-0079 `1b40153` |
| 10 Codex hardening bugs (#11) | absent | open, blocks enablement | — |
| B12 portfolio-caps/slippage | `gated` | silently ON in deployed wrappers (M09) | deploy |

---

## (1) THE SINGLE MOST IMPORTANT NEXT ITEM

**M03 — build + wire the ADR-0029 multi-leg reactor (B01).** It is the one unlock that lets the system ACT on the 22-of-25 SHORT/options-majority universe. Every other built-but-off capability (catalyst, social, admissibility, PMCC shadow, saturation) only converts to value once the reactor can express CC/CSP/wheel/LEAPS. Its foundation (B02/B03) shipped this session, so it is now the highest-leverage, no-longer-blocked headline for the next loop.

**Honorable mention (precondition, not a feature):** M01/M02 — the deploy + anti-drift mechanism. Until it exists, B01 (and every other fix) ships dead-on-arrival to production.

## (2) CORRECT ENABLEMENT / CRITICAL PATH to a live-fidelity multi-strategy paper system

This is the load-bearing sequence M15 must capture as the executable runbook. **Each step is a hard gate for the next:**

0. **Establish deploy + anti-drift first (M01/M02).** Build one-way `ops/scripts/ → ~/.hermes/scripts/` sync + checksum manifest + CI drift check. Without this, all steps below ship non-live. Reconcile the deploy posture audit (M09: B12 is already ON in wrappers; decide intentional-or-revert).
1. **Fix the 10 Codex hardening bugs (#11)** — especially the `effective_size` share-qty unit bug (`autonomous.py:534-541`, M07/M08), and plumb account context (`account_equity`/`available_bp`) into `AdmissibilityContext` (M07) so admissibility stops fail-closed-silencing every short.
2. **Run the live Alpaca + options-chain integration tests (M22) with recorded output** — the only check of real `easy_to_borrow`/`shortable_shares`/chain shape.
3. **Flip `HERMES_QUANT_PORTFOLIO_CAPS=1` + `PAPER_SLIPPAGE_MODEL=v0.2` (B12)** so sizing isn't portfolio-blind (G6) and fills are realistic — *after* reconciling M09 (already on in deploy).
4. **Flip `HERMES_QUANT_DIRECTION_BIAS_GATE=1` in the armed wrapper (M04)** AND redeploy the script (M01) — both, or the AXP-SHORT-via-CSP bug stays live.
5. **Flip `HERMES_QUANT_ADMISSIBILITY=1`** — only after step 1 (#11 + account context) AND after fixing the brief/HITL/reactor admissibility seam (M05) and the post-gate ordering (M06), so shorts route admissibly on ALL paths, not just the autonomous tick.
6. **Build + wire B01 multi-leg reactor (M03)** so CC/CSP/wheel/LEAPS fire; PMCC shadow (M23) then activates as the validation harness; B48 react.live fallback cleanup follows.
7. **Deploy + register the two new crons (M10)** after fixing calibrator-drift's silence-contract violation (M11); this revives the B06/B07/B11 feedback loops.
8. **Run `quant-catalyst-eval-gate.py` (HARD gate) + build the ADR-0075 onboarding eval axis (M12)**, then flip `HERMES_QUANT_CATALYST_ONBOARDING=1` so out-of-universe consumer-trend targets become actable.
9. **Fix the G3 / tool-path semantic decoupling (M17) and extend the no-lookahead gate to the wiring/onboarding path (M21)**, then flip `HERMES_QUANT_SEMANTIC_ENABLED=1` so catalyst reaches ALL paths (not just daily-interim).
10. **Land PDR-1 PerceptionFrame (M13)** to make the three disjoint injection seams structurally one; then PDR-2 → PDR-3 → PDR-4 (M24), each behind its eval gate.

## (3) IS THE BACKLOG COMPLETE? — VERDICT: **NO. Material gaps remain. `backlog_complete = false`.**

The consolidated backlog (B01–B51) was mined at `e7abf50`, **before** the ADR-0079 capstone (`1b40153`) and before this session's admissibility/options/onboarding/render work landed. It is both **stale** (≥6 items have wrong status; see reconciliation table) and **incomplete** (it is missing the entire PDR-1..4 wave structure that the session's own capstone declares the next-wave critical path, plus the 10 Codex hardening bugs, the deploy/anti-drift mechanism, and every enablement-precondition gate).

**Concretely missing from the backlog of record (NEW work this review surfaces):** PDR-1..4 (M13), the 10 #11 hardening bugs, deploy+anti-drift sync (M01/M02), armed-wrapper flag coupling (M04), reactor/brief admissibility seam (M05), admissibility post-gate ordering (M06), account-context plumbing + 38-short premise eval (M07), the admissibility flip-ordering runbook gate (M08), repo↔deploy posture reconciliation (M09), two-cron deploy+register + calibrator silence-contract (M10/M11), ADR-0075 onboarding eval axis (M12), weekly/monthly retro crons (M14), the enablement-order runbook itself (M15), social-arb fixture promotion (M16), tool-path semantic wiring (M17), profitability-boundary test (M19), wiring regression test using the real script (M20), no-lookahead gate extension (M21), live-broker test execution gate (M22), saturation property tests (M24), and render-layer wire-up + status fix (M18).

**Before the next loop runs:** perform a backlog reconciliation pass that (a) corrects the 9 drifted statuses, (b) folds in the 25 M-items above, and (c) inserts PDR-1..4 with their dependency order and eval gates. The backlog cannot safely drive the next loop until that pass lands.

---

## NEW (previously-untracked) findings by severity

- **P0:** 2 — M02 (anti-drift mechanism), M04 (armed-wrapper flag coupling). *(M01/M03 are KNOWN-but-understated; counted under KNOWN.)*
- **P1:** 8 — M05, M06, M07, M08, M10, M12, M13, M16. *(M09/M14/M15 partly-new/known-context.)*
- **P2:** 6 — M17, M19, M20, M21, M22; M11 (P1-severity but NEW; listed once). *Counting M11 as P1-NEW and M17/M19/M20/M21/M22 as P2-NEW.*
- **P3:** 1 — M24.

**NEW totals (deduped):** P0 = 2 · P1 = 9 (incl. M11) · P2 = 5 · P3 = 1 → **17 net-new tracked items**, plus PDR-1..4 as 4 structural waves (M13) and the 10 Codex #11 hardening bugs to enroll.
