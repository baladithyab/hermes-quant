"""hermes_quant.daemon.watermark — Per-symbol resume watermark store.

Per ADR-0038 §D.1 (TradingAgents pattern P3 backfill).

Each row records `(symbol, last_processed_bar_ts, indicator_snapshot_hash,
updated_at)`. Used by `tick_loop.run_one_tick` for resume idempotency: if
the daemon crashes mid-tick after `signal_bus.emit()` returns, the watermark
write may be lost, but downstream consumers idempotency-key on `signal_id`
already (ADR-0038 §D.1 invariant). On recovery, the watermark short-circuits
re-processing of a `(symbol, bar_ts)` we've already journaled.

Storage: single SQLite at `~/.hermes/quant/watermarks.db` (profile-aware,
mirroring `tools.QUANT_HOME` and `halt_state.DEFAULT_STATE_DB`). Schema is
`WITHOUT ROWID`; PK is `symbol`; one row per symbol (latest wins). WAL
journal mode + busy_timeout for concurrent-reader friendliness.

Watermark integration in `tick_loop` is opt-in via env
`HERMES_QUANT_WATERMARK_ENABLED=1`. When the flag is unset (default), the
tick path is bit-identical to legacy — the watermark module never executes.

This is **NOT** a SqliteSaver / LangGraph checkpointer — see ADR-0038 §D.1
for the design rationale (we have no DAG; one flat row per symbol).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def _resolve_profile_path() -> Path:
    """Resolve `watermarks.db` path with profile awareness.

    Per ADR-0013 §D4: when `HERMES_PROFILE` env is set, state lives at
    `~/.hermes/profiles/<name>/quant/`; otherwise the global
    `~/.hermes/quant/`. Mirrors the convention used by
    `hermes_quant.watchlist.get_config_path` and the `state.db` location
    in `hermes_quant.tools.QUANT_HOME`.
    """
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        return Path.home() / ".hermes" / "profiles" / profile / "quant" / "watermarks.db"
    return Path.home() / ".hermes" / "quant" / "watermarks.db"


_SCHEMA = """
-- v2 schema (ADR-0038 §D.1 watermark composite-key correction).
-- Composite PK on (symbol, exchange, timeframe) — keying on `symbol` alone
-- caused multi-timeframe horizons (e.g. 1d + 1w on the same symbol) to
-- silently kill each other when the first emit's bar_ts shadowed the
-- second's in the watermark short-circuit. See codex P2 finding
-- 2026-05-26 (tests + concurrency lens).
CREATE TABLE IF NOT EXISTS watermark_v2 (
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    last_processed_bar_ts TEXT NOT NULL,
    indicator_snapshot_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, exchange, timeframe)
) WITHOUT ROWID;

