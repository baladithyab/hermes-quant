"""hermes_quant.grounding.data_grounding — GroundTruthBlock + Citation HARD RULE.

Wave 5 (ADR-0038 §W5). Pattern ported from HKUDS VibeTrader's
agent/src/swarm/grounding.py Data Grounding Block.

Failure mode eliminated
-----------------------
F3 (SOTA LLM-trading research): LLM-fabricated price levels and phantom chart
patterns. Analysts must cite a Ground Truth ID for every numerical claim or the
ClaimVerifier rejects the view.

Context compression policy (v0.1)
----------------------------------
If the rendered block exceeds 4 KB, automatically trim to the highest-information
rows:
  - Latest 5 trading days (maximum recency signal)
  - Monthly closes for the 8 prior calendar weeks (≈ 8 representative rows)

TODO (v0.2 — HKUDS full 5-layer compression):
  Layer 0 — microcompact row encoding (6-char float repr)
  Layer 1 — per-window LLM summary (1-sentence per week)
  Layer 2 — iterative context update (rolling summary + new bars delta)
  Layer 3 — semantic deduplication (drop rows whose info < 5% marginal)
  Layer 4 — adaptive truncation with information-theoretic score
The v0.1 trim policy implemented here is the structural skeleton; production
upgrade requires only replacing _trim_to_high_info_rows().
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import NamedTuple, Optional, Sequence


# ---------------------------------------------------------------------------
# HARD RULE PREAMBLE — regression-critical constant
# Tests assert this exact string appears verbatim in render_for_prompt output.
# Do NOT paraphrase, abbreviate, or reorder sentences.
# ---------------------------------------------------------------------------

HARD_RULE_PREAMBLE: str = (
    "Every numerical claim in your response MUST cite a Ground Truth ID "
    "(e.g. gt_AAPL_20260527_close). "
    "Uncited numbers will be rejected by the verifier. "
    "Do not state any price, return, ratio, or percentage that is not "
    "derivable from the Ground Truth section above."
)

_MAX_RENDER_BYTES = 4096  # 4 KB context-compression threshold


# ---------------------------------------------------------------------------
# Bar NamedTuple — a single OHLCV bar
# ---------------------------------------------------------------------------


class Bar(NamedTuple):
    """A single OHLCV daily bar.

    Fields
    ------
    date_str  : ISO-8601 date string, e.g. '2026-05-27'
    open      : opening price
    high      : intraday high
    low       : intraday low
    close     : closing price (used for most citations)
    volume    : share/unit volume
    """

    date_str: str
    open: float
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------------
# GroundTruthBlock dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroundTruthBlock:
    """Injected ground-truth data for a single symbol at a decision timestamp.

    Attributes
    ----------
    symbol          : ticker symbol, e.g. 'AAPL'
    asof            : ISO-8601 decision date string, e.g. '2026-05-27'
    ohlcv_60d       : list of Bar tuples (up to 60 trading days, ascending by date)
    current_quote   : dict with keys: decision_price, asof, spread, slippage
    citation_ids    : stable IDs for each bar's close, e.g. 'gt_AAPL_20260527_close'
    context_summary : computed statistics (mean, std, range) over the OHLCV window
    """

    symbol: str
    asof: str
    ohlcv_60d: list[Bar]
    current_quote: dict
    citation_ids: list[str]
    context_summary: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.ohlcv_60d) != len(self.citation_ids):
            raise ValueError(
                f"GroundTruthBlock: ohlcv_60d length ({len(self.ohlcv_60d)}) "
                f"must equal citation_ids length ({len(self.citation_ids)})"
            )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_ground_truth_block(
    symbol: str,
    asof: str,
    lookback_days: int = 60,
    *,
    ohlcv_bars: Optional[Sequence[Bar]] = None,
    decision_price: Optional[float] = None,
    spread: float = 0.0,
    slippage: float = 0.0,
) -> GroundTruthBlock:
    """Build a GroundTruthBlock for *symbol* at *asof*.

    v0.1 stub: if *ohlcv_bars* is provided, use it directly (no yfinance call).
    When the snapshot cache (hermes_quant/data/cache.py OhlcvCache) is wired,
    pass cached bars here. Production wiring is deferred to downstream tasks.

    Parameters
    ----------
    symbol        : ticker, e.g. 'AAPL'
    asof          : ISO-8601 decision date, e.g. '2026-05-27'
    lookback_days : how many calendar days to include (default 60 → ~42 trading days)
    ohlcv_bars    : if given, used as-is (skips any data fetch)
    decision_price: override last close as the decision price
    spread        : estimated bid-ask spread fraction (default 0)
    slippage      : estimated market-impact fraction (default 0)

    Returns
    -------
    GroundTruthBlock with stable, deterministic citation_ids.
    """
    symbol_upper = symbol.upper()
    asof_clean = asof.replace("-", "")  # '20260527'

    if ohlcv_bars is None:
        # Synthetic stub: generate a minimal single-bar block so callers always
        # receive a valid GroundTruthBlock even without a data connection.
        # Replace with OhlcvCache.read() slice when wiring production data.
        bars: list[Bar] = []
    else:
        bars = list(ohlcv_bars)

    # Trim to lookback window (keep the most recent `lookback_days` bars)
    if len(bars) > lookback_days:
        bars = bars[-lookback_days:]

    # Build stable citation_ids: gt_{SYMBOL}_{YYYYMMDD}_{field}
    citation_ids: list[str] = []
    for bar in bars:
        date_compact = bar.date_str.replace("-", "")
        citation_ids.append(f"gt_{symbol_upper}_{date_compact}_close")

    # Current quote
    last_price = bars[-1].close if bars else (decision_price or 0.0)
    if decision_price is not None:
        last_price = decision_price
    current_quote = {
        "decision_price": last_price,
        "asof": asof,
        "spread": spread,
        "slippage": slippage,
    }

    # Context summary (mean, std, pct_range over closes)
    context_summary = _compute_summary(bars)

    return GroundTruthBlock(
        symbol=symbol_upper,
        asof=asof,
        ohlcv_60d=bars,
        current_quote=current_quote,
        citation_ids=citation_ids,
        context_summary=context_summary,
    )


# ---------------------------------------------------------------------------
# Context summary helper
# ---------------------------------------------------------------------------


def _compute_summary(bars: list[Bar]) -> dict:
    """Compute mean/std/range statistics over closes for sanity checks."""
    if not bars:
        return {}
    closes = [b.close for b in bars]
    mean_close = statistics.mean(closes)
    std_close = statistics.pstdev(closes) if len(closes) > 1 else 0.0
    range_close = max(closes) - min(closes)
    pct_range = (range_close / mean_close * 100.0) if mean_close else 0.0
    return {
        "mean_close": round(mean_close, 4),
        "std_close": round(std_close, 4),
        "range_close": round(range_close, 4),
        "pct_range": round(pct_range, 2),
        "n_bars": len(bars),
        "date_start": bars[0].date_str,
        "date_end": bars[-1].date_str,
    }


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_for_prompt(block: GroundTruthBlock) -> str:
    """Render a GroundTruthBlock as a prompt section string.

    Always includes the HARD_RULE_PREAMBLE verbatim at the end.

    If the rendered size exceeds 4 KB, automatically trims to
    highest-information rows before appending the preamble.
    """
    # Build full render first; trim if needed
    rendered = _render_full(block)
    if len(rendered.encode("utf-8")) > _MAX_RENDER_BYTES:
        trimmed_bars, trimmed_ids = _trim_to_high_info_rows(block.ohlcv_60d, block.citation_ids)
        trimmed_block = GroundTruthBlock(
            symbol=block.symbol,
            asof=block.asof,
            ohlcv_60d=trimmed_bars,
            current_quote=block.current_quote,
            citation_ids=trimmed_ids,
            context_summary=block.context_summary,
        )
        rendered = _render_full(trimmed_block)

    return rendered


def _render_full(block: GroundTruthBlock) -> str:
    """Produce the complete prompt section for a block (no trimming)."""
    lines: list[str] = []
    lines.append(f"=== GROUND TRUTH: {block.symbol} as of {block.asof} ===")
    lines.append("")

    # Context summary
    if block.context_summary:
        s = block.context_summary
        lines.append(
            f"Context window: {s.get('n_bars', 0)} bars "
            f"({s.get('date_start', '?')} → {s.get('date_end', '?')}), "
            f"mean_close={s.get('mean_close', '?')}, "
            f"std={s.get('std_close', '?')}, "
            f"range={s.get('range_close', '?')} ({s.get('pct_range', '?')}%)"
        )
        lines.append("")

    # Current quote
    q = block.current_quote
    lines.append(
        f"Current quote: decision_price={q.get('decision_price')}, "
        f"spread={q.get('spread')}, slippage={q.get('slippage')}"
    )
    lines.append(f"  Citation ID: gt_{block.symbol}_{block.asof.replace('-', '')}_quote")
    lines.append("")

    # OHLCV table header
    if block.ohlcv_60d:
        lines.append(f"{'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>12}  Citation ID")
        lines.append("-" * 80)
        for bar, cid in zip(block.ohlcv_60d, block.citation_ids):
            lines.append(
                f"{bar.date_str:<12} "
                f"{bar.open:>8.4f} "
                f"{bar.high:>8.4f} "
                f"{bar.low:>8.4f} "
                f"{bar.close:>8.4f} "
                f"{bar.volume:>12.0f}  [{cid}]"
            )
        lines.append("")

    # HARD RULE preamble (regression-critical — must appear verbatim)
    lines.append(HARD_RULE_PREAMBLE)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5-layer context compression — v0.1 trim policy
# ---------------------------------------------------------------------------


def _trim_to_high_info_rows(
    bars: list[Bar], citation_ids: list[str]
) -> tuple[list[Bar], list[str]]:
    """Trim OHLCV to highest-information rows when the block exceeds 4 KB.

    v0.1 policy:
      - Keep the latest 5 trading days (maximum recency signal).
      - Keep one bar per calendar week for the prior 8 weeks
        (the last bar of each ISO week = Friday close or last available).

    TODO (v0.2): replace with HKUDS full 5-layer compression pipeline:
      Layer 0 — microcompact 6-char float repr
      Layer 1 — per-window LLM summary
      Layer 2 — iterative context update
      Layer 3 — semantic deduplication
      Layer 4 — adaptive information-theoretic truncation
    """
    if not bars:
        return bars, citation_ids

    combined = list(zip(bars, citation_ids))

    # Keep last 5 bars
    recent = combined[-5:]
    recent_dates = {b.date_str for b, _ in recent}

    # Weekly closes for the prior 8 weeks (select last bar of each week)
    weekly: dict[str, tuple[Bar, str]] = {}
    for bar, cid in combined[:-5]:
        try:
            d = date.fromisoformat(bar.date_str)
        except ValueError:
            continue
        # ISO week key: YYYY-WNN
        iso_year, iso_week, _ = d.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        # Keep the latest bar per week
        if week_key not in weekly or date.fromisoformat(bar.date_str) > date.fromisoformat(weekly[week_key][0].date_str):
            weekly[week_key] = (bar, cid)

    # Sort weekly by date, take last 8
    weekly_sorted = sorted(weekly.values(), key=lambda x: x[0].date_str)
    weekly_kept = weekly_sorted[-8:]

    # Merge: weekly + recent (deduplicated), sorted by date
    kept_dict: dict[str, tuple[Bar, str]] = {}
    for bar, cid in weekly_kept:
        kept_dict[bar.date_str] = (bar, cid)
    for bar, cid in recent:
        kept_dict[bar.date_str] = (bar, cid)

    result = sorted(kept_dict.values(), key=lambda x: x[0].date_str)
    result_bars = [b for b, _ in result]
    result_ids = [cid for _, cid in result]
    return result_bars, result_ids
