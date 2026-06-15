"""hermes_quant.daemon.halt_state — Durable SQLite halt registry.

Per ADR-0009 §P0-4 + synthesis-v2 §P1-β:

- Halts are NEVER cleared by trading signals — only by `hermes quant resume`
  CLI (with `--reason`) or by `halted_until` timestamp passing.
- The registry survives daemon restart (durable SQLite at ~/.hermes/quant/state.db).
- `account_id`, `asset_class`, `asset` are NOT NULL with `'*'` sentinels for
  wildcard scope (avoids SQLite's NULL!=NULL ambiguity in PK comparison).
- Table is `WITHOUT ROWID` with `UNIQUE(account_id, asset_class, asset)`.
- `halt_epoch` is monotonic per (account, asset_class, asset) — increments on
  each new halt; an old halt with a lower epoch can never accidentally re-halt.
- A JSON mirror at `~/.hermes/quant/halt_state.json` is written atomically
  on every change for fast cold-start reads (no SQLite open) and for
  external consumers (freqtrade strategy reads the JSON mirror, not SQLite,
  to avoid lock contention).

Per synthesis-v2 §P0-D, emergency-stop ordering is HALT FIRST, then broker
cancel — the durable halt is committed before any broker call so a race
between cancel and the next daemon tick can't resume entries.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

import pandas as pd

from hermes_quant.protocol import HaltRecord

logger = logging.getLogger(__name__)

# Wildcard sentinel — used instead of NULL for ANY-scope halts.
WILDCARD = "*"

DEFAULT_STATE_DB = Path.home() / ".hermes" / "quant" / "state.db"
DEFAULT_HALT_JSON_MIRROR = Path.home() / ".hermes" / "quant" / "halt_state.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS halts (
    account_id   TEXT NOT NULL,
    asset_class  TEXT NOT NULL,
    asset        TEXT NOT NULL,             -- '*' = all assets in class
    reason       TEXT NOT NULL,
    halted_at    TEXT NOT NULL,             -- ISO 8601 UTC
    halted_until TEXT,                      -- NULL = until explicit resume
    halt_epoch   INTEGER NOT NULL,
    cleared_at   TEXT,                      -- NULL = active; ISO when cleared
    cleared_reason TEXT,                    -- audit trail per ADR §P2-θ
    PRIMARY KEY (account_id, asset_class, asset, halt_epoch)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_halts_active ON halts(account_id, asset_class, asset)
    WHERE cleared_at IS NULL;
"""


