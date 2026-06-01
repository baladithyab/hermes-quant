# PHASE 6 — Concurrent Review Team findings (2026-05-31, workflow wagephzop)

7 reviewers over the committed session range (b63f8c9..HEAD), each finding adversarially
re-verified (mutation/reproduction). 23 CONFIRMED. The review also independently flagged the
stale-base synthesize.py revert (already caught + discarded by the orchestrator's whole-tree diff).

## HIGH (fix this wave — RR-prefixed = review-reconcile backlog)
- **RR1 (was B33-adjacent): MultiLegProposal constructor-lock is regression-blind.** Mutation proof:
  neutralizing BOTH __post_init__ checks leaves all 27 multileg tests green. The #38 lock works but
  test_multileg_proposal.py has no test that direct cls(risk_gate_pass=True) raises ValueError, a
  non-bool raises TypeError, or the ContextVar mint-token doesn't leak. FIX: add those behavioral tests.
- **RR2: PDR-3 convergence structurally defeated by the per-item cron synth loop.** quant-catalyst-
  ingest.py:131-135 calls synthesize_packets([it]) ONE item at a time, so validate_convergence always
  sees a 1-item set → n_independent==1 → validated=False → every packet dropped when CONVERGENCE=1.
  The eval batches the full set (passes) while production per-item path is blind. THIS is the real
  root of the 2/244 convergence yield (deeper than the freshness/coverage funnel). FIX: batch all
  items into ONE synthesize_packets(items,...) call; attach per-item asof to the propagation log via
  a grouped field rather than the per-item loop (the 4a29cc3 reason for the split). Default-OFF so no
  live regression today, but the documented rollout would silence the whole semantic feed.
- **RR3: Convergence no-lookahead test is tautological** — the test does the <=asof filtering itself
  then asserts the filtered set, never exercising that the validator/caller enforces it. FIX: feed an
  unfiltered set with a future-dated source and assert it cannot manufacture convergence via the real seam.
- **RR4: PDR-2 TrendVelocity marked DONE but is production-UNWIRED** — only eval/tests thread
  velocity_by_symbol into synthesize_packets; the ingest cron never passes it, and frame.trend_velocity
  is observability-only. (Consistent with the prior session note; enroll as explicit residual.)

## MED (fix this wave or next)
- **RR5: audit_log/approvals/decisions silently skip a corrupt line with ZERO log** (loud-crash →
  silent-skip on integrity/HITL-token surfaces). The bcecff7 sweep should LOG (warn) on a skipped
  non-dict line for these security-sensitive readers, not skip silently. (approvals double-spend
  fail-direction shift.)
- **RR6: PDR-2→PDR-4 velocity-peak decay basis is dead** — saturation reads trend_velocity["peak_asof"]
  but VelocityScore.to_mapping() emits "peak_period". Every PDR-4 test/fixture uses synthetic peak_asof,
  so the suite is green while production silently falls back to packet_age. FIX: align the key + add a
  test feeding a REAL VelocityScore.to_mapping() through compute_saturation asserting basis=velocity_peak.
- **RR7: freqtrade order_filled wraps the whole emit in a broad except Exception** (+ the #37 NameError
  fix shipped with no test). FIX: narrow the except + add a regression test for the order_filled record.
- **RR8: admissibility live_buying_power fail-closed branches + autonomous wiring untested.** Add tests.
- **RR9: JSONL non-dict guard regression test pins only 5 of ~15 guarded readers; promotion_orchestrator
  reader left unguarded.** FIX: extend the regression + guard promotion_orchestrator.
- **RR10: PDR-2/PDR-4 cross-seam unwired (trend_velocity=None hardcoded in builder Step 6b) + key
  mismatch (RR6).** Wire it when RR6 lands.

## LOW (batch — lower value)
RR11 no-lookahead fence for saturation producer; RR12 _filter_by_recency wall-clock-now (inject clock);
RR13 perception enrichment failures only logger.debug for an ENABLED feature; RR14 semantic saturation
bare-except; RR15 live_buying_power zero-BP reason-code mislabel; RR16 live_buying_power fresh client
per tick (cache for the tick); RR17 promotion_orchestrator guard; RR18 convergence eval all-hits N=3
fixture (no discriminating power); RR19 recency naive-timestamp branch untested.

## Reconcile decision
RR1/RR2/RR3/RR6/RR7/RR9 are agent-doable + high-value (test coverage of money-path locks + the real
convergence wiring fix) → next execution wave. RR4/RR10 fold into the PDR wiring fix. RR5 + LOWs batch.
None touch the gate/ladder/kill-switch. RR2 is the headline: the real reason convergence can't fire.
