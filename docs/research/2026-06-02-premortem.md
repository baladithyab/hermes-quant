# 2026-06-02 — Premortem: ADR-0086 + ADR-0087 catastrophic failure modes

_Assumption_: ADR-0086 (share-quantity + dollar accounting) and ADR-0087 (centralized portfolio-cap clip at `PaperReactor.execute`) shipped together, all tests were green, and within 3 months the paper ledger exhibited a **new** catastrophic failure. This document treats that failure as fact and reconstructs the most plausible technical root causes, focusing on:

1. NAV_at_fill bootstrapping circularity.
2. Share-migration blast radius (code that silently misinterprets `quantity`).
3. Double-clip / zero-clip races at the PaperReactor seam under concurrency.
4. Reconcile/replay paths that now diverge from pre-ADR behavior.

Paths and line numbers refer to the tree as of 2026‑06‑02.

---

## 1. NAV_at_fill bootstrapping circularity

### 1.1 Symptom

The first real post-migration fills on the paper account produced **wildly incorrect share counts** and equity deltas. In one observed episode:

- Operator approved a +20% long (`fill_size_pct = +0.20`) on an empty book with `HERMES_QUANT_PAPER_INITIAL_CASH=100_000` and `fill_price=$100`.
- The ledger wrote **2 shares** instead of the intended 200 shares, and cash moved by **$20** instead of $20,000.
- Subsequent fills compounded the error (all position and cash math were off by ~100×), but the track record UI and some risk checks continued to show "plausible" numbers because they re-derived NAV from these corrupted share counts.

The incident invalidated 6+ weeks of paper P&L and broke the invariant that a 20%-of-NAV buy on a flat $100k book yields ~200 shares.

### 1.2 Likely root cause: NAV_at_fill sourced from stale / fractional equity

ADR-0086 specifies that shares on the equity path are derived as:

```python
shares = (fill_size_pct * NAV_at_fill) / fill_price
```

where `NAV_at_fill` is the **pre-fill equity_total** from `state.db` (`cash.equity_total`) or the bootstrap initial cash.

Pre-ADR code:

- `hermes_quant/state/portfolio_state.py` `_default_initial_cash()` (lines 78–89) returns the initial cash env var (default 100k).
- `PortfolioState.reconstruct_from` bootstraps `cash_map[acct]` with `initial_cash` and writes `cash.equity_total = balance + Σ |qty| * avg_entry_price` (lines 372–381).
- `_apply_execution_unsafe` loads `balance_usd` from `cash` or uses `initial_cash` when absent (lines 569–579), then computes:

  ```python
  delta_cash = -fill_size_pct * fill_price
  new_cash = cash_balance + delta_cash
  equity = new_cash + sum(abs(qty) * avg_entry_price)
  ```

ADR‑0086 promises to repurpose this pipeline so **equity_total becomes NAV_at_fill**, with positions tracked in shares and cash in real dollars. The most likely implementation mistake was to:

1. Leave `_apply_execution_unsafe`’s cash bootstrap logic intact: on the **first ever fill**, `crow` is `None`, so `cash_balance = initial_cash` (100k) is correct.
2. But introduce an intermediate reconstruction or migration step that **reset or zeroed** the `cash` row before the first governed fills, or wrote a bogus `equity_total` (e.g., based on NAV-fraction positions).
3. Then have the new equity-path share conversion call something like:

   ```python
   nav = cash.equity_total if cash is not None else _default_initial_cash()
   # or worse: nav = cash.balance_usd or cash.equity_total (0-inclusive)
   ```

   and pass `nav` into the NAV_at_fill division.

In this failure, `cash.equity_total` was either **0** or **O(100)** instead of 100_000 at the time of the first real fill. With `nav≈100`:

```python
shares = 0.20 * 100 / 100 = 0.20  # truncated/rounded to 0 or 0.2 shares
```

If a later migration run or partial reconstruction bumped `equity_total` to, say, ~1_000 while the book still held a residual NAV-fraction quantity, a subsequent fill might compute:

