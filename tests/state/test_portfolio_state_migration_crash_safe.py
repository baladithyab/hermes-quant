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


# ---------------------------------------------------------------------------
# _migrate_positions_unit_kind TOCTOU race (ar-concurrent-unit-kind)
# ---------------------------------------------------------------------------

_LEGACY_POSITIONS_NO_UNIT_KIND = """
    CREATE TABLE positions (
        account_id       TEXT NOT NULL,
        asset_class      TEXT NOT NULL DEFAULT 'equity',
        symbol           TEXT NOT NULL,
        quantity         REAL NOT NULL DEFAULT 0.0,
        avg_entry_price  REAL NOT NULL DEFAULT 0.0,
        last_update_at   TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (account_id, asset_class, symbol)
    );
    INSERT INTO positions VALUES ('paper-default', 'equity', 'AAPL', 0.10, 160.0, '2026-01-01T00:00:00Z');
"""


def test_migrate_positions_unit_kind_concurrent_no_crash(tmp_path) -> None:
    """RED on unpatched code: two connections that both pass the outer PRAGMA check
    (unit_kind absent) BEFORE either issues the ALTER will BOTH attempt the bare ALTER
    TABLE — the second raises OperationalError: duplicate column name: unit_kind
    (crashing the PortfolioState constructor for the second process).

    GREEN after fix: the patched function wraps the ALTER in BEGIN IMMEDIATE + a
    re-check inside the lock, so the second connection finds unit_kind already present
    and exits cleanly without crashing.

    We simulate the TOCTOU window directly: both connections confirm unit_kind is absent
    (outer PRAGMA check), then c1 issues the bare ALTER (as the unpatched code does),
    then we verify c2's bare ALTER crashes — confirming that the unpatched code is
    dangerous. We then call the PATCHED _migrate_positions_unit_kind on a fresh legacy DB
    twice in sequence with both connections having already confirmed "no unit_kind" — the
    second call must not crash.
    """
    db = tmp_path / "positions_race.db"
    # Seed a legacy positions table WITHOUT the unit_kind column.
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_POSITIONS_NO_UNIT_KIND)
    conn.commit()
    conn.close()

    # --- Part 1: confirm the unpatched bare-ALTER race IS a real crash ---
    c1 = sqlite3.connect(str(db), isolation_level=None)
    c2 = sqlite3.connect(str(db), isolation_level=None)

    # Both confirm unit_kind is absent (they are in the TOCTOU window).
    info1 = list(c1.execute("PRAGMA table_info(positions)"))
    info2 = list(c2.execute("PRAGMA table_info(positions)"))
    assert "unit_kind" not in {r[1] for r in info1}, "precondition: legacy DB has no unit_kind"
    assert "unit_kind" not in {r[1] for r in info2}, "precondition: legacy DB has no unit_kind"

    # c1 does the bare ALTER (what the unpatched code would do after the PRAGMA check).
    c1.execute(
        "ALTER TABLE positions ADD COLUMN unit_kind TEXT NOT NULL DEFAULT 'nav_fraction'"
    )
    # c2 now attempts the same bare ALTER — this is the crash the fix prevents.
    crashed = False
    try:
        c2.execute(
            "ALTER TABLE positions ADD COLUMN unit_kind TEXT NOT NULL DEFAULT 'nav_fraction'"
        )
    except Exception as exc:
        crashed = True
        assert "duplicate column name" in str(exc).lower(), (
            f"expected duplicate-column-name OperationalError; got: {exc}"
        )
    assert crashed, (
        "Expected bare ALTER TABLE to crash on duplicate column — "
        "this confirms the unpatched TOCTOU race is real"
    )
    c1.close()
    c2.close()

    # --- Part 2: the PATCHED function must NOT crash in the same scenario ---
    # Use a fresh legacy DB to reset state.
    db2 = tmp_path / "positions_race2.db"
    conn2 = sqlite3.connect(db2)
    conn2.executescript(_LEGACY_POSITIONS_NO_UNIT_KIND)
    conn2.commit()
    conn2.close()

    p1 = sqlite3.connect(str(db2), isolation_level=None)
    p2 = sqlite3.connect(str(db2), isolation_level=None)

    # Both confirm unit_kind is absent (TOCTOU window reproduced).
    pi1 = list(p1.execute("PRAGMA table_info(positions)"))
    pi2 = list(p2.execute("PRAGMA table_info(positions)"))
    assert "unit_kind" not in {r[1] for r in pi1}
    assert "unit_kind" not in {r[1] for r in pi2}

    # p1 migrates via the patched function.
    PortfolioState._migrate_positions_unit_kind(p1)
    # p2 must NOT crash — before the fix this would raise OperationalError because the
    # outer PRAGMA check (done before this call) would have already passed and a bare
    # ALTER would be attempted. With the fix, BEGIN IMMEDIATE + inner re-check handles it.
    PortfolioState._migrate_positions_unit_kind(p2)

    p1.close()
    p2.close()

    # The column must be present and the legacy row preserved.
    with sqlite3.connect(db2) as verify:
        info = list(verify.execute("PRAGMA table_info(positions)"))
        cols = {r[1] for r in info}
        assert "unit_kind" in cols, "unit_kind column must exist after concurrent migration"
        row = verify.execute(
            "SELECT unit_kind FROM positions WHERE account_id='paper-default' AND symbol='AAPL'"
        ).fetchone()
        assert row is not None, "legacy row must survive migration"
        assert row[0] == "nav_fraction", "legacy row backfilled to nav_fraction"


def test_migrate_positions_unit_kind_idempotent(tmp_path) -> None:
    """Calling _migrate_positions_unit_kind twice on the same connection is a no-op;
    calling it on a fresh DB (unit_kind already in schema) is also a no-op.
    """
    db = tmp_path / "positions_idem.db"
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_POSITIONS_NO_UNIT_KIND)
    conn.commit()
    conn.close()

    c = sqlite3.connect(str(db), isolation_level=None)
    PortfolioState._migrate_positions_unit_kind(c)  # first: adds column
    PortfolioState._migrate_positions_unit_kind(c)  # second: must be a silent no-op
    c.close()

    # Fresh DB created by PortfolioState._init_schema already has unit_kind.
    fresh = tmp_path / "fresh_positions.db"
    ps = PortfolioState(state_db_path=fresh)
    fc = sqlite3.connect(str(fresh), isolation_level=None)
    PortfolioState._migrate_positions_unit_kind(fc)  # no-op on already-migrated DB
    fc.close()
