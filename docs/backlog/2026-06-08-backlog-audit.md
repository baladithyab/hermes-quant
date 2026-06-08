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
