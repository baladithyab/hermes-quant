# BACKLOG — live tracker (seeds-style; agents update statuses here)

Statuses: READY | IN_PROGRESS | DONE | GATED(<who/what>) | WONT_BUILD(<why>)
Every item carries its source (PARITY.md row, research note, or review finding).
Rails override every item; nothing here may weaken the gate, ladder, or
no-execution rail.

## Wave 1 (independent, parallel)

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-01 | Scheduled-event calendar: real 2026 FOMC/CPI/NFP seed (researched, cited), loader, freshness check, `events` CLI | ADR-0084 C1/C2 port | DONE |
| B-02 | Portfolio-cap gate rules: gross-exposure cap + max-positions + per-asset net cap (deterministic, additive, reject-only) | ADR-0071/0087 port | DONE |
| B-03 | Regime classifier: deterministic trend/vol heuristic incl. NaN→UNKNOWN fail-open (hermes 7eb148a), `regime` CLI | ADR-0047/0058/0063 port | DONE |
| B-04 | Hypothesis registry: hypotheses.jsonl + Brier-scored forecast ledger + retro wiring | ADR-0048 + SOTA note | DONE |

## Wave 2 (after Wave 1 integration)

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-05 | Calibration-weighted BMA committee aggregation (deterministic `aggregate` CLI; replaces in-prompt arithmetic) | ADR-0003 port | DONE |
| B-06 | Eval harness v0: CPCV splitter + deflated Sharpe + PBO (upgrade past hermes walk-forward per SOTA note) | ADR-0020/0045 + 2026-06-09 SOTA | DONE |
| B-07 | /dashboard command + self-contained HTML template over quant-state | plan doc v0.1 item 6 | DONE |
| B-08 | Review team pass over Waves 0-1 + plugin surface; findings feed this backlog | Phase 6 | DONE |

## Wave 3 (from review findings — R1)

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-09 | R1 P0/P1 batch: F1-F7 fixed by W3 (settle short calibration, fill seam validation, add-is-not-exit, halt persistence, proposal TTL, mark guards, profile rail validation); doc syncs + aggregate CLI wiring by integrator | review | DONE |
| B-10 | R1-10: mark-based exits use the LATEST mark even if it predates horizon expiry | review | GATED(needs mark timestamps decoupled from append time; mitigated — /watch marks immediately before settling in the same turn, so marks are fresh in practice) |
| B-11 | R1-08: unattended-mode hard enforcement (a scheduled prompt could still be injected into approving its own queued proposal) | review | MITIGATED (2026-06-12 Wave 4: PreToolUse deny-hook + `/watch disallowed-tools: AskUserQuestion` + AskUserQuestion fail-closed-stall in non-interactive mode). Residual: add a B-38 drill that attempts unattended self-approval and asserts the block; close then. |
| B-12 | R1 P2 batch (R1-12..22: docs polish, logging, minor) | review | READY |
| B-13 | Morning universe scan: quantcore/universe.py (lean ADR-0075/watchlist-evolution port — broker watchlists as candidate pool, dollar-volume rank, journaled) + /brief wiring | setup dogfood 2026-06-12 | DONE |
| B-14 | Universe scan unit tests + sticky onboard/evict evolution (full watchlist_evolution port) | follow-up to B-13 | READY |

## v0.2 refinement waves (2026-06-12) — from `docs/2026-06-12-v0.2-architecture-refinement.md`

Re-survey (`docs/research/2026-06-12-r-resurvey-and-refinement.md`) drove a refined plan.
Ordered so honesty/integrity land before anything that consumes their outputs. All
deterministic-side, default-OFF, byte-identical-when-off.

### Wave 4 — honesty + integrity foundation (prereq for trusting every later number)

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-30 | Ledger integrity verifier + cross-module consistency check (recompute hash chain on load; analyst-view state == gate state; halt+abstain on break) | arch §4.3; TradeTrap 2512.02261 | DONE (verify_ledger.py; tests green) |
| B-31 | Leakage-masked eval mode (episode alias map; un-mask on query/re-mask on return; de-anon probe CI gate; mandatory for decision-grade numbers) | arch §4.4; KTD-Fin 2605.28359 | DONE (mask.py; tests green) |
| B-32 | Gate-config manifest (SHA-pinned caps/ladder/Kelly/breakers/screener) digest-stamped into ledger + determinism-replay test | arch §4.7; Institutional-AI 2601.11369, DFAH 2601.15322 | DONE (manifest.py + replay.py; tests green) |
| B-33 | Platform rails: PreToolUse deny-hook (no order/transfer tool ever fires) + `/watch` `disallowed-tools: AskUserQuestion` | arch §4.8; docs.claude.com hooks | DONE (exec_guard.py + hooks/; tests green) |
| B-43 | SessionStart hook: run verify_ledger + status, inject halt-state as additionalContext (needs workspace state-dir resolution) | arch §4.8; review split from B-33 | READY |

### Wave 5 — decision quality

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-34 | Committee v2 aggregation: weight = calibrated_confidence × trailing Brier-skill × regime_factor; dissent preserved (not averaged); `analyst_weights.py` | arch §4.1; debate cluster 2601.19921/2508.17536/2511.07784/2602.01011 | READY |
| B-35 | Pre-gate risk screener (concentration/turnover/regime-mismatch/staleness; flag/down-rank/abstain only — NOT a second gate) | arch §4.2; Safiron 2510.09781, FinHarness 2605.27333 | READY |
| B-36 | Alpha-after-attribution (Barra-lite market/style/selection) in ledger; selection alpha is the headline; bright-vs-masked gap = lookahead alarm | arch §4.5; KTD-Fin 2605.28359 §5.2 | READY |
| B-37 | Calibration upgrade: Brier-Skill-vs-control + ECE-with-CI; negative-skill analyst → BMA weight shrunk to ~0 | arch §4.6; KalshiBench 2512.16030 | READY |

