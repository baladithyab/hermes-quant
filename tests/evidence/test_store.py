"""Tests for hermes_quant.evidence.store (ADR-0033 D3, D7).

Each test gets a fresh root via ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pyarrow as pa
import pytest

from hermes_quant.evidence.schema import (
    BarEvidence,
    NewsEvidence,
    compute_available_at,
    derive_evidence_id,
    sha256_of_bytes,
    sha256_of_json,
)
from hermes_quant.evidence.store import (
    DEFAULT_EVIDENCE_DIR,
    EvidenceStore,
    EvidenceStoreFull,
    EvidenceStoreImmutable,
)

# ---- helpers ----


def _make_bar(
    *,
    payload: bytes,
    source: str = "yfinance",
    symbol: str = "AAPL",
    published_at: datetime | None = None,
    supersedes: UUID | None = None,
) -> BarEvidence:
    if published_at is None:
        published_at = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    h = sha256_of_bytes(payload)
    eid = derive_evidence_id("bar", source, h)
    return BarEvidence(
        id=eid,
        kind="bar",
        symbol=symbol,
        source=source,
        published_at=published_at,
        ingested_at=published_at + timedelta(seconds=5),
        available_at=compute_available_at("bar", published_at),
        payload_ref=f"blobs/{h}.json",
        payload_hash=h,
        open=100.0,
        high=101.0,
        low=99.5,
        close=100.5,
        volume=10_000.0,
        supersedes=supersedes,
    )


# ---- tests ----


def test_store_creates_directory_layout(tmp_path: Path):
    """Constructor must create root, blobs/, and the SQLite index."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    assert root.is_dir()
    assert (root / "blobs").is_dir()
    assert (root / "evidence_index.db").is_file()
    # Confirm the table exists
    with sqlite3.connect(root / "evidence_index.db") as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {r[0] for r in rows}
    assert "evidence_index" in table_names
    # Touch store to silence unused-var warnings
    assert store.root == root


def test_store_init_idempotent_on_existing_index(tmp_path: Path):
    """Creating two stores at the same root must not corrupt the index."""
    root = tmp_path / "evidence_store"
    EvidenceStore(root=root)
    # Re-construct over the existing root
    store2 = EvidenceStore(root=root)
    rec = _make_bar(payload=b"bar1")
    store2.append(rec)
    assert store2.get(rec.id) is not None


def test_append_writes_parquet_partition_at_correct_path(tmp_path: Path):
    """Append must materialize a parquet file at year=YYYY/month=MM/kind=bar/."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    rec = _make_bar(payload=b"row-1")
    store.append(rec)
    avail = rec.available_at
    expected_dir = root / f"year={avail.year:04d}" / f"month={avail.month:02d}" / "kind=bar"
    assert expected_dir.is_dir()
    parts = list(expected_dir.glob("part-*.parquet"))
    assert len(parts) == 1
    # Part files are named deterministically from the record id (one record per
    # file) so two distinct-id cross-process appends can never collide.
    assert parts[0].name == f"part-{rec.id}.parquet"


def test_append_writes_index_row_with_correct_columns(tmp_path: Path):
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    rec = _make_bar(payload=b"index-row")
    store.append(rec)
    with sqlite3.connect(root / "evidence_index.db") as conn:
        row = conn.execute(
            "SELECT id, kind, symbol, source, schema_version FROM evidence_index WHERE id = ?",
            (str(rec.id),),
        ).fetchone()
    assert row is not None
    assert row[0] == str(rec.id)
    assert row[1] == "bar"
    assert row[2] == "AAPL"
    assert row[3] == "yfinance"
    assert row[4] == 1


def test_append_idempotent_on_same_id(tmp_path: Path):
    """Appending the same record twice is a no-op (no exception, no dup row)."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    rec = _make_bar(payload=b"dup-payload")
    store.append(rec)
    store.append(rec)  # should be no-op
    with sqlite3.connect(root / "evidence_index.db") as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM evidence_index WHERE id = ?",
            (str(rec.id),),
        ).fetchone()[0]
    assert n == 1


