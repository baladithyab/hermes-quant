# ADR-0041: Signal Provenance & Audit-Trail Observability

> Note (2026-05-27): renumbered from ADR-0039 → ADR-0041 to avoid conflict with the upstream-merged ADR-0039 (Robinhood Agentic Trading MCP Reactor) on origin/main. ADR-0040 is also taken downstream (by docs/adr/ADR-0040-persistent-memory-reflection.md, which we renumbered to ADR-0042 in the same pass).

**Status:** Proposed
**Date:** 2026-05-27
**Supersedes:** Amendment to ADR-0031 (Governance Plane Consolidation)
**Author:** ARIA (deep-work-loop on hermes-quant; research synthesis from TauricResearch/TradingAgents, HKUDS/Vibe-Trading, virattt/ai-hedge-fund, Mai0313/TradingAgents)

## Context

The 2026-05-26 BMA-degenerate-collapse incident (24 EOD picks at conf=1.0 from a single analyst, halt epoch=2) was caught by an out-of-band MoA committee read of the brief, NOT by the audit-log. The post-incident fix in commit `8345f67` added `require_ensemble=True` (default) to `BMAAggregator.__init__`, silencing picks with `n_distinct_analysts < 2`.

The halt was lifted on 2026-05-27 with the stated lift conditions: (a) BMA aggregator patched + (b) `n_distinct_analysts >= 2` verified. Verification was done via an out-of-band `recommend()` re-probe.

A 2026-05-27 deep audit of `~/.hermes/quant/governance/audit_log.jsonl` (489 events, 182 approvals) found:

- **80% of approvals (145/182) carry `confidence=1.00`**
- **0 approvals carry `metadata.n_distinct_analysts`** — the field is `None` everywhere
- **0 approvals carry `metadata.n_views`** — the field is `None` everywhere
- **0 approvals carry `metadata.contributing_analysts`** — the field is `None` everywhere

Today's 4 paper fills (HOOD/CRC/RRC/MRNA on 2026-05-27) all show **`direction=-1, conf=1.00, target_position_pct=-0.20`** — the literal surface signature of the n=1 BMA collapse, with no audit-trail evidence to distinguish "real 2-analyst-agreement saturation" from "the BMA fix didn't load in the running venv."

**Root cause** (located at `hermes_quant/risk/gate.py:244-258`):

```python
def _audit_approval(self, signal: AggregatedSignal, action: Action) -> None:
    _emit_audit(
        kind="gate_approval",
        asof=_ts_to_datetime(signal.asof),
        payload={
            "asset": signal.asset,
            "direction": int(signal.direction),
            "magnitude": float(signal.magnitude),
            "confidence": float(signal.confidence),
            "target_position_pct": float(action.target_position_pct),
            "reason": action.reason,
            "asof": signal.asof.isoformat(),
        },
    )
```

The BMA metadata (`n_distinct_analysts`, `n_views`, `contributing_analysts`, `vote_share`, `bma_weights`) is computed in `BMAAggregator.aggregate()` (`hermes_quant/aggregators/bma.py:365`) and lives on the returned aggregated signal's `metadata` dict, but `_audit_approval` does not read or persist it.

This means every BMA-degeneracy debate is fought on out-of-band evidence (rerun `recommend()`, look at logs, hope the venv had the right code), not on the canonical audit trail. **The audit log is supposed to be the ground truth; it currently isn't.**

## Decision

Add a `signal_provenance` block to every `gate_approval` and `gate_rejection` audit-event payload. The block carries the discriminative metadata required to detect degeneracy retroactively from the audit trail alone.

Required `signal_provenance` fields (all required; default to `null` only when the underlying aggregator does not produce them):

| Field | Type | Source | Purpose |
|---|---|---|---|
| `n_views` | int | `len(signal.components)` | Total analyst-view count entering aggregation |
| `n_distinct_analysts` | int | `len({v.analyst for v in signal.components})` | Distinct analyst classes (BMA-degeneracy discriminator) |
| `contributing_analysts` | list[str] | `sorted({v.analyst for v in signal.components})` | Names of contributing analyst classes |
| `vote_share` | float \| null | `signal.metadata.get("vote_share")` | Fraction of views agreeing with the aggregated direction |
| `n_contributing` | int \| null | `signal.metadata.get("n_contributing")` | BMA's own internal "non-zero-weight" count |
| `bma_weights` | dict[str, float] \| null | `signal.metadata.get("bma_weights")` | Per-analyst posterior weights (sums to 1.0) |
| `aggregator_class` | str | `type(aggregator).__name__` | Which aggregator produced this signal |
| `analyst_view_ids` | list[str] | `[v.view_id for v in signal.components]` | Stable IDs for cross-referencing analyst-view records |
| `data_quality` | dict | `signal.data_quality` (already on signal) | Bars-received, freshness, etc. |

