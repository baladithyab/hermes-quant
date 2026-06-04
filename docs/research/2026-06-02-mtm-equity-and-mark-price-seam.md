# 2026-06-02 — MTM equity_total and mark-price seam in hermes-quant

## 1. Bug summary and current behavior

**Symptom.** `hermes_quant/state/portfolio_state.py` persists `cash.equity_total` to `state.db` using *average entry price* rather than mark price, in two places:

1. **Full reconstruction** (`PortfolioState.reconstruct_from`, lines ~372–380):

```python
# equity_total: cash + open position notionals
 equity = balance + sum(
     abs(p["quantity"]) * p["avg_entry_price"]
     for (a, _, _), p in positions.items()
     if a == acct and abs(p["quantity"]) >= 1e-12
 )
```

2. **Incremental apply** (`PortfolioState._apply_execution_unsafe`, lines ~586–595):

```python
# equity_total: recompute from all positions for this account
# (approximation: use avg_entry_price, not mark price)
all_pos = conn.execute(
    "SELECT quantity, avg_entry_price FROM positions "
    "WHERE account_id=? AND ABS(quantity) >= 1e-12",
    (acct,),
).fetchall()
equity = new_cash + sum(
    abs(float(p["quantity"])) * float(p["avg_entry_price"]) for p in all_pos
)
```

In both, `equity_total` is effectively **cash + sum(|qty| × avg_entry_price)**, with magnitude-only quantity. This diverges from the protocol contract:

- `hermes_quant/protocol.py` `Portfolio` dataclass (line ~262):

```python
@dataclass(frozen=True)
class Portfolio:
    ...
    equity_total: float  # cash + sum(positions.mark_value)
```

- `Portfolio.current_position_pct` (line ~295) computes exposure using **mark price** and **signed qty**:

```python
if pos is None or self.equity_total <= 0:
    return 0.0
return (pos.qty * pos.mark_price) / self.equity_total
```

So `equity_total` is documented as **mark-to-market book equity**, but the SQLite cache stores a rough **cost-basis notionals** proxy instead, and ignores shorting sign. This gap is currently mostly hidden because the main mark-to-market consumer (`daemon.portfolio_loader`) recomputes equity independently from executions + quote marks rather than trusting `state.db`.

## 2. Existing correct MTM implementation (daemon path)

`hermes_quant/daemon/portfolio_loader.py` already implements a **correct MTM fold** of `executions.jsonl` plus a mark-price seam:

```python
from hermes_quant.protocol import Portfolio, Position


def reconstruct_portfolio(
    account_id: str,
    asset_class: str,
    *,
    initial_cash: float = 100_000.0,
    asof: pd.Timestamp | None = None,
    bus_path: Path = EXECUTION_BUS_PATH,
    n_records: int = 100_000,
    mark_prices: dict[str, float] | None = None,
) -> Portfolio:
    ...
    mark_prices = mark_prices or {}
    records = read_jsonl_tail(bus_path, n=n_records)
    matching = [r for r in records
                if r.get("account_id") == account_id
                and r.get("asset_class") == asset_class
                and r.get("schema_version") == 1]
    ...
    positions_qty: dict[str, float] = defaultdict(float)
    positions_cost: dict[str, float] = defaultdict(float)
    positions_last_fill: dict[str, float] = {}
    ...
    for rec in matching:
        ...
        signed_qty = qty if side == "buy" else -qty
        notional = signed_qty * fill
        ...  # update positions_qty / positions_cost, realized P&L, fees, cash

    positions: dict[str, Position] = {}
    for asset, qty in positions_qty.items():
        if abs(qty) < 1e-12:
            continue
        avg_entry = positions_cost[asset] / qty if qty != 0 else 0.0
        mark = mark_prices.get(asset, positions_last_fill.get(asset, avg_entry))
        unrealized = (mark - avg_entry) * qty
        positions[asset] = Position(
            asset=asset,
            qty=qty,
            avg_entry_price=avg_entry,
            mark_price=mark,
            unrealized_pnl=unrealized,
            realized_fees=0.0,
        )

    equity_total = cash + sum(p.qty * p.mark_price for p in positions.values())
```

Key properties of this implementation:

- **Signed quantities** (`qty` long, `-qty` short) so `p.qty * p.mark_price` gives **net** contribution.
- **Mark-price seam** via `mark_prices: dict[str, float]` param, falling back to last fill or avg entry.
- **No dependence on `state.db`**: `Portfolio` is reconstructed directly from the authoritative `executions.jsonl` log (per ADR-0085).

