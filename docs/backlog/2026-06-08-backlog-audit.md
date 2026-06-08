# hermes-quant backlog audit — 2026-06-08

Triggered by operator directive (Codeseys, Discord #hermes-quant): ingest three
Instagram trading-content links, build overnight-drift awareness + macro-event
awareness into the pipeline, then systematically resolve the backlog.

This document is the **honest ground-state + backlog enumeration** (Phases 1–2 of
the directive). It is deliberately scoped: a live trading system's backlog is not a
finite set that can be driven to literal zero. "The backlog" here =
**(a) items identified in the originating thread + (b) concrete open work the repo
itself surfaces** (uncommitted work, unmerged reviewed branches, proposed-status
ADR gates, empty CHANGELOG-unreleased). Open-ended research/feature ideation is NOT
counted as backlog — it is captured as deferred-decision ADRs with reopen conditions.

---

## Phase 1 — Commit / ground state

- **Repo:** `/mnt/e/CS/github/hermes-quant` (editable install → gateway venv; pip dist `hermes_quant-0.6.4`).
- **Branch:** `fix/bma-dissent-cap` — **5 ahead / 1 behind** `main`.
- **HEAD:** `84b29ff docs(postmortem): mark NaN/stop-loss/BMA/rails action items resolved`
- **Recent landed line (this branch):**
  - `c3db1b3` fix(bma): dissent-aware confidence cap (flag-gated)
  - `660d853` fix(risk): enforce stop-loss — root-cause trader fix + tick backstop
  - `dea6d27` fix(autonomous): wire two DEAD safety rails live (concurrent-cap + kill-switch)
  - `f22b6b1` fix(risk): close NaN-fail-open defect class across 5 guard sites
- **Uncommitted:** one untracked file — `hermes_quant/risk/target_weight.py` (a completed
  review-finding fix, P2-3 / INCIDENT-2026-06-02 follow-up; hoists the signed NAV-fraction
  target-weight resolver out of the ops script so prod+tests import the identical object).
  **Loose completed work — must be committed.**
- **Test baseline:** 4137 tests collected. Full run launched 2026-06-08 (result pending).
- **Source TODOs:** 4, all benign (closed-TODO comments + one docstring). Source is clean.

## Phase 2 — Backlog enumeration (categorized)

### Category A — Loose / uncommitted completed work (P0, blocks clean state)
| ID | Item | Evidence | Complexity |
|----|------|----------|-----------|
| A1 | Commit `risk/target_weight.py` (review finding P2-3) | untracked, has module docstring citing the incident | trivial |
| A2 | `[Unreleased]` CHANGELOG section is empty despite 5 unmerged fix commits | CHANGELOG.md | trivial |

### Category B — Unmerged, reviewed feature branches (P1, integration debt)
Each is ahead of main with completed + adversarially-reviewed commits. Merge order
matters (dependency + conflict risk). These are NOT greenfield.
| ID | Branch | Ahead | Theme | Risk |
|----|--------|-------|-------|------|
| B1 | `feat/l2-learning-loop` | 7 | BMA calibration, decay-ring persistence, NaT no-lookahead fix | med |
| B2 | `feat/l3-oos-admission-gates` | 4 | DSR/walk-forward OOS gate for analysts joining committee | med |
| B3 | `feat/l4-claimverifier-watchlist` | 7 | semantic-claim verification, cap-trim protected-row count | med |
| B4 | `feat/flag-ablation-harness` | 6 | flag-ablation backtest harness (money-path reviewed) | med |
| B5 | `fix/bma-dissent-cap` (current) | 5 | the four fixes above → merge to main | low |

### Category C — Originating-thread deliverables (P1, the actual ask)
| ID | Item | Status from recon | Complexity |
|----|------|-------------------|-----------|
| C1 | Overnight-drift awareness (close→open vs open→close) | **ABSENT** in source — genuine new build. Data already present (`MarketContext.bars` has open+close). | med |
| C2 | Macro scheduled-event awareness (CPI/PPI/FOMC/NFP) | **ALREADY BUILT** — ADR-0084, implemented, default-OFF behind `HERMES_QUANT_EVENT_RISK`. Task = evaluate → eval-gate → enable → tune. | low-med |
| C3 | LEAPS / long-horizon convex-bet sleeve (the "penny-stock play for very long holds") | No dedicated sleeve; PMCC LEAPS *shadow tracker* exists (ADR-0029 gap). Capture as deferred-decision ADR + reopen conditions. | research/ADR |
| C4 | From-reel methodology pipeline (turn today's 3 reels into versioned methodology YAML) | ADR-0030 **Proposed, not implemented** — this is the designed home for reel-sourced methodologies. | large (separate) |

### Category D — Proposed-status ADRs (open decision gates)
17 ADRs in `proposed`. Several are intentionally deferred-forever (ADR-0006 RL-deferred,
ADR-0012 LLMAnalyst-deferred) — legitimately proposed. Others are real open gates needing
either acceptance-gate verification or implementation. Triage in Phase 2-detail:
0004, 0009, 0026, 0030, 0032, 0038, 0039, 0067, 0073, 0075, 0078, 0080, 0086, 0087, 0088.
(0004 risk-gate is the immutable authority — its "proposed" status is suspicious and worth a look.)

### Category E — New research-gated items (from this directive)
| ID | Item | Needs research |
|----|------|----------------|
| E1 | Does the overnight anomaly hold on the *current 14-name watchlist*, post-2020 decay, net of cost? | spike + Tavily/Exa lit check |
| E2 | Event-risk tier/window tuning — current defaults `N_earnings=5, N_macro=1`; are they right? | replay eval + lit |
| E3 | LEAPS sleeve sizing/risk model (convex small-size defined-risk) | deepwiki/exa lit |

---

## Execution waves (Phase 4 plan)

- **Wave 0 (serial, now):** A1 + A2 (commit loose work + CHANGELOG) → clean state before anything else.
- **Wave 1 (parallel research, subagents):** E1 (overnight anomaly viability), E2 (event-risk tuning lit), E3 (LEAPS lit) + C2 recon (event-risk impl read).
- **Wave 2 (architect + spike):** C1 read-only overnight spike on 14 names (gated on E1); ADR drafts for C1, C3.
- **Wave 3 (parallel execution):** implement C1 analyst behind flag + tests; C2 enable+tune; merge B-branches in dependency order.
- **Concurrent review track:** runs against every wave's output, feeds findings back.
- **Loop** until A–E resolved or explicitly deferred with justification.

## Honest scope note
C4 (full from-reel pipeline, ADR-0030) is a large separate build — flagged, not silently
deferred. It is the strategic home for reel ingestion but is its own multi-wave effort;
this directive's reel links are handled in the near term via C1/C2/C3 + a methodology-stub,
with C4 recommended as the next major initiative.

---

## 2026-06-08 (later) — Wave progress + C2 honest re-scope

**Wave 0 DONE.** A1 (`target_weight.py`) committed `98fd52c`; A2 CHANGELOG done. B5 (`fix/bma-dissent-cap`) → **PR #75**, rebased onto current main (incl. #72/#73/#74), 8 commits, **4151 passed / 0 failed minus Kronos**, auto-merge watchdog armed (`pr75-automerge` cron, 3min). The "9 regressions" were false — test-isolation gaps, fixed `b7ff2ad`, zero prod-code change.

**C2 re-scoped — HONEST FINDING (changes the estimate):** The audit assumed C2 = "evaluate → eval-gate → enable → tune (low-med)" using the #74 ablation harness. **The harness REFUSES `HERMES_QUANT_EVENT_RISK` by design** — `cli/ablate.py` returns `verdict: NOT_MEASURABLE` because the offline `AdvisorStrategy` path doesn't populate `ctx.extras['event_risk']`, so an ablation would print a misleading null. Confirmed empirically (JSON verdict) + in `NOTES_ABLATION.md` §146.

What IS solid: the event-risk **mechanism** is fully unit-tested and green — `test_event_risk_guard.py` + `test_event_risk_carrier.py` + `test_event_risk_builder.py` = **58/58 pass**. The `in_event_blackout` predicate, the ctx.extras carrier, and perception wiring all work and are asof-honest (ADR-0084 Negative: missing data ⇒ NO blackout, never fabricated).

What's MISSING (the real C2 deliverable, larger than "low-med"): a **return-impact measurement**. We can't eval-gate "should we enable EVENT_RISK" without a gate/reactor-level ablation that injects a synthetic event_risk carrier (e.g. a synthetic FOMC-blackout day) and measures OFF-vs-ON Sharpe/DSR on a window containing the event. Two honest paths:
- **C2a (recommended):** build a small gate-level event-risk ablation (inject carrier → measure blackout-new/hold-existing impact). Bounded — carrier schema + predicate already exist. Turns "flip and hope" into real evidence. New ADR or extend ADR-0084.
- **C2b:** enable EVENT_RISK default-ON on the *mechanism's* unit-test strength alone, accepting we have NO return-impact evidence yet. **NOT recommended** on money-software — it's the rubber-stamp the operator explicitly dislikes.

**Decision surfaced to operator.** Do NOT silently pick C2b.

---

## 2026-06-08 (later still) — C2a SHIPPED: EVENT_RISK is now measurable

Built the missing gate-level measurement (operator directive: "make it robust"). The flag-ablation harness now genuinely measures `HERMES_QUANT_EVENT_RISK` instead of refusing it.

**New module** `hermes_quant/backtest/event_risk_ablation.py`:
- `synthetic_macro_calendar(...)` — deterministic, asof-honest FOMC/CPI/NFP calendar reusing the production `CalendarEvent` dataclass (`announced_at <= scheduled_for` enforced by construction). Release schedules are public ~a year out → asof-honest by construction (the reel's own principle: schedule is knowable, outcome never peeked).
- `build_event_risk_payload(cal, asof)` — builds the `{"events":[...]}` carrier the gate reads, FILTERED to `announced_at <= asof` (defense-in-depth no-lookahead).
- `EventRiskAblationStrategy(AdvisorStrategy)` — overrides ONLY `_gate`: stamps the carrier into the frozen `signal.metadata` via `dataclasses.replace` before delegating to the parent gate.

**Wiring** (`cli/ablate.py`): removed `HERMES_QUANT_EVENT_RISK` from the `NOT_MEASURABLE` refusal set; routes it to `EventRiskAblationStrategy` with a window-spanning synthetic calendar. Both OFF/ON legs get the IDENTICAL calendar → only the flag differs.

**Verified the carrier BITES (not a false null):** end-to-end synthetic ablation shows `d_n_trades < 0` — the blackout guard suppresses ≥1 fresh open inside an event window when ON. Verdict flows honestly (HOLD on synthetic GBM — no real event edge there, exactly right).

**Tests:** `tests/backtest/test_event_risk_ablation.py` (12) — calendar asof-honesty, carrier filter (excludes not-yet-announced = no lookahead), gate stamping, blackout-bites-when-ON, env no-leakage, CLI-not-refused. `test_cli_ablate.py` updated (EVENT_RISK moved from refused-list to a new `test_event_risk_is_measurable`). 161 passed in the backtest+event-risk+cli-ablate slice.

**Honest scope:** this measures the guard's MECHANICAL impact on a synthetic calendar — it proves the flag is now eval-gateable, NOT that enabling it improves real returns. The real-data verdict needs `HERMES_QUANT_RUN_BACKTEST=1` + a real macro calendar over a real window (the vendored FOMC seed exists via `load_fomc_seed`). That run is the actual promote/hold decision; C2a built the instrument that makes it honest. `GROUNDING_ENFORCE` remains refused (same carrier-injection pattern would close it — follow-up).

**C2 status: a→ instrument SHIPPED. Promote/hold decision = a real-data ablation run, no longer a rubber-stamp.**
