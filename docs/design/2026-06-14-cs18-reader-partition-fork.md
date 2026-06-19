# cs18 — reader-partition cross-account pooling: Option A vs B fork (scored)

- Date: 2026-06-14
- Branch: `inc/cs18-reader-partition`
- Status: LIBRARY HALF LANDED (additive, byte-identical) · WIRING HALF RECOMMENDED (operator-gated)
- Money-software: ADR-0004 deterministic gate is final authority. Silence-by-default.
  This change touches a money-path READER consumed by the always-on ADR-0016 §D9 hard
  rail and the ADR-0071/ADR-0087 portfolio-caps gate. Blast radius is real, so the
  risky cutover is PROVE+RECOMMEND, not force.

## The bug (verified against actual code)

`hermes_quant/portfolio/state.py` `reconstruct_portfolio_state` keys its snapshot by
`asset` ALONE — no `account_id`, no `asset_class`:

- `state.py:112` `latest_per_symbol: dict[str, tuple[str, float]]` — key is the bare symbol.
- `state.py:131-133` the ONLY partition filter is `reactor_name` (`reactor_filter`).
- `state.py:150-153` latest-wins per SYMBOL (`ts >= prior[0]`).
- Returns `PortfolioState(positions=dict[str, float])` — one signed target fraction per symbol.

The per-FILL cap-clip uses the OPPOSITE partition — the 3-col PK:

- `state/portfolio_state.py:138-147` `PRIMARY KEY (account_id, asset_class, symbol)`.
- `get_positions(account_id)` filters `WHERE account_id=?` and keys by `(asset_class, symbol)`.

So the two cap layers DISAGREE on partition. `alpaca_paper.py` documents alpaca-paper as a
DELIBERATELY SEPARATE shadow book (`ALPACA_ACCOUNT_ID = "alpaca-paper"`), distinct from the
synthetic paper-default book (`paper` + `deterministic-equity`, both resolving to account
`"paper-default"`).

cs16 (commit `2f1a280`) flipped BOTH rail call sites to `reactor_filter=None`
(`autonomous.py:523` D9 rail ALWAYS-ON; `autonomous.py:551` caps gate). With the reactor
filter OFF, the `alpaca_paper` shadow records survive the only partition filter the reader
has, and pool into the SAME asset-keyed dict the paper-default `PortfolioCaps` gross/net is
computed from.

- Mode (A) different symbols: shadow `|fraction|` is SUMMED into paper-default gross/net →
  caps see exposure not in the book the fires hit → false caps silence / distorted headroom.
- Mode (B) same symbol in both books: the asset-only key COLLAPSES two distinct logical
  positions to ONE row at the latest `asof_execution` → the D9 rail UNDER-counts and the
  surviving target is the WRONG book's.

### Already active on the live bus (NOT strictly latent)

`~/.hermes/quant/executions.jsonl` 2026-06-14: 46 records, 0 with a top-level `account_id`.
reactor distribution `paper`×33, `deterministic-equity`×12, **`alpaca_paper`×1** (asset T,
+0.001, resolved account `alpaca-paper`). Verified directly:

```
reactor_filter=None  -> {AAPL, BA, T}   count 3   gross 0.251   <- what cs16 rail/caps see TODAY
account="paper-default" -> {AAPL, BA}   count 2   gross 0.250   <- the true paper-default book
```

So the shadow T is ALREADY polluting the live rail count (3 vs 2) and caps gross
(0.251 vs 0.250) under cs16. Magnitude is tiny today (one 0.001 shadow fill, no symbol
collision), and the over-count is fail-CLOSED for the count rail, but it is live, not latent.
Mode (B) collapse activates the instant an `alpaca_paper` fill shares a symbol with
paper-default — the documented steady state once `HERMES_QUANT_ALPACA_PAPER` runs alongside
the synthetic book for shadow comparison.

## Every caller of reconstruct_portfolio_state + reaction to each option

