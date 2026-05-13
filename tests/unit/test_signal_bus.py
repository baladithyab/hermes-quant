"""Unit tests for hermes_quant.daemon.signal_bus — flock-based JSONL atomicity.

Anchor: synthesis-v2 §P0-B. Verifies:
- Concurrent producers don't corrupt each other's records.
- 16384-byte cap enforced.
- Schema/path symmetry between signals.jsonl and executions.jsonl.
- Read-tail tolerates partial trailing line.
- Filter helper respects schema_version + asset.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

import pytest

from hermes_quant.daemon.signal_bus import (
    RECORD_BYTE_CAP,
    append_locked,
    emit_execution_record,
    emit_signal_record,
    iter_jsonl_follow,
    read_jsonl_tail,
    read_signals_for_asset,
)
from hermes_quant.protocol import SignalTooLarge


@pytest.fixture()
def tmp_bus(tmp_path: Path) -> Path:
    return tmp_path / "test_bus.jsonl"


def _record(seq: int = 0) -> dict:
    return {
        "schema_version": 1,
        "id": f"sig-test-{seq:04d}",
        "asof": "2026-05-13T00:00:00Z",
        "asset": "BTC/USDT",
        "exchange": "binance",
        "timeframe": "1h",
        "direction": 1,
        "magnitude": 0.012,
        "confidence": 0.65,
        "horizon": "4h",
        "target_position_pct": 0.10,
        "reason": "test",
        "halt": False,
    }


class TestAppendLocked:
    def test_acquires_and_releases(self, tmp_bus: Path):
        """Basic write succeeds, fd is closed after."""
        with append_locked(tmp_bus) as fd:
            os.write(fd, b'{"a":1}\n')
        assert tmp_bus.exists()
        assert tmp_bus.read_bytes() == b'{"a":1}\n'

    def test_multiple_sequential_writes(self, tmp_bus: Path):
        for i in range(5):
            with append_locked(tmp_bus) as fd:
                os.write(fd, f'{{"i":{i}}}\n'.encode())
        lines = tmp_bus.read_text().strip().split("\n")
        assert len(lines) == 5
        assert [json.loads(l)["i"] for l in lines] == [0, 1, 2, 3, 4]

    def test_lock_released_after_exception(self, tmp_bus: Path):
        """An exception inside the context must still release the lock."""
        try:
            with append_locked(tmp_bus) as fd:
                os.write(fd, b'{"a":1}\n')
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # Subsequent acquires should not block
        with append_locked(tmp_bus) as fd:
            os.write(fd, b'{"a":2}\n')
        lines = tmp_bus.read_text().strip().split("\n")
        assert len(lines) == 2


def _producer_worker(args: tuple) -> int:
    """Module-level for pickling. Writes N records; returns count written."""
    bus_path_str, n_records, worker_id = args
    bus_path = Path(bus_path_str)
    # Use the public API from a fresh process (mirrors the freqtrade-strategy
    # case where the consumer is a separate process).
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from hermes_quant.daemon.signal_bus import emit_signal_record  # noqa

    for i in range(n_records):
        rec = _record(seq=worker_id * 1000 + i)
        rec["worker_id"] = worker_id
        emit_signal_record(rec, path=bus_path)
    return n_records


class TestConcurrentFlock:
    """Spawn multiple processes hammering the bus; verify no corruption."""

    @pytest.mark.parametrize("n_workers,n_records", [(3, 50), (5, 30)])
    def test_concurrent_producers_no_corruption(
        self, tmp_bus: Path, n_workers: int, n_records: int
    ):
        """The synthesis-v2 §P0-B canonical test: concurrent producers
        across multiple PROCESSES (not threads) producing well-formed lines.

        Threads would share an FD and serialize via Python's GIL anyway —
        only multi-process tests prove flock works.
        """
        args = [(str(tmp_bus), n_records, w) for w in range(n_workers)]
        with multiprocessing.Pool(n_workers) as pool:
            results = pool.map(_producer_worker, args)

        assert sum(results) == n_workers * n_records

        # Verify the bus has exactly n_workers * n_records well-formed lines
        lines = tmp_bus.read_text().splitlines()
        assert len(lines) == n_workers * n_records, (
            f"expected {n_workers * n_records} lines, got {len(lines)}"
        )

        # Each line must be valid JSON
        records = []
        for line in lines:
            assert line.strip(), "blank line in bus"
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                pytest.fail(f"corrupted line: {line[:100]!r} ({e})")

        # No record was lost — verify by worker_id histogram
        per_worker: dict[int, int] = {}
        for r in records:
            per_worker[r["worker_id"]] = per_worker.get(r["worker_id"], 0) + 1
        for w in range(n_workers):
            assert per_worker[w] == n_records, (
                f"worker {w} wrote {per_worker.get(w, 0)} records, expected {n_records}"
            )


class TestEmitSignalRecord:
    def test_basic_emit(self, tmp_bus: Path):
        emit_signal_record(_record(), path=tmp_bus)
        assert tmp_bus.exists()
        line = tmp_bus.read_text().strip()
        rec = json.loads(line)
        assert rec["asset"] == "BTC/USDT"
        assert rec["schema_version"] == 1

    def test_size_cap_enforced(self, tmp_bus: Path):
        """Records over RECORD_BYTE_CAP raise SignalTooLarge."""
        big = _record()
        big["rationale"] = "x" * (RECORD_BYTE_CAP + 100)
        with pytest.raises(SignalTooLarge):
            emit_signal_record(big, path=tmp_bus)
        # Bus file may be created (open with O_CREAT) but should be empty
        assert not tmp_bus.exists() or tmp_bus.stat().st_size == 0

    def test_creates_parent_dir(self, tmp_path: Path):
        """Missing parent directory is created on first emit."""
        nested_path = tmp_path / "deep" / "nested" / "bus.jsonl"
        emit_signal_record(_record(), path=nested_path)
        assert nested_path.exists()

    def test_executions_bus_symmetry(self, tmp_bus: Path):
        """emit_execution_record uses the same protocol as emit_signal_record."""
        rec = {"schema_version": 1, "exec_id": "exec-1", "asset": "BTC/USDT"}
        emit_execution_record(rec, path=tmp_bus)
        loaded = json.loads(tmp_bus.read_text().strip())
        assert loaded["exec_id"] == "exec-1"


class TestReadJsonlTail:
    def test_empty_file(self, tmp_bus: Path):
        assert read_jsonl_tail(tmp_bus, n=10) == []

    def test_nonexistent_file(self, tmp_path: Path):
        assert read_jsonl_tail(tmp_path / "nope.jsonl", n=10) == []

    def test_returns_last_n(self, tmp_bus: Path):
        for i in range(50):
            r = _record(i)
            r["seq"] = i
            emit_signal_record(r, path=tmp_bus)
        last_5 = read_jsonl_tail(tmp_bus, n=5)
        assert len(last_5) == 5
        assert [r["seq"] for r in last_5] == [45, 46, 47, 48, 49]

    def test_tolerates_partial_trailing_line(self, tmp_bus: Path):
        """A reader hitting the bus mid-write may see a partial last line."""
        emit_signal_record(_record(0), path=tmp_bus)
        emit_signal_record(_record(1), path=tmp_bus)
        # Append a partial line (simulating a writer that crashed mid-write)
        with open(tmp_bus, "ab") as f:
            f.write(b'{"asset":"BTC/USDT","incomp')
        records = read_jsonl_tail(tmp_bus, n=10)
        # We get 2 well-formed records; the partial line is skipped
        assert len(records) == 2

    def test_partial_first_line_skipped(self, tmp_bus: Path):
        """When chunk_size truncates the start of the file, drop the partial first line."""
        # Write a few large-ish records to ensure size > max_chunk
        for i in range(20):
            r = _record(i)
            r["seq"] = i
            r["pad"] = "x" * 200
            emit_signal_record(r, path=tmp_bus)
        # Read with a tiny max_chunk to force truncation
        records = read_jsonl_tail(tmp_bus, n=100, max_chunk=500)
        # All returned records are well-formed
        for r in records:
            assert "seq" in r
            assert isinstance(r["seq"], int)


class TestReadSignalsForAsset:
    def test_filters_by_asset(self, tmp_bus: Path):
        for asset in ["BTC/USDT", "ETH/USDT", "BTC/USDT", "AAPL"]:
            r = _record()
            r["asset"] = asset
            emit_signal_record(r, path=tmp_bus)
        btc = read_signals_for_asset("BTC/USDT", n=10, path=tmp_bus)
        assert len(btc) == 2
        assert all(r["asset"] == "BTC/USDT" for r in btc)

    def test_filters_by_schema_version(self, tmp_bus: Path):
        r1 = _record()
        r1["schema_version"] = 1
        r2 = _record()
        r2["schema_version"] = 2  # future version
        emit_signal_record(r1, path=tmp_bus)
        emit_signal_record(r2, path=tmp_bus)
        # Default schema_version=1 returns only the v1 record
        out = read_signals_for_asset("BTC/USDT", n=10, path=tmp_bus, schema_version=1)
        assert len(out) == 1
        assert out[0]["schema_version"] == 1


class TestIterJsonlFollow:
    """Quick smoke — full follow semantics tested in integration.

    iter_jsonl_follow is an infinite poll-loop generator; we exercise the
    seek-to-end behavior synchronously by writing first, advancing the
    generator past existing records, then writing more.
    """

    @pytest.mark.timeout(10)
    def test_follow_yields_new_records_after_start(self, tmp_bus: Path):
        # Write existing records (these should NOT appear via follow — it seeks to end).
        emit_signal_record(_record(0), path=tmp_bus)
        emit_signal_record(_record(1), path=tmp_bus)

        gen = iter_jsonl_follow(tmp_bus, poll_seconds=0.05)
        # Spawn a thread to write new records after the generator constructs
        import threading
        import time

        def writer():
            time.sleep(0.2)  # give generator time to seek-to-end
            emit_signal_record(_record(99), path=tmp_bus)

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        rec = next(gen)
        assert rec["id"] == "sig-test-0099"
        t.join(timeout=2)
