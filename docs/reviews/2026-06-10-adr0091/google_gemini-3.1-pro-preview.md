# google/gemini-3.1-pro-preview (served: google/gemini-3.1-pro-preview-20260219)

This ADR attempts a localized projection fix but creates catastrophic accounting invariants and fundamentally misunderstands the purpose of an execution log. **Option A is mathematically toxic and semantically invalid.** You must revert to Option B.

### P0 (Blocking) - Cost Basis Math is Fatally Flawed
**Context:** Section "Decision Outcome" (Point 2) states: *“For `quantity_semantics == "absolute_target"`: the symbol's position is **set to** the record's absolute `quantity`... and `avg_entry_price` is **taken from that fill**.”*
**Flaw:** This blindly overwrites cost basis on incremental position scaling. If the reactor issues an initial absolute target of 10 shares at $100 (cb=$100), and later issues a new absolute target of 20 shares at $200, your logic sets the new cost basis to $200. The correct mathematically blended cost basis is $150. Overwriting `avg_entry_price` corrupts unrealized P&L for any position scaled into across multiple ticks. 

### P0 (Blocking) - Corrupts the Settlement Journal (Rejected Option Regret)
**Context:** Section "Decision Outcome" (Point 2) dismisses Realized P&L, claiming it is "owned by the settlement journal (ADR-0010), computed by the FIFO matcher directly on `executions.jsonl`."
**Flaw:** If `executions.jsonl` receives 12 identical re-affirmations of `quantity = 33.33` shares, how does the FIFO matcher handle them? A FIFO matcher operates on *executed trade deltas*. If it reads absolute targets, it will view them as 12 separate buys of 33.33 shares. If you intend for the settlement journal to also parse `quantity_semantics`, the ADR entirely fails to specify this invasive change to ADR-0010. 
**Verdict:** **Option B is the only correct path.** `executions.jsonl` is a ledger of transactions, not a time-series database of state targets. Emitting state snapshots into an execution log destroys the semantic integrity of the ledger. The reactor *must* emit deltas. The ADR’s excuse that Option B requires a "back-reference" is invalid; to submit an execution, a trading system inherently must know the delta to execute. 

### P1 - `reactor_name` Compat Shim is Fragile and Non-Deterministic
**Context:** Section "Decision Outcome" (Point 3) keys the backward-compatibility backfill shim on `reactor_name ∈ {deterministic-equity, paper}`.
**Flaw:** Replay determinism is broken if a reactor's naming convention ever changes, is versioned (e.g., `deterministic-equity-v2`), or was historically different. Tying fundamental database-schema fold semantics to arbitrary string identifiers of upstream services violates immutable event-sourcing principles. A log restamp (migrating the old rows to explicitly include the flag) is strictly superior and avoids permanent conditional rot in the daily projection pipeline.

### P2 - Mixed Semantics Interleaving Race Condition
**Context:** Section "Decision Outcome" (Point 2: Mixed semantics on one symbol interleave correctly).
**Flaw:** They do not interleave correctly under concurrency. If a genuine incremental fill (delta) from a broker arrives asynchronously with a delay, and an absolute-target snapshot (latest-supersedes) is recorded *just before* the delayed delta, the absolute-target will absorb the real world state, and the subsequent delta record will be applied *on top* of the absolute target—causing a double-count of the trade. 

**Recommendation:** Reject this ADR. Fix the producer (Option B). Do not use an execution log to store idempotent state snapshots. Do not accept a rewrite of cost basis that ignores math.