def _utc_now_iso() -> str:
    """ISO 8601 UTC with 'Z' suffix."""
    return pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_atomic_json(path: Path, data: list[dict]) -> None:
    """Atomic-rename pattern for halt_state.json mirror."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic on POSIX


class HaltStateSQLite:
    """SQLite-backed implementation of the HaltState protocol.

    Thread-safe via per-connection locking. Multi-process safe via SQLite's
    own locking (we use WAL journal mode for better concurrent-reader
    performance).

    Per synthesis-v2 §P1-β:
    - All scope columns are NOT NULL; `'*'` is the wildcard sentinel
    - Table is WITHOUT ROWID
    - UNIQUE constraint on (account_id, asset_class, asset, halt_epoch)
    - PK includes halt_epoch so multiple halts with same scope (after
      cleared) don't collide
    """

    def __init__(
        self, db_path: Path = DEFAULT_STATE_DB, mirror_path: Path = DEFAULT_HALT_JSON_MIRROR
    ):
        self.db_path = db_path
        self.mirror_path = mirror_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with WAL + foreign keys + 5s busy timeout."""
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
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

    @staticmethod
    def _normalize_scope(
        account_id: str | None, asset_class: str | None, asset: str | None
    ) -> tuple[str, str, str]:
        """Replace None with WILDCARD sentinel."""
        return (
            account_id if account_id else WILDCARD,
            asset_class if asset_class else WILDCARD,
            asset if asset else WILDCARD,
        )

    def add_halt(
        self,
        account_id: str | None,
        asset_class: str | None,
        asset: str | None,
        reason: str,
        halted_until: pd.Timestamp | None = None,
    ) -> HaltRecord:
        """Insert a new halt for the given scope.

        If a previous halt at the same scope was cleared, this gets a new
        epoch (PK includes epoch so no collision).

        If a previous halt at the same scope is STILL ACTIVE, this raises
        ValueError — the operator must clear the existing halt first or
        use a different scope.

        Args:
            account_id: account or None for wildcard.
            asset_class: asset class or None for wildcard.
            asset: specific asset or None for wildcard.
            reason: required, must be non-empty (audit trail).
            halted_until: timestamp at which auto-clear fires; None = explicit resume only.

        Returns:
            The created HaltRecord.

        Raises:
            ValueError: if reason is empty OR an active halt at this scope exists.
        """
        if not reason or not reason.strip():
            raise ValueError("halt reason is required (audit trail)")

        scope = self._normalize_scope(account_id, asset_class, asset)
        now = _utc_now_iso()
        until = halted_until.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if halted_until is not None else None

        with self._lock, self._conn() as conn:
            # ar04: BEGIN IMMEDIATE acquires the SQLite write lock at transaction
            # start, eliminating the cross-PROCESS check-then-insert race (the
            # process-local RLock above only serializes threads). Mirrors the
            # established pattern at state/portfolio_state.py:910. Without it, two
            # concurrent add_halt() at the same scope both pass the active-halt
            # SELECT and the loser's INSERT raises a raw sqlite3.IntegrityError on
            # the UNIQUE PK — which is NOT a ValueError, so it escapes the CLI's
            # `except ValueError` and crashes emergency-stop BEFORE the bus signal.
            # With it, the 5s busy_timeout serializes the loser, which then sees the
            # winner's committed row and hits the ValueError guard below.
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Reject if active halt exists at this exact scope
                row = conn.execute(
                    "SELECT halt_epoch FROM halts "
                    "WHERE account_id=? AND asset_class=? AND asset=? "
                    "AND cleared_at IS NULL",
                    scope,
                ).fetchone()
                if row is not None:
                    raise ValueError(
                        f"active halt already exists at scope {scope} "
                        f"(epoch {row['halt_epoch']}); resume first or use different scope"
                    )

                # Compute next epoch (max + 1, default 1)
                row = conn.execute(
                    "SELECT COALESCE(MAX(halt_epoch), 0) AS max_e FROM halts "
                    "WHERE account_id=? AND asset_class=? AND asset=?",
                    scope,
                ).fetchone()
                next_epoch = (row["max_e"] or 0) + 1

                conn.execute(
                    "INSERT INTO halts (account_id, asset_class, asset, reason, "
                    "halted_at, halted_until, halt_epoch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*scope, reason, now, until, next_epoch),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

        record = HaltRecord(
            account_id=scope[0],
            asset_class=scope[1],
            asset=None if scope[2] == WILDCARD else scope[2],
            reason=reason,
            halted_at=pd.Timestamp(now),
            halted_until=halted_until,
            halt_epoch=next_epoch,
        )

        # Wave A wiring (ADR-0031 D2): emit kill_switch_fired governance event
        # after the SQLite INSERT but before the JSON mirror write. Audit
        # failure must NEVER block the halt from being created (silence-by-
        # default observation); we swallow exceptions and log a warning.
        try:
            from hermes_quant.governance import audit_log

            asof_dt = record.halted_at.to_pydatetime()
            if asof_dt.tzinfo is None:
                asof_dt = asof_dt.replace(tzinfo=UTC)
            audit_log.append(
                audit_log.GovernanceEvent(
                    kind="kill_switch_fired",
                    asof=asof_dt,
                    source="daemon.halt_state",
                    payload={
                        "account_id": record.account_id,
                        "asset_class": record.asset_class,
                        "asset": record.asset,
                        "reason": record.reason,
                        "halted_until": (
                            record.halted_until.isoformat()
                            if record.halted_until is not None
                            else None
                        ),
                        "halt_epoch": record.halt_epoch,
                    },
                )
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("audit_log.append failed for halt: %s", e)

        self._write_mirror()
        return record

    def clear_halt(
        self,
        account_id: str | None,
        asset_class: str | None,
        asset: str | None,
        reason: str,
    ) -> bool:
        """Clear an active halt at the given scope.

        Per synthesis-v2 §P2-θ: `--reason` is required for audit.

        Args:
            account_id: account or None for wildcard.
            asset_class: asset class or None for wildcard.
            asset: specific asset or None for wildcard.
            reason: why are you lifting this halt? (required, audit trail).

        Returns:
            True if a halt was cleared, False if none was active at this scope.

        Raises:
            ValueError: if reason is empty.
        """
        if not reason or not reason.strip():
            raise ValueError("clear_halt reason is required (audit trail)")

        scope = self._normalize_scope(account_id, asset_class, asset)
        now = _utc_now_iso()

        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE halts SET cleared_at=?, cleared_reason=? "
                "WHERE account_id=? AND asset_class=? AND asset=? "
                "AND cleared_at IS NULL",
                (now, reason, *scope),
            )
            cleared = cur.rowcount > 0

        if cleared:
            self._write_mirror()
        return cleared

    def auto_clear_expired(self) -> int:
        """Auto-clear halts whose `halted_until` has passed.

        Called by the daemon's tick loop at the start of each tick.

        Returns:
            Number of halts auto-cleared.
        """
        now = _utc_now_iso()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE halts SET cleared_at=?, cleared_reason='auto_expired' "
                "WHERE halted_until IS NOT NULL "
                "AND halted_until <= ? "
                "AND cleared_at IS NULL",
                (now, now),
            )
            n = cur.rowcount

        if n > 0:
            self._write_mirror()
        return n

    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool:
        """Check if a (account, asset_class, asset) is in halt scope.

        A halt at scope `('*', '*', '*')` halts everything. A halt at
        `('alpaca-paper', '*', '*')` halts that account at any asset class.
        A halt at `('*', 'crypto', 'BTC/USDT')` halts BTC/USDT at any account.

        Implementation: scope match if (account_match) AND (class_match)
        AND (asset_match), where _match means equal-or-wildcard.

        Args:
            account_id: specific account_id (NOT wildcard — caller's identity).
            asset_class: specific asset class.
            asset: specific asset, or None to mean "any asset in class".

        Returns:
            True if any active halt covers this scope.
        """
        check_asset = asset if asset is not None else WILDCARD

        with self._conn() as conn:
            rows = conn.execute(
                "SELECT account_id, asset_class, asset FROM halts "
                "WHERE cleared_at IS NULL "
                "AND (account_id = ? OR account_id = ?) "
                "AND (asset_class = ? OR asset_class = ?) "
                "AND (asset = ? OR asset = ?)",
                (account_id, WILDCARD, asset_class, WILDCARD, check_asset, WILDCARD),
            ).fetchall()

        return len(rows) > 0

    def active_halts(self) -> list[HaltRecord]:
        """List all active halts."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM halts WHERE cleared_at IS NULL ORDER BY halted_at DESC"
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> HaltRecord:
        return HaltRecord(
            account_id=row["account_id"],
            asset_class=row["asset_class"],
            asset=None if row["asset"] == WILDCARD else row["asset"],
            reason=row["reason"],
            halted_at=pd.Timestamp(row["halted_at"]),
            halted_until=pd.Timestamp(row["halted_until"]) if row["halted_until"] else None,
            halt_epoch=row["halt_epoch"],
        )

    def _write_mirror(self) -> None:
        """Write JSON mirror atomically. Called after every modification."""
        active = self.active_halts()
        data = [
            {
                "account_id": r.account_id,
                "asset_class": r.asset_class,
                "asset": r.asset,
                "reason": r.reason,
                "halted_at": r.halted_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "halted_until": (
                    r.halted_until.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    if r.halted_until is not None
                    else None
                ),
                "halt_epoch": r.halt_epoch,
            }
            for r in active
        ]
        _write_atomic_json(self.mirror_path, data)


def read_halt_mirror(path: Path = DEFAULT_HALT_JSON_MIRROR) -> list[dict]:
    """Read the JSON halt mirror without opening SQLite.

    Used by the freqtrade strategy (separate process, doesn't want SQLite
    lock contention) and by quant_doctor for fast diagnostics.

    Returns empty list if the mirror doesn't exist (no active halts).
    """
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupted mirror — caller should fall back to SQLite.
        return []
