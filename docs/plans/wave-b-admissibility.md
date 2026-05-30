# Wave B — Admissibility / Fidelity Foundation (ADR-0077) — IMPLEMENTATION PLAN

> **Status:** ready to build · **Date:** 2026-05-30 · **Track:** Wave B (fidelity foundation, the six-model P0)
> **Grounds:** [ADR-0077](../adr/ADR-0077-pretrade-admissibility-shortability.md) (pre-trade admissibility + ShortabilityOracle),
> [ADR-0078](../adr/ADR-0078-order-lifecycle-fills-idempotency.md) (sibling — consumes the `REJECTED` verdict; out of scope here),
> research [`2026-05-30-r-admissibility-shortability.md`](../research/2026-05-30-r-admissibility-shortability.md).
>
> **A fresh agent can build this end-to-end from this document with no further research.** All file paths are
> absolute-from-repo-root. All money-software rails (silence-by-default, deterministic-gate-is-final-authority,
> default-OFF behind a `HERMES_QUANT_*` flag, UTC + `asof` honesty) are baked into the acceptance criteria.

---

## 0. The rails this plan must not break (read before coding)

1. **Silence-by-default / fail-closed.** Unknown shortability ⇒ **REJECT** the short. Missing context, oracle
   error, or a `None` ctx field ⇒ REJECT, never "assume admissible." The bug we are fixing is precisely the
   de-facto `NullShortabilityOracle` (everything admissible).
2. **Admissibility is NOT authority.** The oracle sits **upstream** of the ADR-0004 risk gate as a hard
   precondition. It can only **REJECT** a proposed order, or **flatten** an inadmissible held short to `0.0`.
   It can **never** increase a target, never override a gate REJECT, never amplify. A property test asserts
   this forever (the resulting target magnitude is `<=` the pre-admissibility target magnitude, for every verdict).
3. **Default-OFF.** Two independent flags, both unset by default:
   - `HERMES_QUANT_ADMISSIBILITY=1` → activates the pre-trade gate (the `AlpacaShortabilityOracle` / static path).
   - `HERMES_QUANT_BORROW_COST=1` → activates the daily borrow-carry accrual.
   With both unset, `NullShortabilityOracle` is selected and behavior is **bit-for-bit identical to today.**
4. **No fake precision.** Alpaca exposes shortability as a **boolean** (`easy_to_borrow`), no `shortable_shares`
   count, no HTB rates. Model is binary at the admissibility layer (ETB-admissible / else-REJECT) + a coarse
   ETB borrow APR (~0.30%). We **refuse** HTB names rather than invent a precise HTB fee.
5. **`asof` honesty.** Shortability/CBR are point-in-time. Backtest admissibility uses the value *as of decision
   time* via the static-allowlist path keyed by `asof`, never `today`'s `easy_to_borrow`. UTC end-to-end.
6. **Whole-share enforcement for shorts.** A fractional short is rejected (mirrors live HTTP 422). The discrete
   `±0.05…±0.20 × NAV` ladder converts to a share count that must `floor()` to whole shares for shorts.

---

## 1. Codebase idioms to copy (verified, load-bearing)

### 1.1 Flag-gating idiom (copy verbatim)
The canonical default-OFF idiom in this repo (`hermes_quant/react/paper.py:205`, `advisor.py:362`,
`autonomous.py:335`):

```python
import os
if os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") == "1":
    ...        # gate active
# else: NullShortabilityOracle path — behavior identical to today
```

Use the **exact** string form `os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") == "1"`. Do NOT use truthiness
on the raw value (existing tests assert this idiom).

### 1.2 Alpaca `TradingClient` + `get_asset` idiom (copy from `hermes_quant/universe/alpaca_scanner.py`)
- Credentials (lines 65–77): accept either env-name pair, fail closed if missing:
  ```python
  key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
  secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET_KEY")
  if not key or not secret:
      raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET required")
  ```
- Client (line 80): `TradingClient(api_key=key, secret_key=secret, paper=True)`.
- Asset fetch: `client.get_asset(symbol)` → an `alpaca.trading.models.Asset` with fields
  `tradable, marginable, shortable, easy_to_borrow, fractionable: bool`, `margin_requirement_short: str`,
  `attributes: list[str]`. (Confirmed in research §1.) **Import lazily** inside the method so the package
  imports without `alpaca-py` installed (mirror `tools.py:977` lazy-import + `scanner` top-level import only in
  the live-only module). `alpaca-py>=0.30` is already a declared optional dep (`pyproject.toml:45`).

