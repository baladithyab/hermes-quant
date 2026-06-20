"""hermes_quant.state.composite_plays — Composite-play lifecycle store (ADR-0098 Part B).

The PortfolioState fold ALREADY skips multi_leg parents (_is_multileg_family_parent at
state/portfolio_state.py:115 — H2/H3 satisfied at the fold). This module is the COMPOSITE
LIFECYCLE store: it tracks the state-machine of a whole multi-leg composite from open
through partial-decompose to close.

Design decisions
----------------
1. DEFAULT-OFF / ADDITIVE: this is a NEW table + NEW module. No existing fold or path is
   changed. The live system is byte-identical because the store is only written by a future
   multi-leg origination path that is itself flag-gated.

2. State machine for state ∈ {open, decomposed, closed, partial}:
   - open → partial   (first leg close in a decompose sequence; NEVER auto-closed)
   - open → closed    (all legs closed atomically via one shot)
   - open → decomposed (composite explicitly decomposed — all legs now independent)
   - partial → closed  (remaining legs closed after partial)
   - partial → decomposed (remaining legs decomposed after partial)
   - closed/decomposed → anything: RAISES (illegal transition)
   - partial → open: RAISES (cannot un-partial a composite; operator must manually reset)

3. BEGIN IMMEDIATE: every write acquires the write lock at transaction start (ar04 family).
   The table lives in the SAME state.db as PortfolioState (injected path) to avoid
   cross-db atomicity puzzles.

4. Finite guards on every numeric input (ar08 family): net_entry_price, net_fill_price,
   fill_size_pct, max_loss must all be finite when provided.

5. Orphan detection: a composite is orphaned when state=='open' but active_leg_count <
   expected_leg_count (a leg was closed / disappeared without transitioning the composite).
   detect_orphan() is a PURE READ — it does NOT transition state (that is the caller's
   responsibility). The H1 partial guard fires only on an explicit leg-close path.

References
----------
ADR-0098 Part B (docs/adr/ADR-0098-options-strategy-taxonomy-and-two-level-multileg.md):
  §composite_plays table, §H1-H6 hazards, §Confirmation item (4).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = _resolve_quant_home()
DEFAULT_COMPOSITE_DB = QUANT_HOME / "state.db"

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_OPEN = "open"
STATE_PARTIAL = "partial"
STATE_CLOSED = "closed"
STATE_DECOMPOSED = "decomposed"

_ALL_STATES = frozenset({STATE_OPEN, STATE_PARTIAL, STATE_CLOSED, STATE_DECOMPOSED})

# Legal forward-transitions.  A source state maps to the set of states it MAY
# transition TO.  Any other transition raises IllegalTransitionError.
# "closed" and "decomposed" are TERMINAL — no forward edges.
#
# partial → partial is allowed as a "stay" (H1: a partial composite NEVER
# auto-closes; the state machine can be called multiple times on a partial
# composite and it simply stays partial until all remaining legs close).
# This is a self-loop, NOT a back-edge to open.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_OPEN: frozenset({STATE_PARTIAL, STATE_CLOSED, STATE_DECOMPOSED}),
    STATE_PARTIAL: frozenset({STATE_PARTIAL, STATE_CLOSED, STATE_DECOMPOSED}),
    STATE_CLOSED: frozenset(),
    STATE_DECOMPOSED: frozenset(),
}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS composite_plays (
    multi_leg_id         TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL DEFAULT 'paper-default',
    underlying           TEXT NOT NULL,
    strategy_kind        TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'open',
    opened_at            TEXT NOT NULL,
    closed_at            TEXT,
    outer_qty            INTEGER NOT NULL,
    net_entry_price      REAL NOT NULL,
    net_fill_price       REAL,
    fill_size_pct        REAL NOT NULL,
    expected_leg_count   INTEGER NOT NULL,
    max_loss             REAL,
    option_legs_json     TEXT NOT NULL DEFAULT '[]'
);
"""