| # | Caller (file:line) | Today | Option A (additive `account` kwarg) | Option B (return-type change) |
|---|---|---|---|---|
| 1 | `autonomous.py:523` D9 hard rail (ALWAYS ON) | `reactor_filter=None` whole book | WIRE `account="paper-default"`: excludes shadow, live count 3→2 today. Breaks cs16 source-substring test. | signature/return change: must read 3-col key, rewrite `len()`/`set()`. |
| 2 | `autonomous.py:551` portfolio-caps gate (opt-in) | `reactor_filter=None` | WIRE `account="paper-default"`: headroom on true book. Breaks cs16 substring test. | as above; downstream PortfolioState consumers must handle new shape. |
| 3 | `ops/scripts/quant-flatten-paper-default.py:77` | default `reactor_filter='paper'` | UNAFFECTED (default `account=None`). | BREAKS: must pass account + unpack new shape. |
| 4 | `ops/scripts/quant-flatten-paper-default.py:157` post-flatten verify | default `'paper'` | UNAFFECTED. | BREAKS (same). |
| — | `daemon/portfolio_loader.py` (cs14) | MIRRORS the semantics; does NOT call this fn | UNAFFECTED — already has its own `_record_account` (`:103-118`, operator-approved `4d5cc42`) doing exactly Option A. | shared mental model drifts; cs14 mirror must be re-aligned in lockstep. |
| — | tests `test_portfolio_state.py` (both locations), `test_concurrent_rail_counts_whole_book.py` (cs16), `test_tick_lock_race.py` | pass | UNAFFECTED (cs16 asserts source substrings; autonomous.py NOT edited by library half). | many rewrites. |

cs16's `test_d9_rail_counts_all_reactor_names` asserts the EXACT source substrings
`"reactor_filter=None).positions"` and `"reconstruct_portfolio_state(reactor_filter=None)"`
are present in `autonomous.tick`. Wiring `account="paper-default"` into either call site
deletes those exact substrings → breaks the just-merged cs16 test. The wiring half MUST relax
those asserts to a behavioral assertion (rail counts paper-default = paper + det-equity,
EXCLUDES the alpaca-paper shadow) in the same change.

## Option A vs Option B — scored

### Option A — scope the rail to the fires' account (additive, no return-type change) — CHOSEN
Add `account: str | None = None` kwarg + a `_record_account(rec)` helper (top-level
`account_id` → `reactor_metadata.account_id` → `"paper-default"` sentinel). When set, drop
non-matching records BEFORE the asset-key collapse. Return type unchanged. Preserves cs16's
det-equity inclusion (det-equity resolves to `"paper-default"`, so it STAYS; only the
alpaca-paper SHADOW partition is dropped).

- Blast radius: callers 3,4 unaffected (default None); cs14 loader unaffected (separate fn,
  already mirrors this); cs16 test unaffected while autonomous.py is NOT edited.
- Live single-account byte-identical: YES. `account=None` is the whole-book path verbatim;
  even `account="paper-default"` on a single-account bus is identical.
- Risk: LOW. Mirrors a pattern already shipped + operator-approved in cs14.

### Option B — add (account, asset_class, symbol) key + account/asset_class params — REJECTED
Change `latest_per_symbol` to a 3-col key and the return to a multi-account map (or require an
account arg). Correct, but changes the public return SHAPE.

- Blast radius: ALL 3 live callers must unpack a new shape; cs14 loader mirror re-aligned;
  cs16 source-substring test rewritten; both `test_portfolio_state.py` files rewritten.
- Live single-account byte-identical: NO — the return type itself changes.
- Risk: HIGH for a money-path reader under an always-on rail.

## Recommendation — split the landing across the safe boundary

LANDED THIS INCREMENT (low-risk, byte-identical): the LIBRARY half — the additive `account`
param + `_record_account` helper in `portfolio/state.py`. `account=None` default = whole-book
verbatim, so every existing caller and the live single-account book are byte-identical.
RED→GREEN against the new proof. cs16 + portfolio_state + tick_lock_race all still GREEN.

OPERATOR-RECOMMENDED (do NOT force): the WIRING half — passing `account="paper-default"` at
`autonomous.py:523` and `:551`. This is a behavior change to the ALWAYS-ON safety rail (live
count 3→2 today, caps gross 0.251→0.250, dropping the shadow T) AND it breaks the cs16
source-substring test, which must be relaxed to a behavioral assertion in the same change.
Operator should review one tick log confirming the live count moves 3→2 and caps gross moves
0.251→0.250 before approving. The library param makes the wiring a one-line, fully-reversible
follow-up.

This does NOT revert cs16: det-equity (account_id `"paper-default"`) still contributes to the
rail under `account="paper-default"`; only the alpaca-paper SHADOW partition is excluded.

## Out-of-scope follow-ons (filed by the concurrent critique, NOT fixed here)

- `daemon/portfolio_loader.py:118` (cs14-owned) admits `_record_account(r) in {account_id,
  "paper-default"}` — a set-OR that pools the entire paper-default book into ANY requested
  account (the weekly-exit cron requests `account_id="alpaca-paper"`). Same defect CLASS,
  different reader + consumer. Deferred to the cs14 owner; outside cs18's reader.
- `ops/scripts/quant-flatten-paper-default.py:77,157` still read `reactor_filter='paper'`
  (paper-only) while the post-cs16 cap view is `reactor_filter=None` — the headroom-recovery
  tool now under-flattens vs the live cap view. Deferred; sequence AFTER the cs18 wiring half.
