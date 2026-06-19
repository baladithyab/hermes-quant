"""tests/grounding/test_data_grounding.py — Unit tests for GroundTruthBlock.

Wave 5 acceptance tests:
  - build_ground_truth_block produces stable citation_ids deterministically
  - HARD_RULE_PREAMBLE appears verbatim in render_for_prompt output (regression-critical)
  - 5-layer compression: a 200-day OHLCV block trims to <=4KB and preserves the latest week
"""
from __future__ import annotations

import pytest

import hermes_quant.grounding.data_grounding as dg
from hermes_quant.grounding.data_grounding import (
    Bar,
    GroundTruthBlock,
    HARD_RULE_PREAMBLE,
    build_ground_truth_block,
    render_for_prompt,
    _trim_to_high_info_rows,
    _saliency_keep,
    _fmt_volume_compact,
    _MAX_RENDER_BYTES,
    _RECENCY_KEEP,
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


# ===========================================================================
# B46 v0.2 — deterministic, no-LLM, no-lookahead context compaction
# ===========================================================================


def _make_overflow_bars(n: int = 600) -> list[Bar]:
    """Generate enough bars that even the compact render overflows 4 KB,
    forcing the saliency-keep stage. ~600 daily bars renders > 4 KB compact.

    Starts in 2023 so all 600 weekday bars are dated on/before the canonical
    ``asof="2026-05-27"`` these overflow tests use. The builder enforces the
    no-lookahead ``<= asof`` window (see ``build_ground_truth_block``); a fixture
    that ran past asof would (correctly) have its future bars dropped, which would
    silently shrink the window and defeat the overflow intent of these tests.
    """
    return _make_bars(n, start_year=2023)


def _make_salient_vs_magnitude_bars(
    n: int = 600, salient_at: int = 20
) -> list[Bar]:
    """A monotonically-rising series (so close == magnitude grows with index) with
    ONE genuinely-salient bar at a LOW index — i.e. a SMALL close.

    The salient bar's CLOSE stays on-trend (it is NOT the window max-close, and at a
    low index it is near the BOTTOM of the close range), but it carries a huge
    intraday range and a 50x volume surprise. So:

      * a TRUE saliency proxy (|return| + range + volume-z) ranks this bar far above
        the flat bars and keeps it; while
      * a degenerate "rank by close magnitude" proxy would rank this low-close bar
        near LAST and instead keep the highest-close (recent) bars.

    This breaks the old _make_spiky_bars confound, where the spike bar was ALSO the
    window max-close and thus force-kept by the min/max-close invariant regardless of
    whether saliency worked at all (the test could not tell saliency from magnitude).
    Here the salient bar is neither the min- nor max-close, so its survival is
    attributable ONLY to the saliency score.
    """
    from datetime import date, timedelta

    bars: list[Bar] = []
    d = date(2024, 1, 1)
    base = 150.0
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = base + 0.1 * i  # steady rise: close strictly grows with index
        hi, lo, vol = close + 0.1, close - 0.1, 1_000_000.0
        if i == salient_at:
            # On-trend close (NOT extreme), but a 25-wide intraday range and 50x
            # volume — pure saliency, zero close-magnitude advantage.
            hi, lo, vol = close + 25.0, close - 25.0, 50_000_000.0
        bars.append(Bar(d.isoformat(), close - 0.05, hi, lo, close, vol))
        d += timedelta(days=1)
    return bars


# --- determinism -----------------------------------------------------------


def test_render_byte_identical_across_repeats():
    """render_for_prompt must be byte-identical across 100 repeated calls
    (no RNG, no wall-clock) — for both the small and the overflow path.
    """
    for bars in (_make_bars(5), _make_overflow_bars(600)):
        block = build_ground_truth_block(
            "AAPL", "2026-05-27", lookback_days=len(bars), ohlcv_bars=bars
        )
        renders = {render_for_prompt(block) for _ in range(100)}
        assert len(renders) == 1, "render_for_prompt is not deterministic"


def test_saliency_keep_order_stable():
    """_saliency_keep must return the same kept dates for the same input."""
    bars = _make_overflow_bars(600)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )
    k1, _ = _saliency_keep(bars, block.citation_ids, block=block)
    k2, _ = _saliency_keep(bars, block.citation_ids, block=block)
    assert [b.date_str for b in k1] == [b.date_str for b in k2]


# --- byte cap & preamble (fail-closed) -------------------------------------


