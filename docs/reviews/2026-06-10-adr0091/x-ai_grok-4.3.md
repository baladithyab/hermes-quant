# x-ai/grok-4.3 (served: x-ai/grok-4.3-20260430)

**P0: Cash-delta + avg_entry on absolute_target is incorrect (section 2).**  
When absolute_target replaces quantity, setting `avg_entry_price` from the current fill price and computing cash as `−(new−old)×fill_price` is wrong for partial reductions or increases: prior cost basis on the retained shares is discarded, and the delta cash assumes a full notional trade at the *re-affirmation* price rather than actual execution prices. This corrupts the only cash/equity numbers `state.db` ever stores.

**P0: reactor_name compat shim is nondeterministic (section 3).**  
Keying legacy absolute_target treatment on `reactor_name ∈ {deterministic-equity, paper}` means any rename, fork, test reactor, or cross-environment log with the same string silently changes fold semantics on replay. The shim lives "forever" and is the only thing that makes historical records produce 33.33 instead of 399.93.

**P1: Discriminator has an unmitigated silent-regression hole (Consequences + acceptance gate).**  
Records without `quantity_semantics` default to incremental_delta. A new or third-party reactor emitting absolute targets without the flag will inflate positions exactly as before; the gate test only runs at acceptance and does not protect future emitters.

**P1: Realized-P&L / settlement desync risk (section 2).**  
Absolute_target can reduce quantity (implicit close) yet explicitly forbids any realize logic. The settlement journal (ADR-0010) still sees the execution; the projection now shows a different quantity trajectory. No invariant ensures the two match after an absolute_target overwrite.

**P2: Chosen option A is not clearly superior to B on paper track only.**  
B's "state coupling" objection is weaker for deterministic reactors that already re-emit full targets every tick; they could compute delta locally from the same log they read. A permanently adds a shim + flag invariant while still requiring the reconcile heal step.

**P2: No corruption check on firing path stated.**  
Although PR #85 used the other reconstructor, nothing in the ADR prevents a future caller of `reconstruct_from` (status, retro) from feeding an absolute_target-updated `state.db` back into a sizing decision.