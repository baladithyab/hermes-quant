# ADR-0049: Shadow Account Counterfactual Backtest

**Status:** Accepted  
**Wave:** 8b  
**Date:** 2026-05-27  
**Author:** Hermes subagent (Wave 8b)  
**Supersedes:** None  
**Related:** ADR-0031 (governance audit log), ADR-0041 (signal provenance), ADR-0045 (cost model), ADR-0048 (hypothesis registry)

---

## Context

Every production trading decision is reviewed in committee and logged to `audit_log.jsonl`.  
The *post-hoc rationalization* problem: after a decision, it is psychologically easy to find reasons why it was correct regardless of outcome. We need a mechanism that *forces* the system to measure what would have happened under alternative rules.

The HKUDS Vibe-Trading pattern ("shadow account counterfactual backtest") addresses this directly: maintain N shadow portfolios alongside the live portfolio, each following a different decision rule, and compare their P&L to reality after each session.

---

## Decision

Ship **Shadow Account counterfactual backtest** (Wave 8b):

1. **5 concrete shadow rules** covering the most common counterfactual hypotheses practitioners ask.
2. **Isolated SQLite database per rule** — no shared state, no cross-rule contamination.
3. **ShadowAccountRunner** — orchestrates all rules in lockstep with the production audit log.
4. **`scripts/shadow-replay-daily.py`** — operator harness for on-demand replay over any date range.
5. **Read-only relationship to `audit_log.jsonl`** — shadow accounts NEVER write to the production log.

---

## The 5 Shadow Rules

### Rule 1: `AlwaysFollowAdvisorRule`
**Hypothesis:** Would mechanical rule-following at fixed 10% equity beat the production variable-sizing allocation?

- Fires on every `gate_approval` event.
- Action = advisor direction; size = 10%.

### Rule 2: `InverseConsensusRule`
**Hypothesis:** Is the committee systematically wrong? The contrarian counterfactual.

- Fires on every `gate_approval` event.
- Action = *opposite* of advisor direction; size = 10%.
- If this rule consistently wins, it is a strong signal of systematic committee bias.

### Rule 3: `SemanticOnlyRule`
**Hypothesis:** The semantic analyst (news + fundamentals) adds alpha. Filtering to events where semantic analyst voted should improve decision quality.

- Fires only when `contributing_analysts` includes `"semantic"` (or `"semantic_analyst"`).
- Action = advisor direction; size = 10%.

### Rule 4: `SentimentOnlyRule`
**Hypothesis:** Pure sentiment signal (social media, news tone) carries independent predictive power.

- Fires only when `contributing_analysts` includes `"sentiment"` (or `"sentiment_analyst"`).
- Action = sentiment analyst's own direction vote when available; falls back to advisor direction.
- Size = 10%.

### Rule 5: `TrendFollowingRule`
**Hypothesis:** High-conviction confluent signals (technical + committee majority) should produce better outcomes than low-conviction approvals.

- Fires when: `classical_ta_direction` == `advisor_direction` AND `vote_share > 0.6`.
- Action = advisor direction; size = 15% (reward the high-conviction case).

---

## Cost Model Contract

Every shadow fill applies a **deliberately conservative** cost:

```
fill_price = market_price × (1 + sign × 0.0005)   # 5 bps directional slippage
           × (1 + sign × cost_model_bps / 10000)   # default 10 bps explicit cost
```

Default `cost_model_bps = 10.0` (one-way).  Total one-way drag ≈ **15 bps** (5 slippage + 10 model).

**Contract:**  
- A shadow rule must beat the real portfolio *despite* this cost drag.
- No shadow account gets a "free pass" by ignoring transaction costs.
- The cost is intentionally conservative: if a rule still wins, the alpha signal is robust.
- `cost_model_bps` is a constructor parameter so operators can tune the comparison.

This is consistent with the production CostModel in `hermes_quant/backtest/cost_model.py` (ADR-0045), which uses 5 bps half-spread + sqrt-impact. Shadow accounts use a flat-bps model for simplicity.

---

## Isolated-DB-Per-Rule Pattern

Each `ShadowAccount` owns exactly one SQLite file:

```
~/.hermes/quant/shadow/
    always_follow_advisor.db
    inverse_consensus.db
    semantic_only.db
    sentiment_only.db
    trend_following.db
```

**Why isolated?**

1. **No cross-rule contamination.** A bug in one account's state cannot corrupt another.
2. **Independent reset.** An operator can delete a single `.db` to force a fresh replay for one rule without disturbing others.
3. **Parallel replay safety.** Multiple replay processes can operate on different rules simultaneously.
4. **Auditability.** Each DB is a complete record of what that rule would have done. The schema mirrors `PortfolioState` (ADR-0041 wave 1c): positions, cash, fill history, P&L history.

