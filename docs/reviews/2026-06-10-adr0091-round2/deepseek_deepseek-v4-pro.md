# deepseek/deepseek-v4-pro (served: deepseek/deepseek-v4-pro-20260423)

**Findings (P0–P2) on ADR-0091**

---

**P1 – Stale position read due to write-buffer flush not enforced**  
*Section: Decision Outcome → Consequences (negative point about fill-time position read)*  
The reactor’s delta computation requires the current position from `reconstruct_portfolio_state`. If a prior fill in the same execution cycle has been appended to the log file but not flushed/synced, the subsequent reconstruction may not see it, yielding a stale position. This would cause the reactor to compute a delta as if the prior fill never happened, leading to re‑trading (e.g., a second re‑affirmation trades the same delta again). The ADR’s mitigation (“reusing the exact `reconstruct_portfolio_state` call the cap already makes”) does not address this because the cap’s call is performed *before* any fills. The design must guarantee that the log is durable before each fill’s position read (e.g., `fsync` after every append). As written, the reactor can silently drift the portfolio in a multi‑fill cycle.

**P1 – Settlement journal not rebuilt after log repair**  
*Section: Acceptance gate (live heal only covers `state.db` & EOD snapshot)*  
The ADR correctly notes that the settlement journal reads `fill_size_pct` as deltas and would be corrupted before the repair. Replaying the settlement journal on the repaired log would produce correct realized P&L, but the ADR’s acceptance gate does not require rebuilding the settlement journal’s live state. Operators running the migration and reconcile may leave the settlement journal in its old, inflated state, causing divergent realized P&L between `state.db` cash and the journal until a manual rebuild. This is a documentation‑plus‑operational gap that can cause an incorrect accounting view.

**P2 – Incorrect cash‑delta formula in ADR text**  
*Section: Decision Outcome, point 2 (“Δcash = −delta × fill_price”)*  
`fill_size_pct` is a fraction of NAV, not a share count. The actual cash movement in the fold is `−fill_size_pct × NAV` (or equivalently `−delta_shares × fill_price` if shares are first computed). The simplified formula given is wrong when delta is in percentage terms. While the existing code is correct, this documentation error could mislead a future maintainer implementing a new consumer.

**P2 – Missing regression test for fold‑semantics guard**  
*Section: Acceptance gate / Regression guard*  
The ADR adds a test that reactors emit a delta (`fill_size_pct = target − current`). However, no test verifies that the fold (`_replay_record`) would *fail* (i.e., inflate the position) if it were ever changed back to interpret `fill_size_pct` as an absolute target. A future developer altering the fold’s semantics could silently re‑introduce the bug because the existing delta‑correct test data would not trigger the inflation (the test records already contain deltas). A guard test that replays a mix of absolute‑target records and asserts a detectable mismatch is missing.

**P2 – Ambiguity in repair‑script’s “running reconstructed position”**  
*Section: Decision Outcome → One‑time log repair*  
The repair script must compute deltas sequentially: for each record, delta = target − *correct* running position. The phrase “running reconstructed position in timestamp order” could be misinterpreted as using the original (inflated) fold on the original (wrong) fill sizes, which would produce incorrect corrected deltas. The ADR does not explicitly state that the script must maintain its own correct position accumulator using the corrected deltas as it goes. The acceptance test `test_repair_recomputes_deltas` likely catches this, but the specification’s wording leaves room for an implementer to make the wrong choice.

---

*No P0 issues found that would block the decision if the P1 items are addressed; the chosen option is still superior to the rejected ones given the fatal cost‑basis and settlement‑journal flaws in Option A.*