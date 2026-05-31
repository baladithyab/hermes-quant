# Deep Work Log

Append-only log of deep-work-loop runs against hermes-quant.

## Run 2026-05-30 13:00 PT — started at e4ecad5

**Operator prompt:** document commit & work the backlog to zero; research intensively
(tavily/exa/deepwiki); fan out subagents to understand the system + peruse the wiki and
Hermes sessions; concurrent review team; iterate until the backlog is empty.

### PHASE 1 — Commit current state ✅

In-flight work (social-arbitrage + PMCC shadow + AMZN-OOS) was verified (77/77 catalyst+shadow
tests pass; library code ruff-clean) then committed as coherent chunks:

- `af8cd78` feat(catalyst): social-arbitrage integration (ADR-0076) — consumer-trend class, sized fusion, profitability loop
- `69a9b42` feat(shadow): PMCC marked-to-model shadow tracker (counterfactual for ADR-0029)
- `4cb2463` docs: ADR-0076 + PMCC design doc + architecture HTML snapshot
- `571955d` chore(ops): AMZN-weight OOS split + wave-3 candidate sleeve runners
- `f299dd6` docs(changelog): record under Unreleased

Baseline hash for this run: **e4ecad5**. Post-Phase-1 head: **f299dd6**.

### PHASE 2 — Backlog enumeration ✅

4 background understanding agents (wiki / codebase / sessions / consolidated-backlog) →
51-item deduped backlog (P0:4, P1:8, P2:39) at `docs/research/2026-05-30-backlog-consolidated.md`.
Verified 2 code-level gaps myself (dangling `stacking` entry point; no-lookahead gate missing
Kronos/Semantic). Committed: `e7abf50`, `d6500ea`.

### PHASE 3 — Deep research ✅

Per-track research (admissibility/shortability, order-lifecycle/fills, options execution,
catalyst onboarding) via exa/tavily/deepwiki → 4 docs under `docs/research/2026-05-30-r-*`.
Plus the PDR research triplet (vibe-trading, ai-hedge-fund, current-pipeline audit) and the
social-arbitrage method deep-dive (Camillo: detect→validate→link→act→exit).

### PHASE 4 — Architect + ADRs ✅

- ADR-0077 pre-trade admissibility + ShortabilityOracle (the 6/6-unanimous fidelity P0)
- ADR-0078 order-lifecycle + fill realism + idempotency
- ADR-0079 (capstone) unified Perception→Decision→Reaction architecture + signal unification
Committed `6aca497`, `1b40153`; Codex-corrected `1f750d5`.

### PHASE 5 — Plan in waves ✅

4 implementation-ready wave plans (B admissibility, B2 options, C observability, C2 catalyst)
under `docs/plans/wave-*.md`, each citing verified codebase idioms + pytest-verifiable criteria.

### PHASES 6+7 — Execution + concurrent review (LOOP) ✅

Two workflow rounds, each wave adversarially reviewed by an independent code-reviewer that saw
only the plan + diff. ALL waves default-OFF behind HERMES_QUANT_* flags.

- Wave A correctness (6 fixes): `73d7ed4` (direction-bias P0, flag-gated), `836976b` (stacking
  entry point, no-lookahead Kronos/Semantic coverage, stale comment, proposals reconcile, BMA
  audit observability).
- Wave B admissibility: `ee75811`. Wave B2 options foundation: `6b1c05d`. Wave C observability:
  `4f27d19`. Wave C2 catalyst PDR-wiring + onboarding: `2e84f28`, `3776a35`.
- **Review caught a false "merged to main" + false "zero new lint" self-report on Wave C** — it
  was worktree-only with +6 violations; both fixed before commit. This is the loop working.

### PHASE 8 — Final verification + Codex cross-model critique ✅

- 6 parallel Codex (GPT-5.5) facets (5/6 substantive). Dominant convergent finding: **every
  HIGH/P1 is post-go-live-only (flag OFF)** — validates the build-behind-a-flag rail.
- Fixed the ONE live-reachable finding: proposals.py mid-file-corruption now fails loud
  (`0aebd88`). Corrected 3 ADR-0079 authority-boundary issues (`1f750d5`).
- Synthesis: `docs/reviews/2026-05-30-codex-deep-work-loop/synthesis.md`.
- Evidence: no-lookahead release-blocker **11 passed / 1 skipped**; full suite **2653 passed /
  62 failed / 6 skipped** — all 62 pre-existing (optional-dep gaps: torch/ccxt/sklearn missing
  in this .venv; + combinatorial pollution). ZERO session-new tests failed (grep-verified).
