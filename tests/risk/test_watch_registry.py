"""ADR-0099 AG-EQ-3 — WatchRegistry: durable per-play watched-position sidecar.

Tests pin the four mandatory invariants:
  1. peak is monotonic (update_peak with a LOWER value keeps the prior high)
  2. tranches_taken increments correctly
  3. atomic-write survives a simulated mid-write error (no partial state)
  4. get(absent) -> None (fail-CLOSED absent-symbol behaviour)

Plus: record_open idempotency, drop, finite-guard rejections, JSON mirror round-trip.

Every test uses tmp_path for the DB + mirror — NEVER the live ~/.hermes/quant/state.db.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import unittest.mock as mock
from pathlib import Path

import pytest

from hermes_quant.risk.watch_registry import (
    WatchRegistry,
    WatchedPosition,
    read_watch_mirror,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def reg(tmp_path: Path) -> WatchRegistry:
    return WatchRegistry(
        db_path=tmp_path / "state.db",
        mirror_path=tmp_path / "watch_registry.json",
    )


# ---------------------------------------------------------------------------
# 1. get(absent) -> None
# ---------------------------------------------------------------------------


def test_get_absent_returns_none(reg):
    assert reg.get("AAPL") is None


def test_get_after_drop_returns_none(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    assert reg.get("AAPL") is not None
    reg.drop("AAPL")
    assert reg.get("AAPL") is None


# ---------------------------------------------------------------------------
# 2. record_open — basic round-trip + idempotency
# ---------------------------------------------------------------------------


def test_record_open_basic(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    pos = reg.get("AAPL")
    assert pos is not None
    assert isinstance(pos, WatchedPosition)
    assert pos.symbol == "AAPL"
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.stop_pct == pytest.approx(0.08)
    assert pos.tranches_taken == 0
    assert pos.peak_gain_pct == pytest.approx(0.0)
    assert pos.opened_at  # non-empty ISO timestamp


def test_record_open_idempotent(reg):
    """Calling record_open twice must not overwrite existing state."""
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    # Simulate a tick bump and a tranche before the second record_open call.
    reg.update_peak("AAPL", 0.12)
    reg.mark_tranche("AAPL")
    # Re-calling record_open should NOT reset peak or tranches_taken.
    reg.record_open("AAPL", entry_price=999.0, stop_pct=0.99)
    pos = reg.get("AAPL")
    assert pos is not None
    # Idempotent: original values preserved.
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.stop_pct == pytest.approx(0.08)
    assert pos.tranches_taken == 1
    assert pos.peak_gain_pct == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# 3. peak is MONOTONIC — update_peak with a lower value keeps the high (RED proof)
# ---------------------------------------------------------------------------


def test_update_peak_monotonic_never_lowers():
    """The core monotonic-max invariant: update_peak(lower) must NOT lower the stored peak.

    RED proof: if we remove the max() in update_peak and just do an unconditional
    UPDATE SET peak_gain_pct=?, the second update_peak(0.05) WOULD lower the peak
    from 0.20 to 0.05 and this assertion would fail.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = WatchRegistry(db_path=tmp / "s.db", mirror_path=tmp / "m.json")
        r.record_open("X", entry_price=100.0, stop_pct=0.08)
        r.update_peak("X", 0.20)
        assert r.get("X").peak_gain_pct == pytest.approx(0.20)
        # Attempt to lower the peak.
        r.update_peak("X", 0.05)
        pos = r.get("X")
        assert pos.peak_gain_pct == pytest.approx(0.20), (
            "update_peak with a LOWER value must keep the prior high (monotonic max invariant)"
        )


def test_update_peak_raises_higher():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = WatchRegistry(db_path=tmp / "s.db", mirror_path=tmp / "m.json")
        r.record_open("X", entry_price=100.0, stop_pct=0.08)
        r.update_peak("X", 0.10)
        r.update_peak("X", 0.25)
        assert r.get("X").peak_gain_pct == pytest.approx(0.25)


def test_update_peak_nan_is_ignored(reg):
    """NaN update must NOT corrupt the stored peak (silence-by-default)."""
    reg.record_open("TSLA", entry_price=200.0, stop_pct=0.08)
    reg.update_peak("TSLA", 0.15)
    reg.update_peak("TSLA", float("nan"))
    assert reg.get("TSLA").peak_gain_pct == pytest.approx(0.15)


def test_update_peak_inf_is_ignored(reg):
    """Infinite update must NOT corrupt the stored peak."""
    reg.record_open("TSLA", entry_price=200.0, stop_pct=0.08)
    reg.update_peak("TSLA", 0.10)
    reg.update_peak("TSLA", float("inf"))
    assert reg.get("TSLA").peak_gain_pct == pytest.approx(0.10)


def test_update_peak_absent_is_noop(reg):
    """update_peak on an absent symbol must not create a record."""
    reg.update_peak("GHOST", 0.30)
    assert reg.get("GHOST") is None


