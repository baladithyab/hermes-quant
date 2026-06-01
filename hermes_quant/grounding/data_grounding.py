"""hermes_quant.grounding.data_grounding — GroundTruthBlock + Citation HARD RULE.

Wave 5 (ADR-0038 §W5). Pattern ported from HKUDS VibeTrader's
agent/src/swarm/grounding.py Data Grounding Block.

Failure mode eliminated
-----------------------
F3 (SOTA LLM-trading research): LLM-fabricated price levels and phantom chart
patterns. Analysts must cite a Ground Truth ID for every numerical claim or the
ClaimVerifier rejects the view.

Context compression policy (v0.2 — deterministic, NO LLM)
---------------------------------------------------------
This block is rendered into an analyst prompt and is bounded to ``_MAX_RENDER_BYTES``
(4 KB). When the rendered block would exceed that ceiling it is compacted by two
deterministic, no-LLM, no-lookahead layers (see B46 research note
``docs/research/2026-05-31-r-B46.md``):

  - Layer A — microcompact row encoding. A narrower OHLCV line (no 80-char ruler,
    tighter column widths, ``k``/``M`` volume shorthand). Close prices keep their
    exact ``:.4f`` token so the ClaimVerifier (``verifier.py``) can still substring-
    trace every analyst claim — compaction NEVER changes a citeable number.

  - Layer B — deterministic saliency keep (``_saliency_keep``). Non-recent bars are
    ranked by a fixed-weight saliency proxy (|return| + intraday range + volume-z)
    using ONLY in-window bars at or before each bar's own date (no-lookahead). The
    latest 5 bars (recency invariant) and the window min-close / max-close bars
    (so ``window_low``/``window_high`` range claims stay traceable) are force-kept.
    Rows are packed greedily until the next row would breach the byte cap.

Honesty note (load-bearing — do not "upgrade" to an LLM pipeline)
-----------------------------------------------------------------
An earlier TODO here listed a "HKUDS full 5-layer compression pipeline" (per-window
LLM summary, iterative LLM context update, semantic dedup, info-theoretic
truncation). That was a **misattribution**: those are Vibe-Trading's *conversation
agent-loop* context layers (``agent/src/agent/loop.py``), three of which require an
LLM call. Vibe-Trading's actual *Data Grounding Block*
(``agent/src/swarm/grounding.py``) is fully deterministic and simpler than this
module. There is no richer upstream OHLCV-compression pipeline to port, and porting
the LLM layers would break B46's no-LLM / determinism / silence contract. The
"information-theoretic truncation" here is therefore a deterministic *saliency
proxy*, not Shannon self-information (which would need a model). Keep it that way.

Fail-closed silence contract
----------------------------
  - Empty bars in → empty bars out (never fabricate placeholder rows).
  - ``HARD_RULE_PREAMBLE`` is appended last, verbatim, ALWAYS — even under maximal
    trimming. If compact + saliency-keep still cannot fit preamble + the latest 5
    bars within the cap, fall back to latest-5-only + preamble; data rows are
    dropped before the preamble is ever dropped or paraphrased.
  - No LLM call exists in any path. The default and only path is deterministic.
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

# Trim policy selector (deterministic; default-OFF guard for the behavioral change).
# "saliency" (default, v0.2)  — deterministic saliency-keep + forced recency/min/max.
# "weekly"   (legacy, v0.1)   — last-5 + one bar per ISO week for the prior 8 weeks.
# Kept reachable for one release so a downstream-analyst regression is attributable
# to the row-keep change; remove "weekly" once v0.2 has soaked. No .env flag needed:
# this is an internal renderer, not a strategy — it ships behind the green-tests gate.
_TRIM_POLICY: str = "saliency"

# Deterministic saliency weights (fixed; NO RNG, NO wall-clock, NO lookahead).
# saliency(bar) = _W_RETURN*|ret| + _W_RANGE*(intraday range / close) + _W_VOLZ*volume_z
_W_RETURN = 1.0
_W_RANGE = 1.0
_W_VOLZ = 0.5

_RECENCY_KEEP = 5  # latest N bars always survive (recency invariant; tested)


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

    Deterministic, no-LLM, no-lookahead. If the rendered block would exceed
    ``_MAX_RENDER_BYTES`` (4 KB) the bars are compacted in stages, fail-closed,
    so the preamble is NEVER dropped (see module docstring):

      1. Full render. If it fits, return it (default/unchanged path for the
         common <4 KB case — byte-identical to v0.1 in non-compact mode).
      2. Compact render (Layer A microcompact). If it fits, return it.
      3. Compact render over saliency-kept bars (Layer B), packed greedily to
         the byte cap with forced recency + min/max-close rows.
      4. Fail-closed: compact render over the latest ``_RECENCY_KEEP`` bars only.
         The preamble is always present.
    """
    if not block.ohlcv_60d:
        # Silence contract: nothing to compact; full render (with preamble) only.
        return _render_full(block)

    # Stage 1 — full render (unchanged v0.1 layout); the common path.
    rendered = _render_full(block)
    if len(rendered.encode("utf-8")) <= _MAX_RENDER_BYTES:
        return rendered

    # Legacy v0.1 reproduction: under _TRIM_POLICY == "weekly", trim to the weekly
    # row set and render NON-compact (byte-identical to v0.1's overflow path).
    if _TRIM_POLICY == "weekly":
        weekly_bars, weekly_ids = _trim_to_high_info_rows(
            block.ohlcv_60d, block.citation_ids
        )
        rendered = _render_full(_with_bars(block, weekly_bars, weekly_ids))
        if len(rendered.encode("utf-8")) <= _MAX_RENDER_BYTES:
            return rendered
        # If even the weekly set overflows, fall through to compact fail-closed.

    # Stage 2 — Layer A: microcompact the whole window.
    rendered = _render_full(block, compact=True)
    if len(rendered.encode("utf-8")) <= _MAX_RENDER_BYTES:
        return rendered

    # Stage 3 — Layer B: deterministic saliency keep, packed greedily under the cap.
    kept_bars, kept_ids = _saliency_keep(
        block.ohlcv_60d, block.citation_ids, max_bytes=_MAX_RENDER_BYTES, block=block
    )
    rendered = _render_full(_with_bars(block, kept_bars, kept_ids), compact=True)
    if len(rendered.encode("utf-8")) <= _MAX_RENDER_BYTES:
        return rendered

    # Stage 4 — fail-closed: latest-N bars only. Preamble is sacred; drop data, not it.
    tail_n = min(_RECENCY_KEEP, len(block.ohlcv_60d))
    tail_bars = block.ohlcv_60d[-tail_n:]
    tail_ids = block.citation_ids[-tail_n:]
    return _render_full(_with_bars(block, tail_bars, tail_ids), compact=True)


