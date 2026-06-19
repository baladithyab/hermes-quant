# openai/gpt-5.5 (served: openai/gpt-5.5-20260423)

## P0 blocking findings

### P0 — Absolute-target branch corrupts cost basis / unrealized P&L  
**Section: Decision Outcome §2 (`avg_entry_price` is taken from that fill)**

Setting `avg_entry_price = fill_price` on every absolute-target record is wrong.

Cases that break:

- Reaffirm unchanged position: old `100 @ $10`, new absolute target `100 @ $20`. Cash delta is zero, but avg entry becomes `$20`, erasing a real unrealized gain.
- Partial reduction: old `100 @ $10`, target `50 @ $20`. Remaining 50 shares should keep avg `$10`; ADR sets `$20`.
- Add: old `100 @ $10`, target `150 @ $20`. Avg should be weighted, not overwritten.
- Flip: old long to short needs close/open split semantics for cost basis.

The ADR correctly identifies quantity idempotence but replaces additive quantity math with invalid cost-basis math. Acceptance tests only check quantity/cash, not avg-entry invariants. This will poison EOD/retro P&L even if quantity is fixed.

### P0 — “Execution” log semantics are internally contradictory  
**Sections: Context; Decision Outcome §2; Relationship to ADR-0085/ADR-0010**

The ADR says `executions.jsonl` is authoritative, but then reinterprets repeated “fill” records as non-trading target snapshots where unchanged reaffirmations move no cash. That is not an execution log anymore.

Worse: realized P&L is “owned by settlement journal” and computed directly from `executions.jsonl`. Unless the settlement journal is changed to understand `quantity_semantics`, it will still treat the 12 records as 12 fills while `state.db` treats them as one target. You will have two authoritative accounting views: cash/positions healed in `state.db`, realized P&L still inflated in FIFO.

This ADR cannot declare realized P&L out of scope while changing the economic meaning of historical execution records.

## P1 findings

### P1 — Cash-delta math is only conditionally correct  
**Section: Decision Outcome §2 (`Δcash = −(new_shares − old_shares) × fill_price`)**

The formula is correct only if an absolute-target record represents an actual rebalance trade from old quantity to new quantity at that fill price. But the ADR also says reaffirmations are emitted even when no trade occurred.

If quantity changes because NAV or price changed while the reactor is merely restating a 5% target, the formula fabricates a trade. If the producer emits target snapshots every tick, projection cannot know whether an order actually executed. The ADR needs a hard distinction between “target snapshot” and “executed rebalance delta.” Current design conflates them.

### P1 — `reactor_name` backfill shim is unsafe and overbroad  
**Section: Decision Outcome §3**

Keying legacy semantics on `reactor_name ∈ {deterministic-equity, paper}` is not robust.

Breaks if:

- `reactor_name` is missing, renamed, case-varied, namespaced, or versioned.
- `paper` includes multiple strategies, including incremental paper fills or multi-leg/options.
- the same reactor historically emitted both absolute targets and deltas.
- future code changes the compat list and historical replay changes meaning.

This is deterministic only relative to a frozen code mapping, not intrinsically from the log. The ADR asserts “recorded semantics” but legacy replay still guesses from an unstable producer label.

### P1 — Silent-regression hole is acknowledged but not actually closed  
**Sections: Consequences; Acceptance gate**

Defaulting missing `quantity_semantics` to `"incremental_delta"` means a future absolute-target reactor silently reintroduces the exact bug. A unit test that “every reactor” sets the flag is brittle and likely incomplete for plugins, config-created reactors, or renamed reactors.

Safer invariant: if a record has `target_position_pct` plus `reactor_metadata.quantity`, require explicit `quantity_semantics`, except for a narrowly versioned legacy shim. Unknown semantics should fail closed, not silently fold additively.

### P1 — Mixed semantics on one symbol are under-specified  
**Section: Decision Outcome §2 (`Mixed semantics ... interleave correctly`)**

Sequential quantity folding may be fine, but avg-entry/cash/realized-P&L are not specified for interleavings. Example: absolute target sets 100, broker partial incremental sell closes 20, next absolute target sets 100. Is that a rebuy of 20, a target correction, or overwriting broker reality? The ADR asserts correctness without defining precedence between target snapshots and actual fills.

## P2 findings

### P2 — “No firing/cap path affected” is too strong  
**Sections: Blast radius; Acceptance gate**

The ADR depends on PR #85 permanently keeping firing seeded from `reconstruct_portfolio_state`. But `state.db` remains a shared projection. Any future consumer regression, CLI-assisted firing, or cap code path reading `state.db` reopens authority risk. Say “not currently affected,” not “NOT affected.”

### P2 — Option B is dismissed too quickly  
**Section: Considered Options B; Pros/Cons B**

Producer-side delta emission may be cleaner because it preserves `executions.jsonl` as actual fills and keeps settlement/FIFO semantics coherent. The ADR frames it as requiring a dangerous state back-reference, but the reactor already must know prior target/order state to avoid duplicate fills. A producer fix plus one-time legacy repair may be less risky than permanently teaching the projection that some executions are really target snapshots.

### P2 — Missing acceptance tests  
**Section: Acceptance gate**

Add tests for:

- unchanged reaffirmation must not change `avg_entry_price`;
- add/reduce/flip absolute target cost basis;
- settlement journal/FIFO parity with `quantity_semantics`;
- legacy `paper` multi-leg/incremental records not misclassified;
- missing flag with `target_position_pct` fails validation, not silent default.