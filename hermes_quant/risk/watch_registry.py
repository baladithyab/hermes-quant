"""hermes_quant.risk.watch_registry — Durable per-play watched-position registry (ADR-0099 AG-EQ-3).

This is the DURABLE per-play state that the tranche/trailing exit cores
(exit_strategy.evaluate_tranche, already built) need to persist ACROSS ticks.

Per open position, the registry holds:
  - entry_price: the fill price at which the position was opened.
  - stop_pct: the position's stop threshold (1R).
  - tranches_taken: how many tranche steps have been executed (increments, never resets).
  - peak_gain_pct: the highest gain_pct seen so far — MONOTONIC MAX, never lowers.
  - opened_at: ISO 8601 UTC timestamp when the position was recorded.

MIRRORS the sidecar pattern in hermes_quant/risk/baseline_store.py:
  - Same atomic tmp→rename JSON write pattern.
  - Same WAL + 5s busy_timeout + RLock SQLite connection discipline.
  - Same BEGIN IMMEDIATE on read-then-write sequences (ar04/ar06 family).
  - Same finite-guard on every stored numeric (ar08 family: NaN/inf defeats every <= gate).
  - Same fail-CLOSED read posture.

SCOPE: DEFAULT-OFF / ADDITIVE — a NEW module, nothing on the live path consumes it yet.
The autonomous monitor wiring is the next step (main-thread, operator-gated). This module
unblocks the tranche/trailing live wiring by providing the persistence layer the exit cores
depend on.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)

# Co-locate with baseline_store in the same durable state.db.
DEFAULT_STATE_DB = _resolve_quant_home() / "state.db"
DEFAULT_WATCH_JSON_MIRROR = _resolve_quant_home() / "watch_registry.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_registry (
    symbol          TEXT    NOT NULL PRIMARY KEY,
    entry_price     REAL    NOT NULL,   -- fill price at open
    stop_pct        REAL    NOT NULL,   -- 1R stop threshold (e.g. 0.08)
    tranches_taken  INTEGER NOT NULL DEFAULT 0,  -- increments, never resets mid-play
    peak_gain_pct   REAL    NOT NULL DEFAULT 0.0,  -- monotonic max gain; never lowers
    opened_at       TEXT    NOT NULL   -- ISO 8601 UTC
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class WatchedPosition:
    """Per-play durable state the exit cores read each tick.

    All numeric fields are finite-guarded at write time; a read that encounters a
    non-finite value (corruption) returns None (fail-CLOSED).
    """

    symbol: str
    entry_price: float       # fill price; must be > 0
    stop_pct: float          # 1R threshold; must be > 0 and finite
    tranches_taken: int      # 0..2; monotonic increment
    peak_gain_pct: float     # monotonic max gain seen for this play
    opened_at: str           # ISO 8601 UTC


def _utc_now_iso() -> str:
    """ISO 8601 UTC with 'Z' suffix (mirrors baseline_store._utc_now_iso)."""
    return pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_atomic_json(path: Path, data: dict) -> None:
    """Atomic-rename pattern for the JSON mirror.

    Mirrors baseline_store._write_atomic_json: write tmp, then POSIX-atomic rename.
    The mirror is the human-readable cold-start convenience; SQLite is truth.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic on POSIX


def _guard_float(v: float, name: str, label: str) -> float:
    """Finite-guard a stored numeric. Raises ValueError on non-finite/non-numeric."""
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"watch_registry: {label}.{name} non-numeric: {v!r}") from e
    if not math.isfinite(f):
        raise ValueError(f"watch_registry: {label}.{name} non-finite: {f!r}")
    return f