def _with_bars(
    block: GroundTruthBlock, bars: list[Bar], ids: list[str]
) -> GroundTruthBlock:
    """Return a copy of *block* with replaced bars/ids (other fields preserved)."""
    return GroundTruthBlock(
        symbol=block.symbol,
        asof=block.asof,
        ohlcv_60d=bars,
        current_quote=block.current_quote,
        citation_ids=ids,
        context_summary=block.context_summary,
    )


def _fmt_volume_compact(volume: float) -> str:
    """Compact, deterministic volume shorthand: 1_000_000 -> '1.00M', 12_345 -> '12.35k'.

    Layout-only: volume is not a citeable close-price token, so abbreviating it does
    not affect ClaimVerifier substring tracing of price/return/ratio claims.
    """
    av = abs(volume)
    if av >= 1_000_000_000:
        return f"{volume / 1_000_000_000:.2f}B"
    if av >= 1_000_000:
        return f"{volume / 1_000_000:.2f}M"
    if av >= 1_000:
        return f"{volume / 1_000:.2f}k"
    return f"{volume:.0f}"


def _render_full(block: GroundTruthBlock, *, compact: bool = False) -> str:
    """Produce the complete prompt section for a block (no trimming).

    Parameters
    ----------
    compact : if True, emit Layer-A microcompact OHLCV rows — no 80-char ruler,
              tighter column widths, ``k``/``M``/``B`` volume shorthand. Close
              prices KEEP their exact ``:.4f`` token (verifier-coupled) so every
              cited number remains substring-traceable. Layout-only change.
    """
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

    # OHLCV table
    if block.ohlcv_60d:
        if compact:
            # Layer-A microcompact: tighter layout, no ruler, volume shorthand.
            # Close keeps :.4f (verifier-coupled). Other OHLC kept at :.4f too so
            # high/low range claims remain traceable; only whitespace/ruler shrink.
            lines.append("Date        O/H/L/C  Vol  Citation ID")
            for bar, cid in zip(block.ohlcv_60d, block.citation_ids, strict=True):
                lines.append(_compact_row(bar, cid))
        else:
            lines.append(
                f"{'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>12}  Citation ID"
            )
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
# Deterministic context compaction (v0.2) — NO LLM, NO lookahead
# ---------------------------------------------------------------------------


