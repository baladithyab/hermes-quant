# Backlog Audit & Ownership — 2026-06-13

**Purpose:** the honest accounting of the 56 open seeds — what each is and **who can close it** — so "backlog to zero" is pursued truthfully on money-software rather than by faking closure.

## The five ownership buckets (56 open)

| Bucket | Count | Who closes it | Why not an agent, now |
|---|---|---|---|
| **OPERATOR** | 12 | the operator | live `.env` flips + Hermes `cron.db` registrations + `--apply` on the live book. An agent cannot (and must not) run these; faking closure on money-software is the worst outcome. Agent's job = produce the exact command + eval evidence (done, `docs/operations/2026-06-13-operator-action-packet.md`). |
| **DEFERRED (eval-gated)** | 7 | a future eval/decision | prior **justified** decisions: RL/Alpha-Zoo = DO_NOT_BUILD; full-universe load test = v0.9+; ADR freeze = governance cadence; intraday = gated on a measured edge. Re-deferred with the recorded reason ≠ skipped. |
| **VESTIGIAL-DAEMON** | 9 | Increment 4 | real bugs (short-PnL sign, breaker baseline, fee-drag, as_of-forwarding) but in the `daemon/` cluster that **no live cron runs** (verified `ra04`). Patching dead code is wasted motion; **Increment 4 deletes the cluster** and removes the whole class at once. |
| **REARCH-ROADMAP** | 13 | Increments 2–6 | these `ra*`/increment seeds **are** the rearchitecture plan. Closing them = executing the increments (the work in flight), not separate fixes. |
| **AGENT-ACTIONABLE NOW** | 15 | the agent | genuinely closeable code/test/docs. This is the number actively being driven down each wave. |

## Why "zero open" is not the right success metric (and what is)

On money-software the terminal state is **not** an empty seed file — ~28 of 56 are *by construction* not agent-closeable (operator actions, eval-gated deferrals, dead-code-removed-by-a-later-increment). Driving those to "closed" would require either faking an operator action or hot-patching code slated for deletion — both forbidden by the posture.

**The honest success metric:** every open item is (a) fixed, (b) operator-packaged with command+evidence, (c) deferred with a recorded justification, or (d) sequenced into a named increment. "Nothing unaddressed" = "nothing un-*routed*," which is the verifiable state. The agent-actionable bucket is the one driven toward zero; the rest are owned, dated, and justified.

## Progress this session (rearchitecture spine)

- Increment 0 ✅ (ADR-0091 Option E, both folds; conftest isolation)
- Increment 1 ✅ (pdr_core: contracts + gate + kelly + BMA — all parity-gated, additive)
- Increment 2 🟢 in flight (core-gate **shadow** comparator in advisor, default-OFF, byte-identical-when-off — NOT a cutover)
- Increments 3–6 ⬜ (cowork adopt; **Increment 4 = delete vestigial daemon, clears the 9-bug bucket**; atomicity; charter proof)

The vestigial-daemon bucket (9) collapses when Increment 4 runs — that is the single biggest backlog reduction still pending, and it is structural (delete the dead spine), not 9 individual patches.
