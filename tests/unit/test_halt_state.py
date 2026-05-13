"""Unit tests for hermes_quant.daemon.halt_state — durable SQLite halt registry.

Anchor: synthesis-v2 §P0-D + §P1-β. Verifies:
- '*' wildcard sentinels (no NULL ambiguity in PK)
- WITHOUT ROWID storage class
- UNIQUE (account_id, asset_class, asset, halt_epoch) — multiple halts at
  same scope after clearing get a new epoch, never collide
- Wildcard scope semantics (parent halts cover children)
- Persistence across reopens
- JSON mirror atomic-writes
- Required --reason on add/clear (audit trail)
- Protocol contract (HaltState)
- auto_clear_expired
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.daemon.halt_state import (
    WILDCARD,
    HaltStateSQLite,
    read_halt_mirror,
)
from hermes_quant.protocol import HaltState


@pytest.fixture()
def hs(tmp_path: Path) -> HaltStateSQLite:
    """Fresh halt state per test, with isolated DB + mirror."""
    return HaltStateSQLite(
        db_path=tmp_path / "test_state.db",
        mirror_path=tmp_path / "halt_mirror.json",
    )


class TestProtocolContract:
    def test_satisfies_halt_state_protocol(self, hs: HaltStateSQLite):
        """HaltStateSQLite must satisfy the HaltState protocol from protocol.py."""
        assert isinstance(hs, HaltState)


class TestSchemaConstraints:
    """SQLite schema enforces synthesis-v2 §P1-β invariants."""

    def test_wildcard_sentinel_replaces_none(self, hs: HaltStateSQLite):
        """add_halt(account_id=None) stores '*', not NULL."""
        rec = hs.add_halt(None, "crypto", "BTC/USDT", reason="test")
        assert rec.account_id == WILDCARD
        # Verify directly via SQLite
        with hs._conn() as conn:
            row = conn.execute(
                "SELECT account_id FROM halts WHERE asset_class='crypto'"
            ).fetchone()
            assert row["account_id"] == "*"

    def test_columns_are_not_null(self, hs: HaltStateSQLite):
        """All scope columns are NOT NULL — schema constraint."""
        with hs._conn() as conn:
            cols = list(conn.execute("PRAGMA table_info(halts)"))
        col_map = {c["name"]: c for c in cols}
        for col in ["account_id", "asset_class", "asset", "reason",
                    "halted_at", "halt_epoch"]:
            assert col_map[col]["notnull"] == 1, f"{col} must be NOT NULL"

    def test_table_is_without_rowid(self, hs: HaltStateSQLite):
        """Table is WITHOUT ROWID for tighter PK enforcement."""
        with hs._conn() as conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='halts'"
            ).fetchone()["sql"]
        assert "WITHOUT ROWID" in ddl.upper()

    def test_pk_includes_halt_epoch(self, hs: HaltStateSQLite):
        """PK = (account_id, asset_class, asset, halt_epoch)."""
        with hs._conn() as conn:
            cols = list(conn.execute("PRAGMA table_info(halts)"))
        pk_cols = [c["name"] for c in cols if c["pk"] > 0]
        assert set(pk_cols) == {"account_id", "asset_class", "asset", "halt_epoch"}


class TestAddHalt:
    def test_basic_add(self, hs: HaltStateSQLite):
        rec = hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="test halt")
        assert rec.account_id == "alpaca-paper"
        assert rec.asset_class == "crypto"
        assert rec.asset == "BTC/USDT"
        assert rec.reason == "test halt"
        assert rec.halt_epoch == 1
        assert rec.halted_until is None

    def test_add_with_halted_until(self, hs: HaltStateSQLite):
        until = pd.Timestamp("2026-12-31T00:00:00Z")
        rec = hs.add_halt("alpaca-paper", "crypto", "BTC/USDT",
                          reason="cooldown", halted_until=until)
        assert rec.halted_until == until

    def test_reject_empty_reason(self, hs: HaltStateSQLite):
        with pytest.raises(ValueError, match="reason is required"):
            hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="")
        with pytest.raises(ValueError, match="reason is required"):
            hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="   ")

    def test_reject_duplicate_active_halt(self, hs: HaltStateSQLite):
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="first")
        with pytest.raises(ValueError, match="active halt already exists"):
            hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="second")

    def test_re_add_after_clear_increments_epoch(self, hs: HaltStateSQLite):
        """Per synthesis-v2: PK includes epoch so re-halts after clear don't collide."""
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="first")
        hs.clear_halt("alpaca-paper", "crypto", "BTC/USDT", reason="resume")
        rec2 = hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="second")
        assert rec2.halt_epoch == 2


