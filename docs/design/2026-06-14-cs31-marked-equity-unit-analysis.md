# inc-cs31 / cs32 — STAGE PROVE

File under test: `hermes_quant/state/portfolio_state.py`
Worktree: `/mnt/e/CS/github/hermes-quant-wt-cs31` (branch `inc/cs31-mtm-unit`)

---

## (1) UNIT_SPLIT — how the WRITE folds detect real-shares vs nav-fraction, and what the READ API can see

### The write-fold predicate (per-RECORD, not persisted)
Both write folds branch on a per-record field `reactor_metadata.quantity` (aliased `leg_quantity`):

- `_apply_execution_unsafe` (incremental), portfolio_state.py:680-689:
  ```
  rmeta = record.get("reactor_metadata") or {}
  leg_quantity = rmeta.get("quantity") if isinstance(rmeta, dict) else None
  if leg_quantity is not None:
      pos_delta = float(leg_quantity)   # signed contracts/shares  (REAL-SHARES path)
      ...
  else:
      pos_delta = fill_size_pct          # NAV-fraction proxy        (NAV-FRACTION path)
  ```
- `_replay_record` (rebuild), portfolio_state.py:1183-1195 — identical predicate:
  ```
  leg_quantity = rmeta.get("quantity") if isinstance(rmeta, dict) else None
  ...
  pos_delta = float(leg_quantity) if leg_quantity is not None else fill_size_pct
  ```
- The contract multiplier is applied ONLY on the real-shares path AND only for options:
  portfolio_state.py:699-703 and :1224-1228:
  ```
  contract_multiplier = _CONTRACT_MULTIPLIER if (leg_quantity is not None and asset_class == "us_option") else 1.0
  ```
  `_CONTRACT_MULTIPLIER = 100.0` (portfolio_state.py:82).

So the write predicate is `leg_quantity is not None`. REAL-SHARES = a record carrying `reactor_metadata.quantity` (multileg.py:586 signed contracts / :616 signed shares; alpaca_paper.py:266 `signed_qty`). NAV-FRACTION = legacy single-leg `paper.py` fills (paper.py:396 builds `reactor_metadata` WITHOUT a `quantity` key).

### What `get_marked_equity` can see per position — THE STRUCTURAL FINDING
`get_marked_equity` reads positions via `get_positions` (portfolio_state.py:902-928), which selects ONLY:
`account_id, asset_class, symbol, quantity, avg_entry_price, last_update_at`.

The positions table schema (portfolio_state.py:175-183) stores EXACTLY those six columns — **there is NO persisted unit flag**. `reactor_metadata.quantity` / `leg_quantity` is consumed at write time to choose `pos_delta`, then DISCARDED; only the resulting `quantity` (REAL_t) lands in the row. `Position` (state/positions.py:42-47) carries the same six fields, nothing more.

Consequence: at read time `get_marked_equity` **cannot reconstruct the write-time `leg_quantity is not None` predicate**. A NAV-fraction position (e.g. long 0.2) and a real-shares position (e.g. long 50 shares) are stored in the SAME row shape; both can have `asset_class="equity"`. `asset_class` does NOT discriminate either — `multileg.py:603` and `alpaca_paper.py` write real-shares rows with `asset_class="equity"`, the exact class the legacy NAV-fraction `paper.py` path also uses. The only asset_class that is unambiguously real-shares is `us_option` (NAV-fraction options never existed in the legacy path), but equity is genuinely ambiguous.

The clean, schema-honest discriminator therefore is a NEW persisted column on the positions table (e.g. `qty_unit TEXT NOT NULL DEFAULT 'nav_fraction'`), stamped `'shares'` on the real-shares write path (`leg_quantity is not None`) by BOTH folds, defaulting `'nav_fraction'` for every legacy/existing row. A legacy db's existing rows (and every legacy write) read back `'nav_fraction'` → byte-identical. `get_marked_equity` then branches on `pos.qty_unit` — the SAME predicate the folds used, surfaced through the row.

(A magnitude heuristic |quantity|<=1 ⇒ nav-fraction is REJECTED: it is not the predicate the folds use, and fractional-share real-shares positions or >100% leveraged nav-fractions both break it. The discriminator must be the persisted unit, not a guess.)

---

## (2) CORRECT_BRANCHED_MTM formulas