### 1.3 `state.db` positions schema (read-side for the offline restatement)
`hermes_quant/state/portfolio_state.py:97` — the `positions` table is the source of the 38 shorts:
```sql
positions(account_id TEXT, asset_class TEXT, symbol TEXT,
          quantity REAL,         -- SIGNED: <0 == short
          avg_entry_price REAL, last_update_at TEXT,
          PRIMARY KEY (account_id, asset_class, symbol))
```
A short is `quantity < 0`. Read via `PortfolioState(state_db_path=...).get_positions(account_id)` →
`dict[tuple[str, str], hermes_quant.state.positions.Position]` keyed by `(asset_class, symbol)`; iterate
`.values()` and filter `p.is_short` (use `Position.is_short`, `Position.quantity`, `Position.avg_entry_price`).
The restatement script reads this table; it MUST NOT write to it (it is a measurement artifact).

### 1.4 Decision-path seam (where the gate wires in)
`hermes_quant/autonomous.py` — the fire path computes `effective_size` then calls `_react(...)` at line 464.
The admissibility check inserts **between** `effective_size` being finalized and the `_react` call (Phase 2
wiring, default-OFF). For this Wave-B plan the wiring is **opt-in and minimal**: the oracle module + offline
restatement land first; the autonomous-loop wiring is a small guarded block (§4.4) that is a no-op when the
flag is OFF.

### 1.5 New-module header idiom (copy from `hermes_quant/shadow/pmcc.py`)
Module docstring states: what it is, which ADR gap it closes, what it does NOT write, and the rails it honors.
`from __future__ import annotations`; `logger = logging.getLogger(__name__)`; dataclasses with `frozen=True`
for value objects.

### 1.6 ops-script re-exec idiom (copy from `ops/scripts/quant-catalyst-profitability.py:16`)
```python
_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    import os; os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])
```

---

## 2. New files (exact list)

| Path | Purpose |
|---|---|
| `hermes_quant/admissibility/__init__.py` | Package exports: enums, dataclasses, oracle Protocol + 3 impls, factory, borrow-carry fns. |
| `hermes_quant/admissibility/oracle.py` | `AdmissibilityState`, `ShortabilityVerdict`, `AdmissibilityContext`, `ShortabilityOracle` Protocol, `NullShortabilityOracle`, `AlpacaShortabilityOracle`, `StaticETBAllowlistOracle`, `select_oracle()` factory (flag-gated). |
| `hermes_quant/admissibility/order_state.py` | `Side`, whole-share conversion `target_pct_to_shares()`, `apply_verdict_to_target()` (the REJECT/flatten-only adjuster). NOTE: this is the *admissibility-side* order helper; the full ADR-0078 `OrderState` machine lives separately at `hermes_quant/react/order_state.py` and is OUT OF SCOPE for Wave B. This file is named `order_state.py` per the task and holds only the admissibility→sizing bridge. |
| `hermes_quant/admissibility/borrow_pnl.py` | `daily_borrow_fee()`, `payment_in_lieu()`, `accrue_borrow_carry()` over a held-short series. Gated by `HERMES_QUANT_BORROW_COST`. |
| `ops/scripts/quant-admissibility-restate.py` | Offline borrow-aware restatement of the 38-short book; reads `state.db` positions; the operator-audit / promotion artifact (rollout phase 2). |
| `tests/unit/test_admissibility_oracle.py` | Verdict invariants: unknown→REJECT short, fractional→REJECT, ETB whole-share→ACCEPT, flag-OFF bit-identical, authority-boundary property test. |
| `tests/unit/test_admissibility_borrow_pnl.py` | Borrow-carry math: /360, Friday ×3, PIL on ex-div, longs accrue zero. |
| `tests/unit/test_admissibility_restate.py` | The restatement script end-to-end against a synthetic `state.db` of fake shorts (no network). |

> The `AlpacaShortabilityOracle` **live** network path is exercised only in an integration test guarded by
> `--run-integration` (skipped in CI); the unit suite injects a fake `get_asset` callable. Keep the live module's
> `alpaca` import lazy so the package imports without `alpaca-py`.

---

## 3. Module contracts (dataclasses + signatures — build to these exactly)

### 3.1 `hermes_quant/admissibility/oracle.py`