```python
shares = 0.20 * 1_000 / 100 = 2
```

which matches the observed ~100× under-sizing.

### 1.3 Circularity: NAV derived from positions, positions derived from NAV

The deeper structural bug is a **circular dependency** between NAV and positions:

- Equity-path `apply_execution` needs `NAV_at_fill` to convert `fill_size_pct → shares`.
- But `NAV_at_fill` is itself derived from **existing positions** (cash + Σ qty×mark), which were either:
  - carried over from the old fractional ledger and misinterpreted as shares; or
  - recomputed from `executions.jsonl` using the new share semantics but against the wrong unit assumptions for historical records.

In either case, the initial NAV snapshot used for share conversion was **not the true $100,000**; it was a scaled or unit-confused quantity.

Once the first corrupted fill landed, subsequent fills saw a NAV polluted by their own mis-sized predecessors, creating a **self-reinforcing error loop**:

1. First fill: NAV small → shares tiny.
2. Equity recomputed from tiny shares → NAV remains small.
3. Second fill: still uses small NAV → more tiny shares.
4. Over time, the book accumulates dozens of minuscule share positions while cash barely moves; the paper P&L is effectively flat regardless of advisor output.

### 1.4 Why tests didn’t catch it

Existing tests focus on **internal algebra** under controlled NAV:

- `tests/state/test_portfolio_state.py` builds `PortfolioState` in isolation and asserts on `quantity` / `cash` numerics given synthetic records.
- ADR‑0086’s new tests (e.g., `test_long_fill_cash_and_equity_in_dollars`) likely called `apply_execution` directly with an explicitly prepared `cash` row (or used a fresh DB and assumed `_default_initial_cash()` would represent NAV).

But none of them exercised the **bootstrap sequence actually used in production**:

- `PaperReactor.execute` appends to `executions.jsonl` and then calls `get_portfolio_state().apply_execution` on a DB that has just been rebuilt via `quant-ledger-reconcile`, with a new share-based projection that may have written an incorrect initial `cash.equity_total`.

No test combined:

1. Fresh DB created by `quant-ledger-reconcile` running the new fold on a legacy fractional `executions.jsonl` tail.
2. First equity-path `PaperReactor.execute` call on that DB.
3. Assertion that a 20%-of-100k buy at $100 yields 200 shares and $80k cash.

That missing cross-tool integration test let the NAV_at_fill circularity ship.

---

## 2. Share-migration blast radius

### 2.1 Symptom

After migration, two classes of consumers silently misbehaved:

1. **Admissibility restatement and short-trade oracle:** some reopened shorts that should have been rejected as too large, and others were incorrectly flagged as fractional violations.
2. **CLI / reporting tools:** `quant-portfolio-daily` and `quant-status` rendered confusing exposure numbers; retro scripts derived borrow fees and notional exposure off by 10–100×.

No obvious exceptions were thrown: everything continued to run, but the interpretation of `quantity` flipped underneath them.

### 2.2 Offenders that still treat `quantity` as NAV-fraction

Key pre-ADR locations that document / assume quantity==NAV fraction:

1. **Admissibility restatement script** — `ops/scripts/quant-admissibility-restate.py` (lines 98–121, 145–176):

   ```python
   state = PortfolioState(state_db_path=Path(book))
   positions = state.get_positions(account_id)
   cash = state.get_cash(account_id)
   ...
   # state.db stores position.quantity as a NAV FRACTION ... NOT shares
   # ...
   signed_shares = target_pct_to_shares(pos.quantity, account_nav, pos.avg_entry_price)
   qty = abs(signed_shares)
   ...
   est_carry = qty * pos.avg_entry_price * cbr / DAY_COUNT_BASIS * days_held
   rows.append({
       "symbol": pos.symbol,
       "qty": pos.quantity,
       "qty_shares": signed_shares,
       ...
   })
   ...
   "Positions are NAV fractions (cumulative fill_size_pct), converted to whole shares ..."
   "via target_pct_to_shares(quantity, account_equity_total, avg_entry_price); ..."
   "avg_entry_price is the offline quote proxy and equity_total backs both account_equity ..."
   ```

   Post-ADR‑0086, `pos.quantity` for equity legs is **already shares**. Passing shares into `target_pct_to_shares` with `account_nav` still read from `cash.equity_total` double-multiplies by NAV and produces nonsense share counts; borrow P&L estimates become wildly inflated or deflated. The oracle-side qualitative verdicts for historic shorts become untrustworthy.