- No new HERMES_QUANT_* flag is hard-enabled anywhere (grep-verified) — landing alters zero
  live behavior.

### PHASE 9 — Final commit + log ✅

Lint cleanup `6811fb1`. Backlog drawn down from 51 → execution complete; remaining open items
are deliberately deferred (post-go-live hardening #11, test-pollution #12, P2/v0.7-v0.9 roadmap
waves) — none are blockers, each documented.

**Run end. Baseline e4ecad5 → final HEAD (see git log). 23 commits this session.**
Two independent sign-offs: execution team (all waves committed + tested) + review team (Codex:
every HIGH is flag-gated; build-wave reviewers: all waves CONFIRMED post-fix).

---

## Run 2026-05-30 17:00 PT — continuation: enablement + Hermes integration + backlog-to-zero

**Operator prompt:** address the backlog; enable the new features + register the trading crons;
deep-dive Hermes-agent docs to confirm plugin/architecture compatibility + document plugin install.

Ran THREE concurrent workflows (execution + Hermes-research + a concurrent meta-review team).

### Backlog resolution (#11 + #12) ✅
- **#11 pre-go-live hardening** — fixed the 10 Codex findings so admissibility/options can be
  safely enabled: H1 NAV-fraction→share-qty unit bug + H2 fail-closed account context
  (`09ecb6c`); H3 options-gate covering-leg/min-DTE/CSP-collateral/greek-scaling (`2ac69dd`);
  H4 PIL ex-div guard. 142 tests; ruff clean. All still default-OFF.
- **#12 test pollution** — investigated (no raw-os.environ leaker found; affected tests pass in
  isolation); added an autouse HERMES_QUANT_* flag snapshot/restore fixture (`8949d2b`) that
  makes the catalyst tests order-independent regardless of upstream leaker.

### Hermes integration deep-dive (tavily/exa/deepwiki + live probes) ✅
- **KEY FINDING:** hermes-quant is discovered (pip entry-point) but NOT in
  `~/.hermes/config.yaml plugins.enabled` → the 16 tools + /quant slash + CLI are DORMANT. The
  16 crons run independently and ARE live. Fix = `hermes plugins enable hermes-quant` + restart.
- Fixed manifest drift (`0bde804`): version 0.4.4→0.6.4, +quant_recipes, −unwired
  on_session_start. register() smoke now clean (16 tools == registered).
- Docs: `docs/operations/HERMES-INTEGRATION.md`, `CRON-REGISTRY.md`, `FEATURE-ENABLEMENT.md`
  (`6aa5198`).

### Concurrent meta-review (5 lenses, 31 findings, 17 net-new) ✅
- `docs/reviews/2026-05-30-concurrent-meta-review.md`. **Critical discoveries:** deployed
  `~/.hermes/scripts/` are STALE + drifted both ways (session fixes NOT live; 4 live scripts
  never vendored) → built the deploy-audit tool + anti-drift test + reconciliation runbook
  (`6fdb2ed`, `docs/operations/DEPLOY-SYNC.md`); B12 is silently LIVE in deployed armed wrappers
  (repo≠deploy posture); PerceptionFrame/PDR-1..4 + #11 were absent from the backlog of record.
- Backlog reconciled (`b8789c7`): 8 status corrections + 21 net-new items (N1-N21) with the
  10-step enablement critical path.

### Enablement status (honest)
- **The agent CANNOT flip flags (.env tool-guarded), register Hermes crons (no cronjob tool
  here), or edit config.yaml.** All such steps are documented as exact operator commands in the
  ops docs. Flipping ADMISSIBILITY before #11 would have silenced every short — #11 is now fixed,
  so the GATED flags are correct, but still require their eval gates + the operator's flip.
- **SAFE-NOW flags** (abstain-only, post dry-run): DIRECTION_BIAS_GATE (needs redeploy + wrapper
  flag, M04), CALIBRATOR_AUTO_REFIT (after silence-contract fix N10), IC_DEDUP_AT_INGEST.
- **#17 plugin-enable + #18 PDR-1..4 build** = operator action + next-loop build, documented.

