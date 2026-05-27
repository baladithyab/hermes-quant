# ADR-0052 — Promotion Orchestrator and Cron

| Field        | Value                                      |
|--------------|--------------------------------------------|
| Status       | **Accepted**                               |
| Date         | 2025-05-27                                 |
| Deciders     | hermes-quant team                          |
| Closes       | Operational-readiness gap: "PromotionGate exists but nothing calls it" |
| Supersedes   | —                                          |
| Related ADRs | ADR-0048 (HypothesisRunner), ADR-0050 (AlphaZoo) |

---

## Context and Problem Statement

`PromotionGate` (introduced in Wave 6) is a well-designed decision-support
class with a `.check(STOCKBENCHResult) -> PromotionDecision` method.
However, as noted in the MoA review (item I5 — "Brief script unchanged"):

> *"Operational scripts do not exist in diff.  PromotionGate exists but
>  nothing calls it in production."*

The net effect is that the promotion pathway is entirely manual:

1. Operator runs `python scripts/stockbench-smoke.py --window …` and gets a
   JSON blob.
2. Operator reads the JSON manually and decides whether it passes the gate
   criteria.
3. Operator maybe deploys — no record of the decision.

This leaves:
- No audit trail for promotion decisions.
- No ergonomic CLI for operators.
- No scheduled / cron-driven re-evaluation.
- No link between a `PromotionDecision` and the hypothesis that motivated the
  strategy.

---

## Decision

Introduce three new components:

1. **`hermes_quant.eval.promotion_orchestrator`** — library layer that:
   - Defines `PromotionRecord` (Pydantic v2, append-only JSONL storage).
   - Defines `PromotionLog` (JSONL wrapper with `AppendOnlyViolation` enforcement).
   - Defines `PromotionOrchestrator` (`harness.run()` → `gate.check()` → `PromotionRecord`).

2. **`scripts/promotion-decision.py`** — one-shot operator CLI:
   - Accepts `--strategy`, `--universe`, `--window`, `--hypothesis-id`, `--auto-record`.
   - Prints a human-readable summary table + full JSON.
   - Exit code 0 = promote, 1 = no-promote, 2 = error.

3. **`scripts/promotion-cron.py`** — scheduler-friendly batch runner:
   - Reads a YAML config listing evaluations.
   - Exit code 0 = no promotions; 2 = at least one new promotion (operator review).

---

## Operator Workflow (After This ADR)

```
┌─────────────────────────────────────────────────────────────────────┐
│  ONE-SHOT (operator runs manually)                                  │
│                                                                     │
│  python scripts/promotion-decision.py \                             │
│      --strategy buyhold \                                           │
│      --universe AAPL,MSFT \                                         │
│      --window 2025-06-01:2025-08-31 \                               │
│      --hypothesis-id hyp_AAPL_20250601_abc123 \                     │
│      --auto-record                                                  │
│                                                                     │
│  → Prints summary table + PromotionRecord JSON                      │
│  → Appends to promotion_decisions.jsonl                             │
│  → Exit 0 (promote) or 1 (no-promote)                               │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  SCHEDULED (weekly cron / CI)                                       │
│                                                                     │
│  python scripts/promotion-cron.py \                                 │
│      --config ~/.hermes/quant/promotion-cron.yaml                   │
│                                                                     │
│  → For each entry in config: runs orchestrator, appends JSONL       │
│  → Exit 0 (no promotions) or 2 (promotions fired → review needed)   │
└─────────────────────────────────────────────────────────────────────┘
```

### Deliberate Division: Orchestrator vs. HypothesisRegistry

The `PromotionOrchestrator` does **NOT** call
`HypothesisRegistry.update_status()`.  This is intentional:

- `PromotionDecision.promote = True` means the strategy cleared the gate
  *for the evaluated window*.  It is evidence, not a verdict.
- An operator must review the `PromotionRecord` and decide whether to
  formally transition the hypothesis to `'validated'` (or `'falsified'`).
- Automated status transitions were considered and rejected because:
  - A single passing window is insufficient evidence for `'validated'`.
  - The gate criteria are conservative (alpha > 0, Sortino > 0.5,
    drawdown > -20 %) but operators may have additional non-quantitative
    concerns.
  - Irreversible status transitions in an append-only system require a
    human checkpoint.