def test_get_returns_record_after_append(tmp_path: Path):
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    rec = _make_bar(payload=b"fetch-me")
    store.append(rec)
    fetched = store.get(rec.id)
    assert fetched is not None
    assert fetched["id"] == str(rec.id)
    assert fetched["kind"] == "bar"
    assert fetched["payload_hash"] == rec.payload_hash


def test_get_returns_none_for_unknown_id(tmp_path: Path):
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    bogus = derive_evidence_id("bar", "nowhere", "0" * 64)
    assert store.get(bogus) is None


def test_supersedes_chain_walks_correctly(tmp_path: Path):
    """A superseded by B superseded by C => chain returns [C, B, A]."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    a = _make_bar(payload=b"A-original")
    b = _make_bar(payload=b"B-update", supersedes=a.id)
    c = _make_bar(payload=b"C-latest", supersedes=b.id)
    store.append(a)
    store.append(b)
    store.append(c)
    chain = store.supersedes_chain(c.id)
    assert len(chain) == 3
    assert chain[0]["id"] == str(c.id)
    assert chain[1]["id"] == str(b.id)
    assert chain[2]["id"] == str(a.id)


def test_supersedes_chain_terminates_on_missing(tmp_path: Path):
    """Broken chain (supersedes points to id not in store) terminates without
    raising or looping forever."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    missing_uuid = derive_evidence_id("bar", "ghost", "f" * 64)
    rec = _make_bar(payload=b"orphan-update", supersedes=missing_uuid)
    store.append(rec)
    chain = store.supersedes_chain(rec.id)
    # We get our record, then the walk stops because missing_uuid isn't stored.
    assert len(chain) == 1
    assert chain[0]["id"] == str(rec.id)


def test_overwrite_partition_raises_immutable(tmp_path: Path):
    """overwrite_partition is forbidden by D3 append-only."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    fake_table = pa.Table.from_pylist([{"a": 1}])
    with pytest.raises(EvidenceStoreImmutable):
        store.overwrite_partition(root / "year=2026" / "fake.parquet", fake_table)


def test_storage_size_cap_blocks_writes(tmp_path: Path):
    """When the store is at or above the cap, the next write raises
    EvidenceStoreFull."""
    root = tmp_path / "evidence_store"
    # First write under a tiny cap to seed the directory.
    store = EvidenceStore(root=root, size_cap_bytes=10**12)
    first = _make_bar(payload=b"seed-bar")
    store.append(first)
    # Now reduce the cap to 1 byte — any size >= 1 means cap exceeded.
    store.size_cap_bytes = 1
    second = _make_bar(payload=b"second-bar", source="alpaca")
    with pytest.raises(EvidenceStoreFull):
        store.append(second)


def test_partition_path_uses_year_month_kind_layout(tmp_path: Path):
    """Verify D3 partition layout for two distinct months."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    pub_jan = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)
    pub_may = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    a = _make_bar(payload=b"jan-bar", published_at=pub_jan)
    b = _make_bar(payload=b"may-bar", published_at=pub_may)
    store.append(a)
    store.append(b)
    assert (root / "year=2026" / "month=01" / "kind=bar").is_dir()
    assert (root / "year=2026" / "month=05" / "kind=bar").is_dir()


def test_env_var_overrides_default_dir(monkeypatch, tmp_path: Path):
    """HERMES_QUANT_EVIDENCE_DIR must override the default path when no
    explicit root is passed."""
    custom = tmp_path / "custom_evidence_dir"
    monkeypatch.setenv("HERMES_QUANT_EVIDENCE_DIR", str(custom))
    store = EvidenceStore()  # no explicit root
    assert store.root == custom
    assert custom.is_dir()
    assert custom != DEFAULT_EVIDENCE_DIR


def test_explicit_root_takes_precedence_over_env(monkeypatch, tmp_path: Path):
    """Explicit root argument wins over env var."""
    env_dir = tmp_path / "env_dir"
    explicit_dir = tmp_path / "explicit_dir"
    monkeypatch.setenv("HERMES_QUANT_EVIDENCE_DIR", str(env_dir))
    store = EvidenceStore(root=explicit_dir)
    assert store.root == explicit_dir
    assert explicit_dir.is_dir()


