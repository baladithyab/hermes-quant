# google/gemini-3.1-pro-preview (served: google/gemini-3.1-pro-preview-20260219)

This ADR is dangerously flawed. In your zeal to align producer output with downstream accounting, you are shattering the most critical invariant of event sourcing: producers must not depend on derived state. Option B is a catastrophic regression disguised as a clean accounting fix.

**[P0] Read-Modify-Write Race Condition Destroys the Authoritative Log (Decision §1)**
You require producers to compute `fill_size_pct = target - current` by reading `reconstruct_portfolio_state` at execution time. You have just injected a fatal read dependency into an append-only event stream. 
If an async partial fill is in-flight—or if two threads handle targets concurrently—the local projection read is stale. The reactor will read `current = 0`, compute `delta = 5%`, and emit a `5%` fill. A millisecond later, the second thread reads `current = 0` (projection hasn't folded the first write), and emits *another* `5%` delta. You traded projection-time position inflation for *write-time race-condition inflation*. Projections can be rebuilt; permanent log writes cannot. 

**[P0] Erasing Reality via Immutable Log Corruption / Realized P&L (Decision §3 & Cons)**
You claim ADR-0085 "authoritative log" integrity is preserved while literally rewriting `executions.jsonl` to alter `fill_size_pct`. This is an unforgivable anti-pattern. Altering historic transaction records alters reality. 
Furthermore, your `Δcash = −delta × fill_price` math combined with rewriting reaffirms to `delta = 0` **corrupts the settlement journal and realized P&L**. If a reaffirmation historically generated simulated slippage or per-trade commission fees in the paper reactor, forcing the delta to `0` mathematically zeroes out those cash impacts. You are silently falsifying the paper track record (ADR-0080).

**[P1] Contradiction with Broker Partial Fills (Context & Decision §1)**
The Context explicitly notes: *"a partial broker fill of +3% adds +0.03"*. A real broker defines the delta. Yet your Decision §1 mandates the reactor computes `target - current`. If the reactor is wrapping a real or simulated partial fill, doing math against a local state projection rather than accepting the fill's transacted size will miscalculate the delta whenever the state differs from the broker's truth. 

**[P1] Rejected-Option Regret: Option A is demonstrably safer (Considered Options)**
You unanimously rejected Option A because you botched its design. You blamed a `reactor_name`-keyed compat shim for non-determinism, and claimed the discriminator flag had a "silent-regression hole" if a missing flag defaulted to additive. Both are easily solved by simple schema versioning.
If you enforce a `schema_version: 2` strict schema for new events (which map to incremental deltas) and migrate `reconstruct_from` to map `version: 1` as absolute-target records, **you fix the problem without touching `executions.jsonl` and without polluting the producer with read-logic.**
Your panic over Option A corrupting cost basis was a failure of fold algebra, not of the architecture. Cost basis for a target reaffirmation simply requires carrying the previous cost basis forward; it does not mandate a `$200` overwrite if coded correctly.

**[P2] The Firing/Cap Path is NOT Immune (Context)**
You claim the fixing path ignores `fill_size_pct` and relies on `target_position_pct`. But by forcing the paper reactor to read `reconstruct_portfolio_state` dynamically, any lag or race condition in this projection will block execution checks. The network-free fold may be local, but the data it folds relies on the event loop. This heavily couples firing latency to log replication latency. 

**Verdict:** Reject. Revert to a projection-side translation layer (Option A) with strict schema versioning. Never write delta logic that forces a producer to read a state projection, mitigating bugs by permanently editing log history.