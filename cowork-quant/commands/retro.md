---
description: Weekly retrospective — settled outcomes, calibration drift, what to change (advisory only)
allowed-tools: ["Read", "Write", "Bash", "Glob"]
---

Weekly retro (hermes-quant ADR-0026 retrospective-amendment-loop port,
advisory-plane only: this command PROPOSES changes; it never applies them).

1. Pull the week's ledger slice (`status` + read `quant-state/ledger.jsonl`
   events since the last retro file in `quant-state/retros/`).
2. Outcomes: settled trades (win rate, avg realized return, by asset and by
   horizon), gate silence counts by rule, proposals expired unreviewed,
   approval->fill latency.
3. Calibration: per-analyst ECE table; flag any analyst with ECE > 0.10 or
   accuracy inverted vs confidence. Recommend confidence shrinkage factors
   (the `aggregate` CLI applies ECE shrinkage automatically — the retro's
   job is to surface drift to the human, not to fix it silently).
4. Process honesty checks:
   - Did any fill land without an approval? (the CLI refuses these — if one
     appears in the ledger anyway, lead with it)
   - Did the human size UP from a gate target anywhere? (rail violation)
   - Were breakers/halts respected?
5. Hypothesis review (deterministic registry): `python -m quantcore.cli hyp
   summary --state-dir <quant-state>` — resolve any forecasts whose horizon
   has settled (`hyp resolve`, outcome from the settle events, never from
   memory), report per-hypothesis and overall Brier, and propose status
   transitions (supported/refuted) for the user to confirm. New falsifiable
   theses found in proposal rationales get `hyp create` + `hyp link`.
6. Write `quant-state/retros/<date>-retro.md` with an explicit ADVISORY
   section: proposed config changes (e.g. "shrink fundamentals confidence by
   0.05", "raise cost_multiple to 2.5") each with the evidence line. The
   user applies changes manually to config.json if they agree — never edit
   it from this command.
7. Reply with the digest + the advisory list.

Honesty rails: only settled outcomes count (no marking open positions as
wins); deflated expectations — fewer than ~20 settled trades means every
statistic gets a "small n" caveat, not a conclusion.