Per signed position `pos` with an injected `mark`:

- REAL-SHARES (`qty_unit == 'shares'`):
  `unrealized_i = pos.quantity * (mark - pos.avg_entry_price) * (_CONTRACT_MULTIPLIER if pos.asset_class == "us_option" else 1.0)`
- NAV-FRACTION (`qty_unit == 'nav_fraction'`, the legacy path — UNCHANGED):
  `unrealized_i = pos.quantity * nav_ref * (mark / pos.avg_entry_price - 1.0)`

No-mark fallback PRESERVED in both branches: `mark is None` ⇒ skip (zero contribution), exactly as today (portfolio_state.py:997-1002).

Verified arithmetic (matches the task trace):
- long 50 sh @100, mark 110, nav_ref 100000 → nav-frac(WRONG)=500,000 vs real(CORRECT)=500 → off by 1000× (≈ nav_ref/avg_entry).
- us_option long 2 ct @1.50, mark 2.50 → nav-frac(WRONG)=133,333 vs real-with-×100(CORRECT)=200 (and 2.0 if the ×100 were omitted — so the multiplier is mandatory on the real-shares option path).

---

## (3) SHORT_SIGN_PRESERVED

The sign rides on the SIGNED `pos.quantity` in BOTH branches; neither uses `abs()`.

- NAV-FRACTION (unchanged): short −1.0 @41.70, mark 49.70, nav_ref 100000 → −1.0*100000*(49.70/41.70−1) = −19,184.65 (adverse → loss). Matches the regression lock in tests/unit/test_portfolio_state_accounting.py:50.
- REAL-SHARES short −50 sh @100, mark 90 → −50*(90−100) = +500 (favorable, short profits). mark 110 → −50*(110−100) = −500 (adverse, short loses). Sign correct.
- Verified numerically: nav-frac short 50sh equivalent = +499,999.99 and real = +500.0 — both POSITIVE (short profits when mark<entry) under each branch.

The short sign is already correct today on the NAV-fraction path (task-verified) and stays correct on the new real-shares branch because the signed quantity multiplies the signed `(mark - avg_entry)`.

---

## (4) ORPHAN_CONFIRM — grep ALL callers

`grep -rn "get_marked_equity\|MarkedEquity" --include="*.py" .` (non-test):
- `hermes_quant/state/portfolio_state.py:250` — `class MarkedEquity` (definition)
- `hermes_quant/state/portfolio_state.py:948` — `def get_marked_equity` (definition)
- `hermes_quant/state/portfolio_state.py:1017` — constructs `MarkedEquity(...)` (the body itself)
- `hermes_quant/cli/status.py:595` — a COMMENT ("For true MTM use PortfolioState.get_marked_equity(...) from a marked report surface") — NOT a call
- `hermes_quant/react/paper.py:123` — a COMMENT / do-NOT-wire note ("Read-time MTM (get_marked_equity) is a SEPARATE reporting concern and must NOT be wired here")

Every actual invocation is in `tests/unit/test_portfolio_state_accounting.py` (lines 47,77,109,138,145,169,191,238,264). **No live production caller. CONFIRMED ORPHANED.** Do NOT wire it (paper.py:123 explicitly forbids wiring it into the NAV/admissibility seam). Lower risk now; correctness must be fixed before any future wiring (e.g. the quant-portfolio-daily marked-report surface the cli/status.py:595 comment points at).

---

## (5) CS32_FIX — n_positions / equity_basis label

portfolio_state.py:993-994 `continue`s on `avg_entry_price <= 0`, so a bad-avg position never increments `n_marked`. But :1007 sets `n_positions = len(positions)` INCLUDING the skipped row. Therefore a book with one bad-avg position can NEVER satisfy `n_marked == n_positions`, and `equity_basis` collapses to "mixed" even when every VALID (markable) position is marked.

This is a REPORTING-LABEL-only defect: it does NOT touch `total_unrealized` or `marked_equity` (the money figures). The skipped position already contributes 0 P&L correctly.

FIX: count positions actually considered. Track `n_considered` (incremented once per non-skipped position, i.e. avg_entry_price > 0) and use it as the denominator for the label instead of `len(positions)`. Then a fully-marked valid book reports `equity_basis == "mark"`. Reported `n_positions` becomes the count of considered (valid-avg) positions so the label is internally consistent (`n_marked == n_positions` ⇒ "mark").