```python
"""hermes_quant.admissibility.oracle — pre-trade admissibility + ShortabilityOracle (ADR-0077).

A hard, deterministic, fail-closed precondition that sits UPSTREAM of the ADR-0004 risk gate.
It can only REJECT a proposed order (or flatten an inadmissible held short -> 0.0). It can NEVER
force, amplify, or override a trade. Adopts QuantConnect Lean's IShortableProvider tri-state shape.

Gated by HERMES_QUANT_ADMISSIBILITY (default OFF -> NullShortabilityOracle == today's behavior,
bit-for-bit). Borrow carry is a SEPARATE flag (HERMES_QUANT_BORROW_COST) in borrow_pnl.py.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ETB default borrow APR used for carry accrual when easy_to_borrow=True.
# Research §3: ETB ~ 0.25-1.00% APR; we use a single coarse 0.30% default and
# REJECT (never fake a rate for) non-ETB names.
ETB_DEFAULT_ANNUAL_CBR: float = 0.0030

# Reg-T short minimum account equity (research §2). Below this there is no short capability.
MIN_SHORT_ACCOUNT_EQUITY_USD: float = 2_000.0

# Reg-T short initial buying-power requirement multiple (150% of short market value).
REG_T_SHORT_INITIAL_BPR_MULT: float = 1.50

# Alpaca opening-short order value = MAX(limit, 1.03 * ask) * qty (research §2).
ALPACA_SHORT_ASK_MULT: float = 1.03


class AdmissibilityState(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


# Typed rejection/partial reasons. None only on ACCEPTED.
REASON_NOT_SHORTABLE = "NOT_SHORTABLE"
REASON_NOT_ETB = "NOT_ETB"
REASON_FRACTIONAL_SHORT = "FRACTIONAL_SHORT"
REASON_NOT_MARGINABLE = "NOT_MARGINABLE"
REASON_INSUFFICIENT_BPR = "INSUFFICIENT_BPR"
REASON_EQUITY_BELOW_2K = "EQUITY_BELOW_2K"
REASON_PTP_BLOCKED = "PTP_BLOCKED"
REASON_SSR_MARKETABLE_SHORT = "SSR_MARKETABLE_SHORT"
REASON_UNKNOWN_SHORTABILITY = "UNKNOWN_SHORTABILITY"  # fail-closed: ctx/oracle could not determine


@dataclass(frozen=True)
class AdmissibilityContext:
    """Point-in-time inputs the oracle needs. All optional EXCEPT what the
    requested side needs; a missing field that the side requires => fail-closed REJECT.

    asset fields mirror alpaca.trading.models.Asset (research §1).
    """
    # --- asset shortability (from get_asset() or the static allowlist) ---
    tradable: bool | None = None
    marginable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    fractionable: bool | None = None
    attributes: tuple[str, ...] = ()          # e.g. ("ptp_no_exception", "ipo", "has_options")
    margin_requirement_short: float | None = None  # per-asset short margin % (equities); 1.50 if None

    # --- pricing / account (buying-power check) ---
    current_ask: float | None = None          # for the MAX(limit, 1.03*ask)*qty BP charge
    limit_price: float | None = None           # None => market order
    available_bp: float | None = None          # account buying power
    account_equity: float | None = None        # for the < $2,000 gate

    # --- borrow APR for carry (longs => 0.0; ETB => ETB_DEFAULT_ANNUAL_CBR) ---
    annual_cbr: float | None = None

    # --- SSR (Reg SHO Rule 201): conservative posture, no NBB tick data ---
    ssr_active: bool = False                   # latched true once intraday low <= prev_close*0.90
    is_marketable: bool = True                 # marketable short during SSR => deferred/partial


@dataclass(frozen=True)
class ShortabilityVerdict:
    state: AdmissibilityState
    reason: str | None        # one of REASON_* above, or None on ACCEPTED
    annual_cbr: float         # cost-to-borrow APR for carry accrual (0.0 for longs / non-shorts)


@runtime_checkable
class ShortabilityOracle(Protocol):
    def verdict(
        self, symbol: str, side: str, qty: float, asof: datetime, ctx: AdmissibilityContext
    ) -> ShortabilityVerdict: ...


def _is_whole_share(qty: float) -> bool:
    # Tolerance-free integer check; fractional shorts are rejected (live HTTP 422).
    return float(qty) == math.floor(float(qty))


class NullShortabilityOracle:
    """Today's behavior == the bug. Everything ACCEPTED. Selected ONLY when the
    HERMES_QUANT_ADMISSIBILITY flag is OFF, preserving current outputs bit-for-bit."""

    def verdict(self, symbol, side, qty, asof, ctx) -> ShortabilityVerdict:
        return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)


class AlpacaShortabilityOracle:
    """Live source of truth. ctx is populated from TradingClient.get_asset(symbol).

    The class accepts an injectable `get_asset` callable (default: lazy real client) so the
    unit suite can drive it with a fake. On ANY error fetching the asset => fail-closed REJECT
    (UNKNOWN_SHORTABILITY)."""

    def __init__(self, get_asset=None, *, etb_cbr: float = ETB_DEFAULT_ANNUAL_CBR) -> None: ...
    def verdict(self, symbol, side, qty, asof, ctx) -> ShortabilityVerdict: ...


class StaticETBAllowlistOracle:
    """Offline / backtest. A point-in-time ETB set + per-name CBR table keyed by `asof`,
    so historical admissibility uses the value as of decision time (D-5). Honest about the
    limitation: a name with NO snapshot for `asof` => REJECT(NOT_ETB) (fail-closed, not 'assume')."""

    def __init__(self, snapshot: dict[str, "ETBSnapshotEntry"]) -> None: ...
    def verdict(self, symbol, side, qty, asof, ctx) -> ShortabilityVerdict: ...


@dataclass(frozen=True)
class ETBSnapshotEntry:
    symbol: str
    asof: str                  # ISO-8601 UTC date the snapshot applies to
    easy_to_borrow: bool
    shortable: bool
    marginable: bool
    annual_cbr: float          # ETB rate for this name as of `asof`


def evaluate_admissibility(
    symbol: str, side: str, qty: float, asof: datetime, ctx: AdmissibilityContext
) -> ShortabilityVerdict:
    """The shared deterministic core all oracles delegate to once ctx is populated.

    Evaluation order (research §4, roughly Alpaca's order). LONG / BUY side is always ACCEPTED
    (annual_cbr=0.0) — admissibility only constrains opening SHORTS:

      1. side != 'short'              -> ACCEPTED, cbr 0.0
      2. any required ctx field None  -> REJECTED, UNKNOWN_SHORTABILITY   (FAIL-CLOSED)
      3. not tradable/shortable/ETB   -> REJECTED, NOT_SHORTABLE / NOT_ETB
      4. not marginable               -> REJECTED, NOT_MARGINABLE
      5. account_equity < $2,000      -> REJECTED, EQUITY_BELOW_2K
      6. not whole-share              -> REJECTED, FRACTIONAL_SHORT
      7. ptp_no_exception in attrs    -> REJECTED, PTP_BLOCKED
      8. insufficient BP              -> REJECTED, INSUFFICIENT_BPR
      9. ssr_active and is_marketable -> PARTIAL,  SSR_MARKETABLE_SHORT
      10. else                         -> ACCEPTED, cbr = ctx.annual_cbr or ETB_DEFAULT_ANNUAL_CBR
    """
    ...


def select_oracle() -> ShortabilityOracle:
    """Factory honoring the flag. HERMES_QUANT_ADMISSIBILITY != '1' -> NullShortabilityOracle
    (bit-identical to today). When '1', returns AlpacaShortabilityOracle (live default).
    Offline callers (the restatement script, backtest) construct StaticETBAllowlistOracle directly."""
    if os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") == "1":
        return AlpacaShortabilityOracle()
    return NullShortabilityOracle()
```

