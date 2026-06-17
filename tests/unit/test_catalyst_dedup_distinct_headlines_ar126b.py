"""ar126b — the ar123 dedup must NOT collapse two GENUINELY-DISTINCT headlines.

ar123 made log_propagations idempotent by deduping on a content key. The wave-21 review
RED-proved the key lacked a per-headline discriminator, so two distinct headlines for the
SAME edge published in the SAME second (identical asof) collapsed to one row — under-
counting n_scored (fail-CLOSED, the opposite of the over-count ar123 fixed). Fix: stamp a
headline_id (hash of the news link/title) in synthesize_packets and include it in the key.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import load_graph, log_propagations
from hermes_quant.catalyst.synthesize import synthesize_packets


def _read(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _distinct_headlines_same_edge_same_second():
    g, a = load_graph()
    surf = next(iter(a.keys()))  # an alias surface that extract_entities recognizes
    ts = datetime(2024, 1, 2, 13, 0, 0, tzinfo=timezone.utc)
    it1 = CatalystItem(title=f"{surf} surges on revenue beat", published_at=ts, source="news", link="http://x/1")
    it2 = CatalystItem(title=f"{surf} jumps after analyst upgrade", published_at=ts, source="news", link="http://x/2")
    return g, a, [it1, it2]


def test_distinct_headlines_same_edge_same_second_not_collapsed():
    g, a, items = _distinct_headlines_same_edge_same_second()
    p = Path(tempfile.mkdtemp()) / "propagation-log.jsonl"
    log: list[dict] = []
    synthesize_packets(items, graph=g, aliases=a, propagation_log=log, stamp_log_asof=True)
    assert log, "the two headlines must produce propagation rows"
    # Each row carries a headline_id; the two headlines have DISTINCT ids.
    hids = {r.get("headline_id") for r in log}
    assert len(hids) == 2 and None not in hids, (
        f"ar126b: distinct headlines must carry distinct headline_id; got {hids}"
    )
    n = log_propagations(log, path=p)
    assert n == len(log), (
        "ar126b: two distinct headlines for the same edge at the same second must NOT be "
        f"collapsed by the dedup; produced {len(log)} rows, wrote {n}"
    )


def test_true_reingest_of_same_items_is_still_idempotent():
    """The ar123 guarantee must HOLD: re-ingesting the SAME items writes nothing."""
    g, a, items = _distinct_headlines_same_edge_same_second()
    p = Path(tempfile.mkdtemp()) / "propagation-log.jsonl"
    log1: list[dict] = []
    synthesize_packets(items, graph=g, aliases=a, propagation_log=log1, stamp_log_asof=True)
    n1 = log_propagations(log1, path=p)
    log2: list[dict] = []
    synthesize_packets(items, graph=g, aliases=a, propagation_log=log2, stamp_log_asof=True)
    n2 = log_propagations(log2, path=p)
    assert n1 > 0 and n2 == 0, (
        f"ar126b: a true re-ingest of the same items must stay idempotent (ar123); "
        f"first={n1}, rerun={n2}"
    )
    assert len(_read(p)) == n1