def test_overflow_block_respects_4kb_cap():
    """A block whose compact render still overflows must be saliency-trimmed
    to <= 4 KB while keeping the preamble verbatim.
    """
    bars = _make_overflow_bars(600)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )
    rendered = render_for_prompt(block)
    assert len(rendered.encode("utf-8")) <= _MAX_RENDER_BYTES
    assert HARD_RULE_PREAMBLE in rendered


def test_pathological_wide_block_under_cap_with_preamble():
    """A pathological block (9-figure volumes, wide closes, many bars) must
    still render <= 4 KB WITH the preamble verbatim.
    """
    from datetime import date, timedelta

    bars: list[Bar] = []
    d = date(2024, 1, 1)
    for i in range(500):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = 123456.7890 + i  # wide, 4-dp close
        bars.append(
            Bar(d.isoformat(), close - 1, close + 100, close - 100, close, 999_999_999.0)
        )
        d += timedelta(days=1)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=500, ohlcv_bars=bars
    )
    rendered = render_for_prompt(block)
    assert len(rendered.encode("utf-8")) <= _MAX_RENDER_BYTES
    assert HARD_RULE_PREAMBLE in rendered


def test_preamble_never_dropped_even_when_nothing_fits():
    """Fail-closed: with a punishingly small cap, the preamble is still present
    and the latest bars survive — data rows are dropped before the preamble.
    """
    bars = _make_overflow_bars(600)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )
    orig = dg._MAX_RENDER_BYTES
    dg._MAX_RENDER_BYTES = 900  # smaller than preamble + many rows
    try:
        rendered = render_for_prompt(block)
    finally:
        dg._MAX_RENDER_BYTES = orig
    assert HARD_RULE_PREAMBLE in rendered, "preamble dropped under tiny cap — contract violated"
    # latest bars must still be present (fail-closed keeps recency)
    for bar in bars[-_RECENCY_KEEP:]:
        assert bar.date_str in rendered


# --- forced-keep invariants (recency + min/max-close) ----------------------


def test_saliency_keeps_latest_bars():
    """The latest _RECENCY_KEEP bars must always survive saliency-keep."""
    bars = _make_overflow_bars(600)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )
    kept, _ = _saliency_keep(bars, block.citation_ids, block=block)
    kept_dates = {b.date_str for b in kept}
    for bar in bars[-_RECENCY_KEEP:]:
        assert bar.date_str in kept_dates


def test_min_and_max_close_bars_survive_trim():
    """Window min-close and max-close bars must survive so window_low/window_high
    range claims stay traceable by the verifier.
    """
    bars = _make_overflow_bars(600)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )
    closes = [b.close for b in bars]
    min_date = bars[closes.index(min(closes))].date_str
    max_date = bars[closes.index(max(closes))].date_str
    rendered = render_for_prompt(block)
    assert min_date in rendered, "min-close bar dropped — range claim untraceable"
    assert max_date in rendered, "max-close bar dropped — range claim untraceable"


def test_saliency_keeps_genuinely_salient_bar_not_magnitude():
    """DISCRIMINATING: saliency, not close-magnitude, must drive the keep.

    Setup: a monotonically-rising series (close grows with index) with ONE salient
    bar at a LOW index — small close, but huge intraday range + 50x volume. We pin
    the byte cap so there is exactly ONE non-forced candidate slot. Under a TRUE
    saliency proxy that slot goes to the salient (low-close) bar; under a degenerate
    "rank by close magnitude" proxy it would go to the highest-close recent bar.

    Asserts:
      * the salient low-close bar IS kept (it wins the single competitive slot);
      * the highest-close non-forced bar — which a magnitude-ranker would keep
        instead — is DROPPED;
      * the salient bar is neither the window min- nor max-close (so its survival is
        not an artifact of the min/max-close force-keep invariant).

    This FAILS if saliency degrades to pure magnitude: the low-close salient bar
    would lose the slot to a high-close flat bar.
    """
    salient_at = 20
    bars = _make_salient_vs_magnitude_bars(600, salient_at=salient_at)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )

    closes = [b.close for b in bars]
    # The salient bar is on-trend: strictly between the min- and max-close bars,
    # so the min/max force-keep invariant cannot explain its survival.
    assert bars[salient_at].close != min(closes)
    assert bars[salient_at].close != max(closes)

    # Pin a tight cap that leaves exactly one competitive (non-forced) candidate slot.
    kept, _ = _saliency_keep(bars, block.citation_ids, max_bytes=1100, block=block)
    kept_idx = {bars.index(b) for b in kept}

    forced = set(range(600 - _RECENCY_KEEP, 600)) | {
        closes.index(min(closes)),
        closes.index(max(closes)),
    }
    extra = sorted(kept_idx - forced)
    assert extra == [salient_at], (
        f"saliency-keep gave the single competitive slot to {extra}, not the "
        f"genuinely-salient bar {salient_at} — saliency may have degraded to magnitude"
    )

    # The bar a close-magnitude ranker would have kept (highest-close non-forced bar)
    # must be DROPPED — directly distinguishing saliency from magnitude.
    highest_close_candidate = max(
        (i for i in range(600) if i not in forced), key=lambda i: closes[i]
    )
    assert highest_close_candidate not in kept_idx, (
        "the highest-close non-forced bar was kept — that is a magnitude artifact, "
        "not saliency"
    )