**Buying-power check (step 8) formula** (research §2, must match exactly):
```python
order_value = max(ctx.limit_price or 0.0, ALPACA_SHORT_ASK_MULT * ctx.current_ask) * qty
reg_t_required = (ctx.margin_requirement_short or REG_T_SHORT_INITIAL_BPR_MULT) * (current_ask * qty)
# Insufficient if available_bp is known and is below EITHER the order value or the Reg-T requirement.
if ctx.available_bp is not None and ctx.available_bp < max(order_value, reg_t_required):
    -> REJECTED, INSUFFICIENT_BPR
# If available_bp is None we DO NOT fabricate sufficiency: BP is treated as 'unknown but not
# the gating dimension' only when current_ask is also None (pure shortability check). If current_ask
# is provided but available_bp is None, fail-closed REJECT(UNKNOWN_SHORTABILITY).
```

### 3.2 `hermes_quant/admissibility/order_state.py`

```python
"""hermes_quant.admissibility.order_state — admissibility -> sizing bridge (ADR-0077).

Converts a discrete NAV target into a whole-share short count, and applies a ShortabilityVerdict
to a proposed target. This module can ONLY shrink a target (REJECT -> 0.0, flatten -> 0.0). It can
NEVER increase one (the ADR-0004 authority boundary, enforced by a property test).

NOTE: this is NOT the ADR-0078 OrderState/OrderEvent machine (that lives at
hermes_quant/react/order_state.py and is out of scope for Wave B).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .oracle import AdmissibilityState, ShortabilityVerdict


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


def side_of(target_pct: float) -> Side:
    return Side.SHORT if target_pct < 0 else Side.LONG


def target_pct_to_shares(target_pct: float, nav: float, price: float) -> int:
    """Convert a signed NAV fraction to a SIGNED whole-share count.

    Shorts (target_pct < 0) floor toward zero in magnitude so a fractional short can never be
    emitted (live HTTP 422). Longs may be fractional elsewhere, but this helper returns whole
    shares for both so the admissibility path is uniform. price/nav must be > 0 (else 0 shares).
    """
    if price <= 0 or nav <= 0:
        return 0
    raw = (abs(target_pct) * nav) / price
    shares = math.floor(raw)              # floor magnitude -> never over-shorts, never fractional
    return -shares if target_pct < 0 else shares


@dataclass(frozen=True)
class AdmissibilityAdjustment:
    """Result of applying a verdict to a proposed target. `adjusted_target_pct` is ALWAYS
    such that abs(adjusted) <= abs(original) — the authority boundary."""
    original_target_pct: float
    adjusted_target_pct: float
    verdict: ShortabilityVerdict
    flattened_existing_short: bool = False


def apply_verdict_to_target(
    target_pct: float, verdict: ShortabilityVerdict, *, existing_position_qty: float = 0.0
) -> AdmissibilityAdjustment:
    """REJECT-only / flatten-only adjuster.

    - verdict ACCEPTED          -> adjusted = target_pct (unchanged)
    - verdict REJECTED / PARTIAL on an OPENING short -> adjusted = 0.0 (no order)
    - inadmissible HELD short (existing_position_qty < 0 and verdict not ACCEPTED)
                                -> adjusted = 0.0 (flatten), flattened_existing_short=True
    INVARIANT (asserted): abs(adjusted_target_pct) <= abs(target_pct). Never amplifies.
    """
    ...
```