**Continuation end. +10 commits (223983f → see git log). The deployed-script drift + the
plugin-not-enabled findings are the two things that make "enable everything" an operator
sequence, not an agent flag-flip — both now fully documented with exact commands.**

### WF1 hardening — formal completion + the finding I missed (post-hoc)

The hardening workflow's adversarial review landed AFTER I'd committed H1-H4 on my own
verification, and it caught a **BLOCKING fail-open my verification missed**: the options-gate
greek caps (gamma/theta/vega/net-delta) were checked at `structural_contracts` (=1 for every
covered-call/CSP) but `_size_contracts` admits MORE lots — so a 2-lot CC whose 1-lot gamma is
under the cap but whose true 2-lot footprint is 1.8x the cap was ADMITTED. Fixed: re-check all
size-scaling caps at the ADMITTED contract count (`850c474`), with a calibrated regression test
(probing `aggregate_net_greeks` revealed the short-gamma sign + ×100 contract multiplier I'd
gotten wrong twice). WF1 also found the REAL #12 root cause — `test_smoke.py` evicting
`sys.modules` without restore — fixed (`278f87c`); full single-process run 61→39 failures, all
9 order-dependent gone. WF1's remaining findings (live account-context plumbing, CSP sizing
denominator, PIL datetime-key robustness, greeks try/except narrowing) are flag-OFF
pre-go-live items → task #19. Lesson: "evidence before assertions" applies to my own fixes —
confirming a fix LANDED is not confirming it CLOSED the hole. 166/166 final hardening+deploy+
catalyst tests pass.

### Wave S — pre-enablement safety + reactor/PDR-1 architecture (2026-05-30, +5 commits)

Operator: "walk me through what's left and start working on the rest."

- **Wave S (5 items, all CONFIRMED, default-OFF):** S1 admissibility now gates the PaperReactor
  on ALL paths (brief/HITL, not just autonomous-tick) via the shared admit_or_reject seam +
  fixed quant_approve to honestly report admissibility-rejected (was rubber-stamping a 0-fill
  reject as approved) [`632ea61`]; S2 calibrator-drift change-detecting silence contract so it's
  registerable without weekly spam [`d261a09`]; S3 options CSP-denominator/greeks-except + borrow
  PIL interval predicate, S4 wired the orphaned decisions renderer into `quant status` + profit
  boundary tests, S5 extended the no-lookahead release-blocker to the perception-PRODUCING path
  [`d7e3ff8`]. 149 tests pass.
- **Architected (docs, next-loop builds) [`2b5b802`]:** wave-d-multileg-reactor.md (N3/B01 — the
  vision unlock: CC/CSP/wheel FIRE on paper, HITL-only, gate-precondition, PMCC-shadow-validated)
  + wave-e-perceptionframe.md (PDR-1 carrier collapsing the 3 injection seams) — each with a
  concrete eval gate.
- **Wave S review follow-ups → task #22** (unify autonomous admissibility seam, CSP/BPR at
  admitted size, atomic baseline, cron-test-loader hardening). None blocking; all flag-OFF.

**S1 makes HERMES_QUANT_ADMISSIBILITY safe to flip — it no longer lets an inadmissible short
fire through the HITL path. The enablement critical path's step-1 code is now done.**

### Wave D+E — multi-leg reactor + PerceptionFrame BUILT (2026-05-30, +2 commits)

Operator: "keep going" + enabled the plugin live (gateway restart confirmed:
`hermes_quant plugin v0.6.4 registered` at 21:17).

- **PDR-1 PerceptionFrame** (`8c8cb1d`, CONFIRMED): the carrier collapsing the 3 semantic-
  injection seams into one; recommend(perception_frame=None) byte-identical to today; closes the
  M17 tool-path decoupling. Eval gate PASS (byte-identical replay + no-lookahead incl. producing
  path). 46 tests.
- **Multi-leg PAPER reactor** (`dedc8d0`, the headline vision unlock): CC/CSP/wheel FIRE on paper,
  HITL-only (quant_approve dispatch), options-gate-as-precondition, PMCC-shadow-validated, all
  default-OFF behind HERMES_QUANT_MULTILEG_REACTOR. Eval gate PASS 7/7.