**Wire format** (extends payload, does not break existing readers):

```json
{
  "kind": "gate_approval",
  "schema_version": 2,
  "payload": {
    "asset": "MRNA",
    "direction": -1,
    "magnitude": 0.045,
    "confidence": 1.0,
    "target_position_pct": -0.2,
    "reason": "approve",
    "asof": "2026-05-27T04:00:00+00:00",
    "signal_provenance": {
      "n_views": 4,
      "n_distinct_analysts": 2,
      "contributing_analysts": ["ClassicalTA", "Kronos"],
      "vote_share": 1.0,
      "n_contributing": 2,
      "bma_weights": {"ClassicalTA": 0.6, "Kronos": 0.4},
      "aggregator_class": "BMAAggregator",
      "analyst_view_ids": ["view_a3f...", "view_b1c..."],
      "data_quality": {"bars_received": 252, "is_fresh": true}
    }
  }
}
```

## Bumps & migration

1. **`schema_version: 2`** on `audit_log.jsonl`. Existing v1 events stay readable; new code writes v2.
2. **`AuditLogSchemaMismatch`** is raised on v2-reader+v0 file or v1-reader+v2 file. Add a `migrate_v1_to_v2(...)` helper that wraps a v1 record in a v2 envelope with `signal_provenance: null`. **Do not** retro-fill `null` provenance with reconstructed values; the canonical record is what was written.
3. **CURRENT_SCHEMA_VERSION = 2** in `hermes_quant/governance/audit_log.py`.

## Degeneracy discriminator (canonical predicate)

Once `signal_provenance` lands, the BMA-degeneracy check becomes a single Python predicate on an audit-log row, runnable from the CLI with no out-of-band probes:

```python
def is_bma_degenerate(event: dict) -> bool:
    """Return True iff this event is the n=1 BMA-collapse signature."""
    if event.get("kind") != "gate_approval":
        return False
    sp = event.get("payload", {}).get("signal_provenance") or {}
    return (
        sp.get("aggregator_class") == "BMAAggregator"
        and sp.get("n_distinct_analysts") == 1
        and event["payload"].get("confidence") == 1.0
    )
```

This is the predicate operators run in 5 seconds, not a 5-minute reproduction of `recommend()`. Add it to `hermes_quant.governance.audit_log_query`.

## What happens to today's 4 approvals

- HOOD, CRC, RRC, MRNA (2026-05-27, all conf=1.0 -0.20 short) **stay approved** (state advance is correct per the 2026-05-27 lift).
- A retroactive `recommend()` probe will be run for each as part of the lift verification record at `references/incident-2026-05-27-bma-fix-verification.md`. If any returns `n_distinct_analysts == 1`, we re-halt and reject prospectively.
- The 4 audit-log entries remain v1; they are NOT retro-rewritten. The retro probe lives in the incident reference doc, not in the audit log.

## Anti-patterns explicitly rejected

- ❌ **Adding `signal_provenance` only on gate_approval, not gate_rejection.** Rejected: the rejection path is where degeneracy MOST often hides (a degenerate signal that's gated on a threshold rule should leave a trail too).
- ❌ **Storing `signal_provenance` in a sidecar JSONL keyed by audit-event-id.** Rejected: too many files, race conditions, schema drift between files. Audit log is canonical; one file.
- ❌ **Retroactively rewriting v1 events with reconstructed provenance.** Rejected: the audit log is append-only by design (ADR-0031); rewriting is a tampering-grade operation. Retro evidence belongs in incident-reference docs.

## Compliance

This ADR aligns hermes-quant with the cross-project consensus pattern from the 2026-05-27 research scatter:

- **TauricResearch/TradingAgents** — every `CommitteeTurn.metadata` carries an `evidence_ids: List[str]` field; the same idea, applied at the analyst-view layer.
- **HKUDS/Vibe-Trading** — Data Citation Discipline (`agent/src/swarm/grounding.py`): every prompt receives ground-truth OHLCV with citation IDs; agents are forbidden from quoting any number not traceable to an injected citation. Our analog: every approval cites an analyst-view-id list.
- **Mai0313/TradingAgents** — `TradeRecommendation` schema includes `confidence: float (0–1)` AND `warning_message: str | None` for self-flagged degeneracy.
- **virattt/ai-hedge-fund** — `{signal, confidence, reasoning}` wire format per analyst; reasoning lives on the view, not in free-form rationale fields.

## Future work (deferred to ADR-0042, ADR-0043, ADR-0044)

- ADR-0042: Persistent memory + reflection layer (TauricResearch + Mai0313 + Vibe-Trading FTS5)
- ADR-0043: 3-way risk committee (Aggressive/Conservative/Neutral) post-trader-stage (TauricResearch gap)
- ADR-0044: Trader intermediate stage with `TraderProposal` Pydantic schema (entry_price, stop_loss, time_horizon)