2. **CLI status tool** — `hermes_quant/cli/status.py`:

   - Reads positions directly from `state.db` (lines 279–293) and prints `qty={p.quantity:+.4f}` (lines 582–586) with no unit hint.
   - Reads cash rows including `equity_total` (lines 300–311, 591–595).

   After ADR‑0086, the meaning of `quantity` changed from "fraction of NAV" to "shares" for equities, but **no CLI copy was updated** to distinguish the units or to re-derive NAV fractions for display. Operators accustomed to thinking in %NAV now saw small share counts rendered as `+0.2000` and misread them as 20%.

3. **Backtest / retro scripts** — `ops/scripts/quant-strategy-retro-weekly.py` and `ops/scripts/quant-portfolio-daily.py` load `symbol, quantity, avg_entry_price` directly from `positions` (e.g., daily script lines 63–69). Their commentary and P&L calculations implicitly assumed NAV-fraction quantities when estimating notional exposure or borrow cost. With ADR‑0086 in place but these scripts un-migrated, exposure lines like "gross=860%" vs caps would be computed using **shares × price but through formulas that still applied NAV-fraction reasoning**.

4. **Admissibility / borrow P&L helpers** — `hermes_quant/admissibility/borrow_pnl.py` documents that `short_shares` is a signed share count (line 40). Any glue code that formerly passed `pos.quantity` as a NAV fraction would break catastrophically once `quantity` held shares, unless it was carefully audited.

### 2.3 Blast radius inside risk/

`hermes_quant/risk/portfolio_normalize.py` is explicitly written against **NAV-fraction positions**:

- `PortfolioState.positions: dict[str, float]` maps symbol to signed `target_position_pct` (line 101–107), not shares.
- Gross/net exposure and cash_pct are defined as pure sums over these fractions (lines 116–125).

ADR‑0087’s new **central clip at `PaperReactor.execute`** must reconstruct a `risk.PortfolioState` snapshot per fill. The intended design, per ADR‑0086/0087, was:

> "the cap's NAV-fraction headroom must derive `position_pct = qty × mark / equity`, not read `quantity` as a fraction." (ADR‑0087 §Consequences)

The failure mode: the implementation shortcut **reused the old path** and passed `positions` built directly from `state.get_positions()` into `risk.PortfolioState` as-if `quantity` was already a NAV fraction. That makes:

```python
gross_exposure_pct = sum(abs(p) for p in positions.values())  # but p is now shares
```

so for a 200-share AAPL position at $100 on a $100k book, the clip sees `gross=200`, not `0.20`.

The cap then believes the book is at **20,000% gross exposure** and permanently silences new picks (`headroom_breached`), even on a flat, well-behaved book. In response, operators disabled `HERMES_QUANT_PORTFOLIO_CAPS` in production, unintentionally reverting to an uncapped system and recreating the leverage runaway surface.

Because the **same `PortfolioState` dataclass is used by both autonomous-tick Stage‑2 and the seam clip**, but only the autonomous path was tested thoroughly, this unit-mismatch at the seam was not covered until it manifested as either:

- all fires silenced (no trades) under caps-on; or
- operators flipping caps off and reintroducing the original leverage hazard.

---

## 3. Double-clip / zero-clip races at PaperReactor.execute

### 3.1 Symptom

Under load (multiple advisors / playbooks approving fills concurrently), the system exhibited:

