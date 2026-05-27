"""tests/memory/test_retriever.py — Layer 3 retriever tests (ADR-0042).

Tests cover: top-k correctness, BM25 ranking sanity, sector-cache-missing
graceful path.  NO LLM calls — all reflections injected via fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.memory.retriever import (
    AggregateStats,
    PastContext,
    ResolvedDecision,
    get_past_context,
)

UTC_BASE = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_reflection(path: Path, **kwargs) -> None:
    """Write a single reflection row to a JSONL file."""
    defaults = {
        "schema_version": 1,
        "reflection_id": "ref_default",
        "decision_id": "dec_default",
        "asof_resolution": "2026-06-01T10:00:00+00:00",
        "tau_observable": (UTC_BASE - timedelta(hours=1)).isoformat(),
        "ticker": "AAPL",
        "raw_return": 0.05,
        "alpha_return": 0.02,
        "benchmark": "SPY",
        "holding_days": 10,
        "outcome_quality": 4,
        "reflection_text": "Strong quarter beat expectations.",
        "lesson_category": "unknown",
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:abcdef",
        "rating": "Buy",
    }
    row = {**defaults, **kwargs}
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rpath(tmp_path: Path) -> Path:
    return tmp_path / "reflections.jsonl"


@pytest.fixture
def dpath(tmp_path: Path) -> Path:
    return tmp_path / "decisions.jsonl"


@pytest.fixture
def spath(tmp_path: Path) -> Path:
    return tmp_path / "sector-beta-cache.json"


def _ctx(ticker: str, asof: datetime, rpath: Path, dpath: Path, spath: Path | None) -> PastContext:
    return get_past_context(
        ticker,
        asof,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath or Path("/nonexistent/sector-cache.json"),
    )


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_reflections_returns_empty_context(rpath, dpath, spath) -> None:
    ctx = _ctx("AAPL", UTC_BASE, rpath, dpath, spath)
    assert ctx.same_ticker == []
    assert ctx.cross_ticker == []
    assert ctx.cross_sector == []
    assert ctx.aggregate_stats.n_resolved == 0


# ---------------------------------------------------------------------------
# Top-k correctness: same_ticker
# ---------------------------------------------------------------------------


def test_same_ticker_returns_correct_k(rpath, dpath, spath) -> None:
    for i in range(7):
        _write_reflection(
            rpath,
            reflection_id=f"ref_{i:03d}",
            decision_id=f"dec_{i:03d}",
            ticker="NVDA",
            asof_resolution=f"2026-0{i+1}-01T10:00:00+00:00",
            tau_observable=(UTC_BASE - timedelta(days=30 - i)).isoformat(),
        )
    ctx = get_past_context(
        "NVDA", UTC_BASE,
        k_same_ticker=5,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath,
    )
    assert len(ctx.same_ticker) == 5


def test_same_ticker_oldest_first(rpath, dpath, spath) -> None:
    for i in range(3):
        _write_reflection(
            rpath,
            reflection_id=f"ref_{i:03d}",
            ticker="NVDA",
            asof_resolution=f"2026-0{i+1}-10T10:00:00+00:00",
            tau_observable=(UTC_BASE - timedelta(days=90 - i * 30)).isoformat(),
        )
    ctx = get_past_context(
        "NVDA", UTC_BASE,
        k_same_ticker=5,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath,
    )
    dates = [r.asof for r in ctx.same_ticker]
    assert dates == sorted(dates), "same_ticker should be ordered oldest-first"


def test_same_ticker_excludes_other_tickers(rpath, dpath, spath) -> None:
    _write_reflection(rpath, ticker="NVDA", reflection_id="ref_nvda")
    _write_reflection(rpath, ticker="TSLA", reflection_id="ref_tsla")

    ctx = _ctx("NVDA", UTC_BASE, rpath, dpath, spath)
    assert all(r.ticker == "NVDA" for r in ctx.same_ticker)
    assert len(ctx.same_ticker) == 1


# ---------------------------------------------------------------------------
# Top-k correctness: cross_ticker
# ---------------------------------------------------------------------------


def test_cross_ticker_excludes_same_ticker(rpath, dpath, spath) -> None:
    _write_reflection(rpath, ticker="AAPL", reflection_id="ref_aapl",
                      reflection_text="App store growth thesis.")
    _write_reflection(rpath, ticker="MSFT", reflection_id="ref_msft",
                      reflection_text="Cloud Azure expansion.")

    ctx = _ctx("AAPL", UTC_BASE, rpath, dpath, spath)
    assert all(r.ticker != "AAPL" for r in ctx.cross_ticker)


def test_cross_ticker_k_limit(rpath, dpath, spath) -> None:
    for i in range(6):
        _write_reflection(rpath, ticker=f"TK{i}", reflection_id=f"ref_tk{i}",
                          reflection_text="Growth momentum in tech sector.")
    ctx = get_past_context(
        "AAPL", UTC_BASE,
        k_cross_ticker=3,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath,
    )
    assert len(ctx.cross_ticker) <= 3


# ---------------------------------------------------------------------------
# BM25 ranking sanity
# ---------------------------------------------------------------------------


def test_bm25_ranking_more_relevant_ranks_higher(rpath, dpath, spath) -> None:
    """A reflection whose text contains more query terms should rank higher."""
    _write_reflection(
        rpath, ticker="MSFT", reflection_id="ref_msft_cloud",
        reflection_text="Cloud computing AI Azure earnings beat guidance raised.",
    )
    _write_reflection(
        rpath, ticker="GOOG", reflection_id="ref_goog_unrelated",
        reflection_text="Dividend cut surprise caused selloff.",
    )
    ctx = get_past_context(
        "AAPL", UTC_BASE,
        k_cross_ticker=5,
        query_text="cloud AI earnings beat guidance",
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath,
    )
    # MSFT should rank ahead of GOOG for cloud/AI query
    tickers = [r.ticker for r in ctx.cross_ticker]
    assert "MSFT" in tickers
    if "GOOG" in tickers:
        assert tickers.index("MSFT") < tickers.index("GOOG")


# ---------------------------------------------------------------------------
# Sector cache
# ---------------------------------------------------------------------------


def test_sector_cache_missing_graceful(rpath, dpath, tmp_path) -> None:
    """When sector cache is absent, cross_sector is empty and flag is set."""
    missing_path = tmp_path / "nonexistent" / "sector-cache.json"
    _write_reflection(rpath, ticker="NVDA", reflection_id="ref_nvda")

    ctx = get_past_context(
        "NVDA", UTC_BASE,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=missing_path,
    )
    assert ctx.cross_sector == []
    assert ctx.aggregate_stats.sector_cache_unavailable is True


def test_sector_cache_present_filters_by_sector(rpath, dpath, spath) -> None:
    """When cache is present, cross_sector returns same-sector items."""
    # Write sector cache
    sector_data = {
        "NVDA": "Technology",
        "AMD": "Technology",
        "TSLA": "Consumer Discretionary",
    }
    spath.parent.mkdir(parents=True, exist_ok=True)
    spath.write_text(json.dumps(sector_data))

    _write_reflection(rpath, ticker="AMD", reflection_id="ref_amd",
                      reflection_text="Chip design win at cloud hyperscalers.")
    _write_reflection(rpath, ticker="TSLA", reflection_id="ref_tsla",
                      reflection_text="EV demand slowed in Q2.")

    ctx = get_past_context(
        "NVDA", UTC_BASE,
        k_cross_sector=2,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath,
    )
    sector_tickers = {r.ticker for r in ctx.cross_sector}
    assert "AMD" in sector_tickers
    assert "TSLA" not in sector_tickers


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


def test_aggregate_stats_hit_rate(rpath, dpath, spath) -> None:
    _write_reflection(rpath, ticker="AAPL", reflection_id="ref_1",
                      alpha_return=0.03,
                      tau_observable=(UTC_BASE - timedelta(days=10)).isoformat())
    _write_reflection(rpath, ticker="AAPL", reflection_id="ref_2",
                      alpha_return=-0.01,
                      tau_observable=(UTC_BASE - timedelta(days=5)).isoformat())
    ctx = _ctx("AAPL", UTC_BASE, rpath, dpath, spath)
    assert ctx.aggregate_stats.n_resolved == 2
    assert abs(ctx.aggregate_stats.hit_rate - 0.5) < 1e-6


def test_aggregate_stats_open_positions_counted(rpath, dpath, spath) -> None:
    # Write a pending decision
    row = {
        "schema_version": 1,
        "kind": "decision",
        "decision_id": "dec_open_001",
        "asof_decision": "2026-06-10T10:00:00+00:00",
        "ticker": "AAPL",
        "asset_class": "equity",
        "rating": "Buy",
        "direction": 1,
        "confidence": 0.7,
        "target_position_pct": 0.05,
        "thesis_summary": "Open position.",
        "state": "pending",
        "tau_observable": None,
        "resolution": None,
        "thesis_evidence_ids": [],
        "signal_provenance": {},
        "research_plan_text": "",
        "trader_proposal": None,
        "risk_debate_summary": None,
    }
    dpath.write_text(json.dumps(row) + "\n")
    ctx = _ctx("AAPL", UTC_BASE, rpath, dpath, spath)
    assert ctx.aggregate_stats.open_positions_count == 1