**Operator action after a positive PromotionDecision:**

```python
from hermes_quant.research.hypothesis import HypothesisRegistry

registry = HypothesisRegistry()
# After reviewing the PromotionRecord:
registry.update_status("hyp_AAPL_20250601_abc123", "validated")
```

---

## PromotionRecord JSONL Schema (schema_version = 1)

Each line in `~/.hermes/quant/research/promotion_decisions.jsonl` is a JSON
object with the following envelope + payload fields:

| Field                       | Type            | Notes                                      |
|-----------------------------|-----------------|--------------------------------------------|
| `schema_version`            | `int`           | Always `1`                                 |
| `kind`                      | `str`           | Always `"promotion_record"`                |
| `record_id`                 | `str`           | `prom_<8-hex-chars>`, unique               |
| `hypothesis_id`             | `str \| null`  | Reference to `HypothesisRegistry`          |
| `strategy_name`             | `str`           | Human-readable strategy label              |
| `window_start`              | `ISO date str`  | First date of evaluation window            |
| `window_end`                | `ISO date str`  | Last date of evaluation window             |
| `stockbench_result_summary` | `dict`          | ≤ 20 keys; no deep `metadata` sub-fields  |
| `decision`                  | `dict`          | `{promote, reasons, suggested_action}`     |
| `recorded_at`               | `ISO-8601 UTC`  | Timestamp of record creation               |
| `recorded_by`               | `str`           | `"system"`, `"cron"`, or operator identity |
| `schema_version`            | `int`           | `1`                                        |

### Append-only enforcement

`PromotionLog.truncate()` and `PromotionLog.update()` raise
`AppendOnlyViolation` (same pattern as `run_cards.jsonl` and
`decisions.jsonl`).  The file must never be modified in place; only
append-writes are permitted.

---

## Promotion Cron Config Schema

File: `~/.hermes/quant/promotion-cron.yaml`

```yaml
evaluations:
  - strategy: buyhold
    universe: [AAPL, MSFT, NVDA, GOOG, META]
    window_start: "2025-06-01"
    window_end:   "2025-08-31"
    hypothesis_id: null       # optional HypothesisRegistry reference
    auto_record: true
    recorded_by: cron

  - strategy: buyhold
    universe: [TSLA, AMZN]
    window_start: "2025-09-01"
    window_end:   "2025-11-30"
    auto_record: true
```

Required keys per entry: `strategy`, `window_start`, `window_end`.
Optional: `universe` (default: 5-stock standard set), `hypothesis_id`,
`auto_record` (default: true), `recorded_by` (default: "cron").

---

## Consequences

### Positive

- Closes the operational-readiness gap identified by MoA review I5/C2.
- Promotion decisions are now auditable (append-only JSONL).
- Operators get a single CLI command instead of manual JSON inspection.
- Scheduled re-evaluation is possible via `promotion-cron.yaml`.
- `PromotionRecord.hypothesis_id` creates a traceable link between backtest
  evidence and the registered hypothesis.

### Negative / Trade-offs

- The cron script requires PyYAML; falls back to JSON for environments without it.
- `PromotionOrchestrator` uses the synthetic price source by default (same as
  `STOCKBENCHHarness` default) — operators with real price data must wire in a
  custom `PriceSourceProtocol` adapter (future extension).
- Status transitions remain manual — see "Deliberate Division" above.

### Neutral

- No changes to `PromotionGate`, `STOCKBENCHHarness`, `HypothesisRegistry`,
  or any Wave 6-8 components.
- All new files are additive; no existing tests are broken.

---

## Implementation Files

| Path | Purpose |
|------|---------|
| `hermes_quant/eval/promotion_orchestrator.py` | `PromotionRecord`, `PromotionLog`, `PromotionOrchestrator` |
| `scripts/promotion-decision.py` | One-shot operator CLI |
| `scripts/promotion-cron.py` | Scheduler-friendly batch runner |
| `tests/eval/test_promotion_orchestrator.py` | ≥ 12 unit tests |
| `docs/adr/ADR-0052-promotion-orchestrator-and-cron.md` | This document |