# ml00b: ADDITIVE migration for EXISTING composite_plays tables (DBs created
# before option_legs_json existed). A fresh DB gets the column from _SCHEMA
# above; an existing one gets it via ALTER TABLE guarded by a PRAGMA
# table_info check. Pre-existing rows take the DEFAULT '[]' (no destructive
# rewrite, no NULL crash — _decode_legs treats NULL/'' as []).
_OPTION_LEGS_COLUMN = "option_legs_json"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IllegalTransitionError(ValueError):
    """Raised when a caller attempts an illegal composite-state transition.

    Examples of illegal transitions:
      - closed → open  (terminal state)
      - partial → open (cannot un-partial)
      - open → closed when skipping partial (allowed; partial is only required
        when the first leg in a DECOMPOSE sequence closes before the rest)
    """


class CompositeNotFoundError(KeyError):
    """Raised when a multi_leg_id is not found in composite_plays."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CompositePlayRow:
    """Read-view of one composite_plays row."""

    multi_leg_id: str
    account_id: str
    underlying: str
    strategy_kind: str
    state: str
    opened_at: str
    closed_at: str | None
    outer_qty: int
    net_entry_price: float
    net_fill_price: float | None
    fill_size_pct: float
    expected_leg_count: int
    max_loss: float | None
    # ml00b: the option legs of this composite (each a dict with at least an OCC
    # `symbol`, typically {symbol, side, position_intent}). [] for a legacy /
    # pre-migration row. agmon1/agmon2 read this to mark + sign the net P&L.
    option_legs: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CompositePlaysStore
# ---------------------------------------------------------------------------


class CompositePlaysStore:
    """Composite-play lifecycle store (ADR-0098 Part B).

    Tracks the state-machine of a whole multi-leg composite from 'open' through
    'partial' (first leg closed in a decompose sequence, never auto-closed) to
    'closed' or 'decomposed'.

    Thread-safe via a per-instance RLock.  Multi-process safe via SQLite WAL
    mode (same pattern as PortfolioState / HaltStateSQLite).

    The store is injected with a db_path so tests can use a tmpdir path
    without touching ~/.hermes/quant/state.db.

    Usage
    -----
        store = CompositePlaysStore()
        store.open_composite(
            multi_leg_id="prop_...",
            account_id="paper-default",
            underlying="AAPL",
            strategy_kind="covered_call",
            opened_at="2026-06-17T10:00:00.000000Z",
            outer_qty=1,
            net_entry_price=1.50,
            fill_size_pct=0.05,
            expected_leg_count=2,
        )
        store.record_leg_close("prop_...", is_decompose=True)
        store.detect_orphan("prop_...", active_leg_count=1)
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DEFAULT_COMPOSITE_DB
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management (mirrors PortfolioState._conn)
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
            self._migrate_option_legs_column(conn)

    @staticmethod
    def _migrate_option_legs_column(conn: sqlite3.Connection) -> None:
        """ml00b: additively add option_legs_json to an EXISTING table.

        For a fresh DB the column already exists (it is in _SCHEMA); this is a
        no-op. For a DB created before this column, ALTER TABLE adds it with the
        '[]' default so every pre-existing row reads back option_legs == []
        (backward-compatible, no destructive migration, no NULL crash).

        CONCURRENCY (cx3a): _conn() opens with isolation_level=None (autocommit),
        so this check-then-ALTER is NOT atomic. Two cron PROCESSES can both
        PRAGMA-check the column as missing; the first ALTER commits (instantly
        visible under autocommit), and the loser's ALTER then raises
        ``OperationalError: duplicate column name: option_legs_json``. On the live
        path that error is swallowed inside _persist_composite_play AFTER a real
        options fire, leaving NO composite row (the agmon1/agmon2 unblock silently
        fails). The migration must be IDEMPOTENT: the loser's duplicate-column
        ALTER is caught and treated as a no-op (the column ends up present either
        way — the laziest race-safe fix, mirroring portfolio_state's intent).
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(composite_plays)")}
        if _OPTION_LEGS_COLUMN not in cols:
            try:
                conn.execute(
                    f"ALTER TABLE composite_plays "
                    f"ADD COLUMN {_OPTION_LEGS_COLUMN} TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError as exc:
                # Another process won the migration race between our PRAGMA check
                # and this ALTER. The column is now present -> idempotent no-op.
                # Any OTHER OperationalError (e.g. the table genuinely missing) is
                # a real failure and re-raised.
                if "duplicate column name" not in str(exc).lower():
                    raise
                logger.debug(
                    "composite_plays: option_legs_json migration lost the race "
                    "(another process already added it) -> no-op"
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def _assert_finite(name: str, value: float | None) -> None:
        """Finite-guard a numeric input (ar08 family).

        NaN/inf defeats every <= gate. Raises ValueError on non-finite.
        None is explicitly allowed (nullable columns).
        """
        if value is None:
            return
        if not math.isfinite(value):
            raise ValueError(
                f"composite_plays: {name} must be finite, got {value!r}"
            )

    @staticmethod
    def _assert_valid_state(state: str) -> None:
        if state not in _ALL_STATES:
            raise ValueError(
                f"composite_plays: unknown state {state!r}; "
                f"must be one of {sorted(_ALL_STATES)}"
            )

    @staticmethod
    def _encode_legs(option_legs: list[dict] | None) -> str:
        """Validate + JSON-serialize the option legs (ml00b, fail-CLOSED).

        agmon1/agmon2 read each leg's OCC ``symbol`` + ``side`` to mark and sign
        the net P&L. A leg with NO usable symbol would re-create the agmon1
        dead-path (a legless composite row), so we RAISE rather than silently
        store one. The whole list must also be JSON-serializable — a leg that
        cannot round-trip is rejected at write time, never persisted as an
        un-readable row.

        None / [] both serialize to '[]'.
        """
        if not option_legs:
            return "[]"
        for i, leg in enumerate(option_legs):
            if not isinstance(leg, dict):
                raise ValueError(
                    f"composite_plays: option_legs[{i}] must be a dict, "
                    f"got {type(leg).__name__}"
                )
            symbol = leg.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError(
                    f"composite_plays: option_legs[{i}] must carry a non-empty "
                    f"string 'symbol' (OCC); got {symbol!r}"
                )
        # json.dumps raises TypeError on a non-serializable value — fail-CLOSED
        # (the caller's BEGIN IMMEDIATE/ROLLBACK guard rolls back, no row lands).
        return json.dumps(option_legs)

    @staticmethod
    def _decode_legs(raw: str | None) -> list[dict]:
        """Decode the stored JSON back into a list of leg dicts.

        A NULL / empty / pre-migration value reads back as [] (backward-compat).
        A corrupt blob fails CLOSED to [] (observability layer — a malformed
        legs blob must never crash the sweep that reads open composites).
        """
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("composite_plays: corrupt option_legs_json (%r) -> []", raw)
            return []
        return decoded if isinstance(decoded, list) else []

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def open_composite(
        self,
        *,
        multi_leg_id: str,
        account_id: str = "paper-default",
        underlying: str,
        strategy_kind: str,
        opened_at: str | None = None,
        outer_qty: int,
        net_entry_price: float,
        fill_size_pct: float,
        expected_leg_count: int,
        net_fill_price: float | None = None,
        max_loss: float | None = None,
        option_legs: list[dict] | None = None,
    ) -> None:
        """Insert a new composite play with state='open'.

        Parameters
        ----------
        option_legs : list[dict] | None
            ml00b: the option legs of this composite, each a dict carrying at
            least ``symbol`` (OCC-21) — typically ``{symbol, side,
            position_intent}``. Stored as JSON in option_legs_json. None / []
            store as '[]'. agmon1/agmon2 read these back to mark + sign the
            net P&L of the open composite. A leg with no symbol raises
            (fail-CLOSED): a legless row would re-create the agmon1 dead-path.

        Raises
        ------
        ValueError
            If any numeric input is non-finite, OR a leg has no usable symbol.
        TypeError
            If a leg is not JSON-serializable.
        sqlite3.IntegrityError
            If multi_leg_id already exists (PRIMARY KEY conflict).
        """
        if opened_at is None:
            opened_at = self._utc_now_iso()

        # Finite guards (ar08 family)
        self._assert_finite("net_entry_price", net_entry_price)
        self._assert_finite("fill_size_pct", fill_size_pct)
        self._assert_finite("net_fill_price", net_fill_price)
        self._assert_finite("max_loss", max_loss)

        # ml00b: validate + serialize legs BEFORE the write (fail-CLOSED — a
        # malformed/non-serializable leg raises here and no row is inserted).
        option_legs_json = self._encode_legs(option_legs)

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO composite_plays
                        (multi_leg_id, account_id, underlying, strategy_kind,
                         state, opened_at, closed_at, outer_qty, net_entry_price,
                         net_fill_price, fill_size_pct, expected_leg_count, max_loss,
                         option_legs_json)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        multi_leg_id,
                        account_id,
                        underlying,
                        strategy_kind,
                        STATE_OPEN,
                        opened_at,
                        outer_qty,
                        net_entry_price,
                        net_fill_price,
                        fill_size_pct,
                        expected_leg_count,
                        max_loss,
                        option_legs_json,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def record_leg_close(
        self,
        multi_leg_id: str,
        *,
        is_decompose: bool = False,
        legs_remaining: int | None = None,
        net_fill_price: float | None = None,
        closed_at: str | None = None,
    ) -> str:
        """Record a leg close and advance the composite state machine.

        Transition rules
        ----------------
        H1 (ADR-0098): on the FIRST leg close in a DECOMPOSE sequence, write
        state='partial'. A 'partial' composite NEVER auto-closes; it requires
        explicit operator attention.

        When is_decompose=False (direct all-legs-closed path) OR when
        legs_remaining==0 (all legs accounted for), transition to 'closed'.

        Transition matrix (from current state):
          open    + is_decompose=True  → partial   (H1: first leg close)
          open    + is_decompose=False → closed     (atomic single close)
          partial + legs_remaining==0  → closed     (last leg closed)
          partial + legs_remaining>0   → partial    (stays partial — NEVER auto)

        Illegal transitions (raises IllegalTransitionError):
          closed → any
          decomposed → any
          partial → open  (cannot un-partial)

        Parameters
        ----------
        multi_leg_id : str
            The composite to update.
        is_decompose : bool
            True when this leg close is part of a decompose sequence (H1 guard).
        legs_remaining : int | None
            Number of legs still open AFTER this close. None = unknown (treated
            as >0 for partial sequences, i.e. stays partial or stays closed on
            a direct-close path).
        net_fill_price : float | None
            Updated net fill price to write (optional).
        closed_at : str | None
            ISO timestamp for the close; defaults to UTC now.

        Returns
        -------
        str
            The new state after the transition.

        Raises
        ------
        CompositeNotFoundError
            If multi_leg_id is not found.
        IllegalTransitionError
            If the current state does not permit the requested transition.
        ValueError
            If net_fill_price is non-finite.
        """
        if closed_at is None:
            closed_at = self._utc_now_iso()

        self._assert_finite("net_fill_price", net_fill_price)

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state, expected_leg_count FROM composite_plays "
                    "WHERE multi_leg_id = ?",
                    (multi_leg_id,),
                ).fetchone()

                if row is None:
                    conn.execute("ROLLBACK")
                    raise CompositeNotFoundError(
                        f"composite_plays: multi_leg_id {multi_leg_id!r} not found"
                    )

                current_state = row["state"]
                expected_leg_count = row["expected_leg_count"]

                # Determine target state
                target_state = _compute_target_state(
                    current_state=current_state,
                    is_decompose=is_decompose,
                    legs_remaining=legs_remaining,
                    expected_leg_count=expected_leg_count,
                )

                # Apply the write
                _closed_at_val = closed_at if target_state == STATE_CLOSED else None
                update_parts = ["state = ?"]
                update_args: list = [target_state]

                if net_fill_price is not None:
                    update_parts.append("net_fill_price = ?")
                    update_args.append(net_fill_price)

                if target_state == STATE_CLOSED:
                    update_parts.append("closed_at = ?")
                    update_args.append(closed_at)

                sql = (
                    "UPDATE composite_plays SET "
                    + ", ".join(update_parts)
                    + " WHERE multi_leg_id = ?"
                )
                update_args.append(multi_leg_id)

                conn.execute(sql, update_args)
                conn.execute("COMMIT")
                return target_state

            except (CompositeNotFoundError, IllegalTransitionError):
                conn.execute("ROLLBACK")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def transition_state(
        self,
        multi_leg_id: str,
        *,
        target_state: str,
        closed_at: str | None = None,
    ) -> None:
        """Explicit state transition (for decompose / convert paths).

        More general than record_leg_close; validates against the legal
        transition table and raises IllegalTransitionError on any illegal move.

        Parameters
        ----------
        multi_leg_id : str
            The composite to update.
        target_state : str
            Target state — must be in _ALL_STATES.
        closed_at : str | None
            ISO timestamp to write into closed_at when target is STATE_CLOSED.

        Raises
        ------
        CompositeNotFoundError, IllegalTransitionError, ValueError
        """
        self._assert_valid_state(target_state)
        if closed_at is None:
            closed_at = self._utc_now_iso()

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state FROM composite_plays WHERE multi_leg_id = ?",
                    (multi_leg_id,),
                ).fetchone()

                if row is None:
                    conn.execute("ROLLBACK")
                    raise CompositeNotFoundError(
                        f"composite_plays: multi_leg_id {multi_leg_id!r} not found"
                    )

                current_state = row["state"]
                _assert_legal_transition(current_state, target_state)

                closed_at_val = closed_at if target_state == STATE_CLOSED else None
                conn.execute(
                    "UPDATE composite_plays SET state = ?, closed_at = ? "
                    "WHERE multi_leg_id = ?",
                    (target_state, closed_at_val, multi_leg_id),
                )
                conn.execute("COMMIT")

            except (CompositeNotFoundError, IllegalTransitionError):
                conn.execute("ROLLBACK")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, multi_leg_id: str) -> CompositePlayRow | None:
        """Return the composite play row, or None if not found."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM composite_plays WHERE multi_leg_id = ?",
                (multi_leg_id,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_dataclass(row)

    def detect_orphan(self, multi_leg_id: str, active_leg_count: int) -> bool:
        """Return True if the composite appears orphaned (H1 signal).

        A composite is orphaned when:
          - state == 'open', AND
          - active_leg_count < expected_leg_count

        This is a PURE READ — it does NOT mutate state. The caller is
        responsible for acting on the signal (e.g. transitioning to 'partial'
        and triggering operator attention).

        Parameters
        ----------
        multi_leg_id : str
            The composite to check.
        active_leg_count : int
            Number of legs still active (open positions) as of this check.

        Returns
        -------
        bool
            True if orphaned, False otherwise.

        Raises
        ------
        CompositeNotFoundError
            If multi_leg_id is not found.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state, expected_leg_count FROM composite_plays "
                "WHERE multi_leg_id = ?",
                (multi_leg_id,),
            ).fetchone()

        if row is None:
            raise CompositeNotFoundError(
                f"composite_plays: multi_leg_id {multi_leg_id!r} not found"
            )

        if row["state"] != STATE_OPEN:
            return False

        return active_leg_count < row["expected_leg_count"]

    def list_open(self, account_id: str | None = None) -> list[CompositePlayRow]:
        """Return all open composites, optionally filtered by account_id."""
        with self._conn() as conn:
            if account_id is not None:
                rows = conn.execute(
                    "SELECT * FROM composite_plays WHERE state = ? AND account_id = ?",
                    (STATE_OPEN, account_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM composite_plays WHERE state = ?",
                    (STATE_OPEN,),
                ).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def list_partial(self, account_id: str | None = None) -> list[CompositePlayRow]:
        """Return all partial composites (require operator attention)."""
        with self._conn() as conn:
            if account_id is not None:
                rows = conn.execute(
                    "SELECT * FROM composite_plays WHERE state = ? AND account_id = ?",
                    (STATE_PARTIAL, account_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM composite_plays WHERE state = ?",
                    (STATE_PARTIAL,),
                ).fetchall()
        return [_row_to_dataclass(r) for r in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_legal_transition(current_state: str, target_state: str) -> None:
    """Raise IllegalTransitionError if the transition is not in _LEGAL_TRANSITIONS."""
    allowed = _LEGAL_TRANSITIONS.get(current_state, frozenset())
    if target_state not in allowed:
        raise IllegalTransitionError(
            f"composite_plays: illegal transition {current_state!r} → {target_state!r}. "
            f"Allowed from {current_state!r}: {sorted(allowed) or 'none (terminal state)'}"
        )


def _compute_target_state(
    current_state: str,
    is_decompose: bool,
    legs_remaining: int | None,
    expected_leg_count: int,
) -> str:
    """Compute the target state for a record_leg_close call.

    Encodes the H1 state-machine logic (ADR-0098):
    - open + is_decompose=True                → partial  (H1: first leg close in decompose)
    - open + is_decompose=False               → closed   (direct all-legs-close)
    - partial + legs_remaining==0             → closed   (last leg finally closed)
    - partial + legs_remaining>0 (or None)    → partial  (NEVER auto-close)
    - closed/decomposed → any                 → raises   (terminal state)

    Raises
    ------
    IllegalTransitionError
        On any illegal / terminal-state transition.
    """
    if current_state == STATE_OPEN:
        if is_decompose:
            # H1: first leg close in a decompose sequence → partial
            target = STATE_PARTIAL
        else:
            # Atomic all-legs-close
            target = STATE_CLOSED
    elif current_state == STATE_PARTIAL:
        # Only close when explicitly told all legs are done
        if legs_remaining is not None and legs_remaining == 0:
            target = STATE_CLOSED
        else:
            # Stay partial — NEVER auto-close (H1 invariant)
            target = STATE_PARTIAL
    else:
        # closed or decomposed → terminal; any leg_close attempt is illegal
        _assert_legal_transition(current_state, STATE_CLOSED)
        # _assert_legal_transition raises; this line is unreachable
        raise IllegalTransitionError(  # pragma: no cover
            f"terminal state {current_state!r}"
        )

    # Validate the computed transition before returning
    _assert_legal_transition(current_state, target)
    return target


def _row_to_dataclass(row: sqlite3.Row) -> CompositePlayRow:
    # ml00b: option_legs_json may be absent on a row from a connection that
    # predates the migration (defensive — the migration always runs on init,
    # but a NULL/empty value also decodes to []).
    raw_legs = row["option_legs_json"] if _OPTION_LEGS_COLUMN in row.keys() else None
    return CompositePlayRow(
        multi_leg_id=row["multi_leg_id"],
        account_id=row["account_id"],
        underlying=row["underlying"],
        strategy_kind=row["strategy_kind"],
        state=row["state"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        outer_qty=int(row["outer_qty"]),
        net_entry_price=float(row["net_entry_price"]),
        net_fill_price=(float(row["net_fill_price"]) if row["net_fill_price"] is not None else None),
        fill_size_pct=float(row["fill_size_pct"]),
        expected_leg_count=int(row["expected_leg_count"]),
        max_loss=(float(row["max_loss"]) if row["max_loss"] is not None else None),
        option_legs=CompositePlaysStore._decode_legs(raw_legs),
    )