(Current test test_marked_equity_zero_avg_entry_price_skipped:268 asserts `n_positions == 1` for a single bad-avg ZERO position and `equity_basis == "entry"`. Under n_considered the lone bad-avg book has n_considered==0 ⇒ the `n_positions == 0` branch ⇒ `equity_basis == "entry"` still holds, but reported n_positions would be 0, not 1. That existing test will need its `n_positions` expectation updated to 0 to match the considered-count semantics — flagged for the build agent; the label outcome "entry" is unchanged.)

---

## (6) FIX_SHAPE (nav-fraction byte-identical)

1. Schema: add `qty_unit TEXT NOT NULL DEFAULT 'nav_fraction'` to the positions CREATE TABLE (portfolio_state.py:175-183), plus an idempotent `ALTER TABLE ... ADD COLUMN qty_unit` migration for already-populated legacy dbs (default 'nav_fraction' → legacy rows byte-identical). Add `qty_unit` to `Position` (default 'nav_fraction') and to the `get_positions` SELECT.
2. Write folds: in BOTH `_apply_execution_unsafe` (the INSERT OR REPLACE at :809-817) and `_replay_record` (the positions dict + reconstruct_from upsert at :552-566), stamp `qty_unit = 'shares' if leg_quantity is not None else 'nav_fraction'`. This is the SAME predicate the folds already use for pos_delta/cash — no new branch logic, just persist the bit. Legacy/no-leg_quantity writes stamp 'nav_fraction' ⇒ byte-identical.
3. `get_marked_equity` (portfolio_state.py:991-1002): branch the MTM math on `pos.qty_unit`:
   - 'shares' → `pos.quantity * (mark - pos.avg_entry_price) * (_CONTRACT_MULTIPLIER if pos.asset_class == "us_option" else 1.0)`
   - 'nav_fraction' → `pos.quantity * nav_ref * (mark / pos.avg_entry_price - 1.0)` (UNCHANGED).
   Preserve the no-mark fallback (skip → 0) and the avg_entry<=0 skip in both.
4. cs32 in the same pass: introduce `n_considered`, increment it once per non-skipped position, use it for the label denominator and as the reported `n_positions`.
5. NAV-fraction byte-identity rail: every existing test in test_portfolio_state_accounting.py builds positions via single-leg `apply_execution` with `fill_size_pct` and NO `reactor_metadata.quantity` ⇒ they stamp 'nav_fraction' ⇒ hit the unchanged formula ⇒ same numbers (e.g. SMCI −19,184.65 lock at :50, long +3000 at :174). The only existing-test expectation that changes is the bad-avg `n_positions` (cs32: 1 → 0).

RED proof to add: a real-shares long (50 sh @100, mark 110) and short (−50 sh @100, mark 90/110) book gives the WRONG MTM today (500,000 / off by ~1000×) and the CORRECT value (500 / +500 / −500) after the fix; a us_option real-shares position proves the ×100 multiplier; a NAV-fraction book stays byte-identical; a 2-position book where one has avg_entry<=0 and the other is marked reports `equity_basis == "mark"` (cs32) instead of "mixed".

---

## Evidence index (file:line)
- get_marked_equity body + NAV-fraction-only formula: portfolio_state.py:948-1025 (formula :1000, docstring :972-974)
- positions table schema (NO unit column): portfolio_state.py:175-183
- get_positions SELECT (the read path's only fields): portfolio_state.py:909-928
- Position dataclass fields: state/positions.py:42-47
- write-fold unit predicate (incremental): portfolio_state.py:680-703
- write-fold unit predicate (rebuild): portfolio_state.py:1183-1228
- contract multiplier constant: portfolio_state.py:82
- single-leg paper.py writes NO reactor_metadata.quantity (nav-fraction source): react/paper.py:396-405
- multileg writes reactor_metadata.quantity (real-shares source, equity AND us_option): react/multileg.py:586,603,616
- alpaca_paper writes reactor_metadata.quantity (real-shares source): react/alpaca_paper.py:266
- cs32 bug: skip at :993-994, n_positions=len(positions) at :1007
- orphan: only callers are comments cli/status.py:595 + react/paper.py:123; all invocations in tests
