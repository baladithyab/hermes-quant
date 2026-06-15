"""hermes_quant.state.portfolio_state — PortfolioState reconstruction engine.

ADR-0041 wave 1c: materialized-view projection of executions.jsonl into
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
   This is documented in ADR-0041 §D7: share-quantity tracking requires
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
   field.  Documented in ADR-0041 §D7.

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
import math
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

# US equity-option contract multiplier (shares controlled per contract). A
# us_option fill_price is a PER-CONTRACT premium; cash/equity impact = premium ×
# contracts × 100. Mirrors options.data._CONTRACT_MULTIPLIER; defined locally so
# state.db reconciliation does not import the options package (keeps the state
# layer dependency-light). ADR-0088 F1.
_CONTRACT_MULTIPLIER = 100.0

# ---------------------------------------------------------------------------
# cs44: skip the multi-leg family-PARENT audit record in BOTH folds.
#
# react/multileg.py:_write_family appends ONE parent + one child-per-leg to
# executions.jsonl. The CHILDREN carry the real positions and the FULL real cash
# (option legs ×premium×contracts×100 via the leg_quantity true-unit path, equity
# leg ×shares). The PARENT is a pure audit rollup: asset_class=="multi_leg",
# reactor_metadata.role=="parent", NO reactor_metadata.quantity, fill_price==net_fill.
# Folding it would (a) create a PHANTOM ("acct","multi_leg",underlying) position whose
# quantity is the meaningless fill_size_pct NAV fraction, and (b) book a second cash
# delta (-fill_size_pct*net_fill) ON TOP of the children's real cash — a money-state
# DOUBLE-BOOK. state.db's equity_total is the gate-SIZED NAV (react/paper.py +
# autonomous.py _account_nav_usd), so the phantom row + double cash corrupts a LIVE
# risk-gate input.
#
# reconstruct_from() reads EVERY record in executions.jsonl (parents included), so the
# rebuild fold is where the defect bites. The incremental fold (_reconcile_state feeds
# only children today) gets the SAME skip for defense-in-depth: a manual replay or a
# future caller could feed the parent dict to apply_execution directly.
#
# The skip discriminator is asset_class=="multi_leg": this value is parent-ONLY on the
# bus — the multi-leg reactor's parent (multileg.py:531 fill, :667 no-fill) is the only
# producer of an ExecutionRecord with this asset_class; every child uses "us_option" /
# "equity". No real position class is "multi_leg" (positions.py / react/base.py impose
# no such class; the proposals.py "multi_leg" use is on a Proposal store row, never a
# bus ExecutionRecord). So the skip NEVER drops a child or a real position and an
# equity/option-only book is byte-identical (the skip never fires).
_MULTILEG_PARENT_ASSET_CLASS = "multi_leg"


def _is_multileg_family_parent(asset_class: str) -> bool:
    """True for the multi-leg family-PARENT audit record (cs44).

    The parent is the ONLY bus ExecutionRecord with asset_class=="multi_leg"; its
    children use "us_option"/"equity". Skipping it in both folds prevents a phantom
    "multi_leg" position + a double-counted cash delta on top of the children.
    """
    return asset_class == _MULTILEG_PARENT_ASSET_CLASS


def _resolve_account(rec: dict[str, Any]) -> str:
    """Resolve the partition account for one persisted execution record (cs52).

    Mirrors the cs24 daemon-loader resolution (daemon/portfolio_loader._record_account)
    EXACTLY: top-level account_id if truthy, else reactor_metadata.account_id if truthy,
    else the "paper-default" sentinel. This is the SAME seam the live producer uses —
    the reactors inject a top-level account_id into the dict they hand apply_execution at
    runtime (react/paper.py:438-441; react/alpaca_paper.py:432) BEFORE the persisted log
    is written, but react/paper.py:_record_to_dict serializes account_id ONLY inside
    reactor_metadata. So a record read back from executions.jsonl during a full rebuild
    carries its account_id ONLY in reactor_metadata; reading just the top-level field
    re-pools every alpaca-paper fill into paper-default and corrupts the per-account NAV
    partition. A truthy top-level account_id resolves identically to the old
    `.get(..., "paper-default")`, so a paper-default-only log is byte-identical.
    """
    acct = rec.get("account_id")
    if acct:
        return str(acct)
    meta_acct = (rec.get("reactor_metadata") or {}).get("account_id")
    if meta_acct:
        return str(meta_acct)
    return "paper-default"


def _dedup_key(rec: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Build the cs51 5-column idempotency key for one execution record (cs57).

    Returns (proposal_id, asof_execution, dedup_asset, dedup_asset_class, dedup_leg) —
    the EXACT key the incremental fold writes into processed_fills
    (_apply_execution_unsafe). reconstruct_from uses it to drop a true byte-duplicate
    (the C2 append-before-apply crash-retry record) from its in-memory accumulator so the
    rebuild fold agrees with the deduped incremental fold.

    Mirrors the incremental key construction verbatim:
      * dedup_asset / dedup_asset_class come from the true-unit (leg_quantity) path:
        the real (asset, asset_class) when reactor_metadata.quantity is present, else the
        "" / "" legacy sentinel — so a legacy single-leg equity row keys exactly as the
        4-column form did.
      * dedup_leg is the per-leg index the option children carry (react/multileg.py:582),
        "" when absent (NOT the literal "None") — so the cs51 same-OCC legs (leg_index 0
        and 1) claim DISTINCT keys and are NOT re-collapsed, while a true byte-duplicate
        (same leg_index) collides and is dropped.
    """
    asof = rec.get("asof_execution") or _utc_now_iso()
    proposal_id = rec.get("proposal_id") or ""
    rmeta = rec.get("reactor_metadata") or {}
    leg_quantity = rmeta.get("quantity") if isinstance(rmeta, dict) else None
    if leg_quantity is not None:
        dedup_asset = rec.get("asset", "")
        dedup_asset_class = rec.get("asset_class", "equity")
    else:
        dedup_asset = ""
        dedup_asset_class = ""
    _leg_index = rmeta.get("leg_index") if isinstance(rmeta, dict) else None
    dedup_leg = "" if _leg_index is None else str(_leg_index)
    return (proposal_id, asof, dedup_asset, dedup_asset_class, dedup_leg)


