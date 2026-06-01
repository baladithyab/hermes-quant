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

## LOW review-nits batch — disposition (2026-05-31, seed hermes-quant-1ef6 review-nits)
Behavior-preserving polish only; no rail changes; happy path byte-identical when each feature is OFF.

- **RR11 — DONE.** Added a no-lookahead RELEASE-BLOCKER fence for the saturation PRODUCER in
  `tests/test_no_lookahead.py` (Invariant 7d): a pure-producer fence (`compute_saturation` ignores a
  future velocity peak / confirm_date / packet asof → m=1.0, basis `no_basis`, asof stamped == decision)
  AND a frame-builder Step-6b fence (a FUTURE confirm_date on packet metadata must NOT drive the
  multiplier to the floor; the score stamps the bar asof <= decision). Discriminating (a past anchor
  decays). Complements the existing unit-level `test_pdr4_saturation.py::test_saturation_is_lookahead_honest`.
- **RR12 — DONE.** `catalyst/social.py:_filter_by_recency` now takes an injectable `now: datetime | None`
  (default `None` => `datetime.now(UTC)`, byte-identical to the wall-clock path; a naive injected `now`
  is localized to UTC). `ingest_social(..., now=...)` threads it through. Tests in
  `test_catalyst_social.py` pin a FIXED `now` for deterministic recency cuts (no wall-clock dependency),
  the default==wall-clock equivalence, and the naive-now branch.
- **RR13 — DONE.** The four flag-gated enrichment except handlers in `perception/builder.py` (Step 5
  semantic, 5b velocity, 5c convergence, 6b saturation) now log at `warning` (was `debug`): each block is
  reached ONLY when its flag is ON (feature ENABLED), so an always-failing enabled feature is now visible.
  Failure is still swallowed (silence-by-default rail). Happy path emits nothing → byte-identical.
  Tests: `tests/perception/test_builder_enrichment_logging.py`.
- **RR14 — DONE.** Two parts. (a) `analysts/semantic.py` SATURATE multiplier `except` now LOGS a
  `warning` (feature ENABLED) instead of a silent `pass`; still swallows (saturation must never break the
  view). (b) `perception/saturation.py:apply_saturation` narrowed its bare `except Exception` to
  `except (TypeError, ValueError)` — the only exceptions `float()` raises on a missing/non-numeric
  `decay_multiplier`; NaN/inf still pass float() and are rejected by the existing `(0,1]` guard, so the
  malformed-input matrix is byte-identical. An UNEXPECTED error class now propagates (no longer masked).
  Tests added to `test_pdr4_saturation.py` (narrowed-except matrix, propagation, failure-logs-and-view-
  survives, happy-path-logs-nothing).
- **RR15 — NOT cleanly fixable (documented, no code-behavior change).** A genuinely zero/negative-BP
  account collapses to the SAME `None` as an unknown/failed fetch in `oracle.live_buying_power()`
  (`bp if bp > 0 else None`), so the oracle labels it `MISSING_ACCOUNT_CONTEXT` instead of the more
  precise `INSUFFICIENT_BPR`. Both REJECT — the fail-closed DIRECTION is identical (a zero-BP short never
  admits), so it is purely a reason-code label nuance. A real fix would (a) change the observable
  `verdict.reason` on the live zero-BP path (NOT byte-identical), (b) change `live_buying_power`'s
  contract (pinned by `tests/unit/test_admissibility_bp.py::test_live_bp_zero_returns_none`), and (c)
  touch the admissibility seam — all outside a behavior-preserving review-nit. Added a clarifying RR15
  note to the `live_buying_power` docstring (comment-only, runtime byte-identical). Revisit only behind an
  explicit reason-code refinement task. (RR16–RR19 out of scope for this batch.)
