# ADR-0048: Hypothesis Registry + Run Card Artifacts (Research Autopilot)

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** ARIA (Wave 8a subagent, hermes-quant research-autopilot)  
**Related:** ADR-0031 (Audit Log), ADR-0042 (Decision + Reflection Log), ADR-0045 (Walk-Forward Backtester)

---

## Context

hermes-quant has, as of Wave 7, a walk-forward backtester, a contamination guard, a 3-way risk committee, and a persistent memory layer. What it still lacks is a **pre-commitment mechanism** for research: a way to declare *before running a backtest* what would constitute success or failure.

Without such a mechanism, the post-hoc rationalisation failure mode is silently present:

> **Post-hoc rationalisation**: A researcher runs a strategy, observes the results, and *then* constructs criteria that happen to be satisfied by those results. Losses are attributed to "bad conditions" and excluded; wins are showcased. The strategy looks better than it is. The system has no record of what was predicted versus what was observed.

This failure mode is documented in the HKUDS/Vibe-Trading Hypothesis Registry pattern, and is analogous to publication bias / p-hacking in academic research.

The pattern is also related to the **Oracle Fallacy** (ADR-0042): just as a retrieved memory must not contain outcome-knowledge that was unavailable at decision time, a strategy evaluation must not be constructed using outcome-knowledge that was unavailable at hypothesis-formation time.

### Why Now (Wave 8a)

Waves 6–7 gave us the infrastructure to make this pattern implementable:
- Walk-forward backtester with contamination guard (ADR-0045) — so runs are trustworthy.
- Append-only event stores (ADR-0031, ADR-0042) — so the audit trail is tamper-proof.
- StubLLMCommittee (Wave 6a) — so dry-run validation is zero-cost.

---

## Decision

### D1: Hypothesis Registry (append-only JSONL)

**Path:** `~/.hermes/quant/research/hypotheses.jsonl`

Every strategy variant or alpha factor experiment must be **pre-registered as a hypothesis** before any backtest is run. The hypothesis declares:

- A **falsifiable claim** (max 512 chars) — what we expect to be true.
- A **null hypothesis** — what we expect to be false.
- **Success criteria** (max 5, each max 256 chars) — expression strings like `"sharpe >= 0.5"` that must ALL pass for the hypothesis to be validated.
- **Falsification criteria** (max 5) — expression strings; if ANY fires, the hypothesis is falsified regardless of success criteria.
- **Experiment design** — walk-forward window, universe, method.
- **Scope** — universe, time window, env vars, etc. (max 10 keys).
- **Duration target** — how long the experiment should run.

**Two row kinds on the JSONL:**

```
kind="hypothesis"      — the initial registration (never mutated)
kind="status_change"   — a lifecycle event: open→running, running→validated, etc.
```

The "current status" of a hypothesis is materialized by replaying the event chain, exactly as in the audit log (ADR-0031) and decision log (ADR-0042).

**Status lifecycle:**

```
      open
       │
       ├──→ running
       │       │
       │       ├──→ validated   (terminal)
       │       ├──→ falsified   (terminal)
       │       └──→ abandoned   (terminal)
       │
       └──→ abandoned           (terminal, can skip running)
```

`open → validated` and `open → falsified` are **invalid transitions**. A hypothesis must be started (→ running) before it can be resolved. This prevents accidentally marking a hypothesis as validated without a recorded run.

### D2: Run Card Log (append-only JSONL)

**Path:** `~/.hermes/quant/research/run_cards.jsonl`

After every strategy run, a **RunCard** is emitted. The RunCard is the evidence artifact:

```json
{
  "schema_version": 1,
  "kind": "run_card",
  "run_id": "run_hyp_AAPL_20250528_a1b2c3_20250528T1430",
  "hypothesis_id": "hyp_AAPL_20250528_a1b2c3",
  "started_at": "2025-05-28T14:00:00+00:00",
  "ended_at": "2025-05-28T14:30:00+00:00",
  "strategy_name": "SentimentMomentum",
  "strategy_config_hash": "<sha256 of serialised config>",
  "universe": ["AAPL", "MSFT"],
  "window_start": "2025-01-01",
  "window_end": "2025-03-31",
  "contamination_guard_fired": false,
  "metrics": {
    "sharpe": 0.82,
    "sortino": 1.10,
    "max_drawdown": -0.08,
    "vs_buyhold_alpha": 0.05,
    "n_decisions": 42.0,
    "total_return": 0.12
  },
  "artifacts": {
    "backtest_log": "~/.hermes/quant/backtest/walk_forward_20250528.jsonl"
  },
  "verdict": "validated",
  "verdict_reasons": [
    "[PASSED] sharpe >= 0.5 → True",
    "[not fired] sharpe < 0.0 → False"
  ]
}
```

