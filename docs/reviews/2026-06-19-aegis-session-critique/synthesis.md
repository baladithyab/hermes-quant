# Cross-Facet Synthesis — Codex adversarial critique of the AEGIS session (4bb7271..491b11c)

**Scope:** 41 commits, 64 files, +2435 lines merged to main this session (options P/D/R/Monitor stack, ml00b composite store, jw1 audit-trail, ADR-0092 home-decouple, ob5 social provider). **Already merged** — so every confirmed finding becomes a follow-up seed, not a pre-merge block.

**Reviewers:** 6 Codex `gpt-5.5/xhigh` facets fired in parallel. 5 returned substantive findings; **Facet-1 (Vision) returned empty** (the documented gpt-5.5/xhigh hang on the longer prompt — not re-run; Facet-6 Executive covered the vision/scope angle). Codex can read but **cannot run tests** — every finding below was **independently verified against the actual code by Claude** before acceptance.

## Findings (all CONFIRMED against code; none fixed this session — work already merged)

| # | Facet | Sev | Finding | File:line | Verified |
|---|---|---|---|---|---|
| F2-a | Safety | **HIGH** | leg-op order hardcodes `outer_qty=1`+`contracts=1`; a composite with `outer_qty>1` gets marked decomposed after submitting ONE spread → residual contracts unmanaged (options-size-by-contracts bug) | autonomous.py:2442,2455 | ✓ |
| F2-b | Safety | MED | leg-op `proposal_id=f"legop_{underlying}_{play_tag}"` collides for two composites on the same symbol → reactor idempotency returns the prior parent without sending a new order, caller proceeds as if it fired | autonomous.py:2447 | ✓ |
| F4-a | Test | **HIGH** | `aegis-gate2-eval.py` writes the unlock marker under `--home` but loads the settled book from the process-default `EXECUTION_BUS_PATH` (no `executions_path` threaded) → multi-home run can unlock GATE-2 from the WRONG book; the test monkeypatches the loader so it never exercises this | aegis-gate2-eval.py:~100 | ✓ |
| F4-b | Test | MED | `_fake_mleg` ml00b fixture: credit vertical with POSITIVE `net_debit_credit` (real producers store credits NEGATIVE); test never asserts `row.net_entry_price` → a sign-loss regression in `_persist_composite_play` passes while agmon sweeps compute the wrong P&L side | test_ml00b_composite_leg_persistence.py:59 | ✓ |
| F5-a | State | MED | `_persist_composite_play` passes `option_legs=[]`/`expected_leg_count=0` for a legless mleg → durable but every sweep skips it (contradicts the fail-closed-on-malformed row guarantee) | autonomous.py:1768 | ✓ |
| F5-b | State | MED | `option_legs_json` ALTER TABLE runs in autocommit (PRAGMA-check then bare ALTER, no BEGIN IMMEDIATE) → two cron processes both ALTER; loser's duplicate-column error swallowed after a real fire → no composite row (missing-BEGIN-IMMEDIATE race family) | composite_plays.py:242-247 | ✓ |
| F3-a/b | Arch | LOW | 2 home-literal sites the regex sweep missed (`portfolio/state.py:267` `_DEFAULT_EXECUTIONS_PATH`, `quant-watchlist-evolve.py:199` `Path.home()`) | (per file) | ✓ |

## Convergence (high-confidence cross-model signal)

- **Home-coupling residue** flagged by THREE independent angles: F3 (the 2 missed literals), F4-a (gate2-eval reads the wrong home's book), AND it converges with the seed I already filed (`aegis-ra-home2`). Strong signal: the ADR-0092 home-decouple is **incomplete** — the new bf76b/agopt3 scripts and 2 library sites still don't honor the injected home. This is the single most-converged theme.
- **Options-size-by-contracts / idempotency in the ml01b leg-op executor** (F2-a HIGH + F2-b MED) — both in Aria's iter-5 code I cherry-picked. The `outer_qty=1` hardcode is the recurring multileg-units family.
- **Executive verdict (F6) converges with my own session findings**: "solid scaffolding, NOT live-ready until ledger/reconcile/reset + decision-liveness gaps fixed" — matches the NVDA state.db orphan (`hermes-quant-statedb-nvda-orphan` P1) + the "runs but doesn't trade" gap (GAP-2) I already surfaced firing the daily phases live.

## What the critique VALIDATED (positive signal)

- No reviewer found a no-lookahead violation, a fabricated-asof, or a NaN-into-money-gate hole in the NEW code — the hard rails held under adversarial reading.
- No source flag-default flipped to ON; ob5 confirmed default-OFF + unwired.
- The convergence single-platform-pump guard (≥2 independent families) was not refuted.
- jw1 audit-trail fix not challenged.

## Executive verdict

**Rating ~7/10** (Codex F6: "solid scaffolding"). **Single blocking concern: the home-decouple + ledger/reconcile/reset integrity is incomplete** — none dangerous TODAY (all default-OFF, paper, options unarmed), but each would bite the moment options are armed in a multi-home/cron context. **One action this week:** fix the gate2-eval home-threading (F4-a) + the leg-op `outer_qty`/idempotency (F2-a/b) before any options-arming, and resolve the NVDA orphan (already P1).

## Reviewer framing (meta — what NONE of the reviewers checked)

- **The thing that actually matters**: whether the strategy has edge. All 6 facets reviewed *engineering correctness*; none could assess *does the signal make money* — which the live daily-phase run already answered (0 new trades; the watchlist→2-names collapse + single-horizon discard is the real gap). The critique confirms the machine is well-built; it cannot confirm it will earn.
- **No facet ran the tests** (ChatGPT-Plus tool limit) — the "test never asserts X" findings (F4-b) are static reads; I verified them against code, but a real mutation-test sweep would be stronger.
- **cowork-quant (PR #88) was out of scope** — its 3 open P1/P2 findings are the cowork session's.
