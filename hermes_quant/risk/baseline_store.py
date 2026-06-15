"""hermes_quant.risk.baseline_store — Durable drawdown / daily-loss baselines.

cs01 fix — the ADR-0004 risk gate's Rule-1 (drawdown) and Rule-2 (daily-loss)
circuit breakers divide current equity by ``peak_equity`` / ``daily_open_equity``.
Both denominators are collapsed to the inception baseline (``initial_cash``) by
``daemon/portfolio_loader.py`` (no durable cross-tick high-water mark, no
session-boundary reset), so a profitable-from-inception account that suffers a
large peak-to-trough fall measures its loss against its ORIGIN rather than its
running peak / session-open — and the breaker silently does NOT fire. That is a
FAIL-OPEN in the FINAL money-safety authority — the most dangerous bug class.

This module supplies the missing DURABLE baselines, modeled directly on
``daemon/halt_state.py::HaltStateSQLite``:

- A ``state.db`` row per ``(account_id, asset_class)`` partition + an atomic JSON
  mirror (``~/.hermes/quant/drawdown_baselines.json``) written via tmp→replace.
- Per-connection WAL + ``synchronous=NORMAL`` + 5s busy timeout + an ``RLock``,
  copied verbatim from ``HaltStateSQLite._conn``.
- FAIL-CLOSED reads: a corrupt / missing / locked store NEVER reads as "no
  drawdown". ``reconcile`` catches read/open failures and returns a conservative
  in-memory baseline (peak = max seen this process; daily_open = the session-open
  mark) + a warning, and NEVER raises out into the gate.

Safety direction (proof obligation): the durable peak is a monotonic running MAX
(a true high-water mark, never decreases) and the daily_open re-seeds only at the
session boundary, so the recomputed drawdown / daily-loss are ALWAYS
``>=`` the loader's inception-collapsed values. The circuit breaker can therefore
only ever trip EARLIER / equally — which, for a circuit breaker, is always the
safe direction. A fresh account (peak == open == now) recomputes to drawdown 0 /
daily-loss 0 — byte-identical to today, no spurious trip.

SCOPE: this lane wires the store into the gate behind an opt-in
``baseline_store=`` seam (default ``None`` = byte-identical to today). The live
wiring into the advisor / portfolio_loader path is a separate operator-gated step.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Same durable state.db as the halt registry (daemon/halt_state.py:43-44).
DEFAULT_STATE_DB = Path.home() / ".hermes" / "quant" / "state.db"
DEFAULT_BASELINE_JSON_MIRROR = Path.home() / ".hermes" / "quant" / "drawdown_baselines.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drawdown_baselines (
    account_id        TEXT NOT NULL,
    asset_class       TEXT NOT NULL,
    peak_equity       REAL NOT NULL,   -- monotonic running MAX of realized equity (HWM)
    daily_open_equity REAL NOT NULL,   -- session-anchored open (reset at session boundary)
    session_key       TEXT NOT NULL,   -- the session-boundary identity daily_open is anchored to
    updated_at        TEXT NOT NULL,   -- ISO 8601 UTC
    PRIMARY KEY (account_id, asset_class)
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class Baseline:
    """The conservative durable baselines the gate recomputes Rule-1/Rule-2 against.

    ``peak_equity`` is the monotonic running MAX of realized equity (the drawdown
    denominator). ``daily_open_equity`` is the session-anchored open (the
    daily-loss denominator). ``degraded`` is True when this baseline came from the
    fail-closed in-memory fallback (durable store unreadable) rather than the
    persisted row — surfaced so callers/tests can assert the safe path was taken.
    """

    peak_equity: float
    daily_open_equity: float
    session_key: str
    degraded: bool = False


def _utc_now_iso() -> str:
    """ISO 8601 UTC with 'Z' suffix (mirrors halt_state._utc_now_iso)."""
    return pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_atomic_json(path: Path, data: dict) -> None:
    """Atomic-rename pattern for the JSON mirror (mirrors halt_state._write_atomic_json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic on POSIX


def session_key(tz: str, asof: datetime | pd.Timestamp) -> str:
    """Session-boundary identity the daily_open is anchored to.

    Shares the gate's session clock with ``hermes_quant.risk.gate._next_session_open``
    so the daily-loss rail and its halt-until-session re-arm agree on what a
    "session" is:

    - UTC (sessionless 24/7 crypto): the calendar DATE of ``asof`` — a new UTC day
      is a new session, exactly as ``_next_session_open`` rolls to next-UTC-day
      midnight.
    - Non-UTC (equities/futures with sessions): the calendar DATE of ``asof`` in
      that tz — the v0.1.x ``now + 24h`` convention bounds a halt by ~one session;
      anchoring the daily_open per local calendar date is the matching daily rail.
      (v0.1.2 will use ``trading_calendars`` for exact session boundaries; this
      identity is the matching placeholder, never looser than per-day.)

    Pure; never raises on a well-formed tz/asof. A bad tz falls back to UTC date.
    """
    ts = asof if isinstance(asof, pd.Timestamp) else pd.Timestamp(asof)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    if tz.upper() == "UTC":
        return ts.tz_convert("UTC").strftime("%Y-%m-%d")
    try:
        local = ts.tz_convert(tz)
    except Exception:  # noqa: BLE001 - unknown tz string falls back to UTC date
        local = ts.tz_convert("UTC")
    return local.strftime("%Y-%m-%d")


class DrawdownBaselineStore:
    """SQLite-backed durable peak-equity / session-open store.

    The drawdown-baseline analog of ``daemon/halt_state.py::HaltStateSQLite``:
    same ``state.db`` file by default, same atomic JSON mirror, same WAL + RLock +
    5s-busy-timeout posture, same fail-closed read discipline.

    Keyed by ``(account_id, asset_class)`` — the same partition the drawdown halt
    scopes to (protocol.py Portfolio is per (account, asset_class)).
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_STATE_DB,
        mirror_path: Path = DEFAULT_BASELINE_JSON_MIRROR,
    ):
        self.db_path = db_path
        self.mirror_path = mirror_path
        self._lock = threading.RLock()
        # In-memory conservative fallback used when the durable layer is
        # unreadable (fail-closed): the max equity seen this process per
        # partition, and the session-open mark. NEVER decreases.
        self._mem_peak: dict[tuple[str, str], float] = {}
        self._mem_daily_open: dict[tuple[str, str], float] = {}
        self._mem_session: dict[tuple[str, str], str] = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with WAL + 5s busy timeout (mirrors HaltStateSQLite._conn)."""
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

    def reconcile(
        self,
        account_id: str,
        asset_class: str,
        equity_total: float,
        asof: datetime | pd.Timestamp,
        tz: str,
    ) -> Baseline:
        """Reconcile + persist the durable baselines for this partition.

        Conservative-by-construction — can ONLY raise the baselines, so the
        circuit breaker only ever trips EARLIER / equally:

        1. ``peak_equity = max(stored.peak_equity, equity_total)`` — monotonic
           HWM, never decreases. Seed = equity on no row.
        2. session boundary via :func:`session_key`. New session → re-anchor
           ``daily_open_equity = equity_total``; same session → keep the stored
           session-open mark (the anchor is the session OPEN, never a trailing
           high). Seed = equity on no row.
        3. Persist the row + atomic JSON mirror BEFORE returning (a restart can't
           un-track the HWM).

        FAIL-CLOSED: any read/open/write failure (first run, corrupt DB, locked,
        OSError, sqlite error) is caught — the method returns a conservative
        in-memory baseline (``Baseline.degraded=True``) and logs a warning, and
        NEVER raises out into the gate.
        """
        key = (account_id, asset_class)
        cur_session = session_key(tz, asof)
        try:
            eq = float(equity_total)
        except (TypeError, ValueError):
            eq = float("nan")

        # A non-finite equity (NaN / inf) must NOT be written into the NOT NULL
        # REAL columns (it would either be rejected by SQLite or persist a NaN
        # that corrupts the durable HWM). Return a conservative degraded baseline
        # carrying the non-finite numerator so the gate's downstream recompute
        # (_pct_from_baseline → protocol.py 1.0 sentinel) trips the breaker
        # fail-CLOSED, without touching the durable row.
        if not math.isfinite(eq):
            prev_peak = self._mem_peak.get(key)
            return Baseline(
                peak_equity=prev_peak if prev_peak is not None else eq,
                daily_open_equity=self._mem_daily_open.get(key, eq),
                session_key=cur_session,
                degraded=True,
            )

        try:
            with self._lock, self._conn() as conn:
                row = conn.execute(
                    "SELECT peak_equity, daily_open_equity, session_key "
                    "FROM drawdown_baselines WHERE account_id=? AND asset_class=?",
                    (account_id, asset_class),
                ).fetchone()

                if row is None:
                    peak = eq
                    daily_open = eq
                    sess = cur_session
                else:
                    peak = max(float(row["peak_equity"]), eq)
                    if str(row["session_key"]) != cur_session:
                        # New session boundary → re-anchor the daily open mark.
                        daily_open = eq
                        sess = cur_session
                    else:
                        # Same session → keep the session-OPEN mark (NOT a
                        # trailing high). A profitable session keeps the lower
                        # open; a losing session still measures vs the open.
                        daily_open = float(row["daily_open_equity"])
                        sess = str(row["session_key"])

                now_iso = _utc_now_iso()
                conn.execute(
                    "INSERT INTO drawdown_baselines "
                    "(account_id, asset_class, peak_equity, daily_open_equity, "
                    " session_key, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, asset_class) DO UPDATE SET "
                    "peak_equity=excluded.peak_equity, "
                    "daily_open_equity=excluded.daily_open_equity, "
                    "session_key=excluded.session_key, "
                    "updated_at=excluded.updated_at",
                    (account_id, asset_class, peak, daily_open, sess, now_iso),
                )

            # Keep the in-memory conservative shadow in sync (used iff a later
            # call fails closed) — and never let it decrease.
            self._mem_peak[key] = max(self._mem_peak.get(key, peak), peak)
            self._mem_daily_open[key] = daily_open
            self._mem_session[key] = sess

            self._write_mirror_safe()
            return Baseline(
                peak_equity=peak, daily_open_equity=daily_open, session_key=sess
            )
        except (sqlite3.Error, OSError, ValueError) as e:
            # FAIL-CLOSED: a durable failure must NEVER read as "no drawdown".
            return self._fallback_baseline(key, eq, cur_session, e)

    def _fallback_baseline(
        self,
        key: tuple[str, str],
        equity: float,
        cur_session: str,
        exc: Exception,
    ) -> Baseline:
        """Conservative in-memory baseline when the durable store is unreadable.

        peak = max(max-seen-this-process, equity) (HWM, never decreases);
        daily_open = the session-open mark (re-anchored on a new session, else the
        retained mark). Always ``>=`` the loader's inception-collapsed values, so
        the breaker can only trip EARLIER. Logged, never raised.
        """
        logger.warning(
            "DrawdownBaselineStore.reconcile failed for %s (%s) — failing CLOSED "
            "to conservative in-memory baseline (durable layer best-effort)",
            key,
            exc,
        )
        prev_peak = self._mem_peak.get(key)
        peak = equity if prev_peak is None else max(prev_peak, equity)
        # peak must never go below a previously-seen value even if equity is NaN.
        if prev_peak is not None and not (peak == peak):  # NaN guard
            peak = prev_peak
        self._mem_peak[key] = peak

        prev_session = self._mem_session.get(key)
        if prev_session is None or prev_session != cur_session:
            daily_open = equity
        else:
            daily_open = self._mem_daily_open.get(key, equity)
        self._mem_daily_open[key] = daily_open
        self._mem_session[key] = cur_session
        return Baseline(
            peak_equity=peak,
            daily_open_equity=daily_open,
            session_key=cur_session,
            degraded=True,
        )

    def get(self, account_id: str, asset_class: str) -> Baseline | None:
        """Read the persisted baseline for a partition, or None if absent.

        Fail-closed: a read failure returns None (caller treats absence as
        "seed at current equity" / conservative), never a fabricated baseline.
        """
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT peak_equity, daily_open_equity, session_key "
                    "FROM drawdown_baselines WHERE account_id=? AND asset_class=?",
                    (account_id, asset_class),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row is None:
            return None
        return Baseline(
            peak_equity=float(row["peak_equity"]),
            daily_open_equity=float(row["daily_open_equity"]),
            session_key=str(row["session_key"]),
        )

    def _write_mirror_safe(self) -> None:
        """Write the JSON mirror atomically. Best-effort: a mirror failure is
        logged and swallowed — the SQLite row is the source of truth, the mirror
        is only a fast cold-start / external-read convenience (mirrors the halt
        registry's mirror discipline)."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT account_id, asset_class, peak_equity, "
                    "daily_open_equity, session_key, updated_at "
                    "FROM drawdown_baselines ORDER BY account_id, asset_class"
                ).fetchall()
            data = {
                f"{r['account_id']}|{r['asset_class']}": {
                    "account_id": r["account_id"],
                    "asset_class": r["asset_class"],
                    "peak_equity": r["peak_equity"],
                    "daily_open_equity": r["daily_open_equity"],
                    "session_key": r["session_key"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            }
            _write_atomic_json(self.mirror_path, data)
        except (sqlite3.Error, OSError) as e:  # pragma: no cover - defensive
            logger.warning("drawdown_baselines mirror write failed: %s", e)


def read_baseline_mirror(path: Path = DEFAULT_BASELINE_JSON_MIRROR) -> dict:
    """Read the JSON baseline mirror without opening SQLite.

    Mirrors ``daemon.halt_state.read_halt_mirror``. Returns ``{}`` if the mirror
    doesn't exist or is corrupt (caller should fall back to SQLite).
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
