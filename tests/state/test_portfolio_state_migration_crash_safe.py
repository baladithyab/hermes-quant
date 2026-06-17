"""ar116 — crash-safety / idempotency regression for PortfolioState._migrate_processed_fills.

The processed_fills PK rebuild (legacy 2-col or cs44 4-col -> the cs51 5-col PK with
leg_index) runs in autocommit (``_conn()`` opens with ``isolation_level=None``). Before
the fix, the rebuild issued ``CREATE processed_fills_new`` / ``INSERT`` /
``DROP processed_fills`` / ``RENAME`` as four INDEPENDENT autocommitted statements with
no transaction. A crash / SIGKILL / concurrent-process interleave between the CREATE and
the final RENAME left an ORPHAN ``processed_fills_new`` while the legacy
``processed_fills`` (still legacy PK) remained. The NEXT ``PortfolioState()`` construction
re-entered the migration branch (PK still legacy) and the bare ``CREATE TABLE
processed_fills_new`` raised ``OperationalError: table ... already exists`` — permanently
BRICKING every reactor / reconcile-cron that constructs PortfolioState (the money path
could no longer initialize, and retries never recovered). Fail-closed, latent.

The fix wraps the rebuild in one BEGIN IMMEDIATE / COMMIT (so a crash atomically rolls
back to the legacy table — no orphan) AND drops any pre-existing orphan before CREATE (so
a recovery re-run is idempotent). Ported from agent commit 45a6c2d, adapted to this
branch's 5-col (cs51 leg_index) PK target.

Deterministic, no network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_quant.state.portfolio_state import PortfolioState

_FIVE_COL_PK = ["proposal_id", "asof_execution", "asset", "asset_class", "leg_index"]

_LEGACY_2COL_SCHEMA = """
    CREATE TABLE processed_fills (
        proposal_id    TEXT NOT NULL,
        asof_execution TEXT NOT NULL,
        applied_at     TEXT NOT NULL,
        PRIMARY KEY (proposal_id, asof_execution)
    );
    INSERT INTO processed_fills
        VALUES ('prop_old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z');
"""

# The 5-col orphan a crash leaves behind (CREATE committed in autocommit, RENAME never
# ran). Matches the table _migrate_processed_fills tries to CREATE on the recovery run.
_ORPHAN_NEW_5COL_SCHEMA = """
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


def _pk_cols(db: Path) -> list[str]:
    with sqlite3.connect(db) as c:
        info = list(c.execute("PRAGMA table_info(processed_fills)"))
    return [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def _table_exists(db: Path, name: str) -> bool:
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    return row is not None


def test_migration_recovers_from_orphan_processed_fills_new(tmp_path) -> None:
    """RED before fix: a crashed prior migration leaves processed_fills_new; the recovery
    construction must NOT raise "table already exists" and must complete the rebuild
    idempotently, preserving the legacy row.
    """
    db = tmp_path / "state.db"
    # Seed a LEGACY 2-col db ...
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_2COL_SCHEMA)
    conn.commit()
    conn.close()
    # ... plus the orphan a crash-mid-migration leaves behind (CREATE committed in
    # autocommit, RENAME never ran). Reproduces the exact bricked-db on-disk state.
    conn = sqlite3.connect(db)
    conn.execute(_ORPHAN_NEW_5COL_SCHEMA)
    conn.commit()
    conn.close()

    # Recovery: constructing PortfolioState re-runs _migrate_processed_fills.
    # Before the fix this raised OperationalError and the constructor died.
    ps = PortfolioState(state_db_path=db)
    assert ps is not None

    # The rebuild completed: 5-col PK, no orphan left, legacy row preserved.
    assert _pk_cols(db) == _FIVE_COL_PK
    assert not _table_exists(db, "processed_fills_new")
    with sqlite3.connect(db) as c:
        n_old = c.execute(
            "SELECT COUNT(*) FROM processed_fills WHERE proposal_id='prop_old'"
        ).fetchone()[0]
    assert n_old == 1

    # And the recovered db is fully functional on the money path: both legs of a
    # covered call (shared proposal_id+asof, distinct asset) land — the 5-col key is
    # live, no silent drop.
    base = dict(
        proposal_id="prop_x",
        asof_execution="2026-05-30T18:00:00Z",
        account_id="paper-default",
    )
    ps.apply_execution(
        {
            **base,
            "asset": "NVDA260626C00160000",
            "asset_class": "us_option",
            "fill_price": 4.5,
            "fill_size_pct": -0.05,
            "reactor_metadata": {"quantity": -1},
        }
    )
    ps.apply_execution(
        {
            **base,
            "asset": "NVDA",
            "asset_class": "equity",
            "fill_size_pct": 0.05,
            "fill_price": 160.0,
            "reactor_metadata": {"quantity": 100},
        }
    )
    positions = ps.get_positions("paper-default")
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1
    assert positions[("equity", "NVDA")].quantity == 100


def test_migration_recovers_from_orphan_after_cs44_4col_db(tmp_path) -> None:
    """The same orphan-recovery must hold when the legacy table is the cs44 4-col PK
    (asset/asset_class but no leg_index) — the intermediate-version db an operator who
    upgraded once already before cs51 would have on disk.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE processed_fills (
            proposal_id    TEXT NOT NULL,
            asof_execution TEXT NOT NULL,
            asset          TEXT NOT NULL DEFAULT '',
            asset_class    TEXT NOT NULL DEFAULT '',
            applied_at     TEXT NOT NULL,
            PRIMARY KEY (proposal_id, asof_execution, asset, asset_class)
        );
        INSERT INTO processed_fills
            VALUES ('prop_4c', '2026-02-01T00:00:00Z', 'AAPL', 'equity', '2026-02-01T00:00:01Z');
        """
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    conn.execute(_ORPHAN_NEW_5COL_SCHEMA)
    conn.commit()
    conn.close()

    ps = PortfolioState(state_db_path=db)
    assert ps is not None
    assert _pk_cols(db) == _FIVE_COL_PK
    assert not _table_exists(db, "processed_fills_new")
    with sqlite3.connect(db) as c:
        n_4c = c.execute(
            "SELECT COUNT(*) FROM processed_fills WHERE proposal_id='prop_4c'"
        ).fetchone()[0]
    assert n_4c == 1  # the 4-col legacy row carried forward (leg_index defaults to '')


def test_migration_idempotent_across_repeated_construction(tmp_path) -> None:
    """Constructing PortfolioState repeatedly against the same db is a no-op after the
    first migration — the second+ construction must not re-rebuild or raise. Also asserts
    a fresh db (no legacy table) is untouched (byte-identical path).
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_2COL_SCHEMA)
    conn.commit()
    conn.close()

    PortfolioState(state_db_path=db)  # first: migrates legacy -> 5-col
    assert _pk_cols(db) == _FIVE_COL_PK
    PortfolioState(state_db_path=db)  # second: must be a clean no-op
    PortfolioState(state_db_path=db)  # third: still a clean no-op
    assert _pk_cols(db) == _FIVE_COL_PK
    assert not _table_exists(db, "processed_fills_new")

    # Fresh db: _SCHEMA creates the 5-col PK directly; migration is a no-op and no orphan
    # table is ever created.
    fresh = tmp_path / "fresh.db"
    PortfolioState(state_db_path=fresh)
    assert _pk_cols(fresh) == _FIVE_COL_PK
    assert not _table_exists(fresh, "processed_fills_new")