**Schema** (per DB):
- `shadow_positions` — open positions (ticker, quantity, avg_entry_price)
- `shadow_cash` — single-row cash balance
- `shadow_pnl_history` — end-of-day mark-to-market snapshots
- `shadow_fills` — every simulated fill with idempotency guard on `event_id`

---

## Read-Only Relationship to Production Audit Log

**Shadow accounts MUST NOT write to `audit_log.jsonl`.**

Rationale:
- `audit_log.jsonl` is the canonical record of *what the production system decided*. Contaminating it with counterfactual simulation events would make the governance trail uninterpretable.
- The audit log's append-only guarantee (ADR-0031) means any accidental write would be permanent.

Implementation enforcement:
- `ShadowAccountRunner.replay_session()` reads events as plain dicts; it never imports `audit_log.append`.
- `ShadowAccount.apply_signal()` writes only to its own `shadow_<rule_name>.db`.
- Tests assert that applying shadow signals does not touch `AUDIT_LOG_PATH`.

---

## The Post-Hoc Rationalization Defense

The core value of shadow accounts is converting belief into measurement:

> "We believed `TrendFollowingRule` would outperform production because high-conviction confluent signals should be more reliable. After 90 days of shadow tracking, `TrendFollowingRule` is +2.1% vs real. The belief is now **evidence**."

vs.

> "We believed `InverseConsensusRule` would reveal systematic committee bias. After 90 days, `InverseConsensusRule` is -3.4% vs real. The contrarian hypothesis is **falsified** for this period."

Without shadow accounts, both beliefs survive indefinitely because they are never put at risk. With shadow accounts, **every committee decision is simultaneously a bet** on the production rule, the contrarian rule, the semantic-only rule, and so on. The post-session report shows which bets paid off.

This creates an accountability loop:
1. Before the decision: "We believe rule X works."
2. After the session: Report shows rule X +/- vs real.
3. After 30 days: Statistical evidence accumulates.
4. The `ShadowComparisonReport.counterfactual_winners` list is actionable — if `SemanticOnlyRule` consistently wins, consider tightening the production gate to require semantic analyst presence.

---

## `ShadowComparisonReport` — the Evidence Artifact

```python
@dataclass
class ShadowComparisonReport:
    real_pnl:                 float          # actual production P&L
    shadow_pnls:              dict[str, float] # rule_name → shadow P&L
    counterfactual_winners:   list[str]      # rules that beat real
    counterfactual_losers:    list[str]      # rules that lost to real
    biggest_alpha:            tuple[str, float]  # (rule_name, alpha)
    evidence_summary:         str            # max 2048 chars
    asof:                     date
```

The `evidence_summary` is designed to be embedded directly in Slack notifications, operator dashboards, or audit digests without truncation.

---

## Alternatives Considered

### Alt 1: Store shadow results in the production audit log
Rejected. The audit log is the canonical production governance record; shadow events would contaminate it and make it unusable for production audits.

### Alt 2: Shared SQLite for all rules
Rejected. A shared DB creates a single point of contention and makes it impossible to reset or inspect a single rule in isolation.

### Alt 3: In-memory only (no persistence)
Rejected. Without persistence, the runner must replay the entire audit log from the beginning every time it is invoked. At production scale (thousands of events) this becomes expensive. The watermark pattern in `ShadowAccount` allows incremental updates.

### Alt 4: Use the production `PortfolioState` class for each shadow rule
Rejected. `PortfolioState` is coupled to the production account model (account_id, executions.jsonl, state.db). Shadow accounts need a simpler, self-contained model with no coupling to production state files.

---

## Consequences

**Positive:**
- Every production decision is now in a controlled experiment alongside 5 alternate rules.
- The post-hoc rationalization problem is measurably addressed.
- Operators gain evidence to improve the production rule over time.
- Shadow accounts are completely isolated — zero risk of production side effects.

**Negative / risks:**
- 5 SQLite files added to `~/.hermes/quant/shadow/`. Low overhead.
- The cost model for shadow accounts (flat bps) differs from the production CostModel (sqrt-impact). This is a deliberate simplification; the gap could make high-volume shadow rules appear slightly more or less profitable than they would be with the production cost model. Future work: unify the cost models (Wave 8c+).
- Shadow P&L is computed in "fractional shares" (size_fraction × equity / price), not integer lots. This is acceptable for counterfactual analysis but would need adjustment before using shadow results to inform actual order sizing.

---

## Implementation Notes

- `ShadowRule.evaluate()` returns `None` when the rule doesn't apply. The runner silently skips `None` returns.
- `apply_signal()` is idempotent on `event_id`. Re-running the daily script on the same date range is safe.
- `ShadowAccountRunner.__init__()` defaults to `default_rules()` — the canonical 5-rule set.
- The `scripts/shadow-replay-daily.py` script supports `--no-persist` for CI/testing.
- See `tests/shadow/` for the full acceptance test suite (≥25 tests).