def _saliency_keep(
    bars: list[Bar],
    citation_ids: list[str],
    *,
    max_bytes: int = _MAX_RENDER_BYTES,
    block: GroundTruthBlock | None = None,
) -> tuple[list[Bar], list[str]]:
    """Keep the highest-saliency rows that fit under *max_bytes*, deterministically.

    Layer B of the v0.2 compaction (honest replacement for the misattributed
    "semantic dedup + information-theoretic truncation" LLM layers — there is no
    model-free information-theoretic compression, so this is a deterministic
    saliency *proxy*, NOT Shannon self-information).

    Saliency (fixed weights, no RNG, no wall-clock)::

        saliency(bar_i) = _W_RETURN * |close_i - close_{i-1}| / close_{i-1}
                        + _W_RANGE  * (high_i - low_i) / close_i
                        + _W_VOLZ   * z(volume_i)   over the window mean/std

    No-lookahead: the return term uses ``close_{i-1}`` (a strictly-earlier bar
    already inside the as-of window); the volume-z uses the full passed window's
    mean/std — and that window has already been sliced to ``<= asof`` by the
    builder, so no future bar is ever referenced.

    Force-kept rows (always survive, regardless of saliency):
      - the latest ``_RECENCY_KEEP`` bars (recency invariant; a test invariant), and
      - the window min-close and max-close bars, so ``window_low``/``window_high``
        range claims stay substring-traceable by the ClaimVerifier.

    Packing is greedy by descending saliency; ties break by date descending. Rows
    are added until the *next* candidate would push the compact render past
    *max_bytes*. Output is returned in ascending date order.
    """
    if not bars:
        return bars, citation_ids

    n = len(bars)
    indices = list(range(n))

    # --- forced-keep set (recency + min/max-close) -------------------------
    forced: set[int] = set(range(max(0, n - _RECENCY_KEEP), n))
    closes = [b.close for b in bars]
    forced.add(min(indices, key=lambda i: (closes[i], i)))   # min-close (stable)
    forced.add(max(indices, key=lambda i: (closes[i], -i)))  # max-close (stable)

    # --- saliency scores (no-lookahead) ------------------------------------
    vols = [b.volume for b in bars]
    vol_mean = statistics.mean(vols) if vols else 0.0
    vol_std = statistics.pstdev(vols) if len(vols) > 1 else 0.0

    def _saliency(i: int) -> float:
        bar = bars[i]
        ret = 0.0
        if i > 0 and closes[i - 1]:  # close_{i-1}: strictly earlier, in-window
            ret = abs(closes[i] - closes[i - 1]) / abs(closes[i - 1])
        rng = ((bar.high - bar.low) / bar.close) if bar.close else 0.0
        volz = ((vols[i] - vol_mean) / vol_std) if vol_std else 0.0
        return _W_RETURN * ret + _W_RANGE * rng + _W_VOLZ * abs(volz)

    # Candidate (non-forced) rows ranked by saliency desc, then date desc (stable).
    candidates = sorted(
        (i for i in indices if i not in forced),
        key=lambda i: (-_saliency(i), -i),
    )

    # --- greedy pack under byte cap ----------------------------------------
    # Exact incremental byte accounting: _render_full joins lines with "\n", so a
    # render with row-index set S costs `_bytes_for(forced) + sum over the extra
    # rows of (1 + len(row_line))`. We measure the forced-only render once, then
    # each additional data row adds exactly `1 + len(compact_row.encode())` bytes.
    # Deterministic and O(n log n) — no per-trial re-render.
    ref = block if block is not None else _MINIMAL_REF
    row_cost = [1 + len(_compact_row(bars[i], citation_ids[i]).encode("utf-8")) for i in indices]

    kept: set[int] = set(forced)
    used = _bytes_for(ref, sorted(kept), bars, citation_ids)

    for i in candidates:
        if used + row_cost[i] <= max_bytes:
            kept.add(i)
            used += row_cost[i]
        # else: skip this row but keep trying smaller-saliency rows that may fit.

    sel = sorted(kept)
    return [bars[i] for i in sel], [citation_ids[i] for i in sel]


def _compact_row(bar: Bar, cid: str) -> str:
    """The single compact OHLCV row line (must match _render_full compact mode)."""
    return (
        f"{bar.date_str} "
        f"{bar.open:.4f}/{bar.high:.4f}/{bar.low:.4f}/{bar.close:.4f} "
        f"{_fmt_volume_compact(bar.volume)} [{cid}]"
    )


def _bytes_for(
    ref: GroundTruthBlock, sel: list[int], bars: list[Bar], citation_ids: list[str]
) -> int:
    """Exact UTF-8 byte length of the compact render of *ref* over row indices *sel*."""
    sub_bars = [bars[i] for i in sel]
    sub_ids = [citation_ids[i] for i in sel]
    rendered = _render_full(_with_bars(ref, sub_bars, sub_ids), compact=True)
    return len(rendered.encode("utf-8"))


# Minimal reference block for byte-accounting when no full block is supplied.
_MINIMAL_REF = GroundTruthBlock(
    symbol="REF",
    asof="2026-01-01",
    ohlcv_60d=[],
    current_quote={},
    citation_ids=[],
    context_summary={},
)


# ---------------------------------------------------------------------------
# Legacy weekly trim — v0.1 policy (reachable via _TRIM_POLICY == "weekly")
# ---------------------------------------------------------------------------


def _trim_to_high_info_rows(
    bars: list[Bar], citation_ids: list[str]
) -> tuple[list[Bar], list[str]]:
    """Trim OHLCV to highest-information rows (legacy v0.1 weekly policy).

    Kept reachable via ``_TRIM_POLICY == "weekly"`` for one release so a downstream
    analyst regression caused by the v0.2 saliency-keep is attributable; remove once
    v0.2 has soaked. The v0.2 default path (``render_for_prompt``) uses
    ``_saliency_keep`` instead — this function is no longer on the default path.

    Policy:
      - Keep the latest 5 trading days (maximum recency signal).
      - Keep one bar per calendar week for the prior 8 weeks
        (the last bar of each ISO week = Friday close or last available).
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
