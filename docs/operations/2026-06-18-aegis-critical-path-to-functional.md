# AEGIS: critical path from "built" (~31%) to "properly functional / profitably trading"

**Date:** 2026-06-18 · **Branch:** docs/rearchitecture-shared-pdr-core (HEAD 59861da) ·
**Source:** vision-vs-reality assessment (wf_f82ff482) + zero-fill root-cause investigation (wf_415eb856)

## The one-line truth
We built the machine; it has not earned. 48/48 live executions are zero-size silences,
N_settled_round_trips=0, $0 realized, the only real track record is one ASTS trade at -4.64% / 0% win.
Every dimension that scored well scored on ENGINEERING QUALITY; the entire gap is "it has never
actually traded." Overall functional readiness ~31%.

## Dimension scores (functional, not code-completeness)
Reaction/Execution 58 · Risk/Safety 52 · Shared-core arch 48 · Autonomy/self-evolve 48 ·
Perception 42 · Decision 34 · **Evidence/Profitability 12** (the dominating low).

## ROOT CAUSE of the zero-fill (verified live, NOT a bug)
Two compounding causes; the silence is mostly CORRECT rails + one operator regression:
1. **Watchlist collapse (dominant):** scanned universe dropped ~120 -> 14 symbols on 2026-06-10
   (config.yaml quant.autonomous.watchlist edit). Tick log: 6621 SILENCE_GATED_BY_ADVISOR vs 18
   FIRE ever, last fire 2026-06-09, ZERO since. Too few names to surface a >0.50-conf signal.
2. **Single-horizon tick discards the confidence signal the HITL path uses:** the live isotonic
   calibrator (honestly trained, n=3074, is_calibrated=True — NOT cold-start) maps the watchlist's
   raw vote-shares (0.04-0.36) to <=0.4833 (below 0.50 break-even), so the edge-sign guard
   (gate.py:617) CORRECTLY silences. But proposals.jsonl (multi-horizon HITL path) shows 200/200
   risk_gate.pass=True on the SAME symbols — recommend_multi_horizon boosts confidence on
   cross-horizon agreement. The autonomous tick uses the LEGACY single-horizon recommend; the
   multi-horizon wire-up (HERMES_QUANT_HORIZONS, ADR-0036, Wave C) is deferred.

NOT THE CAUSE (ruled out): sizing/Kelly-snap/caps (variance tiny -> any passing edge snaps to 0.20,
never 0.0). DO NOT lower min_confidence (0.65) or bypass the edge-sign guard — both are correct rails.

## THE CRITICAL PATH (strictly ordered — each gates the next)
- **0a [OPERATOR, config]** Restore the ~120-symbol watchlist (universe/alpaca-daily-top100.json
  source). Dominant lever; alone restored FIRE behavior pre-Jun-10.
- **0b [AGENT, default-OFF]** Wire the autonomous tick to recommend_multi_horizon behind a NEW
  HERMES_QUANT_HORIZONS flag. NOTE: recommend_multi_horizon returns list[AnalystView] (raw views),
  NOT the result dict — so this is real Wave-C integration (run the multi-horizon views through BMA
  + risk gate to produce the tick's result dict). CAVEAT to document: the calibrator is fit on
  single-horizon vote-shares; multi-horizon confidence may be over-optimistic -> gate behind the
  clean-window eval, watch N_settled + win-rate before any promotion. Each fix RED-proven, reviewed.
- **1 [OPERATOR elapsed-time]** Accrue GATE-1: N>=20 settled round-trips, win>=0.40, dd<=8%,
  zero kill-switch. The harness (eval/clean_window.py) is ready; it has nothing to measure (N=0).
  This is the first profitability proof the system would EVER have.
- **2 [AGENT, parallel w/ 1]** Make evaluate_gate ENFORCE capital (today read-only: "nothing on the
  live path consumes it yet"). Wire GATE-1/2/3 verdict into the tick's capital decision (bf76b family).
- **3 [OPERATOR flip + AGENT eval]** Close the self-evolution loop: arm HERMES_QUANT_L2_LESSON_HAIRCUT
  (bma.py:732, reflection->decision, built + asof-honest, off). Needs Stage-1 track record.
- **4 [AGENT + OPERATOR + time]** Unlock options — 3 hard walls: (a) structure_intent never populated
  on the advisor path (advisor.py emits no such key); (b) no IV-rank history (no ~/.hermes/quant/
  option_chains/, needs >=30 day-points via agperc3 live-chain cron); (c) options BP hardcoded 0.0.
  Then write the GATE-2 options_unlock.json marker (bf76b cron), arm the chain, earn AG-OPT-EV-1
  (N_options>=30). Depends on equity profitability (Stage 1) first.
- **5 [AGENT + OPERATOR] last** Arm ALPACA_PAPER (real-broker fills -> slippage realism; today only the
  simulator). Land ac01/ac03 (re-point cowork to the mirror, extract the standalone aegis package) ->
  makes ADR-0092/0095 real (flip to accepted), the "two hosts" half of the vision.

## The honest framing
0a + 0b get the tick TRADING. Stage 1 is the gate everything else waits on — and it needs ELAPSED
TIME with real fills, which no agent can shortcut. Options (the back half of the instrument vision)
is correctly LAST: it rides on a proven-profitable equity base. The biggest risk surfaced by
unblocking: the strategy may have no genuine edge (one ASTS trade, -4.64%) — Stage 1 is precisely
the test of that. Unblocking trades is how we find out, safely, on paper, behind every rail.
