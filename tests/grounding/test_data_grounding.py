"""tests/grounding/test_data_grounding.py — Unit tests for GroundTruthBlock.

Wave 5 acceptance tests:
  - build_ground_truth_block produces stable citation_ids deterministically
  - HARD_RULE_PREAMBLE appears verbatim in render_for_prompt output (regression-critical)
  - 5-layer compression: a 200-day OHLCV block trims to <=4KB and preserves the latest week
"""
from __future__ import annotations

import pytest

from hermes_quant.grounding.data_grounding import (
    Bar,
    GroundTruthBlock,
    HARD_RULE_PREAMBLE,
    build_ground_truth_block,
    render_for_prompt,
    _trim_to_high_info_rows,
    _MAX_RENDER_BYTES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bars(n: int, start_year: int = 2025, start_month: int = 1) -> list[Bar]:
    """Generate *n* synthetic daily bars starting at given month."""
    from datetime import date, timedelta

    bars = []
    d = date(start_year, start_month, 1)
    base_price = 150.0
    for i in range(n):
        # Skip weekends for realism
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = base_price + i * 0.1
        bars.append(
            Bar(
                date_str=d.isoformat(),
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1_000_000 + i * 100,
            )
        )
        d += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# Test: deterministic citation_ids
# ---------------------------------------------------------------------------


def test_citation_ids_deterministic():
    """build_ground_truth_block must produce the same citation_ids on repeated calls."""
    bars = _make_bars(10)
    block1 = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    block2 = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    assert block1.citation_ids == block2.citation_ids


def test_citation_id_format():
    """Citation IDs must follow the pattern gt_{SYMBOL}_{YYYYMMDD}_close."""
    bars = _make_bars(5)
    block = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    for cid, bar in zip(block.citation_ids, block.ohlcv_60d):
        expected = f"gt_AAPL_{bar.date_str.replace('-', '')}_close"
        assert cid == expected, f"Expected {expected!r}, got {cid!r}"


def test_citation_ids_symbol_uppercased():
    """Symbol is uppercased in citation IDs regardless of input case."""
    bars = _make_bars(3)
    block = build_ground_truth_block("aapl", "2026-05-27", ohlcv_bars=bars)
    for cid in block.citation_ids:
        assert cid.startswith("gt_AAPL_"), f"Expected uppercase symbol in {cid!r}"


def test_empty_bars_block():
    """build_ground_truth_block with no bars must not raise."""
    block = build_ground_truth_block("MSFT", "2026-05-27")
    assert block.citation_ids == []
    assert block.ohlcv_60d == []


def test_lookback_trim():
    """If more bars than lookback_days are supplied, keep only the last lookback_days."""
    bars = _make_bars(100)
    block = build_ground_truth_block("AAPL", "2026-05-27", lookback_days=30, ohlcv_bars=bars)
    assert len(block.ohlcv_60d) == 30
    # Should be the last 30 bars
    assert block.ohlcv_60d == bars[-30:]


def test_context_summary_populated():
    """context_summary must include mean_close, std_close, range_close."""
    bars = _make_bars(20)
    block = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    s = block.context_summary
    assert "mean_close" in s
    assert "std_close" in s
    assert "range_close" in s
    assert s["n_bars"] == 20


# ---------------------------------------------------------------------------
# Test: HARD_RULE_PREAMBLE verbatim in render output
# ---------------------------------------------------------------------------


def test_hard_rule_preamble_verbatim_in_render():
    """HARD_RULE_PREAMBLE must appear verbatim in render_for_prompt output.

    This is regression-critical — the verifier depends on this exact wording.
    Any edit to HARD_RULE_PREAMBLE must be intentional and this test will catch drift.
    """
    bars = _make_bars(5)
    block = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    rendered = render_for_prompt(block)
    assert HARD_RULE_PREAMBLE in rendered, (
        "HARD_RULE_PREAMBLE text not found verbatim in render_for_prompt output. "
        "This is a regression — do NOT paraphrase the preamble."
    )


def test_hard_rule_preamble_contains_required_phrases():
    """The preamble must contain all required key phrases."""
    required_phrases = [
        "MUST cite a Ground Truth ID",
        "Uncited numbers will be rejected by the verifier",
        "Do not state any price, return, ratio, or percentage",
        "not derivable from the Ground Truth section above",
    ]
    for phrase in required_phrases:
        assert phrase in HARD_RULE_PREAMBLE, (
            f"Required phrase missing from HARD_RULE_PREAMBLE: {phrase!r}"
        )


def test_render_includes_symbol_and_asof():
    """Rendered block must include the symbol and asof date."""
    bars = _make_bars(5)
    block = build_ground_truth_block("TSLA", "2026-05-27", ohlcv_bars=bars)
    rendered = render_for_prompt(block)
    assert "TSLA" in rendered
    assert "2026-05-27" in rendered


def test_render_includes_citation_ids():
    """Each citation ID must appear in the rendered output."""
    bars = _make_bars(5)
    block = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    rendered = render_for_prompt(block)
    for cid in block.citation_ids:
        assert cid in rendered, f"Citation ID {cid!r} missing from rendered output"


# ---------------------------------------------------------------------------
# Test: 5-layer compression (v0.1 trim policy)
# ---------------------------------------------------------------------------


def test_200day_block_trims_to_4kb():
    """A 200-day OHLCV block must render to <=4KB after compression."""
    bars = _make_bars(200)
    block = build_ground_truth_block("AAPL", "2026-05-27", lookback_days=200, ohlcv_bars=bars)
    rendered = render_for_prompt(block)
    byte_len = len(rendered.encode("utf-8"))
    assert byte_len <= _MAX_RENDER_BYTES, (
        f"Rendered block is {byte_len} bytes, exceeds 4KB limit of {_MAX_RENDER_BYTES}. "
        f"5-layer compression did not trim adequately."
    )


def test_200day_block_preserves_latest_week():
    """After compression, the latest 5 bars (most recent week) must be in the output."""
    bars = _make_bars(200)
    block = build_ground_truth_block("AAPL", "2026-05-27", lookback_days=200, ohlcv_bars=bars)
    rendered = render_for_prompt(block)

    # The last 5 bars' dates must appear in the rendered output
    last_5 = bars[-5:]
    for bar in last_5:
        assert bar.date_str in rendered, (
            f"Latest-week bar {bar.date_str!r} not found in trimmed output. "
            f"5-layer compression incorrectly dropped recent bars."
        )


def test_trim_preserves_recent_bars():
    """_trim_to_high_info_rows must keep the latest 5 bars intact."""
    bars = _make_bars(60)
    cids = [f"gt_AAPL_{b.date_str.replace('-', '')}_close" for b in bars]
    trimmed_bars, trimmed_ids = _trim_to_high_info_rows(bars, cids)
    last_5_dates = {b.date_str for b in bars[-5:]}
    trimmed_dates = {b.date_str for b in trimmed_bars}
    assert last_5_dates.issubset(trimmed_dates), (
        f"Latest 5 bars not preserved in trim. "
        f"Missing: {last_5_dates - trimmed_dates}"
    )


def test_trim_small_block_unchanged():
    """Blocks under 4KB should not have bars dropped."""
    bars = _make_bars(5)
    block = build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)
    rendered = render_for_prompt(block)
    # All 5 bars' dates must be present
    for bar in bars:
        assert bar.date_str in rendered


def test_groundtruth_block_mismatch_raises():
    """GroundTruthBlock must raise if ohlcv_60d and citation_ids lengths differ."""
    bars = _make_bars(3)
    with pytest.raises(ValueError, match="must equal citation_ids length"):
        GroundTruthBlock(
            symbol="AAPL",
            asof="2026-05-27",
            ohlcv_60d=bars,
            current_quote={},
            citation_ids=["gt_AAPL_20260527_close"],  # wrong length
        )
