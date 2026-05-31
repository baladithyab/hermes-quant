"""hermes_quant.daemon.signal_bus — Append-only JSONL bus with flock atomicity.

Per synthesis-v2 §P0-B: PIPE_BUF applies to pipes/FIFOs, NOT regular files.
POSIX does NOT guarantee atomic appends to regular files for any size. We use
fcntl.flock() to serialize writes from concurrent producers.

Two buses with identical protocol:
  - signals.jsonl  — daemon → consumer (freqtrade, etc.) signal stream
  - executions.jsonl — consumer → daemon execution back-channel

Both producers in BOTH directions use append_locked(). The freqtrade
strategy is a separate process; it must also acquire the flock.

Records are JSON-serialized with sort_keys=True for deterministic byte
layouts (helps debugging when grep-ing the bus). Records exceeding our
chosen 16384-byte cap raise SignalTooLarge — we cap rationale, components,
metadata at upstream construction time.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_quant.protocol import SignalTooLarge

# Default bus paths. Test code overrides via env or explicit path arg.
QUANT_HOME = Path.home() / ".hermes" / "quant"
SIGNAL_BUS_PATH = QUANT_HOME / "signals.jsonl"
EXECUTION_BUS_PATH = QUANT_HOME / "executions.jsonl"

# Per-record byte cap. Records exceeding this raise SignalTooLarge.
# Rationale + components + metadata are capped upstream so this is a
# defensive backstop, not a primary limit.
RECORD_BYTE_CAP = 16384


@contextmanager
def append_locked(path: Path) -> Iterator[int]:
    """Acquire exclusive flock on the bus file before yielding an FD for append.

    On exit (normal or exception):
      1. fsync the FD (durability)
      2. release the lock
      3. close the FD

    The fsync happens BEFORE lock release so a concurrent reader after the
    lock release sees a fully-flushed write. We tolerate the small latency
    cost (~ms on SSD); the alternative (no fsync) means a kernel panic
    between write and lock-release can lose the record entirely, which
    matters for emergency-stop signals.

    Args:
        path: bus file path. Parent directory must exist.

    Yields:
        Open file descriptor in append mode with an exclusive flock.

    Raises:
        OSError: if open or flock fails.

    Note: on macOS, fcntl.flock uses BSD flock semantics (advisory, per-file).
    On Linux, it's also advisory but kernel-mediated. Both work for the
    intra-host coordination we need (daemon ↔ freqtrade strategy on same box).
    On NFS, flock is advisory and may not coordinate across hosts; the
    user's `~/.hermes/quant/` is local home so this is not a concern in
    practice. AGENTS.md documents the NFS caveat.
    """
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # block until acquired
        yield fd
    finally:
        try:
            os.fsync(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _serialize_record(record: dict[str, Any]) -> bytes:
    """JSON-serialize a record with sort_keys for deterministic layout.

    Raises:
        SignalTooLarge: if encoded line exceeds RECORD_BYTE_CAP.
    """
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > RECORD_BYTE_CAP:
        raise SignalTooLarge(
            f"encoded {len(encoded)} bytes (limit {RECORD_BYTE_CAP}). "
            f"Cap rationale, components, or metadata upstream."
        )
    return encoded


def emit_signal_record(record: dict[str, Any], path: Path = SIGNAL_BUS_PATH) -> None:
    """Append a single record to the signal bus atomically.

    Per synthesis-v2 §P0-B: uses flock for serialization, not PIPE_BUF.

    Args:
        record: dict to serialize. Must contain at least `schema_version`,
            `id`, `asof`, `asset`. Caller is responsible for routing fields
            (timeframe, direction, magnitude, confidence, etc.).
        path: target bus path. Defaults to ~/.hermes/quant/signals.jsonl.

    Raises:
        SignalTooLarge: if the encoded record exceeds RECORD_BYTE_CAP.
        OSError: on filesystem failures.
    """
    encoded = _serialize_record(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with append_locked(path) as fd:
        n = os.write(fd, encoded)
        if n != len(encoded):
            raise OSError(f"short write to {path}: {n}/{len(encoded)} bytes")


def emit_execution_record(record: dict[str, Any], path: Path = EXECUTION_BUS_PATH) -> None:
    """Append a single execution record to the executions bus atomically.

    Same protocol as emit_signal_record. Producers (freqtrade strategy,
    emergency-stop CLI) all use this helper to avoid concurrent-write
    corruption.

    Per synthesis-v2 §P0-B: executions.jsonl needs the same atomicity
    protection — flagged by both Gemini AND GPT-5.5 in v2 review.
    """
    encoded = _serialize_record(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with append_locked(path) as fd:
        n = os.write(fd, encoded)
        if n != len(encoded):
            raise OSError(f"short write to {path}: {n}/{len(encoded)} bytes")


def read_jsonl_tail(path: Path, n: int, *, max_chunk: int = 1_048_576) -> list[dict]:
    """Read the last N JSONL records from a bus file.

    Tolerates partial trailing line (mid-write reader sees the in-progress
    record as malformed JSON; we skip it rather than crash). Memory-budget
    capped at max_chunk bytes from the tail.

    Args:
        path: bus file path.
        n: max records to return (most recent N).
        max_chunk: max bytes to read from the tail (default 1 MB).

    Returns:
        List of decoded records, oldest-first within the returned slice.
        Empty list if the bus doesn't exist.
    """
    if not path.exists():
        return []
    size = path.stat().st_size
    if size == 0:
        return []
    chunk_size = min(size, max_chunk)
    with open(path, "rb") as f:
        f.seek(max(0, size - chunk_size))
        chunk = f.read()

    # Drop any leading partial line (we may have started mid-record).
    if size > chunk_size:
        first_nl = chunk.find(b"\n")
        if first_nl < 0:
            return []
        chunk = chunk[first_nl + 1 :]

    records: list[dict] = []
    for line in chunk.split(b"\n"):
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Mid-write partial OR corrupted record; skip.
            continue
        if not isinstance(rec, dict):
            # Valid JSON but not an object (corrupt/partial append); skip.
            continue
        records.append(rec)
    return records[-n:]


def read_signals_for_asset(
    asset: str,
    *,
    n: int = 100,
    path: Path = SIGNAL_BUS_PATH,
    schema_version: int = 1,
) -> list[dict]:
    """Filter signals.jsonl tail to records matching `asset` and schema_version.

    Used by the freqtrade strategy and quant_show_signals tool.

    Args:
        asset: asset symbol (e.g., "BTC/USDT", "AAPL").
        n: max matching records to return.
        path: bus path.
        schema_version: only return records with this schema_version.
            Per ADR-0008, consumers reject unknown major versions.

    Returns:
        List of records most-recent-first.
    """
    # Read a larger tail and filter; for typical bus rates this is fine.
    raw = read_jsonl_tail(path, n=n * 10, max_chunk=4_194_304)  # 4 MB tail
    matching = [
        r for r in raw if r.get("asset") == asset and r.get("schema_version") == schema_version
    ]
    return matching[-n:]


def iter_jsonl_follow(path: Path, *, poll_seconds: float = 1.0) -> Iterator[dict]:
    """Tail-follow a JSONL bus, yielding new records as they're written.

    Used by `hermes quant signals --follow` and the freqtrade strategy's
    polling loop. Implements a simple poll loop; v0.2 may add inotify.

    Args:
        path: bus path.
        poll_seconds: sleep interval between polls.

    Yields:
        Decoded records. Tolerates partial trailing line.

    Notes:
        - Caller must handle KeyboardInterrupt to exit.
        - If the file is rotated (renamed + recreated), the iterator does
          NOT detect the rotation and continues reading the original inode.
          Bus files are never rotated in v0.1; document for v0.2.
    """
    import time

    # Wait for file to exist
    while not path.exists():
        time.sleep(poll_seconds)

    with open(path, "rb") as f:
        # Seek to end
        f.seek(0, os.SEEK_END)
        buffer = b""
        while True:
            chunk = f.read()
            if not chunk:
                time.sleep(poll_seconds)
                continue
            buffer += chunk
            # Yield complete lines
            while True:
                nl = buffer.find(b"\n")
                if nl < 0:
                    break
                line, buffer = buffer[:nl], buffer[nl + 1 :]
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # Skip malformed records
                    continue
                if not isinstance(rec, dict):
                    # Valid JSON but not an object (corrupt/partial append); skip.
                    continue
                yield rec