# ---------------------------------------------------------------------------
# cs15 (ADR-0086 Phase 1, missed half): signed net-liquidation equity_total.
#
# The cached equity_total folds open positions as cash + Σ qty_factor * avg * mult.
# Historically qty_factor was abs(quantity), which treats a SHORT (negative qty)
# as a positive asset. A short fill ALREADY booked its proceeds into cash, so
# abs() adds the same notional a SECOND time with the wrong sign — inflating a
# net-short book's equity_total by ~2×|notional|. Net-liquidation value uses the
# SIGNED quantity: a long is an asset (+qty*price), a short is a liability whose
# proceeds are in cash and whose close costs |qty|*price (so it contributes
# -|qty|*price = signed_qty*price). Opening at the fill mark is then NAV-neutral.
#
# equity_total is the gate-SIZED NAV consumed by _account_nav_usd (admissibility
# share-conversion sizing in react/paper.py + autonomous.py), so the corrected
# value changes a LIVE sizing number for a book holding shorts. The correction is
# therefore flag-gated default-OFF: flag OFF ⇒ abs(quantity) ⇒ bit-for-bit
# current behavior on every book; flag ON ⇒ signed net-liq. The live flip of
# HERMES_QUANT_SIGNED_EQUITY=1 is a separate eval-gated decision.
_SIGNED_EQUITY_FLAG = "HERMES_QUANT_SIGNED_EQUITY"


def _equity_qty_factor(quantity: float) -> float:
    """Per-position quantity factor for the cached equity_total fold.

    Signed (ADR-0086 net-liq) when HERMES_QUANT_SIGNED_EQUITY=1, else the legacy
    abs(quantity). Called once per position in BOTH folds (rebuild + incremental)
    so they stay in parity by construction. For a long (qty>0) the two regimes are
    identical (abs(qty)==qty), so long books are byte-identical across the flag.
    """
    return (
        quantity
        if os.environ.get(_SIGNED_EQUITY_FLAG, "0") == "1"
        else abs(quantity)
    )


# ---------------------------------------------------------------------------
# ft1 (2026-06-13): delta-normalizer regime stamp.
#
# The normalizer fold (ADR-0091 Option E, default-OFF behind
# HERMES_QUANT_DELTA_NORMALIZER) is consistent ONLY if the rebuild and the
# incremental applies that touch a given state.db agree on whether they
# folded raw absolute-targets (flag OFF, inflating) or carry-forward deltas
# (flag ON, deflating). Flipping the flag ON against a populated state.db that
# was built with the flag OFF differences NEW absolute targets against an
# INFLATED running net => phantom sells. We stamp the regime that BUILT the db
# in PRAGMA user_version (set inside reconstruct_from's BEGIN IMMEDIATE) and
# HARD-REFUSE an incremental apply whose current flag regime disagrees with a
# populated db's stamp — refuse + surface, never phantom-sell.
#
# Byte-identity rail: a legacy / flag-OFF db has user_version == 0
# (NEVER_STAMPED == REGIME_OFF == 0), and the default flag regime is also 0, so
# the guard NEVER fires on the default path. The only way to get a non-zero
# stamp is to run reconstruct_from under flag ON — itself a flag-ON-only action.
_NORMALIZER_FLAG = "HERMES_QUANT_DELTA_NORMALIZER"
# user_version values: 0 doubles as "never stamped" AND "flag OFF" so a legacy
# db (user_version defaults to 0) matches the default-OFF regime byte-for-bit.
_REGIME_OFF = 0
_REGIME_ON = 1


def _current_normalizer_regime() -> int:
    """The delta-normalizer regime implied by the CURRENT flag value.

    1 (ON) when HERMES_QUANT_DELTA_NORMALIZER == "1", else 0 (OFF). Mirrors the
    exact flag test the two folds use (reconstruct_from / _apply_execution_unsafe),
    so the stamp and the folds key off the SAME predicate.
    """
    return _REGIME_ON if os.environ.get(_NORMALIZER_FLAG, "0") == "1" else _REGIME_OFF


def _default_initial_cash() -> float:
    raw = os.environ.get(_INITIAL_CASH_ENV, "")
    if not raw:
        return _DEFAULT_INITIAL_CASH
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = float("nan")
    # ar10: reject non-finite / <=0. This is the NAV source for every _account_nav_usd;
    # a non-finite NAV (a `1e400` operator typo silently overflows to inf — NO ValueError)
    # otherwise CRASHES the tick via math.floor(inf) in the admissibility path, or BYPASSES
    # the multileg gross cap (gross/inf == 0.0 -> "nothing to cap"). Fail CLOSED to the
    # documented default + warn. Byte-identical for any finite positive configured value.
    if not math.isfinite(val) or val <= 0:
        logger.warning(
            "%s is not a finite positive float (%r); using default %.2f",
            _INITIAL_CASH_ENV,
            raw,
            _DEFAULT_INITIAL_CASH,
        )
        return _DEFAULT_INITIAL_CASH
    return val


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
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_replayed_asof TEXT,
    replayed_count INTEGER DEFAULT 0
);