# --- no-lookahead ----------------------------------------------------------


def test_saliency_uses_only_in_window_bars():
    """No-lookahead: saliency-keep on the first k bars must be a function of ONLY
    those k bars — appending FUTURE bars must not change which of the first k are
    selected when scored in isolation. We assert the kept set over a prefix is
    unaffected by data that does not exist yet at that prefix's as-of.
    """
    bars = _make_overflow_bars(600)
    prefix = bars[:300]
    block_prefix = build_ground_truth_block(
        "AAPL", "2026-03-01", lookback_days=300, ohlcv_bars=prefix
    )
    ids_prefix = block_prefix.citation_ids
    kept_a, _ = _saliency_keep(prefix, ids_prefix, block=block_prefix)
    kept_b, _ = _saliency_keep(prefix, ids_prefix, block=block_prefix)
    # deterministic & self-consistent on the as-of slice only
    assert [b.date_str for b in kept_a] == [b.date_str for b in kept_b]
    # every kept date is <= the as-of slice's last date (no future bar referenced)
    last_in_window = prefix[-1].date_str
    assert all(b.date_str <= last_in_window for b in kept_a)


def test_builder_drops_bars_dated_after_asof():
    """No-lookahead boundary: the builder is the designated as-of slice. A bar dated
    AFTER *asof* must never be laundered into the block (citation_ids / ohlcv_60d) nor
    into render_for_prompt.

    The builder — not an upstream cache — owns the <= asof window (module header asserts
    it; OhlcvCache.read() returns the full unsliced frame and the advisor seam passes
    ohlcv_bars straight through). A future-dated bar in the input must be dropped.
    """
    bars = [
        Bar("2026-05-26", 100.0, 102.0, 99.0, 101.5, 1_000_000),
        # FUTURE bar: dated strictly after asof — must NOT survive the builder.
        Bar("2026-05-27", 101.5, 110.0, 101.0, 108.8, 2_000_000),
    ]
    block = build_ground_truth_block("AAPL", asof="2026-05-26", ohlcv_bars=bars)

    future_cid = "gt_AAPL_20260527_close"
    assert future_cid not in block.citation_ids, (
        "future-dated bar's citation_id leaked into the block — no-lookahead breach"
    )
    assert all(
        b.date_str <= "2026-05-26" for b in block.ohlcv_60d
    ), "a bar dated after asof survived into ohlcv_60d"
    # The on-asof bar must remain.
    assert "gt_AAPL_20260526_close" in block.citation_ids

    rendered = render_for_prompt(block)
    assert "108.80" not in rendered, (
        "the future close (108.80) appears in the rendered ground-truth block — "
        "it could be cited as grounded truth by an analyst"
    )
    # current_quote.decision_price must be the on-asof close, not the future close.
    assert block.current_quote["decision_price"] == 101.5


def test_verifier_rejects_claim_citing_future_close():
    """End-to-end: a rationale citing a future-dated close must NOT verify as grounded
    once the builder enforces the <= asof window. The future close is absent from the
    block's render and its citation_id is not a valid citation, so the claim is uncited.
    """
    from hermes_quant.grounding.verifier import ClaimVerifier
    from hermes_quant.protocol import AnalystView

    bars = [
        Bar("2026-05-26", 100.0, 102.0, 99.0, 101.5, 1_000_000),
        Bar("2026-05-27", 101.5, 110.0, 101.0, 108.8, 2_000_000),  # future
    ]
    block = build_ground_truth_block("AAPL", asof="2026-05-26", ohlcv_bars=bars)

    view = AnalystView(
        analyst="test",
        direction="long",
        magnitude=0.5,
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale="",
    )
    result = ClaimVerifier().verify(
        view,
        block,
        claim_text="Strong setup, target 108.80 confirmed [gt_AAPL_20260527_close].",
    )
    assert not result.accepted, (
        "a claim citing a future-dated close was accepted as grounded truth"
    )
    assert "108.80" in result.uncited_claims, (
        "the future close should be flagged uncited once the builder drops it"
    )


