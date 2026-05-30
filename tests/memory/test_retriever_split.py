"""tests/memory/test_retriever_split.py — G15 same-ticker-rich vs cross-lean render.

Pure render tests over PastContext. No network. format_context_block back-compat
is asserted byte-identical against a fixed golden.
"""

from __future__ import annotations

from hermes_quant.memory.retriever import (
    AggregateStats,
    PastContext,
    ResolvedDecision,
    format_context_block,
    format_context_block_split,
)


def _rd(
    ticker: str,
    lesson: str = "",
    *,
    rating: str = "Buy",
    alpha: float = 0.05,
    raw: float = 0.07,
    category: str = "thesis_correct",
    quality: int = 4,
    asof: str = "2026-05-01T00:00:00+00:00",
) -> ResolvedDecision:
    return ResolvedDecision(
        reflection_id=f"refl_{ticker}",
        decision_id=f"dec_{ticker}",
        asof=asof,
        tau_observable="2026-04-01T00:00:00+00:00",
        ticker=ticker,
        rating=rating,
        raw_return=raw,
        alpha_return=alpha,
        holding_days=5,
        lesson=lesson,
        lesson_category=category,
        outcome_quality=quality,
    )


def test_same_ticker_is_rich():
    ctx = PastContext(
        same_ticker=[_rd("AAPL", "LESSON_MARKER held thesis", category="thesis_correct")],
    )
    out = format_context_block_split(ctx)
    assert "LESSON_MARKER" in out
    assert "thesis_correct" in out


def test_cross_ticker_is_lean():
    ctx = PastContext(
        cross_ticker=[_rd("MSFT", "SHOULD_NOT_APPEAR", alpha=0.12)],
    )
    out = format_context_block_split(ctx)
    assert "SHOULD_NOT_APPEAR" not in out
    assert "MSFT" in out
    assert "+12.0%" in out


def test_rich_lesson_truncated():
    long_lesson = "Z" * 1000
    ctx = PastContext(same_ticker=[_rd("AAPL", long_lesson)])
    out = format_context_block_split(ctx, rich_lesson_chars=400)
    # the rendered lesson line is <= 400 + len("...") = 403 chars
    lesson_lines = [ln for ln in out.splitlines() if ln.startswith("Z")]
    assert lesson_lines
    assert len(lesson_lines[0]) <= 403


def test_section_order_and_headers():
    ctx = PastContext(
        same_ticker=[_rd("AAPL", "same lesson")],
        cross_ticker=[_rd("MSFT")],
        cross_sector=[_rd("GOOG")],
    )
    out = format_context_block_split(ctx)
    i_same = out.index("Same-ticker history")
    i_cross = out.index("Cross-ticker analogs")
    i_sector = out.index("Cross-sector analogs")
    assert i_same < i_cross < i_sector


def test_empty_context_none():
    assert format_context_block_split(PastContext()) == "(none)"


def test_max_chars_clip():
    ctx = PastContext(
        same_ticker=[_rd("AAPL", "X" * 500)],
        cross_ticker=[_rd("MSFT")],
    )
    out = format_context_block_split(ctx, max_chars=50)
    assert len(out) <= 50


def test_original_format_unchanged():
    # Back-compat guard: format_context_block must be byte-identical to a fixed
    # golden for a fixed PastContext (the llm_committee caller is untouched).
    ctx = PastContext(
        same_ticker=[_rd("AAPL", "first lesson", asof="2026-05-01T00:00:00+00:00")],
        cross_ticker=[_rd("MSFT", "second lesson", asof="2026-04-15T00:00:00+00:00")],
        aggregate_stats=AggregateStats(ticker="AAPL"),
    )
    expected = (
        "--- Same-ticker history ---\n"
        "[2026-05-01 | AAPL | Buy | +7.0% | +5.0% | 5d]\n"
        "first lesson\n"
        "--- Cross-ticker analogs ---\n"
        "[2026-04-15 | MSFT | Buy | +7.0% | +5.0% | 5d]\n"
        "second lesson"
    )
    assert format_context_block(ctx) == expected