This is the **reference behavior** that other MTM views should align with.

## 3. ADR-0085 constraints: ledger authority and state.db as projection

ADR-0085 (“executions.jsonl is the authoritative event log; state.db is a derived projection”) sets the ground rules:

- `executions.jsonl` is **canonical**, immutable, append-only.
- `state.db` `positions`/`cash` are a **reconstructable cache**: any row must be derivable by folding the log.
- Reporting tools must read the **authoritative source** for each fact and avoid presenting stale/derived data as canonical.
- Tests are sandboxed away from live `state.db`.

Critically for this bug:

- ADR-0085 is purely about **data authority**, not MTM vs cost-basis semantics. It does *not* require that `state.db.cash.equity_total` be MTM — only that it be reconstructable from executions.
- The **daemon** already chooses to derive MTM values *outside* `state.db`, directly from executions plus a mark-price seam.

This argues strongly **against** pulling live mark quotes into the `PortfolioState` write path: `state.db` is a cheap, local projection; quotes are external data with their own lifecycles and failure modes.

## 4. How marks are obtained today

There are two distinct mark-price seams today:

1. **Daemon / risk loop path** — via the abstract `DataProvider` protocol and its yfinance implementation:

   - `hermes_quant/protocol.py` defines `DataProvider`:

     ```python
     @runtime_checkable
     class DataProvider(Protocol):
         name: str
         ...  # fetch_bars, fetch_latest, etc.
     ```

   - `hermes_quant/data/yfinance_provider.py` implements `YFinanceProvider`, which retrieves OHLCV bars from yfinance with retry and lookahead guards. This is used via dependency injection (entry points) in various CLI and analyst paths, and is **network-bound by design**.

2. **EOD portfolio report script** — `ops/scripts/quant-portfolio-daily.py`:

   - Reads positions **directly from `state.db.positions`** (not from executions):

     ```python
     def load_positions(account: str = "paper-default") -> list[dict]:
         conn = sqlite3.connect(STATE_DB_PATH)
         rows = conn.execute(
             "SELECT symbol, quantity, avg_entry_price, last_update_at "
             "FROM positions WHERE account_id = ? AND quantity != 0",
             (account,),
         ).fetchall()
         ...
     ```

   - Fetches **marks via yfinance** in-process (not via `DataProvider` abstraction):

     ```python
     def load_marks(symbols: list[str]) -> tuple[dict[str, float], dict[str, float]]:
         """Return (current_marks, prev_close_marks) via yfinance batch."""
         import yfinance as yf
         tickers_str = " ".join(symbols)
         data = yf.Tickers(tickers_str)
         ...
         marks[sym] = float(px)
         prev_close[sym] = float(pcl)
     ```

   - Computes per-position MTM metrics from those marks and the avg entry prices used in `state.db`:

     ```python
     unreal = (mark - avg) * qty
     unreal_pct = (mark - avg) / avg * (1 if qty > 0 else -1) if avg else 0.0
     today_pnl = (mark - pcl) * qty if pcl is not None else None
     ```

   - Summarizes exposure using **mark-based market values**:

     ```python
     long_mv = sum(p["market_value"] for p in enriched
                   if p["market_value"] is not None and p["qty"] > 0)
     short_mv = sum(p["market_value"] for p in enriched
                    if p["market_value"] is not None and p["qty"] < 0)
     gross_exposure = long_mv + abs(short_mv)
     net_exposure = long_mv + short_mv
     ```

   So the EOD report **already treats `state.db` as a cost-basis inventory** (symbol / qty / avg_entry) and sources mark prices from yfinance at **read time** to compute MTM P&L and exposure.

Net effect today:

- `state.db.cash.equity_total` is *entry-based* and internally inconsistent (unsigned qty), but:
  - The **daemon** does not use it.
  - The **EOD report** ignores it, computing its own MTM exposure from positions + yfinance.
- The mismatch is mostly a **contract-violation landmine**: anything that naively treats `CashState.equity_total` as MTM (per protocol docstring) will get the wrong answer.

## 5. Design questions and options

We need to decide:

1. **Where should mark prices come from** when computing MTM equity?

   - Option A: `PortfolioState` **pulls live quotes** in its write path (reconstruct/apply) and persists true MTM `equity_total` in `state.db`, matching the protocol contract literally.
   - Option B: `state.db` stays **network-free**, storing only execution-derived values (cash, avg_entry-based inventory), and MTM is always computed via a **separate read-time path** with an injected quote provider.

