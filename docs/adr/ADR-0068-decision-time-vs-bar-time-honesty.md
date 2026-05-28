# ADR-0068: Decision-time vs bar-time honesty — `asof_decision` semantics

**Status:** Proposed
**Date:** 2026-05-28
**Wave:** D (paper-trading fidelity)
**Supersedes:** nothing
**Amends:** ADR-0008 (signal record schema) — clarifies semantics of `asof` / `asof_decision`
**Cites:** ADR-0002 (MarketContext + analyst protocol), ADR-0008 (signal record schema), ADR-0015 (HITL propose-decide-react)

---

## Context

Forensic on `~/.hermes/quant/executions.jsonl` (2026-05-28, 112 fills) surfaced a labeling lie. Every fill stamps `asof_decision = 2026-05-28T04:00:00Z` (= 00:00 ET, the canonical 1d-bar boundary), but the BMA actually computed those signals at 13:09 ET and 15:39 ET — **9 to 11 hours after the labeled decision time**.

```
PROPOSAL_ID timestamp (BMA-actually-ran):   2026-05-28T17:09:03Z, 19:39:42Z
asof_decision (labeled):                    2026-05-28T04:00:00Z
asof_execution (PaperReactor wrote fill):   2026-05-28T17:09:03Z, 19:39:43Z
```

`asof_decision` was originally defined as the bar-date anchor (`MarketContext.asof = bar_ts`) so that backtests and live runs would produce reproducible signal IDs against a canonical bar boundary. That choice was correct *for ID purposes* but wrong *for downstream consumers that treat it as "when info became available"*:

- **Reflectors** key off `asof_decision` to compute holding-period returns. With the current label they think a 10:00 ET pick was held since midnight, inflating apparent holding time by 9–11h.
- **Backtests and shadow-account replays** that read live decisions and replay them think the model had the morning's price action available at midnight — it didn't.
- **Audit consumers** ("when did we decide to short HOOD?") get an answer 11h earlier than the truth, defeating the audit trail's purpose.
- **Slippage attribution** (`fill_price - decision_price`) is correct in magnitude but wrong in attribution — it can't distinguish "BMA→fill latency" from "intra-day adverse move."

The current convention conflates two distinct timestamps:

1. **Bar boundary** — the canonical close timestamp of the OHLCV bar the analysts read. Reproducible across replays. Used for dedup IDs and replay equality.
2. **Decision wall-clock** — the actual moment of computation, on the operator's clock. Used for everything else: latency, audit, holding period, slippage attribution.

These should be separate fields.

---

## Decision

### D68.1 Rename and split

The signal record (ADR-0008 schema, persisted to `signals.jsonl` and propagated through `ExecutionRecord`) will carry **two timestamps**:

| Field | Semantics | Example |
|---|---|---|
| `bar_ts` | UTC timestamp of the **last bar's close** the analysts read. Replay-stable. Used for dedup ID (`sha1(asset|exchange|timeframe|bar_ts)`). | `2026-05-28T04:00:00Z` for a 1d-timeframe pick whose latest closed bar is 5/27's session close (UTC convention) |
| `asof_decision` | **Wall-clock** UTC timestamp at which the BMA actually ran. What an outside observer would write down as "the model decided X at this moment." | `2026-05-28T17:09:03Z` |

`MarketContext.asof` continues to mean the bar anchor (replay equality requires this). The signal-record write-out path **decouples** the two: `bar_ts` comes from `ctx.asof`, `asof_decision` comes from `datetime.now(UTC)` at the moment `_emit_signal_record` is called.

### D68.2 Migration: additive, not breaking

`asof_decision` already exists in `executions.jsonl` and `signals.jsonl`. Its CURRENT meaning is the bar boundary. We **do not** rename the wire field, because reflectors and consumers already read it. Instead:

1. Add a new `bar_ts` field to the signal record schema. Populate from `ctx.asof`.
2. Change `asof_decision` to mean wall-clock. Populate from `datetime.now(UTC)` at signal-emit time.
3. Bump signal-record `schema_version` from `1` → `2` so consumers can detect the semantic shift.
4. Old records (schema_version=1) keep being read with their existing `asof_decision == bar_ts` semantics.

