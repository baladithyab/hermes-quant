"""hermes_quant.state.portfolio_state — PortfolioState reconstruction engine.

ADR-0039 wave 1c: materialized-view projection of executions.jsonl into
state.db positions + cash tables.

Key design decisions
---------------------
1. Append-only ethos: state.db is a *cache*. executions.jsonl is canonical.
   Rebuild from scratch via reconstruct_from(); it is idempotent.

2. O(delta) watermark: the executions_replayed table tracks the last ISO
   timestamp that was processed so subsequent calls only replay new fills.
   Full rebuild: call reconstruct_from() which clears position/cash tables
   and replays all executions from scratch.

3. fill_size_pct semantics (from ExecutionRecord):
   ExecutionRecord stores fill_size_pct as a SIGNED fraction of NAV,
   NOT a share quantity. e.g. +0.05 means "buy 5 % of current NAV".
   We store this directly as the position's quantity field in v0.1.
   This is documented in ADR-0039 §D7: share-quantity tracking requires
   knowing NAV at execution time; v0.1 stores the fractional-of-NAV
   representation because it is self-contained in each record. Position
   quantity = cumulative sum of fill_size_pct across all fills.

4. Cash accounting:
   - Bootstrapped from HERMES_QUANT_PAPER_INITIAL_CASH (default 100 000 USD).
   - Long fills (fill_size_pct > 0) DECREASE cash by:
       abs(fill_size_pct) × fill_price  (proxy: pct × price = fraction-notional)
   - Short fills (fill_size_pct < 0) INCREASE cash by the same magnitude.
   This is approximate in v0.1 because fill_size_pct is a NAV fraction, not
   shares.  The approximation is consistent with how PaperReactor uses the
   field.  Documented in ADR-0039 §D7.

5. Sign convention:
   - position.quantity > 0  → long
   - position.quantity < 0  → short
   - Closing a long with a short fill brings quantity toward 0.

6. Failure isolation: apply_execution() failures are swallowed with a
   warning (same pattern as audit_log.append in halt_state.py) so a DB
   write error never blocks PaperReactor.execute().

7. Schema compatibility: CREATE TABLE IF NOT EXISTS so the existing
   'halts' table and any future tables are never touched.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .positions import CashState, Position

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
DEFAULT_STATE_DB = QUANT_HOME / "state.db"
DEFAULT_EXECUTIONS_PATH = QUANT_HOME / "executions.jsonl"

# Env var for bootstrapping initial cash per account.
_INITIAL_CASH_ENV = "HERMES_QUANT_PAPER_INITIAL_CASH"
_DEFAULT_INITIAL_CASH = 100_000.0


def _default_initial_cash() -> float:
    raw = os.environ.get(_INITIAL_CASH_ENV, "")
    try:
        return float(raw) if raw else _DEFAULT_INITIAL_CASH
    except ValueError:
        logger.warning(
            "%s is not a valid float (%r); using default %.2f",
            _INITIAL_CASH_ENV,
            raw,
            _DEFAULT_INITIAL_CASH,
        )
        return _DEFAULT_INITIAL_CASH


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    account_id       TEXT NOT NULL,
    asset_class      TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    quantity         REAL NOT NULL,
    avg_entry_price  REAL NOT NULL,
    last_update_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, asset_class, symbol)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cash (
    account_id     TEXT PRIMARY KEY,
    balance_usd    REAL NOT NULL,
    last_update_at TEXT NOT NULL,
    equity_total   REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS executions_replayed (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    last_replayed_asof   TEXT NOT NULL,
    replayed_count       INTEGER NOT NULL DEFAULT 0
);
"""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ReconstructionResult:
    """Summary of a reconstruct_from() call.

    Attributes
    ----------
    executions_processed:
        Number of JSONL records consumed during this run (delta or full).
    positions_written:
        Number of position rows upserted (created or updated).
    accounts_seen:
        Set of account_id values encountered in this batch.
    errors:
        List of (line_number, error_message) for records that were skipped.
    """

    executions_processed: int = 0
    positions_written: int = 0
    accounts_seen: set[str] = field(default_factory=set)
    errors: list[tuple[int, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PortfolioState
# ---------------------------------------------------------------------------


class PortfolioState:
    """Materialized-view projection of executions.jsonl → state.db.

    Thread-safe via a per-instance RLock.  Multi-process safe via SQLite's
    WAL mode (same pattern as HaltStateSQLite).

    Usage
    -----
    Incremental (typical — called from PaperReactor.execute):
        ps = PortfolioState()
        ps.apply_execution(record_dict)

    Full rebuild (called by crons / after data loss):
        ps = PortfolioState()
        result = ps.reconstruct_from(executions_path)

    Read views:
        positions = ps.get_positions("paper-default")
        cash = ps.get_cash("paper-default")
    """

    def __init__(self, state_db_path: Path | None = None) -> None:
        self.db_path = state_db_path or DEFAULT_STATE_DB
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management (matches halt_state.py pattern)
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with WAL + foreign keys + 5 s busy timeout."""
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Full rebuild (idempotent)
    # ------------------------------------------------------------------

    def reconstruct_from(self, executions_path: Path) -> ReconstructionResult:
        """Full rebuild: clear positions/cash, replay every record.

        Reads executions.jsonl from the beginning, replays every fill,
        and writes the resulting state into state.db. Idempotent — calling
        it twice yields the same result.

        After this call the executions_replayed watermark is updated so
        that subsequent apply_execution() calls start from the delta
        (records newer than the last replayed timestamp).

        Args:
            executions_path: path to executions.jsonl

        Returns:
            ReconstructionResult summary.
        """
        result = ReconstructionResult()

        # ── 1. Read all records ──────────────────────────────────────────
        records = _read_all_jsonl(executions_path)

        # ── 2. Replay into in-memory accumulators ────────────────────────
        positions: dict[tuple[str, str, str], dict[str, Any]] = {}
        cash_map: dict[str, float] = {}
        last_ts: dict[str, str] = {}  # account_id → latest asof seen

        initial_cash = _default_initial_cash()

        for line_no, rec in enumerate(records, start=1):
            try:
                _replay_record(rec, positions, cash_map, last_ts, initial_cash)
                result.executions_processed += 1
                acct = rec.get("account_id", "paper-default")
                result.accounts_seen.add(acct)
            except Exception as e:  # noqa: BLE001
                result.errors.append((line_no, str(e)))
                logger.warning("reconstruct_from: skipping record %d: %s", line_no, e)

        # ── 3. Write to state.db atomically ──────────────────────────────
        latest_asof = _latest_asof(records)

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN")
            try:
                # Clear derived tables (not halts!)
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM cash")

                # Upsert positions
                for (acct, asset_class, symbol), pos in positions.items():
                    qty = pos["quantity"]
                    if abs(qty) < 1e-12:
                        # Flat positions: still write them with qty=0 for
                        # auditability. The get_positions() reader filters them.
                        pass
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO positions
                            (account_id, asset_class, symbol, quantity,
                             avg_entry_price, last_update_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            acct,
                            asset_class,
                            symbol,
                            qty,
                            pos["avg_entry_price"],
                            pos["last_update_at"],
                        ),
                    )
                    result.positions_written += 1

                # Upsert cash
                for acct, balance in cash_map.items():
                    ts = last_ts.get(acct, _utc_now_iso())
                    # equity_total: cash + open position notionals
                    equity = balance + sum(
                        abs(p["quantity"]) * p["avg_entry_price"]
                        for (a, _, _), p in positions.items()
                        if a == acct and abs(p["quantity"]) >= 1e-12
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO cash
                            (account_id, balance_usd, last_update_at, equity_total)
                        VALUES (?, ?, ?, ?)
                        """,
                        (acct, balance, ts, equity),
                    )

                # Update watermark
                if latest_asof:
                    conn.execute(
                        """
                        INSERT INTO executions_replayed (id, last_replayed_asof, replayed_count)
                        VALUES (1, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            last_replayed_asof = excluded.last_replayed_asof,
                            replayed_count = excluded.replayed_count
                        """,
                        (latest_asof, result.executions_processed),
                    )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return result

    # ------------------------------------------------------------------
    # Incremental (apply one execution record)
    # ------------------------------------------------------------------

    def apply_execution(self, record: dict[str, Any]) -> None:
        """Apply a single execution record to the state incrementally.

        Called by PaperReactor.execute() after the JSONL append.
        Failures are swallowed with a warning per ADR-0031 silence-by-default
        (same as audit_log.append failure handling in halt_state.py).

        Args:
            record: dict matching ExecutionRecord fields (as produced by
                    paper._record_to_dict).
        """
        try:
            self._apply_execution_unsafe(record)
        except Exception as e:  # noqa: BLE001
            logger.warning("PortfolioState.apply_execution failed: %s", e)

    def _apply_execution_unsafe(self, record: dict[str, Any]) -> None:
        """Inner implementation — may raise; caller wraps in try/except."""
        acct = record.get("account_id", "paper-default")
        asset_class = record.get("asset_class", "equity")
        symbol = record.get("asset", "")
        fill_size_pct = float(record.get("fill_size_pct", 0.0))
        fill_price = float(record.get("fill_price", 0.0))
        asof = record.get("asof_execution") or _utc_now_iso()

        initial_cash = _default_initial_cash()

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN")
            try:
                # ── load current position ────────────────────────────────
                row = conn.execute(
                    "SELECT quantity, avg_entry_price FROM positions "
                    "WHERE account_id=? AND asset_class=? AND symbol=?",
                    (acct, asset_class, symbol),
                ).fetchone()

                if row is not None:
                    old_qty = float(row["quantity"])
                    old_avg = float(row["avg_entry_price"])
                else:
                    old_qty = 0.0
                    old_avg = 0.0

                new_qty, new_avg = _update_position(old_qty, old_avg, fill_size_pct, fill_price)

                # ── upsert position ──────────────────────────────────────
                conn.execute(
                    """
                    INSERT OR REPLACE INTO positions
                        (account_id, asset_class, symbol, quantity,
                         avg_entry_price, last_update_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (acct, asset_class, symbol, new_qty, new_avg, asof),
                )

                # ── load / bootstrap cash ────────────────────────────────
                crow = conn.execute(
                    "SELECT balance_usd, equity_total FROM cash WHERE account_id=?",
                    (acct,),
                ).fetchone()

                if crow is not None:
                    cash_balance = float(crow["balance_usd"])
                else:
                    cash_balance = initial_cash

                # Long fill: cash decreases; short fill: cash increases.
                # delta_cash = -fill_size_pct * fill_price
                # (fill_size_pct positive = long → decrease cash)
                delta_cash = -fill_size_pct * fill_price
                new_cash = cash_balance + delta_cash

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

                conn.execute(
                    """
                    INSERT OR REPLACE INTO cash
                        (account_id, balance_usd, last_update_at, equity_total)
                    VALUES (?, ?, ?, ?)
                    """,
                    (acct, new_cash, asof, equity),
                )

                # ── update watermark ─────────────────────────────────────
                conn.execute(
                    """
                    INSERT INTO executions_replayed (id, last_replayed_asof, replayed_count)
                    VALUES (1, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        last_replayed_asof = MAX(excluded.last_replayed_asof,
                                                  executions_replayed.last_replayed_asof),
                        replayed_count = executions_replayed.replayed_count + 1
                    """,
                    (asof,),
                )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Read views
    # ------------------------------------------------------------------

    def get_positions(self, account_id: str) -> dict[tuple[str, str], Position]:
        """Return all open positions for account_id.

        Returns:
            Mapping of (asset_class, symbol) → Position.
            Excludes flat positions (|quantity| < 1e-12).
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT account_id, asset_class, symbol, quantity, "
                "avg_entry_price, last_update_at "
                "FROM positions "
                "WHERE account_id=? AND ABS(quantity) >= 1e-12",
                (account_id,),
            ).fetchall()

        return {
            (row["asset_class"], row["symbol"]): Position(
                account_id=row["account_id"],
                asset_class=row["asset_class"],
                symbol=row["symbol"],
                quantity=float(row["quantity"]),
                avg_entry_price=float(row["avg_entry_price"]),
                last_update_at=row["last_update_at"],
            )
            for row in rows
        }

    def get_cash(self, account_id: str) -> CashState | None:
        """Return CashState for account_id, or None if not yet seen."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT account_id, balance_usd, last_update_at, equity_total "
                "FROM cash WHERE account_id=?",
                (account_id,),
            ).fetchone()

        if row is None:
            return None
        return CashState(
            account_id=row["account_id"],
            balance_usd=float(row["balance_usd"]),
            last_update_at=row["last_update_at"],
            equity_total=float(row["equity_total"]),
        )

    def get_watermark(self) -> str | None:
        """Return the last replayed asof timestamp, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_replayed_asof FROM executions_replayed WHERE id=1"
            ).fetchone()
        return row["last_replayed_asof"] if row else None


# ---------------------------------------------------------------------------
# Module-level singleton (used by PaperReactor integration)
# ---------------------------------------------------------------------------

_singleton: PortfolioState | None = None
_singleton_lock = threading.Lock()


def get_portfolio_state(db_path: Path | None = None) -> PortfolioState:
    """Return the module-level PortfolioState singleton.

    Creates it on first access.  Tests should construct their own
    PortfolioState(state_db_path=tmp_path/...) rather than calling this.
    """
    global _singleton  # noqa: PLW0603
    with _singleton_lock:
        if _singleton is None:
            _singleton = PortfolioState(db_path)
        return _singleton


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """ISO 8601 UTC with 'Z' suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_all_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all well-formed records from a JSONL file."""
    if not path.exists():
        return []
    records: list[dict] = []
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("_read_all_jsonl: skipping malformed line")
    return records


def _latest_asof(records: list[dict[str, Any]]) -> str | None:
    """Return the most recent asof_execution timestamp among records."""
    asofs: list[str] = [r["asof_execution"] for r in records if r.get("asof_execution")]
    return max(asofs) if asofs else None


def _update_position(
    old_qty: float,
    old_avg: float,
    fill_size_pct: float,
    fill_price: float,
) -> tuple[float, float]:
    """Compute new quantity and avg_entry_price after a fill.

    Design (ADR-0039 §D7):
    - Weighted-average cost basis (not FIFO) for v0.1.
    - fill_size_pct is SIGNED: positive = long fill, negative = short fill.
    - new_qty = old_qty + fill_size_pct  (position in NAV-fraction units)
    - Adding to an existing same-direction position:
          new_avg = (old_qty × old_avg + fill_size_pct × fill_price) / new_qty
    - Reducing / closing a position (opposite sign):
          avg_entry_price stays at old_avg for the residual lot
          (residual-lot rule, ADR-0039 §D7).
    - Full close (new_qty ≈ 0): avg_entry_price → 0.0.
    - Direction flip (sign changes, |new_qty| > 0 in opposite direction):
          new_avg = fill_price (new position opened at fill_price).

    Args:
        old_qty: current position quantity (signed, NAV-fraction units).
        old_avg: current avg_entry_price.
        fill_size_pct: signed fill size (NAV-fraction).
        fill_price: fill price for this execution.

    Returns:
        (new_qty, new_avg) tuple.
    """
    new_qty = old_qty + fill_size_pct

    # Full close
    if abs(new_qty) < 1e-12:
        return 0.0, 0.0

    same_direction = (old_qty == 0.0) or (old_qty * fill_size_pct > 0)

    if same_direction:
        # Adding to (or opening) a position in the same direction.
        # new_avg = weighted average of old position + new fill.
        numerator = old_qty * old_avg + fill_size_pct * fill_price
        new_avg = numerator / new_qty
    elif (old_qty * new_qty) < 0:
        # Direction flip: old position fully reversed and overshoot.
        # new_avg = fill_price (new position in opposite direction).
        new_avg = fill_price
    else:
        # Partial close (same sign for old and new, opposite fill):
        # residual-lot rule — avg_entry_price of surviving lot is unchanged.
        new_avg = old_avg

    return new_qty, new_avg


def _replay_record(
    rec: dict[str, Any],
    positions: dict[tuple[str, str, str], dict[str, Any]],
    cash_map: dict[str, float],
    last_ts: dict[str, str],
    initial_cash: float,
) -> None:
    """Apply one record to the in-memory accumulators during full rebuild.

    Mutates positions, cash_map, and last_ts in place.

    Args:
        rec:          decoded JSONL dict from executions.jsonl
        positions:    (account_id, asset_class, symbol) → position dict
        cash_map:     account_id → cash balance
        last_ts:      account_id → most recent asof_execution seen
        initial_cash: bootstrap cash for first-seen accounts
    """
    acct = rec.get("account_id", "paper-default")
    asset_class = rec.get("asset_class", "equity")
    symbol = rec.get("asset", "")
    fill_size_pct = float(rec.get("fill_size_pct", 0.0))
    fill_price = float(rec.get("fill_price", 0.0))
    asof = rec.get("asof_execution") or _utc_now_iso()

    # Bootstrap cash for new accounts
    if acct not in cash_map:
        cash_map[acct] = initial_cash

    # Update position
    key = (acct, asset_class, symbol)
    pos = positions.get(key)
    if pos is None:
        old_qty, old_avg = 0.0, 0.0
    else:
        old_qty, old_avg = pos["quantity"], pos["avg_entry_price"]

    new_qty, new_avg = _update_position(old_qty, old_avg, fill_size_pct, fill_price)
    positions[key] = {
        "quantity": new_qty,
        "avg_entry_price": new_avg,
        "last_update_at": asof,
    }

    # Update cash: long fill decreases cash, short fill increases cash.
    cash_map[acct] -= fill_size_pct * fill_price

    # Track latest timestamp per account
    if acct not in last_ts or asof > last_ts[acct]:
        last_ts[acct] = asof
