# Cross-Facet Synthesis — Codex (GPT-5.5) review of the 2026-05-30 deep-work loop

**Scope:** 16 commits `e4ecad5..HEAD` (admissibility, options foundation, direction-bias fix, catalyst PDR wiring, observability, ADR-0079 PDR capstone). +18k lines, 103 files. All new capabilities default-OFF.
**Method:** 6 parallel `codex exec review` facets (gpt-5.5, ChatGPT auth) — rails, fail-closed, admissibility correctness, options correctness, PDR architecture, executive. 5/6 returned substantive output (Facet-6 executive hit the documented gpt-5.5/xhigh empty-output hang).
**Cross-model value:** GPT training ≠ Claude training → genuine independent signal, not echo-chamber.

## The dominant convergent finding (4 facets agree)

**Every HIGH/P1 finding is POST-GO-LIVE-ONLY — gated behind a flag that is OFF today.** Facets 1, 2, 3, 4 independently converged on the same cluster of issues in the options gate and the admissibility live-wiring. Not one is reachable with the current default-OFF flags. This is the strongest possible validation of the "build-behind-a-flag is the safe construction; the flip + pre-go-live eval is the operator's gate" rail: the bugs live in code that cannot execute until someone deliberately enables it.

### Convergent findings → folded into the pre-go-live hardening task (#11)

| Finding | Facets | File:line | Reachable | Disposition |
|---|---|---|---|---|
| `effective_size` (NAV fraction) passed as share `qty` to oracle → every short rejects as FRACTIONAL_SHORT on flip | 1, 3 + build-review | `autonomous.py:473-478` | flag OFF | **#11** — convert via `target_pct_to_shares` + populate `AdmissibilityContext` before flipping `HERMES_QUANT_ADMISSIBILITY` |
| Restatement script passes `abs(pos.quantity)` (NAV fraction) as shares | 3 | `quant-admissibility-restate.py:116` | offline | **#11** — same unit conversion; artifact is wrong until fixed |
| Admissibility accepts ETB short without BP/equity/ask context (fail-open on missing account inputs) | 1, 2, 3 | `oracle.py:167-181` | flag OFF | **#11** — require account context; REJECT when unknown |
| Options gate: unrelated long leg classified DEFINED_RISK → naked short bypasses no-naked check | 1, 2, 4 | `options_gate.py:154-158` | flag OFF | **#11** — validate same-underlying/right/expiry + width before defined-risk |
| Options gate: `min_dte=None` skips BOTH pin-risk + min-DTE envelope (fail-OPEN) | 2, 4 | `options_gate.py:457` | flag OFF | **#11** — require DTE; fail-closed when unknown |
| Options gate: greeks scaled by 1 lot, not `order_qty` → multi-contract breaches caps | 4 | `data.py:272-279` | flag OFF | **#11** — scale by `ratio_qty * order_qty * 100` |
| Options gate: CSP admitted when BP < full assignment cash (under-collateralized) | 2, 4 | `options_gate.py:171` | flag OFF | **#11** — require full `strike*100*contracts` |
| Wheel overlay sizes by `held_shares//100`, ignores NAV target → can widen exposure | 1 | `options_gate.py:257` | flag OFF | **#11** — cap by `target_nav`/`max_position_pct` |
| Missing greeks raises instead of returning `silence` → could abort a tick | 1 | `options_gate.py:328` | flag OFF | **#11** — convert to deterministic silence/reject |
| PIL debited for every supplied dividend, not only those held-across-ex-div | 3 | `borrow_pnl.py:67` | flag OFF | **#11** — guard on ex-div ∈ held dates |

## Findings that landed as code THIS session

