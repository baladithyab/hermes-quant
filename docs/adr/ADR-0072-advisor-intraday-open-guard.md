# ADR-0072: Advisor-layer intraday open-guard (cross-run per-symbol-per-day dedup)

**Status:** Accepted
**Date:** 2026-05-29
**Implemented:** 2026-05-29 — `hermes_quant/risk/open_guard.py` (+ `tests/unit/test_open_guard.py`, 15 tests), wired into `~/.hermes/scripts/quant-daily-interim.py` before `create_proposals_for_actionables`. Verified against the live 2026-05-29 book: a simulated re-run of today's 26 filled names deduped 26/26 with reason `"filled SHORT today at HH:MM UTC"`, kept a genuine HOOD SHORT→LONG flip + a new name. Default-ON; kill-flag `HERMES_QUANT_OPEN_GUARD=0`.
**Wave:** D (paper-trading fidelity → risk control)
**Supersedes:** nothing
**Amends:** [ADR-0015](ADR-0015-proposal-store-and-ttl.md) — adds a pre-propose / pre-fire dedup guard on top of the proposal lifecycle
**Cites:** [ADR-0004](ADR-0004-deterministic-risk-gate.md) (deterministic per-symbol gate), [ADR-0071](ADR-0071-portfolio-aware-dynamic-kelly.md) (portfolio caps — the *aggregate* sibling control to this *per-symbol-per-day* control), `hermes_quant.proposals.ProposalStore.propose`, `hermes_quant.state.portfolio_state.get_portfolio_state` / `PortfolioState.get_positions`, `~/.hermes/scripts/quant-daily-interim.py:auto_approve_actionables`

---

## Context

Forensic on the 2026-05-29 paper book (operator question: "trace the duplicate fills"):

```
executions.jsonl, 2026-05-29:  52 fills
  12:34 UTC batch:  26 names, proposal_id prefix prop_20260529T1234…, HITL=True, reactor=paper
  15:04 UTC batch:  26 names, proposal_id prefix prop_20260529T1504…, HITL=True, reactor=paper
  symbol sets:      IDENTICAL (26 == 26)
  proposal_id overlap: 0  (distinct PKs — NOT a re-fire of the same proposal)
  bar_ts (both):    2026-05-28 04:00 UTC  (SAME stale daily bar)
```

Two advisor crons fired the same 26-symbol universe ~2.5h apart, **off the same daily bar**, doubling every position:

| Cron | Schedule (PT) | UTC fill time | Batch |
|---|---|---|---|
| `quant-daily-premarket-interim` (13b66e53eaa4) | 05:30 Mon-Fri | 12:34 | 26 opens |
| `quant-daily-midday-interim` (c64f4d386cc5) | 08:00 Mon-Fri | 15:04 | 26 opens (same names) |

Both run `quant-daily-interim.py` with `HERMES_QUANT_AUTONOMY=paper`. Each run:

1. scans the universe → `recommend_one()` per symbol,
2. `create_proposals_for_actionables()` → mints a **fresh** `proposal_id` per pick (`prop_<ISO_seconds>_<symbol>_<rand6>`, per ADR-0015 §D3),
3. `auto_approve_actionables()` → fires the PaperReactor on each.

### The structural gap

The advisor layer's only idempotency key is `proposal_id`, which is **fresh by construction on every run**. There is no "have I already opened this symbol today?" guard. So:

- The premarket run shorts HOOD at 12:34.
- The midday run, seeing the same bar produce the same SHORT signal, mints a *new* proposal and shorts HOOD *again* at 15:04.
- Net result: −2× the intended HOOD exposure, with the second fill carrying no new information (same `bar_ts`).

The autonomous-tick layer **does not have this bug** — it writes per-symbol `(symbol, FIRE)` audit entries to `autonomous-tick.jsonl` and skips any pair already present that ET day (`fired_today_pairs()`-style guard). The playbook layer also has its own cross-cron journal guard. The advisor layer is the **only** firing rail missing a per-day open-guard.

### Why this is distinct from ADR-0071

[ADR-0071](ADR-0071-portfolio-aware-dynamic-kelly.md) caps **aggregate** exposure (gross/net/cash) and would shrink each pick's *size* when the book is large. It does **not** prevent the same symbol from being opened twice in one day — two 3.7%-scaled HOOD shorts still double HOOD vs. intent, and ADR-0071's per-pick scaling is computed per-batch, blind to what an *earlier batch the same day* already fired. ADR-0071 is the portfolio-level control; ADR-0072 is the per-symbol-per-day control. They are complementary and both needed:

```
ADR-0071  →  "the whole book may not exceed 200% gross"        (aggregate)
ADR-0072  →  "don't open the same name twice off the same day"  (per-symbol-per-day)
```

Observed compounding: as of 2026-05-29 the advisor crons run with `HERMES_QUANT_PORTFOLIO_CAPS` **unset** (caps are armed only on the autonomous-tick wrapper), so today both controls were absent on the layer doing all the volume. ADR-0071 rollout onto the advisor crons is a separate, immediate stopgap (env flag); ADR-0072 is the durable per-symbol fix.

---

## Decision

