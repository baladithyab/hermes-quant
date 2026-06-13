# x-ai/grok-4.3 (served: x-ai/grok-4.3-20260430)

**P0 | Authoritative log mutation (Decision Outcome §3, Consequences Negative)**  
The one-time repair script rewrites `executions.jsonl` in place after backup. This directly contradicts ADR-0085's "never touch the log" rule and the claim that "the log stays the single source of truth." Even with dry-run + `--apply` guardrails, any future operator error or partial failure leaves a mutated txn log whose deltas no longer match the original reactor output. B does not avoid the integrity problem; it merely makes it explicit and gated.

**P0 | Cash-delta math undefined (Decision Outcome §2)**  
`Δcash = −delta × fill_price` is stated without specifying the NAV basis used to convert `fill_size_pct` (now a delta) into share quantity at fill time. When the position read for delta calculation (`reconstruct_portfolio_state`) occurs under a different mid-price or after an intervening MTM tick, the resulting cash movement will not match the actual broker cash sweep. No handling for partial fills, price slippage between target computation and fill, or reactor_metadata.quantity scaling appears. This breaks both settlement journal cash and any future broker reconcile.

**P1 | Repair non-determinism under reactor_name / timestamp skew (Decision Outcome §3)**  
Repair iterates "in timestamp order per (account, asset)". The ADR never defines tie-breaking when two reactors emit fills for the same symbol within the same second, when reactor clocks differ, or when a reactor is renamed/forked post-repair. The original Option A shim was rejected for exactly this non-determinism; B's repair script silently inherits the same fragility without the explicit `reactor_name` key.

**P1 | 0-delta re-affirmation rows break downstream assumptions (Decision Outcome §1)**  
Emitting `fill_size_pct=0` records turns what used to be no-op target restatements into explicit journal entries. Nothing shows that `settlement_loop.join_exit_fills`, EOD snapshot cron, or any aggregate over "fills with non-zero size" was audited for these rows. The acceptance gates only test folding to correct position; they do not assert absence of P&L or cash side-effects from zero-size entries.

**P2 | Firing-path re-use creates ordering dependency (Consequences Negative, §5)**  
Reactors now call the same `reconstruct_portfolio_state(reactor_filter=...)` that the cap uses, but the ADR provides no ordering guarantee between the cap read and the subsequent delta-emitting fill write when both run inside the same reactor tick. A concurrent proposal from another reactor for the same symbol can produce a stale delta that is recorded forever.

**P2 | Chosen Option B still inferior on operational surface area**  
B replaces one permanent flag/shim with a mandatory, operator-gated, backup-dependent repair step that must be re-run on every historical import or cross-environment restore. The "no new branch" benefit is real for code, but the operational contract (two explicit commands, backup verification, post-repair reconcile) is heavier than the rejected projection-only Option A would have been once its cost-basis and settlement desync P0s were fixed.