### Wave 6 — robustness + the v0.1 READY ports

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-38 | Adversarial drill suite (TradeTrap 4-component × AgentDoG taxonomy; assert gate caps + B-30 catches state-tampering). Supersedes/expands B-25 | arch §4.10; 2512.02261, 2601.18491 | READY |
| B-39 | Evidence snapshot store + `test_analyst_never_reads_future_evidence` (= B-23) | arch §4.11; FutureSim 2605.15188 | READY |
| B-40 | Admissibility shortability/tradability pre-checks in /propose (= B-24) | arch §4.11; ADR-0077 | READY |
| B-41 | Universe sticky onboard/evict evolution + tests (= B-14) | arch §4.11; ADR-0075 | READY |
| B-42 | Live-artifact dashboard (read-only sources ONLY) behind default-OFF `/dashboard --live` | arch §4.9; live-artifacts docs | READY |

### Wave 7 — capabilities (NOW FULLY SPEC'D; stay flag-gated + want real dogfood track record)

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-20 | Options playbook skill + options_gate (deterministic structure-selection table + Greek caps; multi-leg advisory card, no execution) | arch §4.13; ADR-0027/0082 | SPEC'D(2026-06-12) — build behind flag; ≥2wk equity dogfood before weight |
| B-21 | FoundationModelAnalyst (HTTP client; interface-first; abstain-on-error; plugin-side rolling-IC kill-switch; primary self-hosted Kronos/Kairos) | arch §4.12; ADR-0018 | SPEC'D(2026-06-12) — operator hosts/pins backend; default OFF |
| B-22 | Shadow-ledger counterfactual (pre-HITL gate-sized book vs real vs buy-and-hold/random; attribution-honest) | arch §4.14; ADR-0049 | SPEC'D(2026-06-12) — informative after ≥20 settled trades |
| B-26 | Quarterly meta-retro `/retro --quarterly` (ADVISORY config-change proposals; nothing self-applies; reject ATLAS auto-OPRO) | arch §4.15; ADR-0026/0035/0080/0081 | SPEC'D(2026-06-12) — needs a quarter of data |

### Still-READY originals folded into the waves above
B-12 (R1 P2 polish) · B-14→B-41 · B-23→B-39 · B-24→B-40 · B-25→B-38.

## Review-team findings — 2026-06-12 (concurrent review of Wave 4 + v0.2 design)

Two review agents audited Wave 4 code + the v0.2 design. They found and we FIXED
several real bugs (all now green in the 212-test suite): mask day-index substring
collision (`day_1` vs `day_10` → zero-padded + longest-first unmask), mask ticker
regex over-matching prose (→ universe-membership matching), deny-hook matcher not a
strict superset of the predicate (→ broadened + exercise/assign/execute verbs added),
replay not verifying the stored manifest digest (→ now asserted), and a ledger
poisoning path (spurious `resume` lifting a breaker → now detected). New items:

| ID | Item | Source | Status |
|----|------|--------|--------|
| B-44 | Coordination Breakeven Spread (CBS): does the committee beat its best single analyst net of cost? (the falsifiable CPH test) | review; research §1.3 | READY (Wave 5, with B-36) |
| B-45 | FactFin counterfactual-perturbation drill: perturb facts in a replay episode, assert the call flips when the fact flips (probes model-weight memorization head-on) | review; FactFin 2510.07920 | READY (Wave 6, with B-38) |
| B-46 | Allowlist-based PreToolUse guard + CI test asserting NO broker MCP write-verb is unmatched — must land BEFORE B-20 (options introduces exercise/assign verbs) | review finding #4 | READY (Wave 4.5) |
| B-47 | Event-study CAR scorecard for the catalyst analyst (split out of B-36) | review | READY (Wave 5) |
| B-48 | HMM regime classifier + overnight-drift analyst (resolve orphaned PARITY v0.2 promises) | review; PARITY lines 23/31 | READY (Wave 5) or explicitly defer in PARITY |
| B-49 | Manifest CODE-pinning: `code_version`+`kelly_formula_version` added (B-32); residual = a test asserting a kelly.py change flips the digest (currently a manual version bump) | review code finding #10 | READY |

Reclassifications:
- **B-14 → folded into B-41** (universe evolution); closed as a standalone row.
- Dependencies made explicit: B-36 (attribution) and B-22 (shadow) depend on B-31
  (leakage-masked eval); B-30's verify-on-load rail (R11) is only ENFORCED once B-43
  (SessionStart hook) lands — B-43 priority raised.

## Operator-gated (not agent-completable; surfaced, never dropped)

| ID | Item | Status |
|----|------|--------|
| O-01 | GitHub: create baladithyab/cowork-quant, init, push, submodule add (BOOTSTRAP.md) | GATED(operator) |
| O-02 | Install cowork-quant.plugin in Cowork; set watchlist in quant-state/config.json | GATED(operator) |
| O-03 | /doctor pass + /schedule daily → start ≥2wk dogfood | GATED(operator, after O-02) |
| O-04 | Commit sequence in docs/COMMIT-LOG.md | GATED(operator) |
| O-05 | Decide Robinhood-MCP read-only fill-readback wiring (broker MCP is connected in Cowork; needs explicit operator opt-in per MCP-INTEGRATION posture) | GATED(operator) |
