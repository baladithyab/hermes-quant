# cs14: weekly-exit reader fork; the empty-book hole

**Status:** PROPOSED (investigate-then-recommend; the fix is DEFERRED to a later
operator-approved increment). This increment ships only a RED proof test and this
scored recommendation. NO live-path `.py` is edited here.

**Date:** 2026-06-13
**Author:** cs14 increment (agent)
**RED proof:** `tests/unit/test_weekly_exit_empty_book.py` (3 passed, 1 strict-xfail)
**Evidence dump:** `research/temp/inc-cs14/evidence.md`

---

## 1. Problem — producer/consumer shape divergence

The live execution producer and the live book-reconstruction consumer disagree on
the on-disk record shape, and the disagreement silently empties the book.

**Producer** — `react.paper.PaperReactor` writes `react.base.ExecutionRecord`
serialized by `react.paper._record_to_dict` (react/paper.py:56-80). The emitted dict
carries:

- `schema_version = None` — `ExecutionRecord.schema_version: str | None = None`
  (react/base.py:89). It is a **string-or-None** field. New records stamp the string
  sentinel `SCHEMA_ABSOLUTE_TARGET = "absolute-target-v1"` (pdr_core/contracts.py:199).
  It is **never the integer `1`**.
- `target_position_pct` — a signed NAV **fraction** (react/base.py:62), e.g. `+0.20`.
- **No `qty`, no `side`, no `account_id` key** (the serializer at react/paper.py:56-80
  emits none of them; size lives only in `target_position_pct` / `fill_size_pct`).

**Consumer** — `daemon.portfolio_loader.reconstruct_portfolio`:

- **HOLE-0** (portfolio_loader.py:74): filters `r.get("account_id") == account_id`.
  The live record has no `account_id` key, so `None != "alpaca-paper"` → dropped.
- **HOLE-1** (portfolio_loader.py:76): filters `r.get("schema_version") == 1` (int).
  The live record's `schema_version` is `None` (or, for new records, the string
  `"absolute-target-v1"`) → never equals int `1` → dropped.
- **HOLE-2** (portfolio_loader.py:91-92): even past the filter, the loop reads
  `rec["side"]` and `float(rec["qty"])`. Both keys are absent → `KeyError` → logged
  as "malformed exec record skipped" → `continue`.

Net effect: **every** live record is dropped, so `pf.positions == {}`.

The RED test feeds a record built by the REAL `ExecutionRecord` dataclass and
serialized by the REAL `_record_to_dict`, writes it to a temp `executions.jsonl`, and
asserts `reconstruct_portfolio(...).positions == {}` — the documented broken behavior.
A fourth strict-xfail test asserts the CORRECT behavior (one reconstructed position)
that the deferred fix must flip to PASS.

**Why the suite never caught it.** The existing loader tests
(tests/unit/test_daemon_lock_discovery.py:111-124, `_exec()`) HAND-ROLL
`{schema_version: 1, side, qty, account_id, asof, fill_price, ...}` — a shape **no
live producer emits**. They validate the loader against a fiction, so the
producer/consumer divergence stayed invisible.

---

## 2. Verified live-bus evidence

Reconfirmed empirically against the live bus `~/.hermes/quant/executions.jsonl`:

- **46 records.**
- **All 46** have `schema_version = None`.
- **Zero** of the 46 carry `side`, `qty`, or `account_id`.
- The first record's `target_position_pct = -0.2` (a short — fraction shape, not a
  share `qty`).

The producer chain reproduces the same divergent shape in-process: an
`ExecutionRecord` round-tripped through `_record_to_dict` yields
`schema_version=None`, no `account_id`, no `side`, no `qty`.

---

## 3. Live impact