# ---------------------------------------------------------------------------
# 4. tranches_taken increments correctly
# ---------------------------------------------------------------------------


def test_tranches_taken_increments(reg):
    reg.record_open("NVDA", entry_price=400.0, stop_pct=0.08)
    assert reg.get("NVDA").tranches_taken == 0
    reg.mark_tranche("NVDA")
    assert reg.get("NVDA").tranches_taken == 1
    reg.mark_tranche("NVDA")
    assert reg.get("NVDA").tranches_taken == 2


def test_mark_tranche_absent_is_noop(reg):
    reg.mark_tranche("GHOST")
    assert reg.get("GHOST") is None


# ---------------------------------------------------------------------------
# 5. Atomic write — mid-write error leaves state consistent
# ---------------------------------------------------------------------------


def test_atomic_write_survives_mirror_failure(tmp_path):
    """A mirror write failure must NOT corrupt the SQLite state.

    RED proof: if the implementation wrote to SQLite only AFTER a successful mirror
    write, a mid-mirror error would mean the data is never persisted to SQLite.
    The test verifies that even if the mirror write throws, the SQLite row is intact.
    """
    db_path = tmp_path / "state.db"
    mirror_path = tmp_path / "watch_registry.json"
    reg = WatchRegistry(db_path=db_path, mirror_path=mirror_path)
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)

    # Simulate the mirror write raising an error.
    with mock.patch.object(reg, "_write_mirror_safe", side_effect=OSError("disk full")):
        # update_peak tries to write mirror, which now raises — the SQLite write
        # must still complete and the state must be readable.
        try:
            reg.update_peak("AAPL", 0.20)
        except OSError:
            pass  # mirror failure propagated; SQLite already committed

    # Re-open the DB and verify the UPDATE_PEAK write committed to SQLite DESPITE the
    # mirror failure. wave3-review FIX (was VACUOUS): the old assertion checked only
    # entry_price (150.0) which comes from the PRE-PATCH record_open — it passes even if
    # update_peak ROLLED BACK instead of committing. Asserting the peak the PATCHED
    # update_peak wrote is the real test: SQLite commit must precede the mirror write.
    reg2 = WatchRegistry(db_path=db_path, mirror_path=mirror_path)
    pos = reg2.get("AAPL")
    assert pos is not None
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.peak_gain_pct == pytest.approx(0.20), (
        "update_peak's SQLite write must commit BEFORE the mirror write — a mirror "
        "OSError must not lose the peak (got peak that didn't persist past the failure)"
    )


def test_atomic_write_tmp_then_rename(tmp_path):
    """The JSON mirror write must use tmp+rename (no partial writes).

    wave3-review FIX (was VACUOUS): the old test only asserted the .tmp does NOT remain —
    which passes for a DIRECT write that never created a .tmp at all. To actually pin
    tmp+rename, intercept os.replace and assert (a) it was called with a .tmp source and
    (b) the .tmp existed at replace time. If the impl wrote directly via path.write_text,
    os.replace is never called and the test fails.
    """
    db_path = tmp_path / "state.db"
    mirror_path = tmp_path / "watch_registry.json"
    reg = WatchRegistry(db_path=db_path, mirror_path=mirror_path)

    # The impl uses Path.replace (POSIX-atomic rename) from a .tmp source. Spy on it and
    # assert (a) it was called and (b) the source was an existing .tmp — a DIRECT
    # write_text (no tmp+rename) never calls Path.replace, so the test goes RED.
    replace_calls = []
    real_replace = Path.replace

    def _spy_replace(self, target, *a, **k):
        replace_calls.append((str(self), str(target), self.exists(), str(self).endswith(".tmp")))
        return real_replace(self, target, *a, **k)

    with mock.patch.object(Path, "replace", _spy_replace):
        reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)

    assert mirror_path.exists()
    assert "AAPL" in json.loads(mirror_path.read_text())
    assert not (mirror_path.with_suffix(".json.tmp")).exists()  # no stray tmp after
    # The mirror write MUST have gone through Path.replace from an existing .tmp source.
    mirror_replaces = [c for c in replace_calls if c[1] == str(mirror_path)]
    assert mirror_replaces, "mirror write must use tmp+rename (Path.replace), not a direct write"
    src, _dst, src_existed, src_is_tmp = mirror_replaces[-1]
    assert src_is_tmp and src_existed, (
        f"Path.replace must be called with an existing .tmp source; got {mirror_replaces[-1]}"
    )


# ---------------------------------------------------------------------------
# 6. Finite-guard: non-finite inputs to record_open are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), float("-inf"), -1.0, 0.0])
def test_record_open_bad_entry_price_raises(reg, bad_price):
    with pytest.raises((ValueError,)):
        reg.record_open("X", entry_price=bad_price, stop_pct=0.08)
    # Ensure no partial row was written.
    assert reg.get("X") is None