2. **How should the API surface this MTM view?**

   - Option B1: Change `state.db` schema / semantics so `cash.equity_total` is explicitly labeled as **cost-basis equity** and add a separate function `get_marked_equity(account, mark_prices)` that returns true MTM equity.
   - Option B2: Keep `cash.equity_total` as-is but treat it as **deprecated / for migration only**, and push all new consumers to use `daemon.portfolio_loader.reconstruct_portfolio` or a shared helper instead.

3. **How does this interact with ADR-0085 and existing EOD tooling?**

   - ADR-0085 explicitly pushes for **network-free, replayable reconstruction** from executions; adding yfinance calls into `PortfolioState` breaks that posture.
   - EOD script already does the right thing conceptually (marks at read time) but re-implements the mark seam (direct yfinance usage) instead of reusing the `DataProvider` abstraction or the daemon fold.

Below are the main options with pros/cons and how they relate to the “money-software addendum” (no network in hot paths, minimal failure surfaces on the money path).

### Option A — Fetch live marks inside PortfolioState writes

**Idea.** Modify `PortfolioState.reconstruct_from` and `_apply_execution_unsafe` to:

- Consult a `DataProvider` (e.g., `YFinanceProvider`) for mark prices for all symbols in current positions.
- Compute `equity_total = cash + sum(qty * mark_price)` with signed quantities.
- Persist this MTM `equity_total` in `cash` rows.

**Implications.**

- **Pros:**
  - `CashState.equity_total` becomes truly MTM and matches the protocol comment.
  - Consumers that only see `state.db` (e.g., thin CLI viewers) get MTM as a single read.

- **Cons:**
  - Violates ADR-0085’s “projection from executions” ethos by pulling **external, non-replayable state** (quotes) into the projection.
  - Introduces **network calls in the money-path write loop**, which is explicitly called out as an anti-pattern in the design notes: writes become non-deterministic (depending on quote timing) and can fail due to transient network issues.
  - `equity_total` in `state.db` becomes **non-reconstructable** from `executions.jsonl` alone; you also need a historical quote archive to replay exactly, which the system does not currently maintain.
  - You now have two distinct mark seams: one in `PortfolioState`, another in `daemon.portfolio_loader` and EOD scripts. Keeping them consistent is hard.

Given the “money-software” constraints and ADR-0085, Option A is a **non-starter**: it pollutes the authoritative ledger projection with external, side-effectful dependencies.

### Option B — Keep `state.db` entry-based; MTM at read-time only

**Idea.** Treat `state.db` as a **pure execution-derived cache**:

- `positions`: symbol / signed quantity / avg_entry_price, reconstructable purely from fills.
- `cash.balance_usd`: execution-derived cash.
- `cash.equity_total`: *also* execution-derived, but should have a clear meaning (see below).

Then, define one or more **read-time MTM views** that compute `equity_total` and related metrics from `positions` + `cash.balance_usd` + **mark prices provided via an explicit seam** (DataProvider or pre-fetched marks). The daemon’s `reconstruct_portfolio` already does this correctly for the main risk loop.

There are two sub-questions:

1. What should `cash.equity_total` mean in `state.db`?
2. How should new MTM consumers access a consistent mark-aware view?

#### B.1 — Redefine cash.equity_total as cost-basis equity

**Proposal.**

- Clarify in docs and type comments that **`state.db.cash.equity_total` is cost-basis equity**, not MTM:

  > `equity_total`: execution-derived “cost basis equity” = `cash.balance_usd + sum(abs(qty) * avg_entry_price)`; used only inside `PortfolioState` for internal invariants / checks. Do not treat as MTM.

- Fix the **sign bug** by either (a) persisting a signed “gross notional” field in the future, or more conservatively (b) treating `cash.equity_total` as **implementation detail / deprecated** and not exposing it through high-level APIs.

- Introduce a **new read API** to compute true MTM equity:

  - Either in `PortfolioState` itself, or as a thin helper module in `hermes_quant/state` or `hermes_quant/daemon`, expose something like:

    ```python
    def compute_marked_equity(
        cash_balance: float,
        positions: Mapping[str, PositionRow],  # from state.db (qty, avg_entry)
        mark_prices: Mapping[str, float],
    ) -> float:
        """Return MTM equity: cash_balance + sum(qty * mark)."""
    ```

  - Or a higher-level façade:

    ```python
    def get_marked_portfolio(
        account_id: str,
        asset_class: str,
        *,
        mark_prices: Mapping[str, float],
    ) -> Portfolio:
        ...  # read from state.db, then call compute_marked_equity
    ```

- Update EOD and inspection tooling to use **one shared helper** for MTM equity and exposure, rather than re-implementing the fold.