The **RunCard is append-only** — same enforcement as `decisions.jsonl`.

### D3: HypothesisRunner Orchestrator

`HypothesisRunner` is the orchestrator that enforces the lifecycle:

1. Read the registered hypothesis (raises `HypothesisNotFound` if absent).
2. Transition `open → running` (raises `InvalidStatusTransition` if not open).
3. Execute the strategy callable.
4. **Auto-evaluate** success/falsification criteria against the returned metrics.
5. Write a RunCard with the verdict.
6. Transition hypothesis to `validated` or `falsified` (or leave `running` if inconclusive).
7. Return the RunCard.

### D4: Auto-evaluation Safety Constraint

**Criteria expressions** are simple Python comparison expressions evaluated against the metrics dict:

```python
eval(criterion, {"__builtins__": {}}, metrics_dict)
```

`__builtins__` is explicitly removed so:
- No `import` statements.
- No `open()`, `exec()`, `getattr()`, `__class__`, etc.
- Only names present in the metrics dict are in scope.

**This is documented as a v0.1 limitation.** Expressions like `__import__('os').getcwd()` raise `NameError` because `__import__` is not in scope. However, a determined adversary could craft an expression that exploits Python's object model through chained attribute access on numeric types.

**v0.2+ plan:** Replace `eval()` with an AST-based evaluator that parses only `Compare` and `BoolOp` nodes, or use `RestrictedPython`. The current approach is adequate for trusted-operator usage (human-written criteria).

### D5: Contamination Guard Integration

If `WalkForwardEngine` raises `LookaheadViolation` during a run, `contamination_guard_fired=True` is set on the RunCard. The verdict is forced to `"falsified"` because a contaminated run produces meaningless metrics. This prevents a contaminated run from being cherry-picked as validated.

---

## Consequences

### Positive

1. **Post-hoc rationalisation is structurally impossible.** The success/falsification criteria are written to the append-only registry *before* the run. There is no mechanism to retroactively modify them.
2. **Every strategy promotion is auditable.** The RunCard proves which hypothesis was tested, which metrics were observed, and which criteria fired.
3. **Reproducibility.** The `strategy_config_hash` (SHA-256 of serialised config) lets operators re-run the exact same configuration and verify the metrics.
4. **Zero-cost dry-run.** `dry_run=True` (the default) routes through `StubLLMCommittee` — no API calls, no cost. CI tests can run the full lifecycle.
5. **Parallel with ADR-0042.** The append-only pattern, schema_version field, and status-transition-as-new-row discipline are consistent with the existing event stores. No new storage abstractions needed.

### Negative / Risks

1. **Criteria expression eval is v0.1 quality.** See §Safety above. Acceptable for trusted operators; needs hardening before multi-tenant deployment.
2. **`inconclusive` hypotheses accumulate in `running` state.** Operators must manually abandon or re-run them. A future cron could auto-abandon hypotheses older than `duration_target_days`.
3. **No cross-hypothesis deduplication.** Two operators can register identical claims. This is intentional — the registry is a log, not a normalised database.

---

## Alternatives Considered

### Alt A: Mutate status in-place
Simple but destroys the audit trail. Rejected — same reason as ADR-0031/0042.

### Alt B: YAML-based Run Cards
YAML is human-readable but introduces a dependency and has footguns (e.g. `yes` → `True` coercion). Rejected — stick to JSON/JSONL.

### Alt C: Use a relational database (SQLite)
More powerful querying, but defeats the portability and simplicity of the append-only JSONL pattern. Rejected for now; deferred to v0.3+ if query patterns demand it.

### Alt D: Full RestrictedPython from the start
Correct security posture but adds a dependency and requires more implementation time. Deferred to v0.2+; current `eval()` with `__builtins__={}` is adequate for single-operator paper-trading use case.

---

## Implementation Notes

| Module | Path |
|---|---|
| Hypothesis model + registry | `hermes_quant/research/hypothesis.py` |
| RunCard model + log | `hermes_quant/research/run_card.py` |
| HypothesisRunner orchestrator | `hermes_quant/research/orchestrator.py` |
| Public API | `hermes_quant/research/__init__.py` |
| Tests | `tests/research/test_hypothesis.py`, `test_run_card.py`, `test_orchestrator.py` |
| CLI harness | `scripts/research-autopilot.py` |
| Registry storage | `~/.hermes/quant/research/hypotheses.jsonl` |
| Run card storage | `~/.hermes/quant/research/run_cards.jsonl` |

`schema_version=1` on every JSONL row (both files). No YAML dependency introduced.