### D72.1 An intraday open-guard checked before propose-and-fire

Introduce a guard that answers: *"For this account, has this `(symbol, direction)` already been opened (or has a still-pending proposal) during the current ET trading day?"* If yes, **skip** the new pick: do not create a proposal, do not fire.

The guard runs in `quant-daily-interim.py` between `create_proposals_for_actionables()` and `auto_approve_actionables()` — actually, before proposal creation, so we don't even mint a dead proposal:

```
actionable = rank_picks(views)
actionable = open_guard_filter(actionable, account="alpaca-paper")   # NEW (D72.1)
actionable = create_proposals_for_actionables(actionable)            # only survivors get proposals
actionable = auto_approve_actionables(actionable)                    # fire survivors
```

Skipped picks are routed into a new brief bucket (`deduped`) so the operator sees *"HOOD SHORT — already opened today at 12:34, skipped"* rather than the pick silently vanishing.

### D72.2 Two evidence sources, OR'd together

A symbol counts as "already opened today" if EITHER:

1. **Filled today** — a row in `executions.jsonl` with `asset == symbol`, same `account`, same `direction` (sign of `target_position_pct`), and the fill timestamp falls in today's ET trading day. Reuse `hermes_quant.state.portfolio_state.get_portfolio_state()` / `PortfolioState.get_positions(account_id)` for the reconstructed book (this module already exists and walks `executions.jsonl`), plus a today-window filter on the raw executions for the time check.
2. **Pending proposal today** — a `pending` proposal in `proposals.db` for the same `(symbol, direction)` created during today's ET day (catches the case where an earlier run proposed-but-not-yet-fired, e.g. HITL mode without autonomy).

OR semantics: either an open position or a live pending proposal for the name blocks a second open. This makes the guard correct in both autonomy=paper (fires immediately, source 1 dominates) and HITL mode (proposals sit pending, source 2 dominates).

### D72.3 Direction-aware, not symbol-blunt