class TestIsHalted:
    """Wildcard scope semantics: parent halts cover children."""

    def test_specific_scope_halts_specific(self, hs: HaltStateSQLite):
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="t")
        assert hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        assert not hs.is_halted("alpaca-paper", "crypto", "ETH/USDT")
        assert not hs.is_halted("binance-spot", "crypto", "BTC/USDT")

    def test_account_wildcard_halts_all_assets(self, hs: HaltStateSQLite):
        """Halt at (alpaca-paper, *, *) halts all assets in alpaca-paper."""
        hs.add_halt("alpaca-paper", None, None, reason="account-wide circuit breaker")
        assert hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        assert hs.is_halted("alpaca-paper", "equity", "AAPL")
        assert not hs.is_halted("binance-spot", "crypto", "BTC/USDT")

    def test_class_wildcard_halts_all_assets_in_class(self, hs: HaltStateSQLite):
        """Halt at (*, crypto, *) halts crypto across all accounts."""
        hs.add_halt(None, "crypto", None, reason="market closed")
        assert hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        assert hs.is_halted("binance-spot", "crypto", "ETH/USDT")
        assert not hs.is_halted("alpaca-paper", "equity", "AAPL")

    def test_full_wildcard_halts_everything(self, hs: HaltStateSQLite):
        hs.add_halt(None, None, None, reason="emergency stop")
        assert hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        assert hs.is_halted("binance-spot", "equity", "AAPL")
        assert hs.is_halted("ibkr", "fx", "EUR/USD")

    def test_asset_none_in_query_only_matches_class_or_account_wildcard(
        self, hs: HaltStateSQLite
    ):
        """Querying with asset=None ('any asset in class') matches halts at
        (account, class, *) or wider — not at a specific asset."""
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="specific")
        # Asking "is alpaca-paper crypto halted at all?" with asset=None →
        # we have a halt at (alpaca-paper, crypto, BTC/USDT). The query
        # asset=WILDCARD doesn't match the specific BTC/USDT, but the SQL
        # uses (asset = ? OR asset = '*'), where '*' matches our specific
        # row's asset='BTC/USDT'? No — '*' in the row is the row's value,
        # not a wildcard match. The semantics: the halt is BTC/USDT-specific,
        # so "is alpaca-paper-crypto halted (any asset)" → False.
        assert not hs.is_halted("alpaca-paper", "crypto", None)
        # A class-wide halt does match an asset=None query
        hs.add_halt("alpaca-paper", "equity", None, reason="class-wide")
        assert hs.is_halted("alpaca-paper", "equity", None)


class TestClearHalt:
    def test_clear_active(self, hs: HaltStateSQLite):
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="test")
        assert hs.clear_halt("alpaca-paper", "crypto", "BTC/USDT",
                             reason="manual resume after review")
        assert not hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")

    def test_clear_nonexistent_returns_false(self, hs: HaltStateSQLite):
        assert not hs.clear_halt("alpaca-paper", "crypto", "BTC/USDT",
                                  reason="nothing to clear")

    def test_clear_requires_reason(self, hs: HaltStateSQLite):
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="t")
        with pytest.raises(ValueError, match="reason is required"):
            hs.clear_halt("alpaca-paper", "crypto", "BTC/USDT", reason="")

    def test_cleared_halt_persists_in_audit_log(self, hs: HaltStateSQLite):
        """Cleared halts stay in the table with cleared_at + cleared_reason."""
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="orig")
        hs.clear_halt("alpaca-paper", "crypto", "BTC/USDT",
                      reason="reviewed and resuming")
        with hs._conn() as conn:
            rows = list(conn.execute("SELECT * FROM halts"))
        assert len(rows) == 1  # still there for audit
        assert rows[0]["cleared_at"] is not None
        assert rows[0]["cleared_reason"] == "reviewed and resuming"