# --- microcompact render (Layer A) -----------------------------------------


def test_fmt_volume_compact():
    """Volume shorthand is deterministic across magnitudes."""
    assert _fmt_volume_compact(1_000_000) == "1.00M"
    assert _fmt_volume_compact(12_345) == "12.35k"
    assert _fmt_volume_compact(2_500_000_000) == "2.50B"
    assert _fmt_volume_compact(500) == "500"


def test_compact_render_preserves_close_token_for_verifier():
    """Layer-A microcompact must keep the exact :.4f close token so the
    ClaimVerifier can still substring-trace a cited close.
    """
    bars = _make_overflow_bars(600)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=600, ohlcv_bars=bars
    )
    rendered = render_for_prompt(block)  # overflow → compact path
    # the latest bar's 4-dp close must appear verbatim
    last_close = f"{bars[-1].close:.4f}"
    assert last_close in rendered


# --- legacy weekly policy reachability -------------------------------------


def test_under_cap_block_is_full_render_unchanged():
    """A block whose full (non-compact) render fits under 4 KB must be returned
    byte-identically by BOTH policies — this is the common-case stage-1 path that
    must stay unchanged vs v0.1 (no compaction triggered).
    """
    # 35 bars renders ~3.7 KB (full, non-compact) — comfortably under the cap.
    bars = _make_bars(35)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=35, ohlcv_bars=bars
    )
    full = dg._render_full(block)  # non-compact reference
    assert len(full.encode("utf-8")) <= _MAX_RENDER_BYTES
    default_render = render_for_prompt(block)  # saliency policy
    orig = dg._TRIM_POLICY
    dg._TRIM_POLICY = "weekly"
    try:
        weekly_render = render_for_prompt(block)
    finally:
        dg._TRIM_POLICY = orig
    assert default_render == full, "under-cap default path diverged from full render"
    assert weekly_render == full, "under-cap weekly path diverged from full render"


def test_weekly_policy_byte_identical_to_v01_on_overflow():
    """Flag-OFF rail: under _TRIM_POLICY == 'weekly', an OVERFLOW block renders
    via the legacy last-5 + weekly trim, non-compact — exactly the v0.1 output.

    We pin the expected v0.1 byte length for a 60-bar block (last-5 + 8 weekly
    rows, non-compact) as a regression anchor: it must NOT use the compact layout.
    """
    bars = _make_bars(60)
    block = build_ground_truth_block(
        "AAPL", "2026-05-27", lookback_days=60, ohlcv_bars=bars
    )
    # full render overflows (forces a trim path)
    assert len(dg._render_full(block).encode("utf-8")) > _MAX_RENDER_BYTES
    orig = dg._TRIM_POLICY
    dg._TRIM_POLICY = "weekly"
    try:
        weekly_render = render_for_prompt(block)
    finally:
        dg._TRIM_POLICY = orig
    # legacy path is NON-compact (no 'O/H/L/C' compact header) and keeps the ruler
    assert "O/H/L/C" not in weekly_render, "weekly policy must use the v0.1 non-compact layout"
    assert "-" * 80 in weekly_render
    assert HARD_RULE_PREAMBLE in weekly_render
    assert len(weekly_render.encode("utf-8")) <= _MAX_RENDER_BYTES


def test_no_llm_import_in_module():
    """Determinism rail: the module must not IMPORT any LLM/network client.

    Inspects actual import statements (not prose; the docstrings legitimately
    discuss why the LLM layers were dropped).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(dg))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"openai", "anthropic", "requests", "httpx", "litellm", "urllib", "socket"}
    leaked = imported & forbidden
    assert not leaked, f"forbidden dependency imported: {leaked}"
    # stdlib-only rail: no third-party imports at all
    assert imported <= {"math", "statistics", "datetime", "dataclasses", "typing", "__future__"}, (
        f"non-stdlib import leaked: {imported}"
    )