-- Cross-model review C2: idempotency guard. Records (proposal_id, asof_execution)
-- of every fill that has been applied. Prevents double-apply if PaperReactor
-- crashes between executions.jsonl append and apply_execution, or if a
-- reconstruct_from runs after partial incremental applies.
--
-- ADR-0029 multi-leg extension: a multi-leg FAMILY shares (proposal_id,
-- asof_execution) across ALL its child legs, so the single-instrument key would
-- swallow leg 2 as a duplicate of leg 1. The key is extended with asset +
-- asset_class. Legacy single-instrument equity rows use the sentinel "" for both
-- so the equity reconciliation path keys EXACTLY as before (one fill per
-- (proposal_id, asof_execution, "", "")) — bit-identical. Each multi-leg child
-- claims its OWN (proposal_id, asof_execution, asset, asset_class); re-applying the
-- same child is still a no-op (idempotency held per leg).
--
-- cs51 same-OCC extension: a multi-leg family can ROLL the SAME OCC contract —
-- e.g. leg 0 sell-to-close + leg 1 buy-to-open resolve to ONE (asset, asset_class)
-- pair. Those two legs then collide on the 4-column key, so the 2nd leg's
-- INSERT OR IGNORE is silently dropped on the incremental fold while reconstruct_from
-- (no dedup table) folds both — a bus/state divergence on equity_total (the
-- gate-sized NAV). The key is extended with leg_index: the option children already
-- carry reactor_metadata.leg_index (react/multileg.py:582; 0, 1, ...), so the two
-- same-OCC legs claim distinct keys. leg_index is "" (sentinel) for any record
-- WITHOUT a reactor_metadata.leg_index — that is EVERY legacy/single-leg equity row
-- AND the single covered-call equity child (which carries no leg_index) — so those
-- paths key (proposal_id, asof_execution, asset, asset_class, "") EXACTLY as the
-- 4-column form did. A genuine re-apply of the SAME leg has the same leg_index ⇒
-- still a no-op (idempotency held per leg).
CREATE TABLE IF NOT EXISTS processed_fills (
    proposal_id    TEXT NOT NULL,
    asof_execution TEXT NOT NULL,
    asset          TEXT NOT NULL DEFAULT '',
    asset_class    TEXT NOT NULL DEFAULT '',
    leg_index      TEXT NOT NULL DEFAULT '',
    applied_at     TEXT NOT NULL,
    PRIMARY KEY (proposal_id, asof_execution, asset, asset_class, leg_index)
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


@dataclass(frozen=True)
class MarkedEquity:
    """Read-time mark-to-market equity snapshot (ADR-0086 Phase 1).

    Attributes
    ----------
    account_id:
        Account identifier.
    cost_basis_equity:
        The equity basis used for NAV reference (typically cash.equity_total or
        _default_initial_cash()). Positions are sized as fractions of this value.
    marked_equity:
        cost_basis_equity + total_unrealized (mark-to-market equity).
    total_unrealized:
        Sum of unrealized P&L across all positions (signed; shorts profit when
        mark < entry).
    equity_basis:
        'mark' if all positions have injected marks, 'entry' if none do, 'mixed'
        if some do and some don't (fall back to avg_entry_price).
    n_positions:
        Number of open positions considered.
    n_marked:
        Number of positions that received an injected mark (vs. falling back to
        avg_entry_price).
    """

    account_id: str
    cost_basis_equity: float
    marked_equity: float
    total_unrealized: float
    equity_basis: str  # "mark" | "entry" | "mixed"
    n_positions: int
    n_marked: int


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
        # Cross-model review C4: lock down parent dir to 0o700 BEFORE we
        # create the db file. State.db carries portfolio positions and is
        # not safe as world-readable on multi-tenant hosts. Idempotent.
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError as exc:  # pragma: no cover - defensive on shared filesystems
            logger.warning("could not chmod %s to 0o700: %s", self.db_path.parent, exc)
        self._init_schema()
        # Same hardening on the db file itself; only after schema init so
        # the file definitely exists.
        try:
            os.chmod(self.db_path, 0o600)
        except OSError as exc:  # pragma: no cover
            logger.warning("could not chmod %s to 0o600: %s", self.db_path, exc)

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
            self._migrate_processed_fills(conn)

    @staticmethod
    def _migrate_processed_fills(conn: sqlite3.Connection) -> None:
        """Idempotent migration: bring a pre-existing processed_fills table up to the
        5-column PRIMARY KEY (proposal_id, asof_execution, asset, asset_class, leg_index)
        that the multi-leg per-leg idempotency key requires (cs29 asset/asset_class +
        cs51 leg_index).

        A DB created before this wave has either the old 2-column PK (proposal_id,
        asof_execution) or the cs44 4-column PK (+ asset, asset_class). A bare ``ALTER
        TABLE ADD COLUMN`` adds the columns but CANNOT change the PRIMARY KEY in SQLite —
        so the dedup key stays narrow and a multi-leg family (which shares proposal_id +
        asof_execution across its legs) collides: the 2nd leg's ``INSERT OR IGNORE`` is
        treated as a duplicate and the leg is SILENTLY DROPPED from state.db while still
        landing on executions.jsonl — a bus/state divergence on the money path. The
        4-column key still collides when two legs of ONE family resolve to the SAME
        (asset, asset_class) — a same-OCC roll (cs51). So a full PK REBUILD is REQUIRED
        (caught by adversarial review; the fresh-DB tests never hit the legacy path). We
        rebuild only when the PK is not already the 5-column form, so this stays
        idempotent and a no-op on fresh / already-migrated DBs.
        """
        # PK column names, in order, from PRAGMA (pk>0 marks key membership).
        info = list(conn.execute("PRAGMA table_info(processed_fills)"))
        if not info:
            return  # table not created yet (executescript creates it first; defensive)
        pk_cols = [row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        if pk_cols == [
            "proposal_id",
            "asof_execution",
            "asset",
            "asset_class",
            "leg_index",
        ]:
            return  # already on the 5-column PK — nothing to do
        # Legacy table (2-col, or cs44 4-col): rebuild with the 5-column PK, preserving
        # every existing row. The SELECT projects only the prior key columns +
        # applied_at, so the new leg_index column takes its NOT NULL DEFAULT '' for every
        # migrated row — the same '' sentinel a non-multi-leg apply uses, so historical
        # idempotency is preserved exactly (an omitted NOT NULL DEFAULT '' column is
        # filled with the default on INSERT). asset/asset_class likewise default to '' for
        # a 2-col legacy source that lacks them.
        conn.execute(
            """
            CREATE TABLE processed_fills_new (
                proposal_id    TEXT NOT NULL,
                asof_execution TEXT NOT NULL,
                asset          TEXT NOT NULL DEFAULT '',
                asset_class    TEXT NOT NULL DEFAULT '',
                leg_index      TEXT NOT NULL DEFAULT '',
                applied_at     TEXT NOT NULL,
                PRIMARY KEY (proposal_id, asof_execution, asset, asset_class, leg_index)
            )
            """
        )
        existing = {row[1] for row in info}
        asset_expr = "asset" if "asset" in existing else "''"
        class_expr = "asset_class" if "asset_class" in existing else "''"
        conn.execute(
            f"INSERT OR IGNORE INTO processed_fills_new "
            f"(proposal_id, asof_execution, asset, asset_class, applied_at) "
            f"SELECT proposal_id, asof_execution, {asset_expr}, {class_expr}, applied_at "
            f"FROM processed_fills"
        )
        conn.execute("DROP TABLE processed_fills")
        conn.execute("ALTER TABLE processed_fills_new RENAME TO processed_fills")

    # ------------------------------------------------------------------
    # ft1: delta-normalizer regime stamp (read / check)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_user_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("PRAGMA user_version").fetchone()
        # row may be a sqlite3.Row or a plain tuple depending on row_factory.
        return int(row[0]) if row is not None else 0

    def _db_is_populated(self, conn: sqlite3.Connection) -> bool:
        """True if state.db carries any materialized position/cash row.

        A fresh (or fully-flat) db carries no rows; the regime guard only fires
        against a POPULATED db so a brand-new db can be folded under either flag
        without a spurious refusal (the first fold under flag ON will stamp it).
        """
        for table in ("positions", "cash"):
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()  # noqa: S608 — fixed table names
            if row is not None:
                return True
        return False

    def _check_regime_stamp(self) -> None:
        """HARD-REFUSE an apply whose current flag regime disagrees with a populated
        db's build regime stamp (ft1).

        - Fresh / empty db (no position or cash rows): no refusal. The first fold
          stamps the regime (apply stamps via _stamp_regime below; rebuild stamps
          inside its transaction).
        - Populated db whose stamped regime == current flag regime: no refusal.
          A legacy db is stamped 0 and the default flag regime is 0 — byte-identical.
        - Populated db whose stamped regime != current flag regime: RAISE. Flipping
          HERMES_QUANT_DELTA_NORMALIZER against a db built under the other regime
          would phantom-sell (inflated-net vs deflated-net basis mismatch); refuse
          and surface rather than corrupt the money ledger.
        """
        current = _current_normalizer_regime()
        with self._lock, self._conn() as conn:
            if not self._db_is_populated(conn):
                return
            stamped = self._read_user_version(conn)
            if stamped != current:
                raise RuntimeError(
                    "delta-normalizer regime mismatch: state.db was built under "
                    f"regime {stamped} (0=OFF/legacy, 1=ON) but the current "
                    f"{_NORMALIZER_FLAG} flag implies regime {current}. Folding new "
                    "fills against a db built under the other regime would phantom-"
                    "sell (the new absolute target would be differenced against an "
                    "inflated/deflated net). Refusing to apply. Rebuild state.db "
                    "with reconstruct_from() under the intended flag value first."
                )

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

        # ── 1+2. Read + replay UNDER the write lock ──────────────────────
        # ar05: the bus read MUST happen inside the BEGIN IMMEDIATE transaction
        # (below), not here. Reading outside the write lock let a concurrent
        # reactor apply_execution() interleave between the snapshot and the
        # rebuild commit — the rebuild then deleted positions/cash and re-wrote
        # only the STALE snapshot, permanently losing the concurrent fill (and,
        # because processed_fills survived, blocking its incremental re-apply).
        # Acquiring the write lock first (BEGIN IMMEDIATE) serializes any
        # concurrent apply_execution AFTER this rebuild, so the snapshot+rebuild
        # is atomic. Single-writer reconcile is byte-identical.
        positions: dict[tuple[str, str, str], dict[str, Any]] = {}
        cash_map: dict[str, float] = {}
        last_ts: dict[str, str] = {}  # account_id → latest asof seen

        initial_cash = _default_initial_cash()

        with self._lock, self._conn() as conn:
            # Cross-model review I2 + ar05: BEGIN IMMEDIATE acquires the write lock
            # at transaction start, and the bus read + replay now happen INSIDE this
            # transaction so a concurrent apply_execution() cannot interleave between
            # the snapshot and the rebuild commit (which would permanently lose the
            # concurrent fill — see ar05). The replay is pure in-memory; holding the
            # write lock across it is acceptable for the operator-only reconcile path.
            conn.execute("BEGIN IMMEDIATE")
            try:
                # ── 1. Read all records (UNDER the write lock, ar05) ─────────
                records = _read_all_jsonl(executions_path)

                # ── 2. Replay into in-memory accumulators ────────────────────
                # ADR-0091 Option E (default-OFF behind HERMES_QUANT_DELTA_NORMALIZER):
                # convert each absolute-target fill into its TRADED DELTA at fold time
                # via the ONE shared normalizer, so a re-affirmed unchanged target folds
                # to a no-op instead of inflating (the AAPL-12x / BA-6x defect). Flag OFF
                # ⇒ override is None ⇒ _replay_record reads the raw field, bit-for-bit legacy.
                _normalizer = None
                if os.environ.get("HERMES_QUANT_DELTA_NORMALIZER", "0") == "1":
                    from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer

                    _normalizer = FillDeltaNormalizer()
                    # i0b no-lookahead/ordering guard: the carry-forward delta =
                    # target - running_net is ORDER-DEPENDENT, so the normalizer must
                    # see records in asof order, not raw file/append order. Stable-sort
                    # by asof_execution (stable ⇒ same-asof ties keep file order, so the
                    # per-bucket delta stream is deterministic and identical to a
                    # correctly-appended log). This runs ONLY on the normalizer path;
                    # flag OFF leaves `records` in raw file order, bit-for-bit legacy.
                    records = sorted(records, key=lambda r: r.get("asof_execution") or "")

                # cs57: the incremental fold dedups a true byte-duplicate (the C2
                # append-before-apply crash-retry record) via INSERT OR IGNORE on
                # processed_fills; reconstruct_from folds EVERY raw record with no dedup,
                # so a duplicated line double-counts and the rebuild book DIVERGES from
                # the deduped incremental book. Drop a record whose full cs51 5-col key
                # was already folded in THIS rebuild pass, mirroring the incremental key.
                seen_keys: set[tuple[str, str, str, str, str]] = set()

                for line_no, rec in enumerate(records, start=1):
                    try:
                        # cs57: dedup BEFORE folding. Skip the cs44 family-parent.
                        rec_asset_class = rec.get("asset_class", "equity")
                        if not _is_multileg_family_parent(rec_asset_class):
                            proposal_id = rec.get("proposal_id") or ""
                            if proposal_id:
                                key = _dedup_key(rec)
                                if key in seen_keys:
                                    continue
                                seen_keys.add(key)
                        _override = (
                            _normalizer.delta_for(rec) if _normalizer is not None else None
                        )
                        _replay_record(
                            rec, positions, cash_map, last_ts, initial_cash, _override
                        )
                        result.executions_processed += 1
                        # cs52: report the SAME resolved partition the fold booked into.
                        acct = _resolve_account(rec)
                        result.accounts_seen.add(acct)
                    except Exception as e:  # noqa: BLE001
                        result.errors.append((line_no, str(e)))
                        logger.warning("reconstruct_from: skipping record %d: %s", line_no, e)

                # ── 3. Write to state.db atomically ──────────────────────────
                # cs62: derive the watermark from the asofs we actually FOLDED (last_ts
                # is mutated only by a successful _replay_record), NOT from the raw record
                # list. A poisoned future-bound asof raises in _replay_record and never
                # enters last_ts, so it cannot wedge the watermark past real time.
                latest_asof = max(last_ts.values()) if last_ts else None
                # ft1: stamp the regime that BUILT this db so a later
                # incremental apply can hard-refuse a flag-flip mismatch instead
                # of phantom-selling. PRAGMA user_version takes a literal, not a
                # bound param; the value is our own 0/1 constant (never user
                # input). Flag OFF (default) stamps 0 == legacy never-stamped, so
                # a flag-OFF rebuild leaves user_version byte-identical to today.
                conn.execute(f"PRAGMA user_version = {int(_current_normalizer_regime())}")
                # Clear derived tables (not halts!)
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM cash")

                # Upsert positions
                for (acct, asset_class, symbol), pos in positions.items():
                    qty = pos["quantity"]
                    if abs(qty) < 1e-12:
                        # Cross-model review (GPT-5.1 Critical #2): drop flat
                        # positions instead of writing 0-quantity rows. Closed
                        # positions live in executions.jsonl; state.db is the
                        # OPEN-positions cache. Keeping flat rows confused
                        # `positions_written` semantics and risked future
                        # refactors removing the filter on the reader.
                        continue
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
                    # equity_total: cash + open position notionals. The per-position
                    # quantity factor is SIGNED net-liq (ADR-0086) when
                    # HERMES_QUANT_SIGNED_EQUITY=1 (a short contributes a negative
                    # liability term, not a phantom positive asset), else the legacy
                    # abs() (flag-OFF byte-identity). ADR-0088 F1: value us_option
                    # positions at factor × avg × 100 (the contract multiplier; key[1]
                    # is the position's asset_class), equity ×1. The >= 1e-12 filter
                    # is membership (keeps abs()), not the value term.
                    equity = balance + sum(
                        _equity_qty_factor(p["quantity"])
                        * p["avg_entry_price"]
                        * (_CONTRACT_MULTIPLIER if a_cls == "us_option" else 1.0)
                        for (a, a_cls, _), p in positions.items()
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
        Failures are logged AND emit a `state_reconstruction_failed`
        audit event so the silent-divergence failure mode the cross-model
        review flagged (C3) is visible in the canonical audit trail.

        Args:
            record: dict matching ExecutionRecord fields (as produced by
                    paper._record_to_dict).
        """
        # ft1: BEFORE any fold, refuse a flag-flip regime mismatch against a
        # populated db (would phantom-sell). Raised OUTSIDE the swallowing
        # try/except below so the refusal is loud to the caller; the default
        # flag-OFF path never trips it (legacy db stamp 0 == OFF regime 0).
        self._check_regime_stamp()
        try:
            self._apply_execution_unsafe(record)
        except Exception as e:  # noqa: BLE001
            logger.warning("PortfolioState.apply_execution failed: %s", e)
            # Cross-model review C3: emit audit event so silent state.db
            # divergence is visible in the canonical audit log.
            try:
                from hermes_quant.governance import audit_log

                audit_log.append(
                    audit_log.GovernanceEvent(
                        kind="state_reconstruction_failed",
                        asof=datetime.now(UTC),
                        source="state.portfolio_state",
                        payload={
                            "error_type": type(e).__name__,
                            "error_message": str(e)[:512],
                            "proposal_id": record.get("proposal_id"),
                            "asset": record.get("asset"),
                            "asof_execution": record.get("asof_execution"),
                        },
                    )
                )
            except Exception as audit_exc:  # pragma: no cover - defensive
                logger.warning(
                    "could not emit state_reconstruction_failed audit event: %s",
                    audit_exc,
                )

    def _apply_execution_unsafe(self, record: dict[str, Any]) -> None:
        """Inner implementation — may raise; caller wraps in try/except."""
        # cs52: resolve the partition account via the same fallback as the rebuild fold
        # (top-level account_id, else reactor_metadata.account_id, else "paper-default").
        # The LIVE path is unaffected — the reactors pre-inject a top-level account_id
        # before calling apply_execution (react/paper.py:438-441) — but this keeps the
        # incremental fold in parity for a manual raw-log replay where account_id lives
        # only in reactor_metadata. A truthy top-level account_id resolves identically ⇒
        # byte-identical to the prior .get(...,"paper-default").
        acct = _resolve_account(record)
        asset_class = record.get("asset_class", "equity")
        # cs44: skip the multi-leg family-PARENT audit record (asset_class=="multi_leg")
        # BEFORE any position/cash mutation — its children carry the real book; folding
        # the parent would phantom a "multi_leg" position + double-count cash. The early
        # return touches no DB state (no processed_fills claim, no watermark bump), so a
        # later child apply is unaffected. Fires ONLY on the parent marker ⇒ an
        # equity/option-only record is byte-identical.
        if _is_multileg_family_parent(asset_class):
            logger.debug(
                "apply_execution: skipping multi-leg family-parent rollup "
                "(proposal_id=%s asset=%s) — children carry the position+cash",
                record.get("proposal_id"),
                record.get("asset"),
            )
            return
        symbol = record.get("asset", "")
        fill_size_pct = float(record.get("fill_size_pct", 0.0))
        fill_price = float(record.get("fill_price", 0.0))
        asof = record.get("asof_execution") or _utc_now_iso()

        # ADR-0029 multi-leg: a child leg carries an explicit SIGNED contract/share
        # count in reactor_metadata.quantity. When present, the position quantity is
        # tracked in CONTRACTS/SHARES (the true unit), not the NAV-fraction proxy. The
        # idempotency key is then keyed per-leg (asset/asset_class) so two legs of one
        # family don't collide. WITHOUT a quantity (the existing equity path), the
        # behavior is bit-identical: NAV-fraction quantity + the legacy "" key sentinel.
        rmeta = record.get("reactor_metadata") or {}
        leg_quantity = rmeta.get("quantity") if isinstance(rmeta, dict) else None
        if leg_quantity is not None:
            pos_delta = float(leg_quantity)  # signed contracts/shares
            dedup_asset = symbol
            dedup_asset_class = asset_class
        else:
            pos_delta = fill_size_pct  # NAV-fraction proxy (legacy path)
            dedup_asset = ""
            dedup_asset_class = ""
        # cs51: a same-OCC roll resolves two legs of ONE family to the SAME
        # (proposal_id, asof, asset, asset_class), so the 4-column key collides and the
        # 2nd leg's INSERT OR IGNORE is silently dropped on the incremental fold (while
        # reconstruct_from folds both). Disambiguate with the per-leg index the option
        # children already carry (react/multileg.py:582). A MISSING leg_index maps to the
        # "" sentinel — NOT the literal string "None" — so every legacy/single-leg row and
        # the single covered-call equity child (which carries no leg_index) keys exactly
        # as the 4-column form did (byte-identical, dedup_leg == ""). leg_index 0 and 1 on
        # the same OCC then claim distinct keys, so both legs apply.
        _leg_index = rmeta.get("leg_index") if isinstance(rmeta, dict) else None
        dedup_leg = "" if _leg_index is None else str(_leg_index)
        # Contract multiplier (ADR-0088 F1 fix): a us_option fill_price is the
        # PER-CONTRACT premium, but a contract controls 100 shares, so the cash
        # and equity-valuation impact is premium × contracts × 100. An equity
        # fill_price is already per-share (multiplier 1). Omitting this booked a
        # $150 option credit as $1.50 and undervalued option positions 100× —
        # which then corrupts equity_total and, transitively, the deterministic
        # backend's NAV/BP reads. The multiplier applies ONLY on the true-unit
        # path (leg_quantity present, where pos_delta is real contracts); the
        # legacy NAV-fraction path is unaffected (multiplier 1).
        contract_multiplier = (
            _CONTRACT_MULTIPLIER
            if (leg_quantity is not None and asset_class == "us_option")
            else 1.0
        )
        # Cross-model review C2 (Claude Opus): future-bound / unparseable asof rejection.
        # cs62: the SAME guard the rebuild fold (_replay_record) now applies, factored into
        # ONE shared helper so the incremental and rebuild folds can never diverge on which
        # records they drop. Byte-identical to the prior inline block.
        _validate_asof(asof)

        initial_cash = _default_initial_cash()
        proposal_id = record.get("proposal_id") or ""

        with self._lock, self._conn() as conn:
            # Cross-model review I2 (Claude Opus): BEGIN IMMEDIATE acquires
            # the write lock at transaction start, eliminating the
            # read-then-write race when two processes apply concurrently.
            conn.execute("BEGIN IMMEDIATE")
            try:
                # ft1: stamp the current regime on every incremental write so a
                # flag-ON session marks the db (and a flag-OFF write re-affirms 0,
                # a no-op on a legacy db => byte-identical). _check_regime_stamp()
                # at the public entry already refused any populated-db mismatch
                # before we got here, so this only ever (re)writes the AGREEING
                # regime; it cannot silently overwrite a conflicting stamp.
                conn.execute(f"PRAGMA user_version = {int(_current_normalizer_regime())}")
                # Cross-model review C2: idempotency guard. If this
                # (proposal_id, asof_execution) has already been applied,
                # skip — INSERT into processed_fills will fail the UNIQUE,
                # we use INSERT OR IGNORE and check changes() to detect.
                if proposal_id:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO processed_fills "
                        "(proposal_id, asof_execution, asset, asset_class, leg_index, applied_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            proposal_id,
                            asof,
                            dedup_asset,
                            dedup_asset_class,
                            dedup_leg,
                            _utc_now_iso(),
                        ),
                    )
                    if cur.rowcount == 0:
                        # Already applied — this is the duplicate-apply
                        # case. Roll back this no-op transaction and
                        # return cleanly.
                        conn.execute("ROLLBACK")
                        logger.info(
                            "apply_execution: idempotency hit on (%s, %s, %s, %s, %s); skipping",
                            proposal_id,
                            asof,
                            dedup_asset,
                            dedup_asset_class,
                            dedup_leg,
                        )
                        return

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

                # ADR-0091 Option E (i0a) — incremental path, default-OFF behind
                # HERMES_QUANT_DELTA_NORMALIZER. The persisted old_qty IS this
                # bucket's carried-forward net (in the record's lane unit), so the
                # SAME shared derivation the rebuild fold uses — delta = target -
                # net — applies here with net = old_qty. This makes the incremental
                # and rebuild folds converge by construction (the i0a parity gate):
                # a re-affirmed unchanged target yields pos_delta 0 (no-op in
                # position AND cash). Flag OFF ⇒ pos_delta unchanged, bit-for-bit
                # legacy. Re-feed the derived delta into BOTH the position fold and
                # the cash basis (which tracks pos_delta / leg_quantity below).
                if os.environ.get("HERMES_QUANT_DELTA_NORMALIZER", "0") == "1":
                    from hermes_quant.state.fill_delta_normalizer import delta_from_net

                    pos_delta = delta_from_net(record, old_qty)
                    if leg_quantity is not None:
                        leg_quantity = pos_delta
                    else:
                        fill_size_pct = pos_delta

                new_qty, new_avg = _update_position(old_qty, old_avg, pos_delta, fill_price)

                # ── upsert position ──────────────────────────────────────
                if abs(new_qty) < 1e-12:
                    # Position closed: delete the row (state.db caches
                    # OPEN positions only — see reconstruct_from for the
                    # symmetric flat-position drop).
                    conn.execute(
                        "DELETE FROM positions WHERE account_id=? AND asset_class=? AND symbol=?",
                        (acct, asset_class, symbol),
                    )
                else:
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
                # When the record carries a true share/contract count
                # (reactor_metadata.quantity, e.g. the Alpaca-paper reactor),
                # the cash delta MUST use real notional = signed_shares ×
                # per-share price, NOT fill_size_pct × price. fill_size_pct is a
                # NAV FRACTION; multiplying a fraction by a per-share price
                # (the legacy "0da3" unit bug) understates cash by orders of
                # magnitude and corrupts the partition NAV. We branch the cash
                # math the SAME way the position math is branched above
                # (leg_quantity present → true units). With no leg_quantity the
                # legacy NAV-fraction path is bit-identical.
                cash_basis = pos_delta if leg_quantity is not None else fill_size_pct
                # ADR-0088 F1: apply the contract multiplier so a us_option fill's
                # cash uses real notional (premium × contracts × 100), not premium ×
                # contracts. equity-class fills use multiplier 1 (bit-identical).
                delta_cash = -cash_basis * fill_price * contract_multiplier
                new_cash = cash_balance + delta_cash

                # equity_total: recompute from all positions for this account
                # (approximation: use avg_entry_price, not mark price). The per-
                # position quantity factor is SIGNED net-liq (ADR-0086) when
                # HERMES_QUANT_SIGNED_EQUITY=1 (a short is a negative liability term,
                # not a phantom positive asset), else the legacy abs() (flag-OFF
                # byte-identity). ADR-0088 F1: value each position at factor × avg ×
                # its own contract multiplier (us_option ×100, equity ×1) so an option
                # position is not undervalued 100×. The SQL ABS(quantity) >= 1e-12
                # is membership (keeps abs()), not the value term. Both folds call the
                # same _equity_qty_factor on the same signed qty, so a short-holding
                # book yields identical equity from rebuild and incremental (parity).
                all_pos = conn.execute(
                    "SELECT asset_class, quantity, avg_entry_price FROM positions "
                    "WHERE account_id=? AND ABS(quantity) >= 1e-12",
                    (acct,),
                ).fetchall()
                equity = new_cash + sum(
                    _equity_qty_factor(float(p["quantity"]))
                    * float(p["avg_entry_price"])
                    * (_CONTRACT_MULTIPLIER if p["asset_class"] == "us_option" else 1.0)
                    for p in all_pos
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

    def get_marked_equity(
        self,
        account_id: str,
        mark_prices: dict[str | tuple[str, str], float],
        *,
        nav_ref: float | None = None,
    ) -> MarkedEquity:
        """Compute read-time mark-to-market equity (ADR-0086 Phase 1).

        Injected marks are used when available; positions without marks fall back
        to avg_entry_price (no P&L contribution). No network call is made.

        Args:
            account_id: Account identifier.
            mark_prices: dict mapping the position's mark key → current mark price.
                The key may be the bare ``symbol`` (legacy contract) OR the composite
                ``(asset_class, symbol)`` tuple — the composite key is tried first and
                the bare symbol is the fallback, so an equity and a us_option on the
                SAME underlying (distinct PK rows) can carry distinct marks (cs35).
                Positions without an entry fall back to avg_entry_price (no P&L).
            nav_ref: NAV reference against which NAV-fraction position weights are
                sized. Defaults to cash.equity_total (cost-basis equity) or
                _default_initial_cash() if no cash record exists yet.

        Returns:
            MarkedEquity with marked_equity = cost_basis_equity + total_unrealized.
            n_positions counts only CONSIDERED rows (avg_entry_price > 0); rows
            skipped at the avg guard do not inflate the equity_basis denominator
            (cs32).

        Notes:
            For a NAV-FRACTION equity row Position.quantity is a SIGNED weight (e.g.,
            -0.2 = 20% short) and unrealized_i = quantity_i * nav_ref *
            (mark_i / entry_i - 1) — shorts profit when mark < entry. A us_option row
            persists Position.quantity in REAL CONTRACTS, so it is marked on the
            per-contract premium basis: contracts_i * _CONTRACT_MULTIPLIER *
            (mark_i - entry_i) — i.e. ×100 shares per contract (cs31). A mark that is
            None, non-finite, or non-positive is skipped (not marked) so a single bad
            mark cannot poison the whole-account marked_equity (cs34).
        """
        cash = self.get_cash(account_id)
        cost_basis_equity = cash.equity_total if cash else _default_initial_cash()
        if nav_ref is None:
            nav_ref = cost_basis_equity
        # Guard: a non-positive nav_ref would silently zero (or sign-invert) every
        # unrealized contribution. Fall back to the bootstrap initial cash so the
        # MTM estimate stays meaningful rather than collapsing to cost-basis
        # (Phase-8 review finding 2026-06-02).
        if nav_ref <= 0:
            nav_ref = _default_initial_cash()

        positions = self.get_positions(account_id)
        total_unrealized = 0.0
        n_marked = 0
        # cs32: count only positions actually CONSIDERED (avg_entry_price > 0). A row
        # skipped at the avg guard below must not inflate the equity_basis denominator
        # — otherwise a book whose every valid leg is marked reads 'mixed', and a book
        # with a sole bad-avg row can never read 'mark'.
        n_considered = 0

        for pos in positions.values():
            # Guard: skip positions with invalid avg_entry_price (division by zero).
            # NOT considered, NOT marked.
            if pos.avg_entry_price <= 0:
                continue
            n_considered += 1

            # cs35: an equity and a us_option on the SAME underlying persist under
            # distinct PK rows (asset_class differs). Key the mark lookup on the
            # composite (asset_class, symbol) first, falling back to the bare symbol so
            # a legacy symbol-keyed marks dict still resolves byte-identically.
            mark = mark_prices.get((pos.asset_class, pos.symbol))
            if mark is None:
                mark = mark_prices.get(pos.symbol)
            if mark is None:
                # No mark → fall back to avg_entry_price → zero unrealized contribution.
                continue
            # cs34: a None mark is already handled above; a non-finite (NaN/inf) or
            # non-positive mark is nonsense — booking it would either poison the whole
            # account marked_equity to NaN or book a phantom -quantity*nav_ref loss.
            # Skip it WITHOUT incrementing n_marked (it falls back to entry).
            if not math.isfinite(mark) or mark <= 0:
                continue
            n_marked += 1
            # cs31: a us_option row's quantity is REAL CONTRACTS (the true-unit fold),
            # so its MTM is contracts × _CONTRACT_MULTIPLIER × (mark - entry) — the
            # per-contract premium basis (mirrors the equity_total write fold's ×100).
            # Every other class is the legacy NAV-fraction signed weight (UNCHANGED):
            # quantity carries sign, shorts profit when mark < entry.
            if pos.asset_class == "us_option":
                unrealized_i = pos.quantity * _CONTRACT_MULTIPLIER * (mark - pos.avg_entry_price)
            else:
                unrealized_i = pos.quantity * nav_ref * (mark / pos.avg_entry_price - 1.0)
            total_unrealized += unrealized_i

        marked_equity = cost_basis_equity + total_unrealized

        # Determine equity_basis flag (cs32: denominator = considered rows, not raw len)
        n_positions = n_considered
        if n_positions == 0:
            equity_basis = "entry"  # no positions → no marks needed
        elif n_marked == n_positions:
            equity_basis = "mark"
        elif n_marked == 0:
            equity_basis = "entry"
        else:
            equity_basis = "mixed"

        return MarkedEquity(
            account_id=account_id,
            cost_basis_equity=cost_basis_equity,
            marked_equity=marked_equity,
            total_unrealized=total_unrealized,
            equity_basis=equity_basis,
            n_positions=n_positions,
            n_marked=n_marked,
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
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("_read_all_jsonl: skipping malformed line")
                continue
            if not isinstance(rec, dict):
                continue  # valid JSON but not an object (corrupt/partial append) — skip
            records.append(rec)
    return records


def _latest_asof(records: list[dict[str, Any]]) -> str | None:
    """Return the most recent asof_execution timestamp among records."""
    asofs: list[str] = [r["asof_execution"] for r in records if r.get("asof_execution")]
    return max(asofs) if asofs else None


def _validate_asof(asof: str) -> None:
    """cs62: the SHARED no-lookahead/poison guard for asof_execution.

    Cross-model review C2 (Claude Opus): a future-bound asof of "9999-12-31..." would
    wedge the watermark and silently cause future delta-replays to skip every legitimate
    record. A bad ISO format is equally poisonous. Reject anything more than 24h in the
    future of wall-clock-now, or anything unparseable.

    cs62: BOTH folds (the incremental _apply_execution_unsafe AND the rebuild
    _replay_record) call this ONE implementation so reconstruct_from drops exactly the
    records the live incremental book drops — the rebuild can no longer DIVERGE by folding a
    poisoned record the live book correctly rejected, and a `--apply` can no longer corrupt
    state.db / wedge the watermark. A clean (valid, non-future, parseable) asof returns None
    and the caller proceeds bit-for-bit as before.

    Raises:
        ValueError: if asof is unparseable or more than 24h in the future.
    """
    now = datetime.now(UTC)
    try:
        asof_dt = datetime.fromisoformat(asof.replace("Z", "+00:00"))
        if asof_dt.tzinfo is None:
            asof_dt = asof_dt.replace(tzinfo=UTC)
        if asof_dt > now and (asof_dt - now).total_seconds() > 86400:
            raise ValueError(
                f"asof_execution {asof} is more than 24h in the future "
                f"of wall-clock {now.isoformat()}; refusing to apply"
            )
    except (TypeError, ValueError) as exc:
        # Bad ISO format also lands here. We re-raise rather than
        # silently using _utc_now_iso to avoid masking upstream bugs.
        raise ValueError(f"unparseable or future-bound asof_execution: {asof!r}") from exc


def _update_position(
    old_qty: float,
    old_avg: float,
    fill_size_pct: float,
    fill_price: float,
) -> tuple[float, float]:
    """Compute new quantity and avg_entry_price after a fill.

    Design (ADR-0041 §D7):
    - Weighted-average cost basis (not FIFO) for v0.1.
    - fill_size_pct is SIGNED: positive = long fill, negative = short fill.
    - new_qty = old_qty + fill_size_pct  (position in NAV-fraction units)
    - Adding to an existing same-direction position:
          new_avg = (old_qty × old_avg + fill_size_pct × fill_price) / new_qty
    - Reducing / closing a position (opposite sign):
          avg_entry_price stays at old_avg for the residual lot
          (residual-lot rule, ADR-0041 §D7).
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
    pos_delta_override: float | None = None,
) -> None:
    """Apply one record to the in-memory accumulators during full rebuild.

    Mutates positions, cash_map, and last_ts in place.

    Args:
        rec:          decoded JSONL dict from executions.jsonl
        positions:    (account_id, asset_class, symbol) → position dict
        cash_map:     account_id → cash balance
        last_ts:      account_id → most recent asof_execution seen
        initial_cash: bootstrap cash for first-seen accounts
        pos_delta_override: ADR-0091 Option E — when the
            HERMES_QUANT_DELTA_NORMALIZER fold is active, reconstruct_from passes
            the carry-forward-derived TRADED DELTA here (in the record's own size
            unit), replacing the raw absolute-target size field. None ⇒ legacy
            behavior (read the raw field), bit-for-bit unchanged.
    """
    # cs52: resolve the partition account the SAME way the live producer/state-write seam
    # does — top-level account_id, else reactor_metadata.account_id, else "paper-default"
    # (mirrors cs24/daemon _record_account). A persisted alpaca-paper fill carries its
    # account_id ONLY in reactor_metadata (react/paper.py:_record_to_dict emits no
    # top-level field), so the bare .get(...,"paper-default") re-pooled it into
    # paper-default on rebuild. A truthy top-level account_id resolves identically ⇒
    # paper-default-only log byte-identical.
    acct = _resolve_account(rec)
    asset_class = rec.get("asset_class", "equity")
    # cs44: skip the multi-leg family-PARENT audit record (asset_class=="multi_leg")
    # BEFORE touching positions/cash_map/last_ts. reconstruct_from reads EVERY bus
    # record including the parent that _write_family appends; the children (us_option /
    # equity, with reactor_metadata.quantity) carry the real positions + full cash. The
    # parent has no quantity, so folding it phantoms a "multi_leg" position and books a
    # second cash delta on top of the children (double-book). Skip fires ONLY on the
    # parent marker ⇒ an equity/option-only rebuild is byte-identical (incl. last_ts,
    # which the parent shares with its same-asof children, so the watermark is
    # unchanged).
    if _is_multileg_family_parent(asset_class):
        return
    symbol = rec.get("asset", "")
    fill_size_pct = float(rec.get("fill_size_pct", 0.0))
    fill_price = float(rec.get("fill_price", 0.0))
    asof = rec.get("asof_execution") or _utc_now_iso()
    # cs62: apply the SAME asof poison guard the incremental fold
    # (_apply_execution_unsafe) applies, BEFORE any positions/cash/last_ts mutation. A
    # future-bound or unparseable asof raises here; reconstruct_from's per-record
    # try/except catches it, records the error, and skips the fold (and the
    # executions_processed/accounts_seen bump) — exactly mirroring the incremental reject.
    # Without this, the rebuild folded a poisoned record the live book correctly dropped
    # (divergence), and a `--apply` corrupted state.db + wedged the watermark. A clean
    # (valid, non-future, parseable) asof passes through untouched ⇒ byte-identical fold.
    _validate_asof(asof)

    # ADR-0029 multi-leg: a child leg with an explicit signed contract/share count
    # tracks position quantity in that true unit (parity with apply_execution).
    # Without it, the legacy NAV-fraction proxy is used (equity path bit-identical).
    rmeta = rec.get("reactor_metadata") or {}
    leg_quantity = rmeta.get("quantity") if isinstance(rmeta, dict) else None
    if pos_delta_override is not None:
        # Option E: the normalizer already derived the traded delta from the
        # absolute target. Use it for BOTH the position fold and the cash basis
        # below (cash_basis tracks pos_delta), so a re-affirmation (delta 0) is a
        # true no-op in position AND cash.
        pos_delta = pos_delta_override
        fill_size_pct = pos_delta_override if leg_quantity is None else fill_size_pct
        if leg_quantity is not None:
            leg_quantity = pos_delta_override
    else:
        pos_delta = float(leg_quantity) if leg_quantity is not None else fill_size_pct

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

    new_qty, new_avg = _update_position(old_qty, old_avg, pos_delta, fill_price)
    positions[key] = {
        "quantity": new_qty,
        "avg_entry_price": new_avg,
        "last_update_at": asof,
    }

    # Update cash: long fill decreases cash, short fill increases cash.
    # Mirror apply_execution: when a true share/contract count is present
    # (leg_quantity), cash uses real notional (signed_shares × price), not the
    # NAV-fraction × price "0da3" unit bug. Legacy path (no leg_quantity) is
    # bit-identical.
    cash_basis = pos_delta if leg_quantity is not None else fill_size_pct
    # ADR-0088 F1: us_option fill_price is a per-contract premium → cash impact is
    # premium × contracts × 100. equity path uses multiplier 1 (bit-identical).
    contract_multiplier = (
        _CONTRACT_MULTIPLIER
        if (leg_quantity is not None and asset_class == "us_option")
        else 1.0
    )
    cash_map[acct] -= cash_basis * fill_price * contract_multiplier

    # Track latest timestamp per account
    if acct not in last_ts or asof > last_ts[acct]:
        last_ts[acct] = asof