### 3.3 `hermes_quant/admissibility/borrow_pnl.py`

```python
"""hermes_quant.admissibility.borrow_pnl — borrow-aware carry for shorts (ADR-0077 D77.3).

Daily borrow fee on short notional + dividend-on-short payment-in-lieu (PIL), so short P&L is
no longer fictitiously free (Alpaca paper does NOT charge borrow fees: "Borrow Fees: Coming Soon").

Gated by HERMES_QUANT_BORROW_COST (default OFF). Pure functions; no I/O. /360 stock-loan basis;
Friday accrues x3 (weekend). UTC dates only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

DAY_COUNT_BASIS: int = 360  # stock-loan money-market convention (research §3)


def borrow_cost_enabled() -> bool:
    return os.environ.get("HERMES_QUANT_BORROW_COST", "0") == "1"


def daily_borrow_fee(short_shares: float, close_price: float, annual_cbr: float, on: date) -> float:
    """abs(short_shares) * close_price * annual_cbr / 360, x3 on Friday (carries the weekend).

    short_shares is the SIGNED quantity (negative for shorts); longs / non-negative => 0.0.
    Returns a positive cost (a debit). `on` is the UTC calendar date the fee accrues for.
    """
    if short_shares >= 0 or close_price <= 0 or annual_cbr <= 0:
        return 0.0
    weekend_mult = 3 if on.weekday() == 4 else 1   # Friday=4 -> Fri+Sat+Sun
    return abs(short_shares) * close_price * annual_cbr / DAY_COUNT_BASIS * weekend_mult


def payment_in_lieu(short_shares: float, cash_dividend_per_share: float) -> float:
    """abs(short_shares) * dividend/share, debited on pay date if short across ex-div.
    Longs => 0.0. Returns a positive liability (a debit)."""
    if short_shares >= 0 or cash_dividend_per_share <= 0:
        return 0.0
    return abs(short_shares) * cash_dividend_per_share


@dataclass(frozen=True)
class BorrowAccrual:
    symbol: str
    total_borrow_fee: float
    total_pil: float
    days_held: int


def accrue_borrow_carry(
    symbol: str,
    short_shares: float,
    close_by_date: dict[date, float],     # UTC date -> close price (marks daily)
    annual_cbr: float,
    dividends: dict[date, float] | None = None,  # ex-div date -> cash dividend/share
) -> BorrowAccrual:
    """Sum daily_borrow_fee over each held UTC date in close_by_date, plus PIL on any ex-div
    date present in `dividends`. The total carry is a positive number to SUBTRACT from short P&L."""
    ...
```

### 3.4 `hermes_quant/admissibility/__init__.py`
Re-export the public surface so callers do `from hermes_quant.admissibility import select_oracle, AdmissibilityState, ...`:
```python
from .oracle import (
    AdmissibilityContext, AdmissibilityState, AlpacaShortabilityOracle,
    ETBSnapshotEntry, NullShortabilityOracle, ShortabilityOracle, ShortabilityVerdict,
    StaticETBAllowlistOracle, evaluate_admissibility, select_oracle,
    ETB_DEFAULT_ANNUAL_CBR, REASON_NOT_ETB, REASON_FRACTIONAL_SHORT, REASON_UNKNOWN_SHORTABILITY,
)  # + the remaining REASON_* constants
from .order_state import (
    AdmissibilityAdjustment, Side, apply_verdict_to_target, side_of, target_pct_to_shares,
)
from .borrow_pnl import (
    BorrowAccrual, accrue_borrow_carry, borrow_cost_enabled, daily_borrow_fee, payment_in_lieu,
)
__all__ = [ ... ]  # all of the above
```

---

## 4. Offline restatement script — `ops/scripts/quant-admissibility-restate.py`

The rollout-phase-2 promotion artifact (ADR-0077 §Rollout step 2; Verification block). Reads `state.db`
positions, classifies every short through an oracle, accrues borrow carry, and prints a per-symbol
accept/reject table + the count of `NOT_ETB`/`NOT_SHORTABLE` on the shorts + a restated short-P&L delta.
**Read-only on `state.db`** (measurement, never mutation).