**Pros.**

- Keeps `state.db` **network-free, replayable and derivable** from executions only.
- Aligns with ADR-0085: quotes are treated as **separate, per-view data**, not part of the ledger projection.
- Encourages all MTM consumers to **go through a single function** with an explicit mark-price seam, reducing drift.
- Eases testing: you can pass synthetic `mark_prices` in unit tests without mocking yfinance.

**Cons.**

- Leaves a subtle historical mismatch in the `Portfolio` protocol docstring (`equity_total` comment). We would either need to:
  - Clarify that `Portfolio` from `daemon.portfolio_loader` is MTM, while `CashState.equity_total` in `state.db` has different semantics; or
  - Introduce a separate “state view” type for `state.db` projections vs the MTM `Portfolio` used by the daemon.
- Existing code that **already reads `CashState.equity_total` and assumes MTM** would need to be audited and likely changed.

Given the small number of `CashState` consumers (most code reads only positions + balances), this seems tractable.

#### B.2 — Treat cash.equity_total as deprecated and route consumers to daemon view

**Variant.**

- Leave `PortfolioState`’s internal `equity_total` computation as-is but explicitly mark it **deprecated for external consumption**:
  - Update docstrings and maybe type hints to say “do not rely on `cash.equity_total`; use `daemon.portfolio_loader.reconstruct_portfolio` for MTM views”.
- For new features, **never expose `CashState` directly**; instead, provide functions that return `Portfolio` dataclasses from the daemon fold.

**Pros.**

- Minimal immediate change; avoids touching the money-path reconstruction code.

**Cons.**

- Leaves a persistent **foot-gun** in the codebase: a field named `equity_total` that is not MTM, is unsigned, and is easy to misuse.
- Doesn’t resolve the contract drift between `protocol.Portfolio.equity_total` and `CashState.equity_total` semantics.

Given recent issues uncovered by adversarial reviews, leaving such a landmine is undesirable.

## 6. Recommended design

Given ADR-0085, the existing daemon implementation, and the EOD script’s current behavior, the cleanest path is:

> **Keep `state.db` free of live mark prices and treat `equity_total` there as a legacy / cost-basis field; provide a single, explicit MTM view function that computes `equity_total` from positions + cash + mark prices, and migrate reporting / risk code to use it.**

Concretely, the design has three parts.

### 6.1. Clarify semantics and nudge away from state.db.equity_total

**File:** `hermes_quant/state/portfolio_state.py`

- Update docstrings around the `cash` schema and `get_cash` to something like:

  - For the `cash` table schema:

    ```sql
    -- equity_total: execution-derived cost-basis equity
    --   = balance_usd + sum(abs(qty) * avg_entry_price) over open positions.
    --   Used internally for migration and sanity-checks only; NOT mark-to-market.
    ```

  - For the `CashState` dataclass (in `hermes_quant/state/positions.py`):

    ```python
    equity_total: float  # cost-basis equity from state.db; not MTM.
    ```

- Optionally, add a small **warning log** or deprecation comment in any code path that exposes `CashState.equity_total` to higher-level APIs.

**Tradeoff:** this makes it explicit to maintainers that `state.db` is not the MTM truth and discourages future consumers from misusing the field, while avoiding any behavior change in the hot path.

### 6.2. Introduce a shared MTM helper on top of state.db

**New helper module (proposal):** `hermes_quant/state/mtm_view.py` (name illustrative).

- Export a small, pure, deterministic function that composes:

  - `PortfolioState.get_positions(account_id)` → provides `(asset_class, symbol) → PositionRow` with `quantity` (signed) and `avg_entry_price`.
  - `PortfolioState.get_cash(account_id)` → provides `balance_usd`.
  - `mark_prices: Mapping[str, float]` (from a **call-site-specific provider** — daemon, EOD script, or CLI).

- Example **function signatures only** (no production code here):

  ```python
  from collections.abc import Mapping
  from hermes_quant.state.positions import Position as DbPosition, CashState
  from hermes_quant.protocol import Portfolio, Position

  def build_marked_portfolio(
      account_id: str,
      asset_class: str,
      *,
      state: PortfolioState,
      mark_prices: Mapping[str, float],
      initial_cash: float | None = None,
  ) -> Portfolio:
      """Return a mark-to-market Portfolio by combining state.db and marks.

      - Reads positions and cash from state.db (no network).
      - Uses caller-supplied mark_prices for MTM.
      - Fills any missing marks with avg_entry_price or last fill price
        (matching daemon.portfolio_loader fallback semantics).
      """
  ```