@pytest.mark.parametrize("bad_stop", [float("nan"), float("inf"), float("-inf"), -0.08, 0.0])
def test_record_open_bad_stop_pct_raises(reg, bad_stop):
    with pytest.raises((ValueError,)):
        reg.record_open("X", entry_price=150.0, stop_pct=bad_stop)
    assert reg.get("X") is None


# ---------------------------------------------------------------------------
# 7. Multiple symbols are independent
# ---------------------------------------------------------------------------


def test_multiple_symbols_independent(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    reg.record_open("NVDA", entry_price=400.0, stop_pct=0.10)
    reg.update_peak("AAPL", 0.20)
    reg.mark_tranche("NVDA")

    aapl = reg.get("AAPL")
    nvda = reg.get("NVDA")
    assert aapl.peak_gain_pct == pytest.approx(0.20)
    assert aapl.tranches_taken == 0
    assert nvda.peak_gain_pct == pytest.approx(0.0)
    assert nvda.tranches_taken == 1


# ---------------------------------------------------------------------------
# 8. drop is idempotent
# ---------------------------------------------------------------------------


def test_drop_idempotent(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    reg.drop("AAPL")
    reg.drop("AAPL")  # second drop must not raise
    assert reg.get("AAPL") is None


def test_drop_absent_is_noop(reg):
    reg.drop("GHOST")  # must not raise
    assert reg.get("GHOST") is None


# ---------------------------------------------------------------------------
# 9. all_symbols
# ---------------------------------------------------------------------------


def test_all_symbols_empty(reg):
    assert reg.all_symbols() == []


def test_all_symbols_lists_known(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    reg.record_open("NVDA", entry_price=400.0, stop_pct=0.10)
    assert set(reg.all_symbols()) == {"AAPL", "NVDA"}


def test_all_symbols_after_drop(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    reg.record_open("NVDA", entry_price=400.0, stop_pct=0.10)
    reg.drop("NVDA")
    assert reg.all_symbols() == ["AAPL"]


# ---------------------------------------------------------------------------
# 10. JSON mirror round-trip via read_watch_mirror
# ---------------------------------------------------------------------------


def test_read_watch_mirror_round_trip(tmp_path):
    mirror_path = tmp_path / "watch_registry.json"
    reg = WatchRegistry(db_path=tmp_path / "state.db", mirror_path=mirror_path)
    reg.record_open("TSLA", entry_price=200.0, stop_pct=0.08)
    reg.update_peak("TSLA", 0.12)
    reg.mark_tranche("TSLA")

    data = read_watch_mirror(mirror_path)
    assert "TSLA" in data
    assert data["TSLA"]["entry_price"] == pytest.approx(200.0)
    assert data["TSLA"]["peak_gain_pct"] == pytest.approx(0.12)
    assert data["TSLA"]["tranches_taken"] == 1


def test_read_watch_mirror_absent_returns_empty(tmp_path):
    assert read_watch_mirror(tmp_path / "nonexistent.json") == {}


def test_read_watch_mirror_corrupt_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert read_watch_mirror(p) == {}


# ---------------------------------------------------------------------------
# 11. WatchedPosition dataclass is frozen (immutable)
# ---------------------------------------------------------------------------


def test_watched_position_is_frozen(reg):
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    pos = reg.get("AAPL")
    with pytest.raises(Exception):  # frozen=True -> FrozenInstanceError on set
        pos.tranches_taken = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 12. Concurrent peak updates are monotonic (thread safety)
# ---------------------------------------------------------------------------


def test_concurrent_update_peak_monotonic(tmp_path):
    """Two threads racing to set the peak must leave the DB at the higher value."""
    db_path = tmp_path / "state.db"
    mirror_path = tmp_path / "m.json"
    reg = WatchRegistry(db_path=db_path, mirror_path=mirror_path)
    reg.record_open("X", entry_price=100.0, stop_pct=0.08)

    errors: list[Exception] = []
    peaks = [0.30, 0.10, 0.25, 0.05, 0.35, 0.20]

    def worker(val: float) -> None:
        try:
            reg.update_peak("X", val)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(v,)) for v in peaks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    pos = reg.get("X")
    assert pos is not None
    assert pos.peak_gain_pct == pytest.approx(max(peaks))


def test_mark_tranche_caps_at_2(tmp_path):
    """wave3-review fix: tranches_taken is CAPPED at 2 (the exit_strategy max).

    RED: revert the min(cur+1, 2) to cur+1 -> the 3rd mark_tranche pushes it to 3.
    """
    reg = WatchRegistry(db_path=tmp_path / "s.db", mirror_path=tmp_path / "m.json")
    reg.record_open("AAPL", entry_price=150.0, stop_pct=0.08)
    reg.mark_tranche("AAPL")  # 1
    reg.mark_tranche("AAPL")  # 2
    reg.mark_tranche("AAPL")  # would be 3 without the cap
    assert reg.get("AAPL").tranches_taken == 2, "tranches_taken must cap at 2 (exit_strategy max)"