The key is `(symbol, direction)`, not `symbol` alone. Rationale: a legitimate **flip** (premarket says SHORT HOOD, midday's fresh bar says LONG HOOD because the signal genuinely reversed) is NOT a duplicate — it's a new decision that should be allowed (and ideally would close the short first, but that's the rebalancer's job per ADR-0035, out of scope here). What we block is the *same-direction re-open* off effectively the same information.

`direction = sign(target_position_pct)`: `+1` long, `-1` short, `0` flat (flat picks never fire, so they're irrelevant to the guard).

### D72.4 The guard is a filter, not a hard halt

The guard skips individual picks; it does not halt the account. If 26 names are proposed and 24 were already opened earlier today, the 2 genuinely-new names still fire. This is deliberately narrower than a halt — halts are the kill-switch for *systemic* problems; this guard is *per-pick hygiene*.

### D72.5 ET-trading-day boundary, not UTC calendar day

"Today" = the current US equity trading day in `America/New_York`, consistent with `fired_today_pairs()` in the playbook/autonomous layers. A premarket run at 05:30 PT (08:30 ET, pre-open) and a midday run at 08:00 PT (11:00 ET) are the **same** ET trading day → midday is correctly blocked. A guard keyed on UTC calendar day would also work for these two crons (both fall on the same UTC date here) but would silently break if a cron ever ran in the 19:00–24:00 PT window (next UTC day, same ET day) — so key on ET day to match the other layers and avoid that latent bug.

### D72.6 Override hatch for intentional adds

A pick may carry an explicit `allow_intraday_add=True` flag (set by a future scaling-in strategy or operator override) that bypasses the guard for that pick. Default absent → guard applies. This keeps the door open for deliberate position-building (e.g. a wheel strategy legging in) without weakening the default-safe behavior. Until a consumer sets it, the flag is dormant.

### D72.7 Where the guard lives (code-vs-script)

The reusable predicate belongs in the package, not the cron script, so the playbook and any future advisor surface can share it:

- New: `hermes_quant.risk.open_guard.already_opened_today(symbol, direction, account, *, executions_path=None, store=None, now_et=None) -> tuple[bool, str | None]` — returns `(blocked, reason)` where reason is a human string like `"filled SHORT today at 12:34 ET"` or `"pending proposal prop_20260529T1234… SHORT"`.
- New: `hermes_quant.risk.open_guard.open_guard_filter(picks, account) -> tuple[list[kept], list[deduped]]` — the batch wrapper the cron calls.
- The cron script (`quant-daily-interim.py`) imports and calls `open_guard_filter`, routes `deduped` into the brief.

Keeping it in `hermes_quant.risk` (alongside `gate`, `kelly`, and the ADR-0071 `portfolio_normalize`) groups all sizing/admission controls in one place.

---

## Consequences

**Positive:**
- Same-day double-opens eliminated on the advisor layer — the 2026-05-29 HOOD-×2 pattern cannot recur. Premarket + midday + EOD now compound into *at most one open per name per direction per day* unless a genuine flip or explicit add occurs.
- Brief gains a `deduped` bucket → operator sees *why* the midday run fired fewer names, instead of wondering whether something broke.
- Shared predicate (`hermes_quant.risk.open_guard`) is reusable by the playbook layer if it ever drops its journal-based guard, unifying the dedup story across rails.
- Direction-aware → legitimate signal flips are preserved, not suppressed.
- Cheap: reads the already-reconstructed `PortfolioState` + a `list_pending` scan; no new persistent state, no new cron.

**Negative / risks:**
- A genuine *scale-in* (intentionally adding to a winning position intraday) is blocked by default. Mitigated by D72.6's `allow_intraday_add` hatch, but no consumer sets it yet — so until then, the advisor cannot leg in. Acceptable: the advisor is a single-shot directional entry layer, not a scaling strategy; legging-in is a playbook/wheel concern.
- The guard depends on `PortfolioState` reconstruction being correct and current. If `executions.jsonl` is corrupt or the reconstruction lags, the guard could fail open (allow a dup) or closed (block a legit pick). Mitigated: reconstruction already powers the daily portfolio snapshot and is exercised daily; a unit test on the today-window filter covers the boundary.
- ET-day boundary requires a tz-aware "now" — must use `America/New_York`, not naive UTC. A test asserts the 19:00–24:00 PT next-UTC-day-same-ET-day case.
- Does NOT fix the *stale-bar* root issue (both runs acting on yesterday's close). That's a separate concern: the advisor reads daily bars and the still-forming-bar discipline (ADR-0069) governs which bar is used. ADR-0072 prevents the *double-open symptom*; it does not make the midday run use fresher data. If intraday freshness is wanted, that's a future ADR on intraday-bar timeframes for the advisor.

---

## Alternatives considered

**A. Collapse the two advisor crons into one daily run.** Removes the double-fire by removing the second run. Rejected: the midday run exists on purpose — to catch signals that flipped after the open. Killing it loses that coverage. The right fix lets midday run but blocks *redundant* re-opens while still firing genuine flips/new names.

**B. Make `proposal_id` deterministic per `(symbol, ET-day)` so the second propose collides.** Rejected: `proposal_id` is an ADR-0015 contract (`prop_<ISO_seconds>_<symbol>_<rand6>`, TTL clock from creation) used as the audit PK and HITL approve target. Overloading it as a dedup key breaks TTL semantics and the "one proposal = one operator decision window" model. The guard belongs *upstream* of proposal creation, not inside the PK scheme.

**C. Rely on ADR-0071 portfolio caps alone.** Rejected per D72-Context: caps shrink size, they do not prevent same-name re-opens. Two scaled HOOD shorts still double HOOD vs. intent. Aggregate control ≠ per-symbol-per-day control.

**D. A cross-cron flock / journal like the autonomous-tick layer.** Viable, but the autonomous-tick journal is a side-file; reconstructing "did I open this today" from the *authoritative* `executions.jsonl` + `proposals.db` (D72.2) is more robust than a parallel journal that can drift from the real book. Reuse the existing `PortfolioState` reconstruction rather than add a third source of truth.

---

## Rollout

Follows the ADR-0071 default-OFF discipline for risk-changing behavior, but inverted: this guard is *strictly safer* and removes phantom exposure, so it ships **default-ON** behind a kill-flag rather than default-OFF behind an enable-flag.

1. Land `hermes_quant.risk.open_guard` with unit tests (today-window boundary, direction-flip allowed, pending-proposal source, ET-day vs UTC-day boundary case).
2. Wire `open_guard_filter` into `quant-daily-interim.py` before `create_proposals_for_actionables`; add the `deduped` brief bucket.
3. Ship default-ON. Kill-flag `HERMES_QUANT_OPEN_GUARD=0` disables it for debugging only.
4. Verify on the next premarket+midday pair: midday brief should show a non-empty `deduped` bucket for names already opened at premarket, and `executions.jsonl` should show the same-name double-open pattern gone.
5. **Immediate stopgap, independent of this ADR:** add `HERMES_QUANT_PORTFOLIO_CAPS=1` and `HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2` to the three advisor cron env prefixes (premarket / midday / EOD), so the advisor layer gets the ADR-0071 aggregate cap + ADR-0070 slippage that the autonomous-tick layer already has. This does not require ADR-0072 code and should land first.

---

## Verification

```python
# After the guard is live, on a premarket+midday day:
import json, pathlib, collections
ex = [json.loads(l) for l in (pathlib.Path.home()/".hermes/quant/executions.jsonl").read_text().splitlines() if l.strip()]
today = "<YYYY-MM-DD>"
by_time = collections.defaultdict(set)
for e in ex:
    if str(e.get("asof_execution",""))[:10] == today:
        hhmm = str(e["asof_execution"])[11:16]
        by_time[hhmm].add((e["asset"], 1 if e["target_position_pct"] > 0 else -1))
# EXPECT: later batches contain ZERO (symbol, direction) pairs already present in earlier batches.
batches = sorted(by_time.items())
seen = set()
for hhmm, pairs in batches:
    dupes = pairs & seen
    print(f"{hhmm}: {len(pairs)} opens, {len(dupes)} same-day re-opens (EXPECT 0): {sorted(dupes)}")
    seen |= pairs
```

A passing run prints `0 same-day re-opens` for every batch after the first.