-- v1 schema (legacy, tombstone). New writes go to watermark_v2; this
-- table is preserved so any pre-migration row remains queryable for
-- forensic purposes. Reads do NOT fall back to v1 — a missing v2 row
-- is treated as "no watermark" and the bar is re-processed (safe per
-- ADR-0038 §D.1: signal_id is the canonical idempotency key).
CREATE TABLE IF NOT EXISTS watermark (
    symbol TEXT PRIMARY KEY,
    last_processed_bar_ts TEXT NOT NULL,
    indicator_snapshot_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;
"""


def _ts_to_iso(ts: pd.Timestamp) -> str:
    """Serialize tz-naive UTC pandas Timestamp to ISO 8601."""
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ts_from_iso(s: str) -> pd.Timestamp:
    """Parse ISO 8601 back to tz-naive UTC pandas Timestamp.

    Raises ValueError on malformed input.
    """
    ts = pd.Timestamp(s)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


@dataclass(frozen=True, slots=True)
class Watermark:
    """Per-(symbol, exchange, timeframe) resume marker.

    Per ADR-0038 §D.1.

    Attributes:
        symbol: e.g. "BTC/USDT" or "AAPL".
        exchange: e.g. "binance" or "alpaca". Required for the composite
            PK — a symbol can be quoted on multiple venues.
        timeframe: e.g. "1d", "1h", "5m". Required for the composite PK
            so multi-timeframe horizons on the same symbol don't shadow
            each other in the watermark short-circuit.
        last_processed_bar_ts: tz-naive UTC. Inclusive — a tick whose
            `ctx.bar_ts <= last_processed_bar_ts` is a replay and is skipped.
        indicator_snapshot_hash: 16-hex-char prefix of sha256 over a
            deterministic projection of `MarketContext` (asset, timeframe,
            asset_class, last_close, last_volume, asof_iso). Used for
            replay-detection diagnostics; mismatch logs a warning but does
            NOT raise (per ADR-0038 §D.1 "hash-mismatch warning path").
        updated_at: tz-naive UTC clock anchor; monotonic per-process.
    """

    symbol: str
    exchange: str
    timeframe: str
    last_processed_bar_ts: pd.Timestamp
    indicator_snapshot_hash: str
    updated_at: pd.Timestamp


class WatermarkStore:
    """SQLite-backed ((symbol, exchange, timeframe) -> Watermark) store.

    Writes are atomic via WAL + INSERT OR REPLACE on the composite PK.
    Reads are lock-free under WAL. Profile-aware via
    `_resolve_profile_path`.

    Thread-safe via per-instance RLock; multi-process safe via SQLite's
    own file locking (we set `busy_timeout` so writers don't fail-fast on
    contention).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.db_path = path if path is not None else _resolve_profile_path()
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with WAL + 5s busy timeout."""
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def get(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
    ) -> Watermark | None:
        """Return latest watermark for `(symbol, exchange, timeframe)`, or None.

        Raises ValueError if the stored row is malformed (corrupt
        timestamp). Callers should treat ValueError as a "missing
        watermark" signal and re-process the bar.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT symbol, exchange, timeframe, last_processed_bar_ts, "
                "indicator_snapshot_hash, updated_at "
                "FROM watermark_v2 "
                "WHERE symbol = ? AND exchange = ? AND timeframe = ?",
                (symbol, exchange, timeframe),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_watermark(row)

    def set(self, wm: Watermark) -> None:
        """Insert or overwrite the watermark for the composite PK.

        Atomic via INSERT OR REPLACE on the PK. Latest write wins.
        """
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO watermark_v2 "
                "(symbol, exchange, timeframe, last_processed_bar_ts, "
                "indicator_snapshot_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    wm.symbol,
                    wm.exchange,
                    wm.timeframe,
                    _ts_to_iso(wm.last_processed_bar_ts),
                    wm.indicator_snapshot_hash,
                    _ts_to_iso(wm.updated_at),
                ),
            )

    def all_for_keys(
        self,
        keys: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], Watermark]:
        """Batch-read watermarks for a list of (symbol, exchange, timeframe).

        Returns a dict mapping composite-key tuple -> Watermark, omitting
        keys that have no row. Empty `keys` returns `{}` without touching
        the DB.

        Malformed rows raise ValueError (consistent with `.get`); the
        store is not corrupted by the failed read since reads are
        side-effect-free.
        """
        if not keys:
            return {}
        # Build IN-clause with a fresh placeholder triple per key.
        placeholders = ",".join("(?, ?, ?)" for _ in keys)
        flat: list[str] = []
        for k in keys:
            flat.extend(k)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT symbol, exchange, timeframe, last_processed_bar_ts, "
                f"indicator_snapshot_hash, updated_at "
                f"FROM watermark_v2 "
                f"WHERE (symbol, exchange, timeframe) IN ({placeholders})",
                tuple(flat),
            ).fetchall()
        out: dict[tuple[str, str, str], Watermark] = {}
        for row in rows:
            wm = self._row_to_watermark(row)
            out[(wm.symbol, wm.exchange, wm.timeframe)] = wm
        return out

    @staticmethod
    def _row_to_watermark(row: sqlite3.Row) -> Watermark:
        """Convert a sqlite3.Row to a Watermark, validating timestamps.

        Raises ValueError on malformed timestamp columns.
        """
        try:
            last_ts = _ts_from_iso(row["last_processed_bar_ts"])
            updated_at = _ts_from_iso(row["updated_at"])
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"corrupt watermark row for "
                f"(symbol={row['symbol']!r}, exchange={row['exchange']!r}, "
                f"timeframe={row['timeframe']!r}): {e}"
            ) from e
        return Watermark(
            symbol=row["symbol"],
            exchange=row["exchange"],
            timeframe=row["timeframe"],
            last_processed_bar_ts=last_ts,
            indicator_snapshot_hash=row["indicator_snapshot_hash"],
            updated_at=updated_at,
        )