### 4.1 CLI
```
quant-admissibility-restate.py
  --book PATH                 # state.db (default: ~/.hermes/quant/state.db)
  --account-id ID             # default: paper-default
  --asof-snapshot PATH        # JSON: { "asof": "2026-05-30", "etb": {SYMBOL: {easy_to_borrow,shortable,marginable,annual_cbr}} }
  --oracle {static,alpaca}    # default static (offline, no network). 'alpaca' uses live get_asset (needs creds).
  --json                      # machine-readable output (for the cron/operator-audit pipeline)
```

### 4.2 Behavior (deterministic, no network in `static` mode)
1. Re-exec into the venv (idiom §1.6).
2. `PortfolioState(state_db_path=Path(args.book)).get_positions(account_id)`; filter `p.is_short`.
3. Build the oracle: `static` → `StaticETBAllowlistOracle(snapshot)` from `--asof-snapshot`; `alpaca` →
   `AlpacaShortabilityOracle()` (real `get_asset`, fail-closed on error).
4. For each short, build `AdmissibilityContext` from the snapshot/asset and call `oracle.verdict(symbol,
   "short", abs(qty), asof, ctx)`. `asof` = `Position.last_update_at` parsed to UTC.
5. Borrow carry (when `HERMES_QUANT_BORROW_COST=1` OR `--json` always reports it for the audit): a coarse
   single-mark accrual using `avg_entry_price` as the daily close proxy and `ETB_DEFAULT_ANNUAL_CBR` (or the
   snapshot CBR), over the held-day count (`now - last_update_at`, calendar days). Honest caveat printed: this
   is a coarse one-mark estimate, not a daily mark-to-market (we lack the historical bar series here).
6. Output: a per-symbol row `(symbol, qty, verdict.state, verdict.reason, annual_cbr, est_borrow_carry_usd)`,
   then summary: `n_shorts`, `n_rejected`, `n_rejected_not_etb`, `total_est_borrow_carry_usd`, and
   `restated_note` describing how much of the book is inadmissible.

### 4.3 Output contract (the operator-audit artifact, `--json`)
```json
{
  "asof_snapshot": "2026-05-30",
  "account_id": "paper-default",
  "n_shorts": 38,
  "n_rejected": 31,
  "n_rejected_not_etb": 29,
  "n_accepted": 7,
  "total_est_borrow_carry_usd": 0.0,
  "rows": [
    {"symbol": "SMALLCAP", "qty": -100, "state": "REJECTED", "reason": "NOT_ETB",
     "annual_cbr": 0.0, "est_borrow_carry_usd": 0.0},
    {"symbol": "AAPL", "qty": -50, "state": "ACCEPTED", "reason": null,
     "annual_cbr": 0.003, "est_borrow_carry_usd": 1.23}
  ]
}
```
(Counts are illustrative; the real numbers are the audit deliverable.)

### 4.4 Autonomous-loop wiring (default-OFF; minimal, ships in this wave but is a no-op when flag OFF)
In `hermes_quant/autonomous.py`, just before the `_react(...)` call (line ~464), insert a guarded block:
```python
if os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") == "1" and effective_size < 0:
    from hermes_quant.admissibility import (
        AdmissibilityContext, apply_verdict_to_target, select_oracle,
    )
    oracle = select_oracle()
    # ctx populated from the live asset + portfolio snapshot already in scope.
    verdict = oracle.verdict(entry.symbol, "short", abs(shares_for(effective_size)),
                             asof_decision_dt, ctx)
    adj = apply_verdict_to_target(effective_size, verdict)
    if adj.adjusted_target_pct == 0.0:
        decision.gate = "SILENCE_ADMISSIBILITY"
        decision.details = {"reason": verdict.reason, "admissibility_state": verdict.state.value}
        result.silences += 1
        result.decisions.append(decision)
        continue
    effective_size = adj.adjusted_target_pct
```
With the flag OFF the block is skipped entirely → bit-identical to today. The verdict
(`state`, `reason`, `annual_cbr`) is written to `decision.details` so "did admissibility fire and why?"
is queryable (ADR-0077 D77.4 observability). **Do not** touch the gate itself; this is strictly upstream.

---

## 5. Test files (the invariants — every acceptance criterion below is a real assert)

### 5.1 `tests/unit/test_admissibility_oracle.py`
Deterministic, no network. Build an `AdmissibilityContext` per case; call `evaluate_admissibility(...)`
directly (and via `AlpacaShortabilityOracle` with an injected fake `get_asset`). Cases:

- **`test_unknown_shortability_rejects_short`** — ctx with `easy_to_borrow=None` (or `shortable=None`) →
  `state is REJECTED and reason == REASON_UNKNOWN_SHORTABILITY`. **The silence-by-default headline.**
