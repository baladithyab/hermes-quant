"""Unit tests for hermes_quant.risk.baseline_store.DrawdownBaselineStore.

cs01 fix — durable high-water-mark peak + session-anchored daily-open backing the
ADR-0004 risk gate's Rule-1 (drawdown) / Rule-2 (daily-loss) circuit breakers.

Mirrors the durability / fail-closed posture of daemon/halt_state.py
HaltStateSQLite. Every test uses tmp_path for the DB + mirror — NEVER the live
~/.hermes/quant/state.db.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.risk.baseline_store import (
    Baseline,
    DrawdownBaselineStore,
    read_baseline_mirror,
    session_key,
)


@pytest.fixture()
def store(tmp_path: Path) -> DrawdownBaselineStore:
    return DrawdownBaselineStore(
        db_path=tmp_path / "state.db",
        mirror_path=tmp_path / "drawdown_baselines.json",
    )


_ASOF = pd.Timestamp("2026-05-13T12:00:00Z")


# ---------------------------------------------------------------------------
# session_key — shares the gate's session clock
# ---------------------------------------------------------------------------


class TestSessionKey:
    def test_utc_is_calendar_date(self):
        assert session_key("UTC", pd.Timestamp("2026-05-13T23:59:00Z")) == "2026-05-13"
        assert session_key("UTC", pd.Timestamp("2026-05-14T00:01:00Z")) == "2026-05-14"

    def test_non_utc_is_local_calendar_date(self):
        # 2026-05-13T01:00:00Z is 2026-05-12 21:00 in New York → local date 05-12.
        ts = pd.Timestamp("2026-05-13T01:00:00Z")
        assert session_key("America/New_York", ts) == "2026-05-12"

    def test_naive_asof_treated_as_utc(self):
        assert session_key("UTC", pd.Timestamp("2026-05-13T10:00:00")) == "2026-05-13"

    def test_bad_tz_falls_back_to_utc_date(self):
        assert session_key("Not/AZone", pd.Timestamp("2026-05-13T10:00:00Z")) == "2026-05-13"


# ---------------------------------------------------------------------------
# reconcile — seed, monotonic HWM, session anchor
# ---------------------------------------------------------------------------


class TestReconcileSeed:
    def test_first_call_seeds_peak_open_to_equity(self, store):
        b = store.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        assert isinstance(b, Baseline)
        assert b.peak_equity == 100_000.0
        assert b.daily_open_equity == 100_000.0
        assert b.degraded is False

    def test_fresh_account_recomputes_to_zero(self, store):
        # peak == open == now → drawdown 0 / daily-loss 0 (byte-identical to today)
        b = store.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        drawdown = max(0.0, (b.peak_equity - 100_000.0) / b.peak_equity)
        daily_loss = max(0.0, (b.daily_open_equity - 100_000.0) / b.daily_open_equity)
        assert drawdown == 0.0
        assert daily_loss == 0.0


class TestMonotonicHWM:
    def test_peak_is_running_max(self, store):
        store.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        b = store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        assert b.peak_equity == 130_000.0

    def test_peak_never_decreases_on_drawdown(self, store):
        store.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        b = store.reconcile("acct", "crypto", 90_000.0, _ASOF, "UTC")
        # Ran 100k → 130k → 90k: HWM stays 130k; recomputed drawdown is large.
        assert b.peak_equity == 130_000.0
        drawdown = max(0.0, (b.peak_equity - 90_000.0) / b.peak_equity)
        assert drawdown == pytest.approx(0.30769, abs=1e-4)

    def test_peak_recovers_but_hwm_stays(self, store):
        store.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        store.reconcile("acct", "crypto", 90_000.0, _ASOF, "UTC")
        b = store.reconcile("acct", "crypto", 110_000.0, _ASOF, "UTC")
        assert b.peak_equity == 130_000.0
        drawdown = max(0.0, (b.peak_equity - 110_000.0) / b.peak_equity)
        assert drawdown == pytest.approx(0.15385, abs=1e-4)

    def test_profitable_then_drawdown_trips_15pct(self, store):
        # The cs01 scenario: inception 100k → peak 130k → 104k (down 20% peak-to-
        # trough, still +4% vs inception). Durable HWM peak=130k → drawdown 0.20.
        store.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        b = store.reconcile("acct", "crypto", 104_000.0, _ASOF, "UTC")
        drawdown = max(0.0, (b.peak_equity - 104_000.0) / b.peak_equity)
        assert drawdown == pytest.approx(0.20, abs=1e-9)
        assert drawdown > 0.15  # Rule-1 default threshold → would TRIP


class TestSessionAnchor:
    def test_same_session_keeps_open_mark(self, store):
        # Session open at 125k, falls to 117k same UTC day → daily_open stays 125k.
        t0 = pd.Timestamp("2026-05-13T09:30:00Z")
        t1 = pd.Timestamp("2026-05-13T15:00:00Z")
        store.reconcile("acct", "crypto", 125_000.0, t0, "UTC")
        b = store.reconcile("acct", "crypto", 117_000.0, t1, "UTC")
        assert b.daily_open_equity == 125_000.0
        daily_loss = max(0.0, (b.daily_open_equity - 117_000.0) / b.daily_open_equity)
        assert daily_loss == pytest.approx(0.064, abs=1e-3)
        assert daily_loss > 0.05  # Rule-2 default threshold → would TRIP

    def test_new_session_reanchors_open(self, store):
        t0 = pd.Timestamp("2026-05-13T15:00:00Z")
        t1 = pd.Timestamp("2026-05-14T09:30:00Z")  # next UTC day → new session
        store.reconcile("acct", "crypto", 125_000.0, t0, "UTC")
        store.reconcile("acct", "crypto", 117_000.0, t0, "UTC")
        b = store.reconcile("acct", "crypto", 117_000.0, t1, "UTC")
        # New session anchors the open to the new equity → daily-loss resets to 0.
        assert b.daily_open_equity == 117_000.0
        assert b.session_key == "2026-05-14"
        daily_loss = max(0.0, (b.daily_open_equity - 117_000.0) / b.daily_open_equity)
        assert daily_loss == 0.0

    def test_session_anchor_is_open_not_trailing_high(self, store):
        # A profitable intraday move does NOT raise the daily anchor (it is the
        # session OPEN, not a trailing high) — so a later fall is measured vs the
        # open, not the intraday peak.
        t = pd.Timestamp("2026-05-13T10:00:00Z")
        store.reconcile("acct", "crypto", 100_000.0, t, "UTC")  # open
        store.reconcile("acct", "crypto", 120_000.0, t, "UTC")  # intraday high
        b = store.reconcile("acct", "crypto", 99_000.0, t, "UTC")  # fall
        assert b.daily_open_equity == 100_000.0  # NOT 120k


# ---------------------------------------------------------------------------
# Durability across instances (restart survival) + mirror
# ---------------------------------------------------------------------------


class TestDurability:
    def test_survives_new_instance(self, tmp_path):
        db = tmp_path / "state.db"
        mirror = tmp_path / "drawdown_baselines.json"
        s1 = DrawdownBaselineStore(db_path=db, mirror_path=mirror)
        s1.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        s1.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")

        s2 = DrawdownBaselineStore(db_path=db, mirror_path=mirror)
        got = s2.get("acct", "crypto")
        assert got is not None
        assert got.peak_equity == 130_000.0
        # A reconcile on the fresh instance keeps the durable HWM.
        b = s2.reconcile("acct", "crypto", 104_000.0, _ASOF, "UTC")
        assert b.peak_equity == 130_000.0

    def test_json_mirror_written(self, store, tmp_path):
        store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        data = read_baseline_mirror(tmp_path / "drawdown_baselines.json")
        assert "acct|crypto" in data
        assert data["acct|crypto"]["peak_equity"] == 130_000.0

    def test_get_returns_none_for_unknown_partition(self, store):
        assert store.get("nope", "crypto") is None

    def test_partitions_are_independent(self, store):
        store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        store.reconcile("acct", "equities", 50_000.0, _ASOF, "UTC")
        assert store.get("acct", "crypto").peak_equity == 130_000.0
        assert store.get("acct", "equities").peak_equity == 50_000.0


# ---------------------------------------------------------------------------
# FAIL-CLOSED — a durable failure NEVER reads as "no drawdown"
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_corrupt_db_fails_closed_conservative(self, tmp_path):
        # Seed the in-memory HWM via a working call, then corrupt the DB file so
        # the next reconcile's SQLite op raises → fail-CLOSED to conservative
        # in-memory baseline (NEVER raises, peak preserved).
        db = tmp_path / "state.db"
        mirror = tmp_path / "drawdown_baselines.json"
        s = DrawdownBaselineStore(db_path=db, mirror_path=mirror)
        s.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")

        # Corrupt the DB on disk.
        db.write_bytes(b"not a sqlite database at all" * 100)

        b = s.reconcile("acct", "crypto", 104_000.0, _ASOF, "UTC")
        assert b.degraded is True
        # Conservative: peak is the max seen this process (>= 130k), never lost.
        assert b.peak_equity >= 130_000.0
        drawdown = max(0.0, (b.peak_equity - 104_000.0) / b.peak_equity)
        assert drawdown >= 0.20  # still trips — fail-CLOSED, not fail-open

    def test_non_finite_equity_does_not_corrupt_durable_row(self, store):
        # Seed a good HWM, then feed NaN equity → degraded baseline that does NOT
        # overwrite the durable peak with NaN.
        store.reconcile("acct", "crypto", 130_000.0, _ASOF, "UTC")
        b = store.reconcile("acct", "crypto", float("nan"), _ASOF, "UTC")
        assert b.degraded is True
        # The persisted HWM is untouched (still finite 130k).
        got = store.get("acct", "crypto")
        assert got is not None
        assert got.peak_equity == 130_000.0

    def test_reconcile_never_raises_on_bad_path(self, tmp_path):
        # A directory where the DB file should be → connection/exec failure.
        bad = tmp_path / "subdir"
        bad.mkdir()
        # Point the db_path at the directory itself (cannot open as a DB file).
        s = DrawdownBaselineStore.__new__(DrawdownBaselineStore)
        s.db_path = bad
        s.mirror_path = tmp_path / "m.json"
        import threading

        s._lock = threading.RLock()
        s._mem_peak = {}
        s._mem_daily_open = {}
        s._mem_session = {}
        # No _init_schema (we bypassed __init__) — reconcile must still not raise.
        b = s.reconcile("acct", "crypto", 100_000.0, _ASOF, "UTC")
        assert b.degraded is True
        assert b.peak_equity == 100_000.0  # seeded conservatively to equity