- The implementation should **mirror `daemon.portfolio_loader.reconstruct_portfolio` semantics** as closely as possible for mark choice and signed quantities. The main difference is that instead of folding the executions log to derive pos/cash, it reads from `state.db`’s derived projection.

- For non-daemon consumers that don’t need the full `Portfolio` dataclass, a narrower helper may be useful:

  ```python
  def compute_marked_equity_from_state(
      account_id: str,
      *,
      state: PortfolioState,
      mark_prices: Mapping[str, float],
  ) -> float:
      """Return MTM equity: cash.balance_usd + sum(qty * mark_price)."""
  ```

**Tradeoffs.**

- **Pros:**
  - Provides a single, unit-testable seam for computing MTM equity from state.db.
  - Keeps `PortfolioState` itself network-free and aligned with ADR-0085.
  - Allows callers (daemon, EOD scripts, CLIs) to plug in different **mark sources** (live yfinance, cached prices, test fixtures) without changing the ledger.

- **Cons:**
  - Introduces another layer of abstraction; maintainers need to choose between **fold-from-executions** vs **read-from-state** depending on their use case.
  - Risk of divergence between `build_marked_portfolio` and `daemon.portfolio_loader.reconstruct_portfolio` if not carefully tested — mitigated by designing tests that compare the two against the same executions + marks.

### 6.3. Standardize mark-price sourcing at read time

We should reduce duplicate yfinance wiring and push reads toward the `DataProvider` abstraction.

**File:** `hermes_quant/daemon/portfolio_loader.py`

- **No change in behavior** needed. This file already:
  - Uses an explicit `mark_prices` parameter, supplied by the caller.
  - Folds executions to a MTM `Portfolio` independently of `state.db`.

**File:** `ops/scripts/quant-portfolio-daily.py`

- Today, this script does its own yfinance calls and MTM math. To align with the shared seam, future revisions should:

  - Replace direct yfinance usage with a call to a **thin adapter** that returns `mark_prices: dict[str, float]` for a list of symbols, using e.g. `YFinanceProvider` or another `DataProvider`.
  - Then call `build_marked_portfolio` or `compute_marked_equity_from_state` (once implemented) instead of re-implementing `unreal = (mark - avg) * qty` and exposure calculations.

- This would yield a consistent MTM view for both the daemon and the EOD report, while keeping marks decisively **read-time only**.

**Tradeoffs.**

- **Pros:**
  - Reduces duplicated mark logic and encourages a single “how we interpret marks” policy.
  - Keeps EOD behavior (yfinance marks at read time) conceptually unchanged.

- **Cons:**
  - Requires careful refactor and tests to avoid breaking the existing daily snapshot format.

## 7. Answering the key questions explicitly

1. **Where should mark prices come from when writing `state.db`?**

   - They **should not** come from any live quote provider. `PortfolioState.reconstruct_from` and `_apply_execution_unsafe` should remain **purely execution-driven** and offline-replayable, with no network calls. Mark prices belong in **read-time/report-time views** only.

2. **Should `equity_total` in `state.db` stay entry-based, with a separate MTM view?**

   - Yes. Treat `state.db.cash.equity_total` as an **entry-based / cost-basis artifact** and **do not rely on it for MTM**. Introduce a separate read API (`compute_marked_equity_from_state` or `build_marked_portfolio`) that computes true MTM equity from `state.db` + marks.

3. **How does this interact with the EOD portfolio report and existing mark seams?**

   - The EOD script already follows the desired pattern conceptually: it reads positions from `state.db` and **fetches yfinance marks at read time** to compute MTM P&L and exposure. The recommended design simply:
     - Encapsulates that MTM computation in a reusable helper.
     - Moves mark fetching toward the shared `DataProvider` abstraction instead of ad hoc yfinance calls.
     - Clarifies that `state.db` is never assumed to contain live marks.

4. **What single design is recommended overall?**

   - **Design summary:**
     - Keep `PortfolioState` write paths network-free and purely derived from `executions.jsonl`.
     - Reinterpret `state.db.cash.equity_total` as cost-basis-only and de-emphasize it in external APIs.
     - Add a shared helper that builds a mark-to-market `Portfolio` (or just MTM equity) from `state.db` + externally supplied mark prices.
     - Migrate EOD and future reporting/risk tooling to rely on that helper, sourcing marks via the existing `DataProvider`/`YFinanceProvider` seam or equivalent.

   This design fixes the **equity_total MTM contract gap** without compromising ADR-0085’s event-sourced ledger guarantees or introducing network calls into the money-path projection.