- **Double-clipped** orders: a proposed 10% Kelly that should have landed at 5% after caps instead landed at ~2.5%.
- **Zero-clipped but filled** orders: a fill that logically should have been silenced by the cap still wrote a non-zero fill to `executions.jsonl`, but subsequent risk summaries treated it as 0%.

These discrepancies only appeared under concurrent runs; single-threaded simulations and tests remained green.

### 3.2 Likely race patterns

ADR‑0087 centralizes clip logic at `PaperReactor.execute`, but doesn’t remove all per-layer logic in a single atomic step. The shipped system ended up with **two live cap applications**:

1. **Autonomous-tick path** — `hermes_quant/autonomous.py` still calls:

   ```python
   clipped = clip_one_to_remaining_headroom(...)
   effective_size = clipped.portfolio_target_pct
   ```

   before it constructs the action and calls `_react` (lines 502–525, 589–600 region).

2. **Reactor seam** — the new ADR‑0087 code in `PaperReactor.execute` re-computes portfolio state and calls `clip_one_to_remaining_headroom` again on the same `fill_size_pct` just before recording the fill.

Under concurrency, the following patterns emerge:

- **Double-clip:** autonomous computes `effective_size=0.10→0.05` based on state at t0 and passes `fill_size_pct=0.05` into `PaperReactor.execute`. The seam then reconstructs state from `executions.jsonl` (which does not yet include the in-flight fill) and clips **again** against the remaining headroom, scaling `0.05→0.025`. Result: the UI shows a 2.5% position where both Stage‑1 and Stage‑2 logic intended 5%.

- **Zero-clip but filled:** process A and process B call `PaperReactor.execute` concurrently with the same symbol and `fill_size_pct`. Both reconstruct state from `state.db` / `executions.jsonl` **before either write lands**. Each sees headroom `h` and decides to clip to `h`. Due to missing cross-process locking around the seam clip (only `_apply_execution_unsafe` uses `BEGIN IMMEDIATE` for DB writes), both fills are allowed through; by the time `PortfolioState.apply_execution` runs, the book is over the cap, but the reed check is too late—the fill is already on the bus.

No test exercises two `PaperReactor.execute` calls in separate processes hitting the seam at the same time, so the race survives unit and integration test sweeps.

### 3.3 Interaction with admissibility NAV provider

`PaperReactor` and `autonomous._account_nav_usd` both use `_account_nav_usd()` as NAV provider, which in turn reads `PortfolioState.get_cash().equity_total` (paper.py:33–56, autonomous.py:150–156). With ADR‑0086, that `equity_total` is now share-based MTM NAV.

In the failure, different calls observed **different NAVs** (depending on whether they ran before or after another process’s `apply_execution` had committed), leading to non-deterministic share counts and admissibility outcomes for otherwise identical fills. The combination of **double-clip** and **racy NAV reads** turned the seam into a non-deterministic, order-dependent gate.

---

## 4. Replay / reconcile divergence from stored snapshots

### 4.1 Symptom

Post-migration, running `ops/scripts/quant-ledger-reconcile.py` on an old `executions.jsonl` and comparing the resulting `state.db` to:

- archived daily snapshots,
- old `tests/state/test_portfolio_state.py` fixtures, and
- shadow-account P&L histories

showed **systematic numeric drift**:

- Equity totals differed by tens to hundreds of dollars on the same book and same mark prices.
- Positions that had historically reconstructed to flat (0 quantity) now showed small residual shares.
- Stored snapshot-based tests failed, especially those asserting exact `equity_total` or `quantity` values for regression purposes.

### 4.2 Root causes

1. **Changed fold semantics:**

   - Pre‑ADR‑0086, `_replay_record` used `fill_size_pct` directly as `pos_delta` for equity (`pos_delta = fill_size_pct`, line 826) and used `cash_map[acct] -= fill_size_pct * fill_price` (line 847) for a dimensionally-wrong but deterministic cash adjustment.
   - `reconstruct_from` then wrote `positions.quantity` in NAV-fraction units and `cash.equity_total = balance + Σ |qty|×avg_entry_price` (lines 372–381).

   ADR‑0086 changes this to a **share-based** fold, recomputing both positions and equity in different units. Applied retroactively to a legacy `executions.jsonl` that encodes fills whose **intent** was NAV fraction, replay now interprets them as share units via NAV_at_fill conversion. The resulting `state.db` cannot match the historical one by design.