- **`test_not_etb_rejects_short`** — `easy_to_borrow=False`, everything else fine → `REJECTED`/`NOT_ETB`.
- **`test_not_shortable_rejects_short`** — `shortable=False` → `REJECTED`/`NOT_SHORTABLE`.
- **`test_not_marginable_rejects_short`** — `marginable=False` → `REJECTED`/`NOT_MARGINABLE`.
- **`test_equity_below_2k_rejects_short`** — `account_equity=1_999.0` → `REJECTED`/`EQUITY_BELOW_2K`.
- **`test_fractional_short_rejected`** — `qty=10.5`, ETB ctx → `REJECTED`/`FRACTIONAL_SHORT` (mirrors HTTP 422).
- **`test_insufficient_bp_rejected`** — `current_ask=100, qty=1000, available_bp=1000` →
  `REJECTED`/`INSUFFICIENT_BPR` (`1.03*100*1000 = 103_000 > 1_000`).
- **`test_ptp_no_exception_rejected`** — `attributes=("ptp_no_exception",)` → `REJECTED`/`PTP_BLOCKED`.
- **`test_ssr_marketable_short_partial`** — `ssr_active=True, is_marketable=True`, else admissible →
  `PARTIAL`/`SSR_MARKETABLE_SHORT`.
- **`test_etb_whole_share_accepted_with_low_cbr`** — full ETB ctx, `qty=100`, BP sufficient →
  `ACCEPTED`, `reason is None`, `0.0 < annual_cbr < 0.02`.
- **`test_long_side_always_accepted_zero_cbr`** — `side="long"` (or `buy`) → `ACCEPTED`, `annual_cbr == 0.0`,
  regardless of `easy_to_borrow`.
- **`test_alpaca_oracle_fail_closed_on_get_asset_error`** — inject a `get_asset` that raises →
  `REJECTED`/`UNKNOWN_SHORTABILITY` (never ACCEPTED on error).
- **`test_null_oracle_accepts_everything`** — `NullShortabilityOracle().verdict("ANY","short",100,t,ctx).state
  is ACCEPTED` (flag-OFF == today, bit-for-bit). Includes a case with `easy_to_borrow=False` still ACCEPTED.
- **`test_select_oracle_flag_off_is_null`** / **`test_select_oracle_flag_on_is_alpaca`** — `monkeypatch.delenv`
  / `monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY","1")` → factory returns the right type.
- **`test_static_allowlist_missing_snapshot_rejects`** — symbol absent from snapshot → `REJECTED`/`NOT_ETB`
  (fail-closed, never "assume admissible"); `asof` keying verified (a snapshot for a different date does not
  apply).
- **`test_authority_boundary_never_amplifies`** — for the full discrete ladder
  `target_pct in {0, ±0.05, ±0.10, ±0.15, ±0.20}` crossed with each `AdmissibilityState`,
  `apply_verdict_to_target(...).adjusted_target_pct` satisfies `abs(adjusted) <= abs(target_pct)` AND
  `sign(adjusted) in {0, sign(target_pct)}`. **The ADR-0004 boundary.** Use `@pytest.mark.parametrize` over the
  ladder × states (NOT `hypothesis` — it is not a declared dep and no test in this repo uses it; AGENTS.md's
  hypothesis suggestion does not override the actual dependency set). If broader coverage is wanted, add a small
  deterministic loop over `[round(x, 4) for x in <fine grid>]` rather than introducing a new dependency.

### 5.2 `tests/unit/test_admissibility_borrow_pnl.py`
- **`test_long_accrues_zero_borrow`** — `short_shares=+100` → `daily_borrow_fee == 0.0`.
- **`test_daily_borrow_360_basis`** — `short_shares=-1000, close=100, cbr=0.02`, a Wednesday →
  `≈ 1000*100*0.02/360 == 5.555...` (assert `pytest.approx`).
- **`test_friday_accrues_triple`** — same inputs on a Friday → exactly `3x` the Wednesday value.
- **`test_pil_debited_for_short`** — `payment_in_lieu(-100, 0.50) == 50.0`; long → `0.0`.
- **`test_accrue_sums_over_held_days_and_divs`** — `close_by_date` over 5 weekdays + one ex-div date →
  `BorrowAccrual.total_borrow_fee` equals the per-day sum and `total_pil` equals the PIL; `days_held == 5`.
- **`test_whole_share_short_floor`** (in oracle/order_state test or here) — `target_pct_to_shares(-0.10,
  nav=10_000, price=33.0)` → `floor(1000/33)=30` shares → `-30` (never `-30.3`).

### 5.3 `tests/unit/test_admissibility_restate.py`
- Build a synthetic `state.db` in `tmp_path` (open sqlite, create the `positions` table per §1.3, insert
  ~4 shorts + 1 long), write a snapshot JSON marking some ETB and some not, run the script's `main()` (import
  it as a module via `runpy`/`importlib` or refactor the core into an importable `restate(...)` function — prefer
  the latter: put the logic in `restate_book(book, account_id, snapshot, oracle) -> dict` and have `main()` thin).
- **`test_restate_rejects_non_etb_shorts`** — non-ETB shorts appear with `state == "REJECTED"`,
  `reason == "NOT_ETB"`; ETB short is `ACCEPTED`.