The `id` field's dedup-tail formula (`sha1(asset|exchange|timeframe|bar_ts)`) is unchanged — it's already keyed on bar-time, so replay equality holds. The `id` *prefix* (`sig-{asof.strftime('%Y%m%dT%H%M%SZ')}-...`) currently uses bar-time; after this change, signals on the same bar but different decision wall-clocks will have **different ID prefixes** but the **same dedup tail**. That's intentional and matches what humans expect: two reruns on the same bar produce the same dedup hash but the prefix tells you "morning run" vs "afternoon run."

### D68.3 ExecutionRecord propagation

`ExecutionRecord` (writes to `executions.jsonl`) currently has `asof_decision`. Add `bar_ts`. Populate both from the underlying signal record. Old records remain valid; new records carry the extra field.

### D68.4 Reflector and journal compatibility

The reflector (`hermes_quant.memory.reflector`) computes `tau_observable = max(asof_resolution, asof_decision + holding_days*86400)`. With `asof_decision` becoming wall-clock, this calculation **becomes correct** for the first time — previously it was anchored to midnight UTC of the bar date, which made the holding-period boundary an artifact of bar conventions, not real time. No code change required, but a correctness comment should be added.

The journal writer (`hermes_quant.journal.writer`) reads `advisor_result["as_of"]` for `asof_decision`. The advisor produces `as_of` from `ctx.asof` today. After this ADR, the advisor needs to also expose `decision_wall_clock` and the journal writer prefers that field with `as_of` as fallback for old replays.

---

## Consequences

**Positive:**
- The audit trail tells the truth about when decisions happened.
- Slippage attribution can finally distinguish model latency from adverse selection.
- Backtests and shadow-account replays stop reasoning about "midnight info availability" that never existed.
- Reflector holding-period math becomes correct on first principles.

**Negative / risks:**
- Two timestamp fields where there used to be one — slightly more cognitive load on tool authors.
- One-time backfill question: do we rewrite historical records? **No.** Historical `asof_decision == bar_ts` is preserved by `schema_version=1`; consumers branch on schema version.
- Brief scripts that print "as of X" need to choose which timestamp to print. Recommendation: print both, e.g. `decided 13:09 ET on the 5/28 close bar`.

**Out of scope:**
- Whether `bar_ts` for daily-timeframe mid-session reads should be the still-forming bar or the previous completed bar. That's [ADR-0069](ADR-0069-still-forming-bar-discipline.md).
- Modeling slippage between `asof_decision` and `asof_execution`. That's [ADR-0070](ADR-0070-paper-execution-fidelity.md).

---

## Implementation hooks

- `hermes_quant/daemon/tick_loop.py:_emit_signal_record` — split `asof` into `asof_decision := datetime.now(UTC)` and `bar_ts := ctx.asof`; bump `schema_version` to 2.
- `hermes_quant/advisor.py` — `recommend()` returns both `as_of` (legacy) and `decision_wall_clock` (new) in the result dict.
- `hermes_quant/journal/writer.py` — prefer `decision_wall_clock`, fall back to `as_of`.
- `hermes_quant/journal/models.py` — add `bar_ts: datetime` to JournalEntry alongside existing `asof_decision`.
- `hermes_quant/cli/status.py` — when displaying decisions, show `decided HH:MM (bar HH:MM)` if both available.
- Brief scripts (`~/.hermes/scripts/quant-daily-interim.py` etc.) — print decision wall-clock in human summaries.

The **minimum viable inline fix** is the first bullet only — split the timestamps in the signal record. Everything else can land in follow-ups.

---

## Verification

After the inline fix:

```python
# Run a fresh BMA, confirm the labels are honest:
from hermes_quant.advisor import recommend
from datetime import datetime, timezone
before = datetime.now(timezone.utc)
r = recommend(symbol="AAPL", asset_class="equity", timeframe="1d")
after = datetime.now(timezone.utc)

# decision_wall_clock should fall in [before, after]
# bar_ts should be the bar boundary (e.g. 04:00 UTC for daily)
assert before <= r["decision_wall_clock"] <= after
assert r["bar_ts"].hour == 4 and r["bar_ts"].minute == 0  # for daily UTC convention
```

For replayability:

```python
# Two runs on the same bar produce the same bar_ts and the same dedup tail,
# but different asof_decision wall-clocks.
r1 = recommend(symbol="AAPL", asset_class="equity", timeframe="1d")
import time; time.sleep(1)
r2 = recommend(symbol="AAPL", asset_class="equity", timeframe="1d")
assert r1["bar_ts"] == r2["bar_ts"]                          # bar identity
assert r1["decision_wall_clock"] != r2["decision_wall_clock"]  # decision identity
```
