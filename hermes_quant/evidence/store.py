"""hermes_quant.evidence.store — parquet partitioned store + WAL SQLite index
(ADR-0033 D3+D7).

Layout::

    ~/.hermes/quant/evidence_store/year=YYYY/month=MM/kind=<kind>/part-NNNN.parquet
    ~/.hermes/quant/evidence_store/evidence_index.db   (WAL-mode SQLite)

Append-only. Updates emit a new record with ``supersedes=<old_uuid>``.
50GB local cap (overridable via ``HERMES_QUANT_EVIDENCE_DIR`` env var).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq

from hermes_quant.evidence.schema import EvidenceRecord

DEFAULT_EVIDENCE_DIR = Path.home() / ".hermes" / "quant" / "evidence_store"
DEFAULT_SIZE_CAP_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB


class EvidenceStoreImmutable(Exception):
    """Raised when attempting to overwrite an existing partition or row."""


class EvidenceStoreFull(Exception):
    """Raised when storage is at or above the size cap."""


class EvidenceStore:
    """Thread-safe parquet-partitioned evidence store.

    Construction precedence for the root directory:

    1. Explicit ``root`` argument
    2. ``HERMES_QUANT_EVIDENCE_DIR`` environment variable
    3. ``~/.hermes/quant/evidence_store/`` default
    """

    def __init__(
        self,
        root: Path | None = None,
        size_cap_bytes: int | None = None,
    ) -> None:
        env_root = os.environ.get("HERMES_QUANT_EVIDENCE_DIR")
        if root is not None:
            self.root = Path(root)
        elif env_root:
            self.root = Path(env_root)
        else:
            self.root = DEFAULT_EVIDENCE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        self.size_cap_bytes = (
            size_cap_bytes if size_cap_bytes is not None else DEFAULT_SIZE_CAP_BYTES
        )
        self._index_path = self.root / "evidence_index.db"
        self._init_index()
        self._lock = threading.Lock()

    def _init_index(self) -> None:
        with sqlite3.connect(self._index_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_index (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    symbol TEXT,
                    source TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    partition_path TEXT NOT NULL,
                    row_offset INTEGER NOT NULL,
                    supersedes TEXT,
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kind ON evidence_index(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON evidence_index(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_avail ON evidence_index(available_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_supersedes ON evidence_index(supersedes)")
            conn.commit()

    @contextmanager
    def _index_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._index_path, timeout=30.0)
        try:
            yield conn
        finally:
            conn.close()

    # ---- size accounting ----
    def total_size_bytes(self) -> int:
        total = 0
        for p in self.root.rglob("*.parquet"):
            total += p.stat().st_size
        blobs = self.root / "blobs"
        if blobs.exists():
            for p in blobs.glob("*"):
                if p.is_file():
                    total += p.stat().st_size
        return total

    def _check_size_cap(self) -> None:
        if self.total_size_bytes() >= self.size_cap_bytes:
            raise EvidenceStoreFull(
                f"evidence_store at or above cap "
                f"({self.total_size_bytes()} >= {self.size_cap_bytes}). "
                f"Run `hermes quant evidence prune` or relocate via "
                f"HERMES_QUANT_EVIDENCE_DIR."
            )

    # ---- partition layout ----
    def _partition_path(self, record: EvidenceRecord) -> Path:
        avail = record.available_at
        return (
            self.root
            / f"year={avail.year:04d}"
            / f"month={avail.month:02d}"
            / f"kind={record.kind}"
        )

    def _next_part_file(self, partition_dir: Path) -> Path:
        partition_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(partition_dir.glob("part-*.parquet"))
        if not existing:
            return partition_dir / "part-0001.parquet"
        last = int(existing[-1].stem.split("-")[1])
        return partition_dir / f"part-{last + 1:04d}.parquet"

    # ---- write ----
    def append(self, record: EvidenceRecord) -> None:
        """Append a record. Idempotent on duplicate id; the second write is a
        no-op. The deterministic ``id`` (per
        :func:`hermes_quant.evidence.schema.derive_evidence_id`) makes
        same-payload re-appends harmless."""
        with self._lock:
            with self._index_conn() as conn:
                row = conn.execute(
                    "SELECT id FROM evidence_index WHERE id = ?",
                    (str(record.id),),
                ).fetchone()
                if row is not None:
                    # Idempotent: same id is already stored.
                    return
            self._check_size_cap()
            partition_dir = self._partition_path(record)
            target = self._next_part_file(partition_dir)
            row_data = record.model_dump(mode="json")
            # Convert to PyArrow table.
            table = pa.Table.from_pylist([row_data])
            # Write to a temp file then atomic-rename for crash safety.
            tmp = target.with_suffix(".parquet.tmp")
            pq.write_table(table, tmp)
            os.replace(tmp, target)
            with self._index_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO evidence_index (id, kind, symbol, source, "
                    "published_at, available_at, partition_path, row_offset, "
                    "supersedes, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(record.id),
                        record.kind,
                        record.symbol,
                        record.source,
                        record.published_at.isoformat(),
                        record.available_at.isoformat(),
                        str(target.relative_to(self.root)),
                        0,
                        str(record.supersedes) if record.supersedes else None,
                        record.schema_version,
                    ),
                )
                conn.commit()

    def get(self, evidence_id: UUID | str) -> dict[str, Any] | None:
        """Lookup by id. Returns the row dict or None."""
        sid = str(evidence_id)
        with self._index_conn() as conn:
            row = conn.execute(
                "SELECT partition_path, row_offset FROM evidence_index WHERE id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                return None
            part_path = self.root / row[0]
            # Read the single parquet file directly (NOT as a Hive dataset).
            # The directory tree contains ``kind=<kind>`` segments which would
            # otherwise be auto-detected as partition columns and collide with
            # the ``kind`` column inside the file.
            pf = pq.ParquetFile(part_path)
            table = pf.read()
            data = table.to_pylist()
            if 0 <= row[1] < len(data):
                return data[row[1]]
            return None

    def supersedes_chain(self, evidence_id: UUID | str) -> list[dict[str, Any]]:
        """Walk back from ``evidence_id`` through ``supersedes`` pointers.

        Returns ``[most_recent, ..., original]``. Empty list if id not found.
        Loops are guarded against by a ``seen`` set.
        """
        chain: list[dict[str, Any]] = []
        cur: str | None = str(evidence_id)
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            row = self.get(cur)
            if row is None:
                break
            chain.append(row)
            nxt = row.get("supersedes")
            cur = str(nxt) if nxt else None
        return chain

    def overwrite_partition(self, partition_path: Path, table: pa.Table) -> None:
        """Forbidden: ALWAYS raises :class:`EvidenceStoreImmutable`.

        Provided so that upstream code that tries to overwrite gets a clear
        error rather than silently corrupting the store.
        """
        raise EvidenceStoreImmutable(
            f"overwrite_partition is forbidden (ADR-0033 D3 append-only). Path: {partition_path}"
        )
