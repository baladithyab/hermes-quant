# openai/gpt-5.5 (served: openai/gpt-5.5-20260423)

## P0 blocking findings

1. **New producer will emit bogus “correction trades” until repair is complete**  
   **ADR section:** Decision Outcome §1; Consequences “state.db stays inflated until…”  
   The reactor computes `delta = target − current` from the current projection. But before the operator runs repair + reconcile, `current` is known inflated. BA current `−0.80`, target `−0.20` ⇒ producer emits `+0.60` buy, creating a fake transaction and corrupting cash/P&L further. This can also hit the firing/cap path. You need an explicit freeze/ordering gate: repair log, rebuild projection, then re-enable reactors. Current ADR allows unsafe mixed operation.

2. **Cash math is dimensionally wrong / unit-confused**  
   **ADR section:** Decision Outcome §2  
   `Δcash = −delta × fill_price` is only valid if `delta` is shares/contracts. But `fill_size_pct` is described as NAV fraction / target weight. For a pct delta, cash is `−delta_pct × NAV` or shares must be computed first. Deterministic-equity uses `reactor_metadata.quantity` as shares, while paper uses pct. The ADR claims one unchanged fold fixes “cash, realized P&L, state.db” without proving each consumer uses the correct unit. This is a correctness blocker.

3. **Repair algorithm cannot safely infer semantics from history**  
   **ADR section:** Decision Outcome §3; Acceptance gate repair tests  
   “Recompute each record’s correct delta from running position in timestamp order per `(account, asset)`” assumes every record’s `target_position_pct` is a reliable absolute post-fill target and every old fill was target-semantics. That is not established. It will corrupt genuine incremental fills, partial fills, manual records, mixed reactors, multi-leg/option records, or records with missing/stale target fields. If the script scopes by `reactor_name`, it reintroduces the rejected fragile shim; if it does not, it is overbroad.

4. **In-place authoritative-log rewrite breaks settlement/journal integrity**  
   **ADR section:** Decision Outcome §3–4; Consequences negative  
   Backing up `executions.jsonl` is not the same as preserving an auditable ledger. If ADR-0010 settlement journal already posted realized P&L/cash entries from the corrupt fills, rewriting executions without explicit reversal/rebuild of the settlement journal creates two histories. The ADR says “settlement journal, replayed…” but does not specify deleting/reversing prior journal rows, idempotency keys, or settlement-date consequences.

5. **Repair ordering is nondeterministic**  
   **ADR section:** Decision Outcome §3  
   “Timestamp order per `(account, asset)`” is insufficient. Equal timestamps, append order, clock skew, delayed writes, and cross-reactor same-symbol records produce different deltas. The authoritative order should be log sequence number / file offset, possibly partitioned by book. Without a deterministic total order, the repaired log is not reproducible.

## P1 findings

6. **Partial fills are mishandled**  
   **ADR section:** Context “partial broker fill”; Decision Outcome §1  
   A fill record should contain the actually executed size. `target − current` is desired remaining rebalance, not necessarily executed fill. If a broker/paper simulator partially fills, the ADR will overstate the fill. The correct source is the execution report/fill quantity, not target math.

7. **Concurrency/stale-read race remains**  
   **ADR section:** Decision Outcome §1; Consequences negative  
   Two proposals for the same symbol can read the same current position and both emit the same delta. Or a write may not yet be visible to `reconstruct_portfolio_state`. Re-affirmation idempotence only holds under serialized read→append semantics. The ADR needs locking, per-symbol sequencing, or compare-and-append validation.

8. **Book/account partitioning is underspecified**  
   **ADR section:** Decision Outcome §1 and §3  
   Producers read `reconstruct_portfolio_state(reactor_filter=<book>)`, but repair runs per `(account, asset)`. Those are different partitions. If paper and deterministic-equity trade the same asset, repair and live emission compute against different positions.

9. **The rejected `reactor_name` shim critique still applies to the migration**  
   **ADR section:** Pros/Cons Option A; Decision Outcome §3  
   The ADR says “no reactor_name compat shim,” but deterministic-equity `reactor_metadata.quantity` must be repaired while ADR-0029 multi-leg quantity must not. How is that distinguished? Asset class? reactor name? metadata shape? Any heuristic can be nondeterministic under rename/version/fork.

10. **Silent-regression hole still exists**  
   **ADR section:** Decision Outcome §5; Acceptance gate  
   Tests for current reactors are not a contract. A future reactor can still append absolute targets to `fill_size_pct` and pass unless explicitly covered. Without schema-level validation at append time — e.g. reject if `fill_size_pct != target_position_pct − prior_position` for target-style reactors, or require declared semantics — the system can silently regress.

11. **MTM/current-weight ambiguity is unresolved**  
   **ADR section:** Decision Outcome §1; ADR-0086 relationship  
   If `current` is last target weight, unchanged target emits zero. If `current` is mark-to-market weight, price drift can require a rebalance delta even when the advisor target is unchanged. The ADR does not specify which “current” is used. This matters for share migration and deterministic-equity sizing.

## P2 findings

12. **Zero-delta fills may poison downstream assumptions**  
   **ADR section:** Decision Outcome §1; Consequences positive  
   Settlement FIFO, cost-basis code, stats, slippage, fill counts, and win-rate analytics may not expect executable fills with zero size. The ADR asserts they are useful audit records but does not require consumers to ignore zero-quantity lots.

13. **Chosen option is better than Option A only if repair is safe — currently it is not**  
   **ADR section:** Considered Options; Pros/Cons B  
   Producer-side delta emission is directionally right. The regretted alternative is not Option A as written; it is an append-only correction-event model or explicit mixed-era semantics. This ADR chooses destructive rewrite plus inference, which may be worse for auditability and for records whose original semantics cannot be proven.