- **P0 caught by review, NOT the eval gate** (the session's most consequential catch): the
  legacy-state.db idempotency migration did a bare ALTER (can't change a SQLite PK), so a covered
  call's 2nd leg shared the 2-col key and was SILENTLY DROPPED from state.db while landing on the
  bus — the exact bus/state divergence the fidelity effort exists to prevent. The LIVE
  ~/.hermes/quant/state.db is that legacy shape (435 rows). Fixed: _migrate_processed_fills now
  rebuilds to the 4-col PK; dry-run on a copy of the real DB migrates clean + preserves all 435
  rows. Eval gates use fresh DBs so they structurally couldn't catch it — adversarial review on
  the un-testable input did. Lesson reinforced: eval-gate-green ≠ correct.

**Environment migration (this turn):** repaired 124 broken hermes shebangs (venv.uv→venv;
`hermes` was failing "required file not found"), finished conda→uv (commented .bashrc conda block,
exported 4 env specs to ~/conda-env-backups/, deleted ~/miniconda3 reclaiming 16G), and enabled
hermes-quant in the gateway via the config.yaml allow-list (NOT `hermes plugins enable`, which
errors on entry-point plugins). Gateway restarted → plugin LIVE with all 16 tools.

**Remaining (next loops, all flag-OFF / operator):** PDR-2/3/4 perception primitives; the
reactor's recipe->proposal producer (PR-5); deploy-sync reconciliation of the 9 DRIFT scripts;
operator flag-flips per FEATURE-ENABLEMENT.md (the GATED flags still wait on their evals).

### SELF-EVOLUTION ARC — R1→R4 ladder, waves W1-W7 BUILT (2026-05-30/31)

Operator goal (/goal): evolve hermes-quant from a newbie trading system into a full self-evolving
quantitative researcher (reflect / critique / deliberate / self-evolve), using the reference
repos + papers in ~/wiki. Worked autonomously: research → architect → plan → build → review, in waves.

**Research+architecture (committed 0eb757d, 9f4d581):** R0→R4 capability map (hermes-quant was
R1-reflective-but-DARK + R2-deliberative at the R3 threshold); 9 ranked open loops; rails-preserving
SOTA mechanisms (FINCON CVRF, SkillOpt held-out gate, QuantAgent inner/outer, FINMEM decay,
RedDebate). ADR-0080 self-evolution framework (advisory plane vs outer standard-of-truth —
concentrates authority at the gate in the BACKWARD/learning direction as ADR-0079 does forward;
multi-rate T0-T3 tiers; universal held-out eval-gate contract; propose-only) + ADR-0081 bounded
decaying belief store (CVRF weekly / FINMEM monthly distillation).

**Waves built (all default-OFF, held-out-eval-gated, advisory-plane-only, adversarially reviewed):**
- W1 (08326e1) — IGNITE the dark loop: record_decision on open (O1). The keystone — the whole
  reflect→retrieve→PM-prompt edge could never fire because record_decision had zero prod callers.
- W2 (0bf0008) — weekly pattern-mining retro, T2 distillation (O2) + writes the dangling
  weekly_retro_promotion_readiness producer (O3). The literal M14 gap.
- W4/W5/W7 (fbc0516) — factor-weight proposer (O4, silence-only) + B10 graph miner (O5) +
  Socratic devil's-advocate red-team turn (CRITIQUE axis ◐→●).
- W3/W6 (0273149) — monthly meta-retro, the missing T3 tier (O7/O8) + hypothesis→backtest→promote
  research-loop cron with INDEPENDENTLY-TRACED zero-auto-promotion.

**Verification:** 138/138 self-evolution tests together (no cross-wave interference); 58+ live-path
regression green; grep-verified NO self-evolution flag is hard-enabled anywhere — the live default
path is byte-identical, so this lands inert until the operator flips each flag after its eval gate.

**Net:** every open loop O1-O8 now has a closing component built behind a flag. The system has
climbed from R1-dark to the R4 threshold: per-trade→weekly→monthly distillation, self-critique,
factor/graph/hypothesis evolution — all PROPOSE-only, the deterministic gate + operator the sole
path to live. The advisory plane evolves; the risk gate / sizing ladder / kill-switch are immutable
by it. Follow-ups (tasks #28, #23, #22, #19) are non-blocking pre-flip hardening.

### SELF-EVOLUTION ARC — COMPLETE + hardened + operator-runbook (2026-05-31)

Closing state of the /goal "newbie → full self-evolving quant researcher":
- Waves W1-W7 built + hardened (11 commits this arc); 155/155 self-evolution+reactor+admissibility
  tests green together; live default path byte-identical (no flag hard-enabled).
- Flippable-hardening (dda7805/e6e9e1d): W4 now has a REAL time-ordered held-out OOS split
  (proposer-blind, no-lookahead test-proven) so it can actually promote — was a never-promoting
  stub; admissibility account-context plumbed + 3 paths unified to one seam; options BPR/collateral
  re-checked at admitted size.
- Live-cron robustness (edee1be): non-dict-line silence guard on the 4 live no_agent readers +
  W4 holdout off-by-one.
- Operator runbook: docs/operations/SELFEVOLVE-ENABLEMENT.md (7 flags, dependency order, per-flag
  eval gate + .env one-liner + cronjob registration + rollback).

The system now sits at the R4 threshold: per-trade→weekly→monthly distillation (W1/W2/W3),
self-critique via a Socratic devil's-advocate (W7), factor/graph/hypothesis evolution (W4/W5/W6) —
ALL propose-only into an advisory plane; the deterministic gate / sizing ladder / kill-switch are
immutable by the loop; promotion to live is always a human act. Adversarial review caught a real
money-path bug in EVERY build wave (legacy-DB covered-call leg-drop; W4 never-promoting stub) —
the discipline that made eval-gate-green trustworthy.

Remaining (next loops, none blocking): PDR-2/3/4 perception primitives (#18); cosmetic nits (#31);
full-suite env test-pollution (torch/ccxt/sklearn, #12); operator enablement (flip flags per the
runbooks after each eval gate).

### PDR-2/3/4 PERCEPTION LAYER — COMPLETE (2026-05-31)

The last named self-evolution waves: the three perception primitives that turn social-arbitrage
from "linked to a ticker" into the actual Camillo edge (DETECT → VALIDATE → EXIT). All built
default-OFF, eval-gated, perception-layer evidence-only, each adversarially reviewed CONFIRMED with
the rails re-verified on-machine before commit.

- **Plans (f22007a)** — seam recon + 3 impl-ready plans, every file:line seam verified against HEAD.
  The audit caught a silent-no-op bug in the PDR-4 plan pre-build: frame.semantic_packets holds
  DICTS, so the planned getattr() produce-code would have made saturation dead even flag-ON; fixed
  to .get() + a CRITICAL DATA-SHAPE callout so the build inherited the right seam.
- **PDR-2 TrendVelocity (28a0a3e)** — GAP-A/DETECT. Week-over-week acceleration (the SLOPE, not
  keyword severity) re-sources packet magnitude under HERMES_QUANT_TREND_VELOCITY. Band-bounded to
  the severity scale [0,0.06] so a flag flip can't widen the ladder. D74.7 ≥0.6 gate on the labeled
  Camillo corpus, now PROMOTED off /tmp to versioned tests/fixtures/socialarb/ (N13). 14 files.
- **PDR-3 ConvergenceValidator (633b0ee)** — GAP-B/VALIDATE. Cross-SOURCE require_ensemble at the
  perception layer (≥2 independent source families, policed taxonomy counting distinct ORIGINS),
  gating packet EMISSION under HERMES_QUANT_CONVERGENCE via a subtract-only haircut. Two-pass
  synthesize refactor preserves PDR-2 verbatim AND propagation_log byte-identity (propagate() called
  exactly once per item, instrumented). Complementary to BMA's cross-ANALYST guard — a lone view
  clearing PDR-3 is still silenced by BMA. ≥0.65 higher-bar eval. The build caught a real bug in the
  plan's own pseudocode (entity leak via a 3-tuple) and fixed it to a 4-tuple. 12 files.
- **PDR-4 SaturationScore (106c4ce)** — GAP-C/EXIT. Silence-only confidence decay (m∈(0,1]) on the
  semantic analyst's OWN view BEFORE BMA under HERMES_QUANT_SATURATION. Two property tests are the
  hard gate: (A) post-saturation ≤ pre for every adversarial input; (B) VIEW-LOCAL — every
  non-semantic view bit-identical sat-on-vs-off (the D79.4 no-cross-view-veto rail that distinguishes
  it from the rejected aggregate placement). Backtest flips social-arb-slice Sharpe -0.52 → +0.42 on
  the versioned exit set. 6 files.

**Verification:** 201 perception+catalyst+no-lookahead tests green together with all three flags OFF;
grep-verified no flag hard-enabled in source (the only os.environ["HERMES_QUANT_CONVERGENCE"]="1" is
the scoped eval helper that restores in a finally). Live default path byte-identical — all three land
inert until the operator flips each flag after its eval gate (PDR-2/3 additionally need B08 real
Reddit/Trends producers + B09 larger labeled set; PDR-4 needs the larger B09 exit set + side-by-side
audit). The mechanism + unit gates build NOW; live-influence waits on data volume.

**Net:** with PDR-1 (PerceptionFrame) shipped earlier, the PDR-1..4 perception layer is fully
realized in code. Combined with W1-W7, the self-evolution architecture from the capability map is
complete: perception (velocity/convergence/saturation) → decision (BMA + deterministic gate, the
final authority) → reaction (admissibility + paper), with the advisory plane evolving and the
gate/ladder/kill-switch immutable by it. The Camillo social-arb edge is now LATENT-but-built:
every mechanism exists behind a flag; only the operator's eval-gated flips arm it.

### ACTIVATION — flag-flip discipline + B08 social producers unblocked (2026-05-31)

Operator authorized the agent to flip `.env` flags directly ("flip the env flags yourself").
The disciplined outcome was NOT to append 9 lines but to find why each flag was/wasn't safe,
fix the real blocker, and flip the one that genuinely advances the goal. Full analysis:
`docs/operations/2026-05-31-selfevolve-flag-flip-decision.md`.

- **Per-flag analysis vs LIVE state (not just unit gates):** of 9 self-evolution/PDR flags, W1 is
  already live and the other 8 were each blocked by an unmet precondition — flipping blindly would
  be inert (undeployed crons / RESEARCH_DEBATE off / no beliefs corpus) or HARMFUL (CONVERGENCE on a
  100%-news_rss feed would drop every packet; SATURATION would decay live confidence with no B09
  audit). Backed up `.env` → `~/.hermes/.env.pre-selfevolve-enable`; flipped nothing blindly.
- **Traced the PDR-3 blocker to B08:** `catalyst/social.py` (Reddit+Trends producers) was built but
  (a) unwired into the ingest cron and (b) its live endpoints were dead. Fixed BOTH, no operator
  OAuth needed: Trends `dailytrends`(404)→`trending/rss` RSS-2.0 (cac3af0); Reddit `.json`(403)→
  public Atom `.rss` (42f6cb4). Each via build→adversarial-review→self-verify, LIVE-tested (not just
  mocked) — asof from <pubDate>/<published> never now(), never-raises, source tags preserved.
- **Deploy-drift reconciled (the "look at the target before overwriting" rule):** the deployed
  `~/.hermes/scripts/quant-catalyst-ingest.py` had uncommitted FEATURES the repo lacked (consumer-
  trend sweeps + per-item `log_propagations`). A naive `cp` of the repo version would have regressed
  live behavior. Reconciled repo = deployed-base + B08 social wiring (1933064/4a29cc3), committed,
  THEN deployed (DEPLOYED == REPO). Widened the Reddit query set for CELH/CROX/TPR coverage (6edd268).
- **FLIPPED `HERMES_QUANT_SOCIAL_INGEST=1`** (.env:440): live cron now produces a genuinely
  multi-source feed (148 reddit + 360 news → 309 packets); TSLA/RIVN/LCID multi-family in the store;
  a live recommend runs clean (gate + haircut + require_ensemble still govern). Pure upside —
  adds evidence, drops nothing.
- **HELD `HERMES_QUANT_CONVERGENCE` (PDR-3) OFF — measured reason:** with SEMANTIC_ENABLED=1 the
  ingest-time drop is consequential; a kept-vs-dropped check showed flipping now would KEEP only
  TSLA/RIVN/LCID and DROP 16 single-source symbols INCLUDING CELH/CROX (the thesis's own names —
  they lack ACCUMULATED cross-source overlap in a single pull; convergence is temporal over PDR-3's
  freshness window). Gate for the flip: let the deployed cron accumulate cross-source overlap across
  its cadence, re-measure, then flip. Forcing it now would silence the social-arb edge it validates.

**Lesson reinforced:** a flag's safety is a property of the RUNNING system (live data + deploy
state), not the code. The honest path to "flip the flag" ran through building the producers +
reconciling the deploy, not editing `.env`. Memories corrected: `.env` is operator-authorizable;
deploying a cron SCRIPT is agent-doable but ALWAYS diff the target for diverged features first.
