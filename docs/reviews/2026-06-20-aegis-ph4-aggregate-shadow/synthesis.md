# Cross-Facet Synthesis — Codex critique of the AEGIS ph4 PERCEIVE-onboarding (d1709ff..715cdd6)

**Scope:** 4 commits (395c5c5 build + 2ffaaac/715cdd6 seeds + c5acffc review-fix), ~1254 LOC across `hermes_quant/pdr_core_adapter.py` (+468, the aggregate runtime shadow), `hermes_quant/advisor.py` (+29, the live Step 6.6 seam), `ops/scripts/quant-pdr-core-parity-report.py` (+layer/comparable split), and 2 test files (+672). On branch `aegis-phase4-aria` (NOT yet merged) — so confirmed findings are fix-on-branch, not post-merge follow-ups.

**Reviewers:** 6 Codex `gpt-5.5/xhigh` facets, all 6 returned substantive output (the xhigh tail ran long — ~8-14 min — but none hung). Codex can read but cannot run tests; every finding below was **independently verified against the actual code by Claude** before acceptance.

## Findings (all verified against code)

| # | Facets (convergence) | Sev | Finding | File:line | Verified |
|---|---|---|---|---|---|
| A | **F6** | **HIGH / MUST-FIX** | The 3 new `recommend()` flag-OFF tests build the DEFAULT analyst roster (incl. `KronosAnalyst`) with no `analysts=` loadout and no HF-offline guard (the guard lives only in `tests/integration/conftest.py`, not `tests/pdr_core/`). On a full-deps `[all,dev]` box this hangs in torch/HF inference — the `aegis-ci-hang` family. | test_aggregate_shadow.py:480-548; advisor.py:467-469 | ✓ confirmed (Kronos in default loadout; no guard in tests/pdr_core) |
| B | **F1 + F2 + F5** (triple) | MED (P2) | `run_shadow_aggregate` does NOT thread `horizon_agreement_bonus`/`horizon_disagreement_penalty` off the live `aggregator.config`; a recipe/injected `BMAConfig` with non-default horizon multipliers makes the core use defaults → **FALSE divergence** on a faithful port. | pdr_core_adapter.py:878-885; bma.py:1314/1319 | ✓ confirmed |
| C | **F3 + F4 + F5** (triple) | MED (P2) | `_signal_primitives` drops `components` (and `asset/timeframe/asset_class`), and `_compare_signals` never checks the `asof` it stores → a runtime projection/core bug in those fields logs as **agreement** (missed divergence). The static parity test DOES assert component parity. | pdr_core_adapter.py:573-623, 641-702 | ✓ confirmed |
| D | **F1 + F5** | MED (P2) | A REUSED `BMAAggregator` with accumulated `update()` calls (non-uniform posteriors) but still on `ColdStartCalibrator` passes both comparability gates, then the core's fixed 0.5 weights diverge from live learned `_weight_for()` → FALSE divergence on a by-design-different state. | pdr_core_adapter.py:807-824 | ✓ confirmed (gate only checks calibrator-type + env flags) |
| E | **F1** | MED (P2) | An injected `ic_dedup_gate` or `regime_detector` changes the live vote inputs even when env flags are unset; the shadow runs the off-path core over unadjusted views → FALSE divergence. | pdr_core_adapter.py:807-824 | ✓ confirmed (no gate for injected collaborators) |
| F | **F2** | LOW (P2) | On divergence, the WARNING logs the full `live_signal` dataclass repr, which includes `AnalystView.rationale/metadata/evidence_ids` (the JSONL persist path correctly reduces to primitives, but the log line does not) → richer analyst payload leak to logs. | pdr_core_adapter.py:886-891 | ✓ confirmed |
| G | **F4** | MED | No end-to-end test of a REAL **divergent** report being logged AND persisted (the persist-true test writes only an agreement line; the divergence RED-proof uses `persist=False`) → a serialization bug in divergent records could slip. | test_aggregate_shadow.py:140-159, 414-417 | ✓ confirmed |
| H | **F6** | LOW (nice-to-have) | Not-comparable records persist `reason` but drop `flag`/`detail` from the returned report — the report won't say WHICH flag blocked coverage. | pdr_core_adapter.py:721-738 | ✓ confirmed (persist record omits flag/detail) |

## Convergence (cross-model signal)

- **Comparability gate is INCOMPLETE** — flagged by FOUR facets (B/D/E from F1/F2/F5). The single strongest theme: the gate excludes fitted-calibrator + the 7 env learning flags, but NOT (i) custom-config horizon multipliers, (ii) non-uniform learned posteriors on a cold-start calibrator, (iii) injected ic_dedup_gate/regime_detector. Each produces a FALSE divergence that would pollute the parity sample and make a faithful port look broken — directly defeating the evidence-before-cutover purpose.
- **Comparison surface is INCOMPLETE** — flagged by THREE facets (C from F3/F4/F5). `_signal_primitives` drops load-bearing fields the static parity test compares (`components`) plus identity fields (`asset/timeframe/asof`), so a runtime-only port bug there is invisible (missed divergence).

## What the critique VALIDATED (positive signal)

- **Live-decision safety is SOUND** (F2 + F6 explicit): advisor Step 6.6 runs after `agg_signal` is finalized, never reassigns it, swallows all errors, persist failure is contained. Flag-OFF byte-identical. Rollback trivial (default-OFF, additive JSONL).
- **No test-vacuity** (F4): tests build real `protocol.AnalystView`/`MarketContext`/`BMAAggregator`, not hand-rolled shapes.
- **RED-proofs are genuine** (F4): would go RED on a real direction port bug.
- **The post-review `_AGG_LEARNING_FLAGS` set EXACTLY matches the live BMA env reads** (F2 explicit) — the P1 false-divergence hole fixed in c5acffc is confirmed closed.
- **Parity-report comparable/not-comparable math + gate back-compat is sound** (F4 + F6).

## Executive verdict

**Rating 7/10** (Codex F6). **Single blocking concern: the new tests must not run Kronos/default-advisor inference (finding A)** — fix before the branch is trusted. The P2 cluster (B/C/D/E) does not threaten the live decision (all default-OFF, observe-only) but **weakens the parity proof** the shadow exists to provide — each must be fixed before the operator arms `HERMES_QUANT_PDR_CORE_AGG_SHADOW=1` in a clean window, else the divergence log fills with false positives. **One action this week:** fix A (hang) + B/C (the two triple-converged correctness gaps) + D/E (gate completeness) + F (log-leak), then re-run.

## Reviewer framing (meta — what NONE of the reviewers checked)

- **Does the shadow's agreement actually mean the port is correct *in production*?** All facets reviewed the comparison machinery; none could assess whether the live AnalystView distribution the running advisor produces ever reaches a comparable state at all (the live cron may always be on a learning-flag path → 0% comparable coverage forever). That is the operator's clean-window question, answerable only by arming the flag and reading the report.
- **No facet ran the tests** (ChatGPT-Plus tool limit) — finding A's hang was reproduced by Codex with `--timeout=15` but the P2s are static reads; Claude verified each against code.
