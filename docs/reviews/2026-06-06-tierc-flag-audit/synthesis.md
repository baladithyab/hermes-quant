# Tier-C flag retirement audit — two-reviewer synthesis (2026-06-06)

**Question:** which Tier-C flags (FLAGS.md "likely retire" list) are safe to DELETE?

**Method:** machine-generated inventory of 34 flags (read-site count, SET-status in
env/wrappers, ADR refs, git last-touch) → independent classification by TWO reviewers
on different training distributions, each reading the actual read-sites:
- **Hermes / Claude Opus 4.8** (delegate_task, 9 tool calls, read bma.py/builder.py/llm_committee.py/etc.)
- **OpenAI / Codex** (`codex exec -s read-only`, ~9 min, grepped tests + read source)

## Convergent verdict

| | RETIRE | KEEP | PROMOTE-CAND | INVESTIGATE |
|---|---|---|---|---|
| Hermes (Opus 4.8) | **0** | 16 | 18 | 0 |
| Codex (GPT)        | **0** | 16 | 16 | 2 |

**Both independently returned RETIRE = 0.** Nothing in Tier-C is safely deletable.

## Why (convergent reasoning)
- Every flag's ON-path is live, wired, and behavior-changing — no unreachable/superseded no-op spikes.
- The codebase is fresh: HEAD 2026-06-05, nearly every flag touched within the prior ~2 weeks.
  The "experiments that never graduated" premise behind the retire-list does not hold.
- 6 flags (CONVERGENCE, CALIBRATOR_AUTO_REFIT, HORIZONS, CATALYST_ONBOARDING, SATURATION,
  ADMISSIBILITY) are SET in deploy env/wrappers → live config, mis-tiered as C.
- The decision-core / risk-gate / BMA / analyst-pool flags are default-OFF PROMOTE-CANDIDATEs —
  they need the flag-ablation eval, not deletion.

## Divergences (immaterial — none flips to RETIRE)
- SHADOW_RULE_MINING, TRADER_LLM, SNAPSHOT_V2, PLAYS_OPEN: KEEP (Codex/one) vs PROMOTE-CAND (other).
- STRUCTURE_SELECT, WATCHLIST_CAP_TRIM: Codex flagged INVESTIGATE (no confirmed production caller).
  → worth a follow-up read, but neither is RETIRE either way.

## Action taken
- FLAGS.md Tier-C section + "next actions" #3 rewritten: RETIRE set empty; reduction path is
  PROMOTION (eval-gated), not deletion. No retirement lane dispatched (there's nothing to retire).