class WatchRegistry:
    """SQLite-backed durable watched-position registry.

    API:
      record_open(symbol, entry_price, stop_pct)   -- register or idempotently re-seed
      update_peak(symbol, gain_pct)                -- monotonic max; never lowers peak
      mark_tranche(symbol)                         -- increment tranches_taken
      get(symbol) -> WatchedPosition | None        -- read or None if absent
      drop(symbol)                                 -- remove on full close

    POSTURE: all writes are fail-CLOSED (non-finite inputs are rejected; if SQLite is
    unreadable, get returns None — the caller treats absence as "no exit action"). The
    BEGIN IMMEDIATE on every read-then-write prevents the ar04/ar06 lost-update race.
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_STATE_DB,
        mirror_path: Path = DEFAULT_WATCH_JSON_MIRROR,
    ):
        self.db_path = db_path
        self.mirror_path = mirror_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with WAL + 5s busy timeout (mirrors baseline_store._conn)."""
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def record_open(
        self,
        symbol: str,
        entry_price: float,
        stop_pct: float,
    ) -> None:
        """Register a new open position (or idempotently re-seed if already present).

        Finite-guards entry_price and stop_pct — both must be positive finite numbers.
        If the position already exists, the call is a no-op (idempotent), so safe to
        call on every tick start.

        Raises ValueError on non-finite / non-positive inputs (fail-CLOSED: a bad
        entry_price or stop must not silently open a play with garbage state).
        """
        ep = _guard_float(entry_price, "entry_price", symbol)
        sp = _guard_float(stop_pct, "stop_pct", symbol)
        if ep <= 0.0:
            raise ValueError(f"watch_registry: {symbol}.entry_price must be > 0, got {ep}")
        if sp <= 0.0:
            raise ValueError(f"watch_registry: {symbol}.stop_pct must be > 0, got {sp}")

        opened_at = _utc_now_iso()
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO watch_registry "
                    "(symbol, entry_price, stop_pct, tranches_taken, peak_gain_pct, opened_at) "
                    "VALUES (?, ?, ?, 0, 0.0, ?)",
                    (symbol, ep, sp, opened_at),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        self._write_mirror_safe()

    def update_peak(self, symbol: str, gain_pct: float) -> None:
        """Update the monotonic-max peak gain. NEVER lowers the stored peak.

        Non-finite gain_pct values are silently ignored (silence-by-default: a NaN
        tick must not clobber a valid stored peak). If the symbol is not present,
        this is a no-op.
        """
        if not (isinstance(gain_pct, (int, float)) and not isinstance(gain_pct, bool)):
            return  # non-numeric: no-op
        if not math.isfinite(gain_pct):
            return  # NaN / inf: silence-by-default, do not corrupt the peak

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT peak_gain_pct FROM watch_registry WHERE symbol=?",
                    (symbol,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return  # absent: no-op
                stored_peak = float(row["peak_gain_pct"])
                if not math.isfinite(stored_peak):
                    stored_peak = gain_pct  # repair corrupted peak
                new_peak = max(stored_peak, gain_pct)
                conn.execute(
                    "UPDATE watch_registry SET peak_gain_pct=? WHERE symbol=?",
                    (new_peak, symbol),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        self._write_mirror_safe()

    def mark_tranche(self, symbol: str) -> None:
        """Increment tranches_taken by 1 for the given symbol, CAPPED at 2.

        If the symbol is not present, this is a no-op. wave3-review FIX: the docstring
        promised a cap at 2 but the code did `cur + 1` UNCAPPED. The cap is now enforced —
        2 is the max exit_strategy.evaluate_tranche recognizes (it treats tranches_taken>=2
        as "all tranches taken -> HOLD"), so an un-capped count drifting past 2 would
        misrepresent the play's exit state to a consumer that branches on the exact value.
        """
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT tranches_taken FROM watch_registry WHERE symbol=?",
                    (symbol,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return  # absent: no-op
                cur = int(row["tranches_taken"])
                new_val = min(cur + 1, 2)  # CAP at 2 (the exit_strategy max; wave3-review fix)
                conn.execute(
                    "UPDATE watch_registry SET tranches_taken=? WHERE symbol=?",
                    (new_val, symbol),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        self._write_mirror_safe()

    def drop(self, symbol: str) -> None:
        """Remove a position from the registry on full close.

        Idempotent: dropping an absent symbol is a no-op.
        """
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM watch_registry WHERE symbol=?", (symbol,)
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        self._write_mirror_safe()

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, symbol: str) -> WatchedPosition | None:
        """Read the persisted state for a symbol, or None if absent.

        Fail-CLOSED: a read failure (SQLite error, non-finite stored values) returns
        None. The caller (exit monitor) treats absence as "no exit action" — which
        is the conservative direction (silence-by-default, no fabricated state).
        """
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT symbol, entry_price, stop_pct, tranches_taken, "
                    "peak_gain_pct, opened_at "
                    "FROM watch_registry WHERE symbol=?",
                    (symbol,),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row is None:
            return None
        try:
            ep = _guard_float(float(row["entry_price"]), "entry_price", symbol)
            sp = _guard_float(float(row["stop_pct"]), "stop_pct", symbol)
            peak = float(row["peak_gain_pct"])
            if not math.isfinite(peak):
                peak = 0.0  # repair to conservative default (no peak seen)
        except (ValueError, TypeError):
            logger.warning("watch_registry: non-finite stored value for %s — returning None", symbol)
            return None
        return WatchedPosition(
            symbol=symbol,
            entry_price=ep,
            stop_pct=sp,
            tranches_taken=int(row["tranches_taken"]),
            peak_gain_pct=peak,
            opened_at=str(row["opened_at"]),
        )

    def all_symbols(self) -> list[str]:
        """Return all symbols currently in the registry.

        Fail-CLOSED: a read failure returns an empty list (no fabricated state).
        """
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT symbol FROM watch_registry ORDER BY symbol"
                ).fetchall()
        except (sqlite3.Error, OSError):
            return []
        return [str(r["symbol"]) for r in rows]

    # ------------------------------------------------------------------
    # JSON mirror (best-effort)
    # ------------------------------------------------------------------

    def _write_mirror_safe(self) -> None:
        """Write the JSON mirror atomically. Best-effort: a mirror failure is
        logged and swallowed — SQLite is the source of truth.
        """
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT symbol, entry_price, stop_pct, tranches_taken, "
                    "peak_gain_pct, opened_at "
                    "FROM watch_registry ORDER BY symbol"
                ).fetchall()
            data = {
                r["symbol"]: {
                    "symbol": r["symbol"],
                    "entry_price": r["entry_price"],
                    "stop_pct": r["stop_pct"],
                    "tranches_taken": r["tranches_taken"],
                    "peak_gain_pct": r["peak_gain_pct"],
                    "opened_at": r["opened_at"],
                }
                for r in rows
            }
            _write_atomic_json(self.mirror_path, data)
        except (sqlite3.Error, OSError) as e:
            logger.warning("watch_registry mirror write failed: %s", e)


def read_watch_mirror(path: Path = DEFAULT_WATCH_JSON_MIRROR) -> dict:
    """Read the JSON watch mirror without opening SQLite.

    Mirrors ``baseline_store.read_baseline_mirror``. Returns ``{}`` if the mirror
    doesn't exist or is corrupt.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