| Finding | Facet | Fix commit |
|---|---|---|
| **proposals.py: mid-file malformed line silently skipped → could resurrect a closed HITL proposal as `pending`** (the ONE finding reachable with today's flags) | 2 | `_reconcile_index` now FAILS LOUD (`ProposalLogCorruptionError`) on a non-trailing malformed line; tolerates only a trailing partial write. +2 tests (mid-file-raises, trailing-skips). |
| **ADR-0079/design: SaturationScore applied to the post-BMA aggregate → a stale social trend could veto unrelated TA/Kronos analysts** | 5 (P1) | D79.4 + design §3.3 rewritten: saturation decays the semantic analyst's OWN view BEFORE BMA, never the aggregate. +2nd property test (non-semantic views bit-identical sat-on vs off). |
| **ADR-0079/design: LLM committee "selects within envelope" AFTER the gate → makes LLM the final actuator (contradicts ADR-0004)** | 5 (P1) | New D79.6a: LLM is evidence UPSTREAM of the gate; any post-gate role is tighten-only (quieter/unchanged, never re-select). |
| **ADR-0079/design: `[ADMIT]` placed after `[GATE]` in the diagram but described as upstream precondition** | 5 (P2) | Design §1.1 diagram reordered: `[SATURATE]`(view-level) → `[FUSE]` → `[ADMIT]`(precondition) → `[GATE]`(final). |

## Findings NOT addressed this session (deferred, with reason)

- All 10 post-go-live options/admissibility findings → task **#11** (pre-go-live hardening). Correct to defer: unreachable until a flag flips, and the flip is itself gated on an eval the operator runs.
- Facet-1 P2 "direction-bias OFF still injects semantic extras in semantic-enabled envs": not a bug — the semantic injection (C2-2) is independently governed by `HERMES_QUANT_SEMANTIC_ENABLED`; `HERMES_QUANT_DIRECTION_BIAS_GATE` only governs the bias screen. Both are individually flag-gated. Noted as a coupling smell (two features share one wrapper); no action.

## What the critique VALIDATED (positive signal)

- ETB short predicate, /360 borrow basis, Friday weekend ×3, dividend-debit sign, and the `apply_verdict_to_target` no-amplification adjuster — "largely aligned with ADR-0077" (Facet 3).
- OCC-21 format/parse and the disabled multi-leg reactor scaffold — "contained"; confirmed it cannot fire / writes nothing while its flag is unset (Facet 4).
- The PDR decomposition is "directionally sound" (Facet 5) — the three corrections were doc precision, not a flawed architecture.
- The convergent-rejection thesis (every reference repo lets an LLM be final authority; we invert it) held up under GPT scrutiny.

## Verification evidence (Phase 8)

- **Release-blocker no-lookahead gate: 11 passed, 1 skipped** (Kronos importorskip).
- **Full suite: 2653 passed, 62 failed, 6 skipped (882s).** ALL 62 failures are pre-existing: optional-dep gaps in this `.venv` (`torch.manual_seed` missing → 14 kronos; `ccxt.NetworkError` missing → 3 ccxt — fails in ISOLATION, i.e. env not code; sklearn-missing calibrators) + combinatorial test-pollution (`llm_committee_caller`, `research_debate_wiring` — PASS in isolation; wave_d). **ZERO of the session's ~25 new test files appear in the failure list** (verified by grep). Matches the documented R2b baseline.
- **No new `HERMES_QUANT_*` flag is hard-set to 1 anywhere in repo/deploy** (grep-verified) — every new capability is genuinely default-OFF; landing this alters zero live behavior.

## Executive verdict (synthesized; Facet-6 produced no body)

**Quality: high.** Single most important pre-go-live blocker: the `effective_size`-as-share-qty unit bug (3 reviewers) — fix before flipping `HERMES_QUANT_ADMISSIBILITY`. Top inherited risks: (1) the options gate's collateral/greek-scaling correctness gaps (all flag-OFF); (2) full-suite test pollution masking real signal; (3) the dev `.venv` missing `[stacking]`+torch+current-ccxt so local full-suite is red. Default-OFF "safe to land" claim: **TRUE** (grep-verified, plus the proposals.py live-path finding now fixed).

## Reviewer framing (what NONE of the reviewers checked — a human still must)

- **Live broker behavior.** Codex reviewers cannot hit Alpaca; the admissibility predicate's correctness against the *real* `easy_to_borrow`/`shortable_shares` API response is unverified. Run the integration test (`tests/integration/test_admissibility_alpaca_live.py`) against paper creds before trusting it.
- **The offline restatement's actual numbers.** Whether the 38 synthetic shorts are truly mostly NOT_ETB (the premise of ADR-0077) is an empirical claim only the operator can confirm by running `quant-admissibility-restate.py` against the real `state.db` — and only after the unit-bug fix (#11).
- **Economic realism of the saturation decay curve** (PDR-4, not built) — no reviewer can judge whether a given decay function actually tracks Camillo's information-parity exit without a labeled exit-timing backtest.