class TestAutoClearExpired:
    def test_expired_halt_auto_clears(self, hs: HaltStateSQLite):
        past = pd.Timestamp.utcnow() - pd.Timedelta(minutes=5)
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT",
                    reason="cooldown", halted_until=past)
        assert hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        n = hs.auto_clear_expired()
        assert n == 1
        assert not hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")

    def test_future_halt_not_cleared(self, hs: HaltStateSQLite):
        future = pd.Timestamp.utcnow() + pd.Timedelta(hours=1)
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT",
                    reason="cooldown", halted_until=future)
        n = hs.auto_clear_expired()
        assert n == 0
        assert hs.is_halted("alpaca-paper", "crypto", "BTC/USDT")

    def test_no_halted_until_not_auto_cleared(self, hs: HaltStateSQLite):
        """halted_until=None means explicit-resume only; auto_clear ignores."""
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT",
                    reason="manual halt", halted_until=None)
        n = hs.auto_clear_expired()
        assert n == 0


class TestPersistenceAcrossReopens:
    def test_halt_survives_reopen(self, tmp_path: Path):
        """Critical for synthesis-v2 §P0-D: durable across daemon restart."""
        db = tmp_path / "state.db"
        mirror = tmp_path / "halt_mirror.json"
        hs1 = HaltStateSQLite(db, mirror)
        hs1.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="persist")

        # Close and reopen
        hs2 = HaltStateSQLite(db, mirror)
        assert hs2.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        assert len(hs2.active_halts()) == 1


class TestJsonMirror:
    def test_mirror_written_on_add(self, hs: HaltStateSQLite):
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="mirror test")
        data = read_halt_mirror(hs.mirror_path)
        assert len(data) == 1
        assert data[0]["account_id"] == "alpaca-paper"
        assert data[0]["asset"] == "BTC/USDT"

    def test_mirror_updated_on_clear(self, hs: HaltStateSQLite):
        hs.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="t")
        assert len(read_halt_mirror(hs.mirror_path)) == 1
        hs.clear_halt("alpaca-paper", "crypto", "BTC/USDT", reason="r")
        assert read_halt_mirror(hs.mirror_path) == []

    def test_mirror_handles_wildcard_correctly(self, hs: HaltStateSQLite):
        hs.add_halt(None, None, None, reason="emergency")
        data = read_halt_mirror(hs.mirror_path)
        assert data[0]["account_id"] == "*"
        assert data[0]["asset_class"] == "*"
        # asset=None in the record (mirror normalizes)
        assert data[0]["asset"] is None or data[0]["asset"] == "*"

    def test_read_mirror_missing_returns_empty(self, tmp_path: Path):
        assert read_halt_mirror(tmp_path / "no_such.json") == []

    def test_read_mirror_corrupted_returns_empty(self, tmp_path: Path):
        p = tmp_path / "corrupted.json"
        p.write_text("not valid json {{")
        assert read_halt_mirror(p) == []


class TestActiveHalts:
    def test_returns_only_active(self, hs: HaltStateSQLite):
        hs.add_halt("a", "crypto", "BTC", reason="t1")
        hs.add_halt("b", "equity", "AAPL", reason="t2")
        hs.clear_halt("a", "crypto", "BTC", reason="r")
        active = hs.active_halts()
        assert len(active) == 1
        assert active[0].account_id == "b"
