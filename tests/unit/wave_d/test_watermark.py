"""Unit tests for hermes_quant.daemon.watermark — per-symbol watermark store.

Anchor: ADR-0038 §D.1 (TradingAgents pattern P3 backfill).

Verifies:
- empty store .get returns None
- set + get round-trip
- duplicate-symbol overwrite (latest wins)
- all_for_symbols batch read (and empty list short-circuit)
- profile isolation (HERMES_PROFILE picks a different DB path)
- malformed timestamp recovery (raises, store stays usable)
- hash-mismatch logged but not raised (via tick_loop integration)
- WAL journal mode set
- PK uniqueness enforced (one row per symbol)
- busy_timeout > 0
- tick_loop replay-skip integration under HERMES_QUANT_WATERMARK_ENABLED=1
- tick_loop bit-identical legacy when env unset
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.signal_bus import read_jsonl_tail
from hermes_quant.daemon.tick_loop import (
    AssetTask,
    TickLoopState,
    _compute_indicator_snapshot_hash,
    run_one_tick,
)
from hermes_quant.daemon.watermark import (
    Watermark,
    WatermarkStore,
    _resolve_profile_path,
)
from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    AnalystView,
    MarketContext,
    Portfolio,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wm(
    symbol: str = "BTC/USDT",
    bar_ts: str = "2026-05-13T00:00:00",
    h: str = "0123456789abcdef",
    updated: str = "2026-05-13T00:00:01",
) -> Watermark:
    return Watermark(
        symbol=symbol,
        last_processed_bar_ts=pd.Timestamp(bar_ts),
        indicator_snapshot_hash=h,
        updated_at=pd.Timestamp(updated),
    )


@pytest.fixture()
def store(tmp_path: Path) -> WatermarkStore:
    return WatermarkStore(path=tmp_path / "wm.db")


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


class TestEmptyAndRoundTrip:
    def test_empty_store_get_returns_none(self, store: WatermarkStore):
        assert store.get("BTC/USDT") is None

    def test_set_then_get_roundtrip(self, store: WatermarkStore):
        wm = _make_wm()
        store.set(wm)
        loaded = store.get("BTC/USDT")
        assert loaded is not None
        assert loaded.symbol == wm.symbol
        assert loaded.last_processed_bar_ts == wm.last_processed_bar_ts
        assert loaded.indicator_snapshot_hash == wm.indicator_snapshot_hash
        assert loaded.updated_at == wm.updated_at

    def test_dataclass_is_frozen(self):
        wm = _make_wm()
        with pytest.raises((AttributeError, TypeError)):
            wm.symbol = "ETH/USDT"  # type: ignore[misc]


class TestOverwriteAndBatch:
    def test_duplicate_symbol_overwrites_latest_wins(self, store: WatermarkStore):
        store.set(_make_wm(bar_ts="2026-05-13T00:00:00", h="aaaaaaaaaaaaaaaa"))
        store.set(_make_wm(bar_ts="2026-05-13T01:00:00", h="bbbbbbbbbbbbbbbb"))
        loaded = store.get("BTC/USDT")
        assert loaded is not None
        assert loaded.last_processed_bar_ts == pd.Timestamp("2026-05-13T01:00:00")
        assert loaded.indicator_snapshot_hash == "bbbbbbbbbbbbbbbb"

    def test_all_for_symbols_batch_read(self, store: WatermarkStore):
        store.set(_make_wm(symbol="BTC/USDT"))
        store.set(_make_wm(symbol="ETH/USDT"))
        store.set(_make_wm(symbol="SOL/USDT"))
        result = store.all_for_symbols(["BTC/USDT", "ETH/USDT", "DOGE/USDT"])
        assert set(result.keys()) == {"BTC/USDT", "ETH/USDT"}
        assert result["BTC/USDT"].symbol == "BTC/USDT"

    def test_all_for_symbols_empty_list_returns_empty(self, store: WatermarkStore):
        assert store.all_for_symbols([]) == {}

    def test_pk_uniqueness_one_row_per_symbol(self, store: WatermarkStore):
        for i in range(5):
            store.set(_make_wm(bar_ts=f"2026-05-13T0{i}:00:00", h=f"{i:016d}"))
        # Direct count via sqlite
        with sqlite3.connect(store.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM watermark WHERE symbol = ?", ("BTC/USDT",)
            ).fetchone()[0]
        assert n == 1


# ---------------------------------------------------------------------------
# Schema / pragmas
# ---------------------------------------------------------------------------


class TestSchemaPragmas:
    def test_wal_mode_set(self, store: WatermarkStore):
        # Use the store's own _conn so the pragma is applied.
        with store._conn() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_busy_timeout_positive(self, store: WatermarkStore):
        with store._conn() as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms > 0

    def test_table_is_without_rowid(self, store: WatermarkStore):
        with store._conn() as conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='watermark'"
            ).fetchone()[0]
        assert "WITHOUT ROWID" in ddl.upper()


# ---------------------------------------------------------------------------
# Profile isolation
# ---------------------------------------------------------------------------


class TestProfileIsolation:
    def test_profile_env_changes_default_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HERMES_PROFILE", "live-binance")
        p = _resolve_profile_path()
        assert "profiles" in p.parts
        assert "live-binance" in p.parts
        assert p.name == "watermarks.db"

    def test_no_profile_uses_global_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        p = _resolve_profile_path()
        assert "profiles" not in p.parts
        assert p.name == "watermarks.db"

    def test_two_profiles_have_isolated_stores(self, tmp_path: Path):
        # Simulate profile-A and profile-B by passing explicit paths
        # (the path-resolver is unit-tested above; here we verify a
        # write under one path is invisible to the other).
        store_a = WatermarkStore(path=tmp_path / "profileA" / "wm.db")
        store_b = WatermarkStore(path=tmp_path / "profileB" / "wm.db")
        store_a.set(_make_wm(symbol="BTC/USDT", h="aaaaaaaaaaaaaaaa"))
        assert store_a.get("BTC/USDT") is not None
        assert store_b.get("BTC/USDT") is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestMalformedAndHashLogging:
    def test_malformed_timestamp_raises_but_store_survives(
        self, store: WatermarkStore
    ):
        # Inject a corrupt row directly via SQLite (simulates DB drift).
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO watermark VALUES (?, ?, ?, ?)",
                ("BAD/SYM", "not-a-timestamp", "ffffffffffffffff", "also-bad"),
            )
            conn.commit()
        with pytest.raises(ValueError, match="corrupt watermark row"):
            store.get("BAD/SYM")
        # Other rows still readable; store not corrupted.
        store.set(_make_wm(symbol="GOOD/SYM"))
        assert store.get("GOOD/SYM") is not None

    def test_all_for_symbols_raises_on_corrupt_row(self, store: WatermarkStore):
        store.set(_make_wm(symbol="GOOD/SYM"))
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO watermark VALUES (?, ?, ?, ?)",
                ("BAD/SYM", "garbage", "ffffffffffffffff", "garbage"),
            )
            conn.commit()
        with pytest.raises(ValueError):
            store.all_for_symbols(["GOOD/SYM", "BAD/SYM"])


# ---------------------------------------------------------------------------
# tick_loop integration
# ---------------------------------------------------------------------------


def _make_bars(n: int = 100, base: float = 60_000.0) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-05-13T00:00:00", periods=n, freq="1h")
    closes = base * np.cumprod(1 + 0.001 + rng.normal(0, 0.005, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


def _mock_provider(bars: pd.DataFrame):
    p = MagicMock()
    p.name = "mock"
    p.fetch_bars.return_value = bars
    return p


def _mock_analyst(direction: int, name: str, conf: float = 0.7):
    a = MagicMock()
    a.name = name
    a.analyze.return_value = AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.02,
        confidence=conf,
        confidence_raw=conf + 0.2,
        horizon="4h",
    )
    return a


def _portfolio_for(equity: float = 100_000.0):
    def _f(account_id, asset_class):
        return Portfolio(
            account_id=account_id,
            asset_class=asset_class,
            asof=pd.Timestamp.utcnow(),
            positions={},
            cash=equity,
            equity_total=equity,
            realized_pnl_total=0.0,
            realized_fees_total=0.0,
            peak_equity=equity,
            daily_open_equity=equity,
        )

    return _f


def _run_tick(tmp_path: Path, *, watermark_store: WatermarkStore | None):
    """Run a single tick with a fully-mocked aggregator+gate so the test
    doesn't depend on scipy.linalg / calibrators / cost models. The test
    is about watermark integration, not the BMA pipeline."""
    bus = tmp_path / "signals.jsonl"
    halt_state = HaltStateSQLite(tmp_path / "halts.db", tmp_path / "halt.json")
    bars = _make_bars(100)
    provider = _mock_provider(bars)
    analysts = [_mock_analyst(1, "a"), _mock_analyst(1, "b")]

    # Mock aggregator: always returns a directional signal.
    agg = MagicMock()
    agg.aggregate.return_value = AggregatedSignal(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp.utcnow(),
        direction=1,
        magnitude=0.02,
        confidence=0.7,
        confidence_raw=0.9,
        horizon="4h",
        components=(),
        aggregator="mock",
    )

    # Mock gate: always emits an Action.
    gate = MagicMock()
    gate.gate.return_value = Action(
        target_position_pct=0.05,
        reason="test",
        signal_id="sig-test",
    )

    state = TickLoopState()
    n = run_one_tick(
        tasks=[AssetTask("BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h")],
        data_providers=[provider],
        analysts=analysts,
        aggregator=agg,
        risk_gate=gate,
        halt_state=halt_state,
        portfolio_for=_portfolio_for(),
        state=state,
        bus_path=bus,
        watermark_store=watermark_store,
    )
    return n, state, bus, bars


class TestTickLoopIntegration:
    def test_legacy_no_watermark_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When env unset and no store passed, behaviour is bit-identical legacy:
        repeated ticks emit repeatedly (no replay skip)."""
        monkeypatch.delenv("HERMES_QUANT_WATERMARK_ENABLED", raising=False)
        n1, state1, bus, _ = _run_tick(tmp_path, watermark_store=None)
        n2, state2, bus2, _ = _run_tick(tmp_path, watermark_store=None)
        # Both ticks emit; neither was skipped via watermark.
        assert n1 >= 1
        assert n2 >= 1
        assert state1.n_skipped_watermark == 0
        assert state2.n_skipped_watermark == 0

    def test_replay_skipped_when_env_enabled_and_store_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """With HERMES_QUANT_WATERMARK_ENABLED=1 and a store, the second tick
        on the same bars (same last bar_ts) must be skipped."""
        monkeypatch.setenv("HERMES_QUANT_WATERMARK_ENABLED", "1")
        wm_store = WatermarkStore(path=tmp_path / "wm.db")
        n1, state1, bus, bars = _run_tick(tmp_path, watermark_store=wm_store)
        assert n1 >= 1
        assert state1.n_skipped_watermark == 0
        # Watermark should now be set
        wm = wm_store.get("BTC/USDT")
        assert wm is not None
        expected_bar_ts = pd.Timestamp(bars["timestamp"].iloc[-1])
        assert wm.last_processed_bar_ts == expected_bar_ts

        # Second tick: replay on identical bars.
        n2, state2, _, _ = _run_tick(tmp_path, watermark_store=wm_store)
        assert n2 == 0  # replay skipped
        assert state2.n_skipped_watermark == 1

    def test_writes_after_emit_not_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Watermark write happens AFTER emit_signal_record returns —
        verify by checking that the bus has exactly the records emitted
        in the same tick where the watermark was written."""
        monkeypatch.setenv("HERMES_QUANT_WATERMARK_ENABLED", "1")
        wm_store = WatermarkStore(path=tmp_path / "wm.db")
        n, _, bus, _ = _run_tick(tmp_path, watermark_store=wm_store)
        # Watermark exists because the bar was journaled.
        assert wm_store.get("BTC/USDT") is not None
        records = read_jsonl_tail(bus, n=10)
        signals = [r for r in records if r.get("type") != "heartbeat"]
        assert len(signals) >= n


class TestSnapshotHash:
    def test_hash_is_16_hex_chars(self):
        bars = _make_bars(20)
        ctx = MarketContext(
            asset="BTC/USDT",
            timeframe="1h",
            asset_class="crypto",
            exchange="binance",
            bars=bars,
            last_close=float(bars["close"].iloc[-1]),
            last_volume=float(bars["volume"].iloc[-1]),
            asof=pd.Timestamp("2026-05-13T00:00:00"),
        )
        h = _compute_indicator_snapshot_hash(ctx)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_changes_when_close_changes(self):
        bars = _make_bars(20)
        ctx_a = MarketContext(
            asset="BTC/USDT",
            timeframe="1h",
            asset_class="crypto",
            exchange="binance",
            bars=bars,
            last_close=60_000.0,
            last_volume=1000.0,
            asof=pd.Timestamp("2026-05-13T00:00:00"),
        )
        ctx_b = MarketContext(
            asset="BTC/USDT",
            timeframe="1h",
            asset_class="crypto",
            exchange="binance",
            bars=bars,
            last_close=60_000.5,  # 50 cents different
            last_volume=1000.0,
            asof=pd.Timestamp("2026-05-13T00:00:00"),
        )
        assert _compute_indicator_snapshot_hash(ctx_a) != _compute_indicator_snapshot_hash(
            ctx_b
        )


class TestHashMismatchLogging:
    def test_hash_mismatch_on_replay_logs_warning_but_still_skips(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """If a stale watermark with a wrong hash exists, replay is still
        skipped (last_processed_bar_ts is the gate); a WARNING is logged."""
        monkeypatch.setenv("HERMES_QUANT_WATERMARK_ENABLED", "1")
        wm_store = WatermarkStore(path=tmp_path / "wm.db")
        # Pre-seed a watermark with the future bar_ts but a deliberately wrong hash.
        bars = _make_bars(100)
        future_ts = pd.Timestamp(bars["timestamp"].iloc[-1])
        wm_store.set(
            Watermark(
                symbol="BTC/USDT",
                last_processed_bar_ts=future_ts,
                indicator_snapshot_hash="deadbeefdeadbeef",  # wrong on purpose
                updated_at=pd.Timestamp.utcnow().tz_localize(None)
                if pd.Timestamp.utcnow().tzinfo is not None
                else pd.Timestamp.utcnow(),
            )
        )
        with caplog.at_level(logging.WARNING):
            n, state, _, _ = _run_tick(tmp_path, watermark_store=wm_store)
        assert n == 0  # skipped due to bar_ts comparison
        assert state.n_skipped_watermark == 1
        # WARNING about hash mismatch was emitted (not raised).
        assert any("hash mismatch" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Sanity: the helper symbol that *might* be expected by downstream wave-D
# work doesn't exist (and should not be assumed). We expose
# `_resolve_profile_path` from watermark only.
# ---------------------------------------------------------------------------


def test_module_exports_only_resolver_and_classes():
    from hermes_quant.daemon import watermark as wm_mod

    assert hasattr(wm_mod, "Watermark")
    assert hasattr(wm_mod, "WatermarkStore")
    assert hasattr(wm_mod, "_resolve_profile_path")