2. **MTM vs cost-basis equity:**

   If ADR‑0086 also upgraded `equity_total` to a mark-to-market value in some paths (e.g., reconstruct using injected `mark_prices`), then replay without access to the exact historical marks produces equity numbers that differ from old snapshots by the unrealized P&L accumulated since the snapshot. Older tests that asserted exact equality on `equity_total` (e.g., `tests/unit/wave_d/test_watermark.py`, `tests/unit/test_tick_settlement.py`, which fabricate `equity_total` fields) are now comparing **cost-basis assumptions** to MTM values.

3. **Round‑trip drift between state.db and daemon Portfolio loader:**

   - The daemon’s `portfolio_loader.reconstruct_portfolio` folds `executions.jsonl` independently, using `qty` in shares and a mark-price seam to compute `equity_total = cash + Σ qty×mark`.
   - ADR‑0086 attempted to align `PortfolioState` with this reference fold, but subtle differences in fallback order (last fill vs avg_entry), or which executions subset is included, produced tiny but non-zero differences.

   Reconcilers that previously compared `state.db` to the daemon’s view with a `==` or `pytest.approx` tolerance tuned for fractional units now frequently tripped diff thresholds.

### 4.3 Specific breakages

- `ops/scripts/quant-ledger-reconcile.py` (lines 32–48) expects that reading `symbol, quantity, avg_entry_price` from the reconciled DB and then diffing against a saved snapshot yields **bit-identical** numbers. After ADR‑0086, `quantity` is in shares and `equity_total` semantics have changed, so stored snapshots for legacy episodes are no longer comparable.

- `tests/state/test_portfolio_state.py` includes schema and behavior checks (e.g., `test_cash_table_has_equity_total_column` at lines 755–758) but not updated semantics; any tests that hard-coded expectations like `pos.quantity == 0.10` or `equity_total == initial_cash` after particular fill sequences now fail or had to be loosened, eroding the regression net.

- Shadow-account tests (`tests/shadow/test_account.py`) assume a specific relationship between `positions_value`, `cash`, and `equity_total`. If any path started reusing `PortfolioState`’s share-based quantities or MTM semantics to seed shadow histories, historical tests would start failing as well.

---

## 5. Summary of worst failure modes

1. **NAV_at_fill circularity corrupts first fills**: A bad or stale `cash.equity_total` is used as NAV_at_fill, producing share counts and cash deltas off by 10–100× on the very first post-migration fills. Once polluted, NAV stays wrong and every subsequent fill compounds the error.

2. **Risk and admissibility read `quantity` in the wrong unit**: Code paths like `quant-admissibility-restate.py` and the seam’s portfolio caps continue to treat `state_db.positions.quantity` as a NAV fraction. After ADR‑0086 it is shares, so borrow estimates, cap headroom, and oracle restatements silently become garbage.

3. **Centralized clip double-applies or races under concurrency**: Autonomous-tick still pre-clips, and `PaperReactor.execute` clips again, leading to double-clip under-sizing. Concurrent executes see stale state when computing headroom and admit fills that should be silenced, or silence fills that should fire.

4. **Replay and reconcile become non-idempotent relative to history**: `quant-ledger-reconcile` folding legacy `executions.jsonl` under share semantics produces `state.db` that disagrees with archived snapshots and daemon reconstructions. Tests that relied on exact equality for `quantity` and `equity_total` either fail or are weakened, masking accounting regressions.

5. **Operator and monitoring surfaces mislead rather than alert**: CLI tools and daily reports continue to render `quantity` and `equity_total` without clarifying units or semantics. Operators see plausible-looking numbers while the underlying ledger is wrong, delaying detection and greatly increasing blast radius.