- **`test_restate_does_not_mutate_state_db`** — assert the `positions` row count/content is unchanged after the
  run (read-only guarantee).
- **`test_restate_long_positions_ignored`** — the long is not in the shorts table; `n_shorts` excludes it.
- **`test_restate_json_shape`** — `--json` output has the §4.3 keys and `n_rejected_not_etb <= n_rejected <=
  n_shorts`.

---

## 6. Build order (for the executing agent)

1. `oracle.py` — enums, dataclasses, `evaluate_admissibility`, `NullShortabilityOracle`, `select_oracle`.
   Then `AlpacaShortabilityOracle` (lazy import) + `StaticETBAllowlistOracle`.
2. `order_state.py` — `target_pct_to_shares`, `apply_verdict_to_target`.
3. `borrow_pnl.py` — the three pure functions + `accrue_borrow_carry`.
4. `__init__.py` — exports.
5. Tests 5.1 + 5.2 → run `pytest tests/unit/test_admissibility_oracle.py tests/unit/test_admissibility_borrow_pnl.py -q`.
6. `ops/scripts/quant-admissibility-restate.py` (logic in importable `restate_book`) + test 5.3.
7. Autonomous-loop guarded wiring (§4.4) — verify the flag-OFF no-op with the existing autonomous tests.
8. `ruff check`, `ruff format`, `mypy hermes_quant/admissibility/`. Run full suite `pytest tests/ -q`.

---

## 7. Acceptance criteria (all verifiable by `pytest` / a deterministic command)

> Run: `~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/unit/test_admissibility_oracle.py tests/unit/test_admissibility_borrow_pnl.py tests/unit/test_admissibility_restate.py -q`

1. **Silence-by-default (the P0):** unknown shortability (`easy_to_borrow=None`) on an opening short →
   `ShortabilityVerdict.state is AdmissibilityState.REJECTED` and `reason == "UNKNOWN_SHORTABILITY"`.
   (`test_unknown_shortability_rejects_short`)
2. **Not-ETB / not-shortable short rejected:** `NOT_ETB` and `NOT_SHORTABLE` cases both REJECT.
3. **Whole-share enforcement:** fractional short (`qty=10.5`) → `REJECTED`/`FRACTIONAL_SHORT`; and
   `target_pct_to_shares` floors short magnitude to a whole integer (never fractional).
4. **Fail-closed on live error:** `AlpacaShortabilityOracle` with a `get_asset` that raises →
   `REJECTED`/`UNKNOWN_SHORTABILITY`, never ACCEPTED.
5. **Authority boundary (ADR-0004):** the parametrized `test_authority_boundary_never_amplifies` passes — for
   the discrete ladder × every verdict state, `abs(adjusted_target_pct) <= abs(target_pct)` and the sign never
   flips/inflates. The oracle can only subtract.
6. **Flag-OFF == today, bit-for-bit:** `NullShortabilityOracle` ACCEPTS everything (incl. `easy_to_borrow=False`);
   `select_oracle()` returns `NullShortabilityOracle` when `HERMES_QUANT_ADMISSIBILITY` is unset/`!=1`, and
   `AlpacaShortabilityOracle` when `==1`.
7. **Borrow carry math:** `daily_borrow_fee` uses `/360`, Friday is exactly `3x`, longs accrue `0.0`; PIL =
   `abs(short_shares) * div/share` for shorts and `0.0` for longs; `accrue_borrow_carry` sums correctly.
8. **`asof` honesty (static path):** `StaticETBAllowlistOracle` REJECTS a symbol with no snapshot for the
   requested `asof` (no look-ahead, no "assume admissible").
9. **Restatement is read-only:** `test_restate_does_not_mutate_state_db` passes — the `positions` table is
   byte-unchanged after a run; the script produces the §4.3 JSON with `n_rejected_not_etb <= n_rejected <= n_shorts`.
10. **No regression / quality:** `pytest tests/ -q` stays green; `ruff check hermes_quant/ tests/`,
    `ruff format --check`, and `mypy hermes_quant/admissibility/` are clean. The package imports with
    `alpaca-py` absent (live import is lazy).

---

## 8. Out of scope (do NOT build in Wave B)

- The full ADR-0078 `OrderState`/`OrderEvent` state machine, dedup ledger, and tick semaphore
  (`hermes_quant/react/order_state.py`, `daemon/tick_lock.py`) — separate wave; this plan only produces the
  `REJECTED` verdict that layer will consume.
- Real HTB borrow rates / a point-in-time historical ETB feed (Alpaca does not expose them).
- Options-leg admissibility (multi-leg options ADR).
- Daily mark-to-market historical borrow accrual in the restatement (the one-mark coarse estimate is the
  documented Wave-B artifact; full daily marking is a follow-up).
- Flipping either flag on in production — that is the operator's explicit, separate call after auditing the §4.3
  restatement (ADR-0077 §Rollout steps 3–4).
