"""ar123 — log_propagations must be idempotent under a cron re-run / overlapping run.

catalyst-ingest is scheduled every 30-60 min in-market. A retry after a transient
failure, or two overlapping invocations, re-fetch the SAME news window (Google News
`when:1d` returns the same items) -> identical classify -> identical propagate ->
BYTE-IDENTICAL propagation rows. The append-only consumers
(profitability.measure_profitability, graph_mining) count each row as an independent
observation, so duplicates inflate n_scored and prematurely cross MIN_SAMPLE, flipping a
relation's verdict on non-independent evidence — which gates the consumer-trend
confidence-weight RAISE + graph edge prune/flip.

The fix dedups at the WRITE boundary (log_propagations skips a row whose content key is
already on disk), so the consumer's "count rows" semantics is preserved (synthetic-volume
test fixtures that hand-write identical rows are unaffected) and a re-run is a no-op. A
genuinely-new headline for the same edge has a DIFFERENT asof (publication time) and is
preserved.
"""
from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.catalyst.profitability import MIN_SAMPLE, measure_profitability
from hermes_quant.catalyst.propagation import log_propagations


def _rows(n: int, asof: str = "2024-01-02T13:00:00+00:00") -> list[dict]:
    """n DISTINCT propagation rows (different symbols) at one asof."""
    return [
        {
            "symbol": f"SYM{i}",
            "source": "celsius",
            "relation": "brand_self",
            "effect_sign": 1,
            "weight": 0.6,
            "symbol_sign": 1,
            "catalyst_sign": 1,
            "asof": asof,
        }
        for i in range(n)
    ]


def _read(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_rerun_same_batch_is_noop(tmp_path):
    """A second log of the same batch (retry / overlapping run) writes ZERO rows."""
    p = tmp_path / "propagation-log.jsonl"
    batch = _rows(5)
    n1 = log_propagations(batch, path=p)
    n2 = log_propagations(batch, path=p)
    assert n1 == 5
    assert n2 == 0, "ar123: re-logging the same batch must be a no-op (idempotent)"
    assert len(_read(p)) == 5, "the log must hold exactly one copy of each row"


def test_duplicate_rows_within_one_batch_collapse(tmp_path):
    """A batch carrying the same row twice writes it once."""
    p = tmp_path / "propagation-log.jsonl"
    row = _rows(1)[0]
    n = log_propagations([row, dict(row), dict(row)], path=p)
    assert n == 1
    assert len(_read(p)) == 1


def test_new_headline_distinct_asof_is_preserved(tmp_path):
    """A genuine SECOND headline for the same edge (different publication time) is NOT
    collapsed — distinct asof = distinct observation."""
    p = tmp_path / "propagation-log.jsonl"
    log_propagations(_rows(1, asof="2024-01-02T13:00:00+00:00"), path=p)
    n = log_propagations(_rows(1, asof="2024-01-03T09:00:00+00:00"), path=p)
    assert n == 1, "a new-asof headline for the same edge must be logged"
    assert len(_read(p)) == 2


def test_rerun_does_not_inflate_n_scored_past_min_sample(tmp_path):
    """THE MONEY ASSERTION: 10 genuine observations (below MIN_SAMPLE=20). A duplicate
    re-run must NOT inflate n_scored to 20 and flip the verdict from INSUFFICIENT_SAMPLE
    to a real verdict on non-independent evidence.
    """
    p = tmp_path / "propagation-log.jsonl"
    # 10 DISTINCT real observations (distinct symbols, all brand_self, sign +1).
    batch = _rows(10)
    log_propagations(batch, path=p)
    log_propagations(batch, path=p)  # the duplicate re-run (the bug's trigger)

    # Every symbol's forward return is +5% (a hit for sign +1).
    fetcher = lambda sym, asof_date: 5.0  # noqa: E731
    stats = measure_profitability(fetcher, path=p)
    bs = stats["brand_self"]

    assert bs.n_scored == 10, (
        f"ar123: a duplicate ingest re-run must not inflate n_scored; got {bs.n_scored} "
        "(20 would mean the duplicates were counted as independent observations)"
    )
    assert bs.n_scored < MIN_SAMPLE
    assert bs.verdict == "INSUFFICIENT_SAMPLE", (
        "with only 10 genuine observations the verdict must stay INSUFFICIENT_SAMPLE, "
        f"not flip on duplicate-inflated evidence; got {bs.verdict}"
    )