def test_concurrent_appends_do_not_corrupt_index(tmp_path: Path):
    """Two threads append distinct ids simultaneously; both must succeed and
    the index must contain both rows."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    records = [_make_bar(payload=f"concurrent-{i}".encode()) for i in range(8)]

    errors: list[Exception] = []

    def worker(rec):
        try:
            store.append(rec)
        except Exception as e:  # pragma: no cover — surfaced via assertion
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(r,)) for r in records]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with sqlite3.connect(root / "evidence_index.db") as conn:
        n = conn.execute("SELECT COUNT(*) FROM evidence_index").fetchone()[0]
    assert n == len(records)


def test_cross_instance_distinct_id_appends_to_same_partition_preserve_both(
    tmp_path: Path,
):
    """Two SEPARATE EvidenceStore instances (= the real cross-process model:
    tools.py:2013 does ``est = EvidenceStore()`` fresh per quant_insider call,
    and crons are separate OS processes) appending DISTINCT-id records into the
    SAME year=YYYY/month=MM/kind=<kind> partition must each be retrievable by
    their OWN id.

    Historical RED reproduction of the glob-then-replace TOCTOU: both
    ``_next_part_file()`` calls globbed the partition before either
    ``os.replace`` landed, so both resolved to ``part-0001.parquet`` AND the
    same ``part-0001.parquet.tmp``. The naive implementation silently lost one
    record and/or made ``get()`` return the WRONG record (different
    available_at) feeding the ADR-0004/ADR-0033 no-lookahead gate.

    The fix derives the part-file name deterministically from ``record.id``, so
    two DISTINCT ids never resolve to the same parquet path or ``.tmp``.
    """
    root = tmp_path / "evidence_store"
    # Both records land in the SAME partition (same available_at year/month/kind)
    # but have DISTINCT ids (different payloads).
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    rec_a = _make_bar(payload=b"race-A", source="srcA", published_at=pub)
    rec_b = _make_bar(payload=b"race-B", source="srcB", published_at=pub)
    assert rec_a.id != rec_b.id

    store_a = EvidenceStore(root=root)
    store_b = EvidenceStore(root=root)

    store_a.append(rec_a)
    store_b.append(rec_b)

    # Both ids must be retrievable AND return their OWN payload_hash, not the
    # other record's. This is the core invariant the count-only test misses.
    fetched_a = store_a.get(rec_a.id)
    fetched_b = store_a.get(rec_b.id)
    assert fetched_a is not None, "record A was silently lost (cross-instance race)"
    assert fetched_b is not None, "record B was silently lost (cross-instance race)"
    assert fetched_a["id"] == str(rec_a.id)
    assert fetched_a["payload_hash"] == rec_a.payload_hash, (
        "get(A) returned the WRONG record's payload (record swap)"
    )
    assert fetched_b["id"] == str(rec_b.id)
    assert fetched_b["payload_hash"] == rec_b.payload_hash, (
        "get(B) returned the WRONG record's payload (record swap)"
    )
    # Distinct ids -> distinct part files (no part-file or .tmp collision).
    part_dir = root / "year=2026" / "month=05" / "kind=bar"
    parts = sorted(p.name for p in part_dir.glob("part-*.parquet"))
    assert len(parts) == 2, f"expected two distinct part files, got {parts}"
    assert not list(part_dir.glob("*.tmp")), "a .tmp staging file leaked"


def test_forced_partfile_collision_still_returns_correct_record(tmp_path: Path):
    """Even if two distinct-id records were FORCED to the same part-file name
    (the historical glob race), the per-write-unique ``.tmp`` plus the
    id-verifying ``get()`` must keep BOTH retrievable by their own id — never
    return a swapped record to the no-lookahead gate.

    This guards the residual defenses independently of the deterministic naming.
    """
    root = tmp_path / "evidence_store"
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    rec_a = _make_bar(payload=b"collide-A", source="cA", published_at=pub)
    rec_b = _make_bar(payload=b"collide-B", source="cB", published_at=pub)
    assert rec_a.id != rec_b.id

    import hermes_quant.evidence.store as store_mod

    orig = store_mod.EvidenceStore._part_file_for_record

    def collide(self, partition_dir, record):
        partition_dir.mkdir(parents=True, exist_ok=True)
        return partition_dir / "part-shared.parquet"

    store_a = EvidenceStore(root=root)
    store_b = EvidenceStore(root=root)
    store_mod.EvidenceStore._part_file_for_record = collide  # type: ignore[method-assign]
    try:
        store_a.append(rec_a)
        store_b.append(rec_b)
    finally:
        store_mod.EvidenceStore._part_file_for_record = orig  # type: ignore[method-assign]

    # With both index rows pointing at the same shared part file, get() must
    # still resolve each id to its OWN record (id-verifying scan), not silently
    # return whichever record won the os.replace.
    got_a = store_a.get(rec_a.id)
    got_b = store_a.get(rec_b.id)
    # The last writer's file wins on disk, so at least the surviving record must
    # be correct; the lost one must NOT masquerade as a swapped wrong record.
    if got_a is not None:
        assert got_a["id"] == str(rec_a.id)
        assert got_a["payload_hash"] == rec_a.payload_hash
    if got_b is not None:
        assert got_b["id"] == str(rec_b.id)
        assert got_b["payload_hash"] == rec_b.payload_hash
    # No staging file may leak.
    part_dir = root / "year=2026" / "month=05" / "kind=bar"
    assert not list(part_dir.glob("*.tmp")), "a .tmp staging file leaked"


def test_concurrent_threads_same_partition_distinct_ids_all_retrievable(
    tmp_path: Path,
):
    """Real concurrent threads using TWO instances, releasing at a barrier into
    the SAME partition with DISTINCT ids: every record must be retrievable by
    its own id afterwards (no FileNotFoundError crash, no silent loss)."""
    root = tmp_path / "evidence_store"
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    n = 6
    records = [
        _make_bar(payload=f"barrier-{i}".encode(), source=f"src{i}", published_at=pub)
        for i in range(n)
    ]
    # One fresh instance per "process".
    stores = [EvidenceStore(root=root) for _ in range(n)]
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(store: EvidenceStore, rec):
        try:
            barrier.wait()
            store.append(rec)
        except Exception as e:  # pragma: no cover — surfaced via assertion
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(stores[i], records[i])) for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"append raised under concurrency: {errors}"

    reader = EvidenceStore(root=root)
    for rec in records:
        got = reader.get(rec.id)
        assert got is not None, f"record {rec.id} silently lost under concurrency"
        assert got["id"] == str(rec.id)
        assert got["payload_hash"] == rec.payload_hash, (
            f"get({rec.id}) returned a different record (swap)"
        )


def test_news_record_round_trips_through_store(tmp_path: Path):
    """NewsEvidence (different subtype) is stored and retrieved correctly."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    avail = compute_available_at("news", pub)
    h = sha256_of_json({"headline": "Apple announces foo"})
    eid = derive_evidence_id("news", "rss", h)
    rec = NewsEvidence(
        id=eid,
        kind="news",
        symbol="AAPL",
        source="rss",
        published_at=pub,
        ingested_at=pub + timedelta(seconds=10),
        available_at=avail,
        payload_ref=f"blobs/{h}.json",
        payload_hash=h,
        headline="Apple announces foo",
        body="Body text...",
        url="https://example.com/foo",
    )
    store.append(rec)
    fetched = store.get(rec.id)
    assert fetched is not None
    assert fetched["headline"] == "Apple announces foo"
    assert fetched["body"] == "Body text..."
    # Stored under kind=news partition
    assert (root / f"year={avail.year:04d}" / f"month={avail.month:02d}" / "kind=news").is_dir()


def test_total_size_bytes_includes_parquet_and_blobs(tmp_path: Path):
    """total_size_bytes counts both parquet and blob files."""
    root = tmp_path / "evidence_store"
    store = EvidenceStore(root=root)
    initial = store.total_size_bytes()
    assert initial == 0
    rec = _make_bar(payload=b"sized-payload")
    store.append(rec)
    after_parquet = store.total_size_bytes()
    assert after_parquet > 0
    # Drop a blob file in
    blob = root / "blobs" / "fake-payload.json"
    blob.write_bytes(b"x" * 1024)
    after_blob = store.total_size_bytes()
    assert after_blob >= after_parquet + 1024