- The weekly cron `30 6 * * 1` (quant-playbook-weekly.py:5; orchestrator-confirmed
  enabled registry row #12 on the live deploy box) calls the loader via
  `load_portfolio` at quant-playbook-weekly.py:296
  (`account_id="alpaca-paper", asset_class="equity"`).
- With an empty book, `run_weekly` early-returns the `weekly_empty_portfolio` event at
  quant-playbook-weekly.py:397.
- Therefore the **swing >60d-and-losing stop** and **3×ATR take-profit** (`decide_swing`),
  and the **LEAPS −25% drawdown / thesis-broken closes** (`decide_leaps`), in the loop
  at quant-playbook-weekly.py:408+, **NEVER run on a real book**.
- Two earlier fixes are made **UNREACHABLE in prod** because they sit downstream of a
  book that is always empty: **cs00** (short-PnL sign fix, portfolio_loader.py:152-154)
  and **cs01** (fail-closed drawdown breakers).

The RED test pins this with an asserted-empty characterization plus a strict-xfail
tripwire.

---

## 4. Companion finding (NOT in the fork — flag for the later increment)

Three additional reader sites read raw-execution fields that live records do not
carry. Neither Option A nor Option B fixes them; they are a **third seam** that must
also be patched in the later increment:

- `infer_play_tag` (quant-playbook-weekly.py:204-226) gates on `rec.get("side") != "buy"`.
  Every live record fails this, so `play_tag` silently defaults to `"swing"` for every
  position.
- `find_entry_record` (quant-playbook-weekly.py:230-235) gates on
  `rec.get("side") == "buy"` → returns `None` → quant-playbook-weekly.py:430 logs an
  ERROR ("no opening execution found for held position") and HOLDs.
- `days_between_iso` reads `entry.get("asof", "")` (quant-playbook-weekly.py:460); live
  records use `asof_execution`, with no `"asof"` key.

So even if Options A or B repopulate `pf.positions`, the weekly's raw-executions
readers still misbehave until this third seam is fixed. **Record this; do not fix it
here.**

---

## 5. Option A — re-point the weekly at `portfolio.state.reconstruct_portfolio_state`

Change `load_portfolio` (quant-playbook-weekly.py:280-301) to call
`hermes_quant.portfolio.state.reconstruct_portfolio_state` (state.py:35,
`reactor_filter="paper"`), which already reads the live `target_position_pct` shape.

**The shim it forces.** `reconstruct_portfolio_state` returns a `PortfolioState` whose
`positions` is `dict[str, float]` — a **fraction-only** map (state.py:115;
portfolio_normalize.PortfolioState same at :113). But the weekly reads, per position,
`pos.avg_entry_price` (quant-playbook-weekly.py:461), `pos.mark_price` (:464), and
`pos.qty` (:534) — **none of which exist on `PortfolioState`**. So Option A needs a new
enrichment shim at the weekly that:

1. re-walks fills to reconstruct each symbol's average **entry cost basis**,
2. fetches a **mark price** per symbol,
3. converts `fraction × NAV ÷ price → share qty`.

That re-implements most of the loader body inside the cron.

- **Vestigiality dividend:** lets `reconstruct_portfolio` (and its ~12
  `test_daemon_lock_discovery.py` tests) be deleted, since the weekly is its only live
  consumer (settlement_loop.py does not call it — stale docstring mention at :287 only).
- **Risk:** introduces NEW, untested cost-basis / qty math in a money path, and
  **duplicates the cs00 sign logic** (the short-PnL convention) in a second place where
  it can drift.

---

## 6. Option B — fix the loader in place

Keep the weekly's consumer contract (`Mapping[str, Position]`) unchanged; fix
`reconstruct_portfolio` to consume the REAL `ExecutionRecord` shape.

- **Seed B1 (filter):** replace `schema_version == 1` (and the absent-`account_id`
  requirement) with `is_absolute_target_record(rec)` (react/base.py:25) **and**
  `reactor_name == "paper"`. Source the account from `reactor_metadata` / accept a
  `"paper-default"` sentinel rather than requiring an absent key. MUST NOT silently
  re-admit crypto / other partitions (still filter `asset_class`).
- **Seed B2 (derive position):** derive `qty` / `side` from `target_position_pct`
  deltas (latest target minus carried-forward net, per the Option-E
  fill-delta-normalizer semantics) plus decision/fill price and a NAV input; keep
  returning `protocol.Portfolio` with `avg_entry_price` / `mark_price`.

- **Scope:** the loader today consumes ABSOLUTE share `qty` + `side`. Converting it to
  fraction-delta interpretation is a real rewrite. The loader docstring **GATES OFF**
  the partial-close / direction-flip branches (portfolio_loader.py:107-131) with known
  sign-convention bugs; Seed B2 must derive deltas WITHOUT relying on those gated
  branches (or it inherits the gate).
- **Vestigiality finding:** this adds NEW code to a near-vestigial module whose ONLY
  live consumer is this one caller (plus its identical ops/scripts twin). But it
  **preserves the weekly's `Mapping[str, Position]` contract with zero shim** and makes
  **cs00 / cs01 reachable** for the first time in prod.

---

## 7. Scoring table

Scores are qualitative (1=best … 10=worst for effort/risk; higher=better for the
other axes). The PROVE seed scores are noted inline.

| Axis | Option A (re-point + shim) | Option B (fix loader in place) |
|---|---|---|
| **Correctness** (right per-position avg_entry / mark / qty for the exit math) | Fresh shim re-derives all three; new untested cost-basis math, risk of drift from cs00. Medium. | Reuses the module that already owns the `Position` contract and the cs00 fix; the gated sign branches are a hazard if B2 leans on them. Medium-high if B2 avoids the gate. |
| **Contract fit** (does it satisfy the weekly's `pos.avg_entry_price` / `mark_price` / `qty` reads) | POOR — `PortfolioState` is fraction-only; needs a translation shim. | GOOD — returns `protocol.Portfolio` / `Position` unchanged; zero shim. |
| **Reachability of cs00 / cs01** | cs00 sign logic must be re-implemented in the shim (or those breakers stay dark / diverge). | Directly makes cs00 / cs01 reachable in prod. |
| **Blast radius / vestigiality** | Lets the loader + ~12 tests die (dividend). New math lives in a cron script. | Adds code to a near-vestigial module (only this caller). No deletions. |
| **Effort** | ~7/10 — new enrichment shim (cost basis + marks + qty conversion) is most of the loader body re-written at the weekly. | ~6/10 — filter swap is small; the fraction-delta derivation in B2 is the real work. |
| **Risk** | ~6/10 — duplicated money-math, two cs00 sign sites that can diverge. | ~7/10 — rewrites a money path; must avoid the gated sign-bug branches (loader:107-131). |

---

## 8. Recommendation — **Option B**

Both options add the **same enrichment** — entry price + mark + share `qty` derived
from the fraction shape. The only real question is **WHERE that math lives**: a new
shim bolted onto the weekly cron (Option A) versus inside the one module that already
owns the `Portfolio` / `Position` contract and already carries the cs00 sign fix
(Option B).

Option B is recommended because it:

1. preserves the weekly's `Mapping[str, Position]` contract with **zero shim**;
2. concentrates the cost-basis / qty / sign math in **one tested seam** instead of
   duplicating cs00's sign convention in a second location where it can drift;
3. makes **cs00 and cs01 reachable in prod** as a direct consequence.

The accepted cost: it adds code to a module whose only live consumer is this caller
(so the vestigiality argument for Option A is real) — but the contract-fit and
single-source-of-money-math wins outweigh the chance to delete the module. If a later
operator prefers to retire the loader entirely, Option A remains a viable
consolidation, but it should not be chosen for *this* fix because it forces a
parallel re-implementation of money math under deadline.

**Deferred-fix increment:** the actual change (Seeds B1 + B2, plus the Section 4
companion raw-executions readers in the weekly — `infer_play_tag`, `find_entry_record`,
`days_between_iso`) lands in a **later operator-approved increment**. The strict-xfail
test (`test_green_live_record_reconstructs_one_position`) is the tripwire: when the fix
lands, it XPASSes and strict-xfail FAILS, forcing the implementer to drop the marker.

**Central tradeoff (one line):** both options bolt on the identical
entry-price + mark + share-qty enrichment; the choice is a shim at the weekly (A) vs.
inside the loader (B), and B keeps the money-math single-sourced.

**Companion note:** the raw-executions readers in Section 4 must be fixed in the SAME
later increment — repopulating `pf.positions` alone is not sufficient, because the
weekly's per-position `side`/`asof` reads would still misbehave.

---

## 9. What this increment ships

- The RED proof test: `tests/unit/test_weekly_exit_empty_book.py` (3 passed, 1
  strict-xfail; ruff-clean).
- This decision doc.
- **NO live-path `.py` edit.** The actual fix lands in a later operator-approved
  increment.
