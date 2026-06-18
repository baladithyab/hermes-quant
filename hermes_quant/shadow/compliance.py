"""hermes_quant.shadow.compliance — Human-compliance telemetry + shadow-vs-real pairing.

ADR-0096 Gate 2.

The ADR-0049 shadow ledger drives the UNFILTERED (pre-HITL) view.  After each
trading session, the /retro surface needs to quantify *the human's alpha/cost*:

    shadow  = what the system proposed (gate-sized, pre-HITL)
    real    = what the human actually approved and filled

GAP = shadow_realized_return − real_realized_return

    positive gap  →  human filtering left money on the table
    negative gap  →  human filtering improved outcomes (rejected bad trades)

Public surface
--------------
    from hermes_quant.shadow.compliance import (
        ShadowDecisionRecord,
        RealFillRecord,
        ComplianceTelemetry,
        compute_compliance_telemetry,
        MIN_SAMPLE,
    )

Design constraints
------------------
- PURE given the inputs — read-only over the two ledgers, no DB writes.
- DEFAULT-OFF / ADDITIVE — nothing auto-runs this module.  A future /retro
  flag gates the call.
- Finite-guard throughout: NaN/inf in a numeric field → treat as missing
  rather than fabricate a rate.
- Thin-sample → None/flagged, never a fabricated rate (ar08 family posture).
- Forward-only honest numbers: the pairing is by matching shadow decisions to
  their corresponding real fills on the same ticker+direction+day, so we are
  measuring a real causal pair, not cherry-picking a biased sub-sample.

Pairing logic
-------------
A shadow decision and a real fill are PAIRED when:
    - same ticker (case-insensitive)
    - same direction ("buy"/"sell")
    - same calendar date (UTC)

If a shadow decision has no matching real fill it was REJECTED by the human.
If a real fill has no matching shadow decision it was a human-initiated trade
(out-of-scope for this analysis; counted but not included in the gap).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thin-sample sentinel
# ---------------------------------------------------------------------------

#: Minimum number of shadow decisions required before any rate is meaningful.
#: Below this, approval_rate and related derived metrics return None.
MIN_SAMPLE: int = 10


# ---------------------------------------------------------------------------
# Input records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowDecisionRecord:
    """A single gate-approved (pre-HITL) shadow decision.

    Parameters
    ----------
    ticker:
        Asset symbol (stored upper-cased).
    direction:
        ``"buy"`` or ``"sell"``.
    asof:
        UTC datetime of the gate approval (the DECISION timestamp, not fill).
    size_fraction:
        Fraction of equity the gate proposed to commit (0.0–1.0).
    signal_type:
        Categorical label for the signal class (e.g. ``"semantic"``,
        ``"trend_following"``, ``"sentiment"``, ``""`` if unknown).
    conviction:
        Numerical conviction score as emitted by the gate (vote_share or
        equivalent, 0.0–1.0).  NaN / inf are stored as-is and excluded from
        aggregation.
    shadow_realized_return:
        Realized return fraction if the trade was simulated in the shadow
        account (exit_price / entry_price − 1, signed).  ``None`` if not yet
        closed.
    """

    ticker: str
    direction: str
    asof: datetime
    size_fraction: float = 0.0
    signal_type: str = ""
    conviction: float = float("nan")
    shadow_realized_return: Optional[float] = None

    def __post_init__(self) -> None:
        # Normalize ticker to uppercase.
        object.__setattr__(self, "ticker", self.ticker.upper() if self.ticker else "UNKNOWN")
        # Normalize direction.
        low = (self.direction or "").lower()
        object.__setattr__(self, "direction", low if low in ("buy", "sell") else "unknown")

    @property
    def utc_date(self) -> date:
        """Calendar date (UTC) of this decision."""
        if self.asof.tzinfo is None:
            return self.asof.date()
        return self.asof.astimezone(timezone.utc).date()


@dataclass(frozen=True)
class RealFillRecord:
    """A single human-approved + executed real fill.

    Parameters
    ----------
    ticker:
        Asset symbol (stored upper-cased).
    direction:
        ``"buy"`` or ``"sell"``.
    fill_time:
        UTC datetime of the actual execution.
    fill_price:
        Execution price (USD per share, or equivalent).  Non-positive → None.
    fill_size_fraction:
        Fraction of equity committed by the human (may differ from the gate
        proposal).  NaN / negative → None.
    real_realized_return:
        Realized return fraction if the fill has been closed.  ``None`` if
        the position is still open.
    """

    ticker: str
    direction: str
    fill_time: datetime
    fill_price: Optional[float] = None
    fill_size_fraction: Optional[float] = None
    real_realized_return: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", self.ticker.upper() if self.ticker else "UNKNOWN")
        low = (self.direction or "").lower()
        object.__setattr__(self, "direction", low if low in ("buy", "sell") else "unknown")
        # Sanitize fill_price.
        fp = self.fill_price
        if fp is not None and (not math.isfinite(fp) or fp <= 0):
            object.__setattr__(self, "fill_price", None)
        # Sanitize fill_size_fraction.
        fsf = self.fill_size_fraction
        if fsf is not None and (not math.isfinite(fsf) or fsf < 0):
            object.__setattr__(self, "fill_size_fraction", None)

    @property
    def utc_date(self) -> date:
        """Calendar date (UTC) of this fill."""
        if self.fill_time.tzinfo is None:
            return self.fill_time.date()
        return self.fill_time.astimezone(timezone.utc).date()


# ---------------------------------------------------------------------------
# Per-pair analysis
# ---------------------------------------------------------------------------


@dataclass
class PairedDecision:
    """A matched shadow–real pair.

    A pair is formed by matching a ShadowDecisionRecord to a RealFillRecord on
    (ticker, direction, utc_date).  One-to-one: the first matching fill wins
    and is removed from the pool so a single fill does not pair twice.
    """

    shadow: ShadowDecisionRecord
    real: Optional[RealFillRecord]  # None = rejected (no matching fill)

    @property
    def was_approved(self) -> bool:
        """True if the human executed a matching fill."""
        return self.real is not None

    @property
    def fill_delay_seconds(self) -> Optional[float]:
        """Seconds from decision to fill execution.  None if rejected or
        fill_time is unavailable / before the decision (guard: non-negative).
        """
        if self.real is None:
            return None
        dt = (self.real.fill_time - self.shadow.asof).total_seconds()
        if not math.isfinite(dt) or dt < 0:
            return None
        return dt

    @property
    def size_ratio(self) -> Optional[float]:
        """real_fill_size / shadow_size_fraction.  < 1.0 → human downsized.
        None if either side is unavailable or the shadow fraction is zero.
        """
        if self.real is None or self.real.fill_size_fraction is None:
            return None
        shadow_frac = self.shadow.size_fraction
        if not math.isfinite(shadow_frac) or shadow_frac <= 0:
            return None
        real_frac = self.real.fill_size_fraction
        if not math.isfinite(real_frac) or real_frac < 0:
            return None
        return real_frac / shadow_frac

    @property
    def return_gap(self) -> Optional[float]:
        """shadow_realized_return − real_realized_return.
        Positive means the shadow book did better (human filtering cost alpha).
        None if either leg has no closed return.
        """
        s = self.shadow.shadow_realized_return
        r = self.real.real_realized_return if self.real else None
        if s is None or r is None:
            return None
        if not math.isfinite(s) or not math.isfinite(r):
            return None
        return s - r


# ---------------------------------------------------------------------------
# Approval-rate bucket
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRateBucket:
    """Approval rate and count for a discrete slice of the decision space.

    A rate is only computed when ``n_decisions >= MIN_SAMPLE``; otherwise
    ``approval_rate`` is ``None`` (fail-closed: we do not fabricate a rate
    from noise).

    Parameters
    ----------
    label:
        Human-readable slice label (e.g. ``"signal_type=semantic"``).
    n_decisions:
        Total shadow decisions in this bucket.
    n_approved:
        Number matched to a real fill.
    approval_rate:
        ``n_approved / n_decisions`` when ``n_decisions >= MIN_SAMPLE``,
        else ``None``.
    """

    label: str
    n_decisions: int
    n_approved: int
    approval_rate: Optional[float]  # None → thin sample

    @classmethod
    def build(cls, label: str, n_decisions: int, n_approved: int) -> "ApprovalRateBucket":
        if n_decisions < MIN_SAMPLE:
            rate: Optional[float] = None
        else:
            rate = n_approved / n_decisions
        return cls(
            label=label,
            n_decisions=n_decisions,
            n_approved=n_approved,
            approval_rate=rate,
        )


# ---------------------------------------------------------------------------
# Performance gap
# ---------------------------------------------------------------------------


@dataclass
class PerformanceGap:
    """Shadow-vs-real aggregate performance comparison.

    Only includes PAIRED trades where both shadow and real have a closed
    realized return.  An empty/thin paired set → all fields are ``None``.

    Parameters
    ----------
    n_pairs_with_returns:
        Number of pairs where both sides have a realized return.
    mean_shadow_return:
        Mean realized return of the shadow leg (all paired trades).
    mean_real_return:
        Mean realized return of the real leg.
    mean_gap:
        ``mean_shadow_return − mean_real_return``.  Positive = shadow beat
        real on average (human filtering cost alpha).  None on thin sample.
    shadow_win_rate:
        Fraction of pairs where ``shadow_realized_return > real_realized_return``.
        None on thin sample.
    """

    n_pairs_with_returns: int
    mean_shadow_return: Optional[float]
    mean_real_return: Optional[float]
    mean_gap: Optional[float]
    shadow_win_rate: Optional[float]

    @classmethod
    def from_pairs(cls, pairs: list[PairedDecision]) -> "PerformanceGap":
        gaps = [p.return_gap for p in pairs if p.return_gap is not None]
        n = len(gaps)
        if n == 0:
            return cls(
                n_pairs_with_returns=0,
                mean_shadow_return=None,
                mean_real_return=None,
                mean_gap=None,
                shadow_win_rate=None,
            )

        shadow_returns = [
            p.shadow.shadow_realized_return
            for p in pairs
            if p.return_gap is not None
        ]
        real_returns = [
            p.real.real_realized_return  # type: ignore[union-attr]
            for p in pairs
            if p.return_gap is not None
        ]

        mean_shadow = sum(shadow_returns) / n
        mean_real = sum(real_returns) / n
        mean_gap = mean_shadow - mean_real

        if n < MIN_SAMPLE:
            shadow_win_rate: Optional[float] = None
            mean_gap_out: Optional[float] = None
            mean_shadow_out: Optional[float] = None
            mean_real_out: Optional[float] = None
        else:
            shadow_win_rate = sum(1 for g in gaps if g > 0) / n
            mean_gap_out = mean_gap
            mean_shadow_out = mean_shadow
            mean_real_out = mean_real

        return cls(
            n_pairs_with_returns=n,
            mean_shadow_return=mean_shadow_out,
            mean_real_return=mean_real_out,
            mean_gap=mean_gap_out,
            shadow_win_rate=shadow_win_rate,
        )


# ---------------------------------------------------------------------------
# Top-level telemetry result
# ---------------------------------------------------------------------------


@dataclass
class ComplianceTelemetry:
    """Full human-compliance telemetry report for a session.

    Parameters
    ----------
    n_shadow_decisions:
        Total shadow decisions passed in.
    n_real_fills:
        Total real fills passed in.
    n_paired:
        Decisions matched to a real fill (approved).
    n_rejected:
        Decisions with no matching fill (rejected by human).
    n_human_initiated:
        Real fills with no matching shadow decision.
    overall_approval_rate:
        ``n_paired / n_shadow_decisions`` when ``n_shadow_decisions >= MIN_SAMPLE``,
        else ``None``.
    by_signal_type:
        Approval rate breakdown by ``signal_type`` field.
    by_direction:
        Approval rate breakdown by ``direction`` (``"buy"`` / ``"sell"``).
    by_conviction_band:
        Approval rate breakdown by conviction quartile:
        ``"high"`` (> 0.75), ``"mid"`` (0.5–0.75), ``"low"`` (< 0.5), ``"unknown"``.
    fill_delay_seconds:
        Sorted list of fill-delay values (seconds from decision to fill) for
        all approved pairs.  Empty list if no delays are available.
    fill_delay_p50:
        Median fill delay (seconds).  ``None`` if fewer than MIN_SAMPLE delays.
    fill_delay_p95:
        95th-percentile fill delay.  ``None`` if fewer than MIN_SAMPLE delays.
    down_size_frequency:
        Fraction of approved pairs where ``size_ratio < 1.0`` (human downsized
        vs gate proposal).  ``None`` if fewer than MIN_SAMPLE approved pairs.
    performance_gap:
        Shadow-vs-real return gap analysis (only meaningful for closed trades).
    thin_sample_warning:
        ``True`` when the overall sample is below ``MIN_SAMPLE``.
    """

    n_shadow_decisions: int
    n_real_fills: int
    n_paired: int
    n_rejected: int
    n_human_initiated: int
    overall_approval_rate: Optional[float]
    by_signal_type: list[ApprovalRateBucket]
    by_direction: list[ApprovalRateBucket]
    by_conviction_band: list[ApprovalRateBucket]
    fill_delay_seconds: list[float]
    fill_delay_p50: Optional[float]
    fill_delay_p95: Optional[float]
    down_size_frequency: Optional[float]
    performance_gap: PerformanceGap
    thin_sample_warning: bool

    def to_dict(self) -> dict:
        return {
            "n_shadow_decisions": self.n_shadow_decisions,
            "n_real_fills": self.n_real_fills,
            "n_paired": self.n_paired,
            "n_rejected": self.n_rejected,
            "n_human_initiated": self.n_human_initiated,
            "overall_approval_rate": self.overall_approval_rate,
            "by_signal_type": [
                {
                    "label": b.label,
                    "n_decisions": b.n_decisions,
                    "n_approved": b.n_approved,
                    "approval_rate": b.approval_rate,
                }
                for b in self.by_signal_type
            ],
            "by_direction": [
                {
                    "label": b.label,
                    "n_decisions": b.n_decisions,
                    "n_approved": b.n_approved,
                    "approval_rate": b.approval_rate,
                }
                for b in self.by_direction
            ],
            "by_conviction_band": [
                {
                    "label": b.label,
                    "n_decisions": b.n_decisions,
                    "n_approved": b.n_approved,
                    "approval_rate": b.approval_rate,
                }
                for b in self.by_conviction_band
            ],
            "fill_delay_seconds": self.fill_delay_seconds,
            "fill_delay_p50": self.fill_delay_p50,
            "fill_delay_p95": self.fill_delay_p95,
            "down_size_frequency": self.down_size_frequency,
            "performance_gap": {
                "n_pairs_with_returns": self.performance_gap.n_pairs_with_returns,
                "mean_shadow_return": self.performance_gap.mean_shadow_return,
                "mean_real_return": self.performance_gap.mean_real_return,
                "mean_gap": self.performance_gap.mean_gap,
                "shadow_win_rate": self.performance_gap.shadow_win_rate,
            },
            "thin_sample_warning": self.thin_sample_warning,
        }


# ---------------------------------------------------------------------------
# Pairing engine
# ---------------------------------------------------------------------------


def _pair_decisions(
    shadow_decisions: list[ShadowDecisionRecord],
    real_fills: list[RealFillRecord],
) -> tuple[list[PairedDecision], list[RealFillRecord]]:
    """Match shadow decisions to real fills on (ticker, direction, utc_date).

    Matching is greedy: for each shadow decision (in order), we consume the
    FIRST available real fill that shares the same (ticker, direction, date).
    A fill is consumed at most once.

    Returns
    -------
    (pairs, unmatched_fills)
    """
    # Build a mutable pool of fills keyed by (ticker, direction, date).
    from collections import defaultdict

    pool: dict[tuple[str, str, date], list[RealFillRecord]] = defaultdict(list)
    for fill in real_fills:
        key = (fill.ticker, fill.direction, fill.utc_date)
        pool[key].append(fill)

    pairs: list[PairedDecision] = []
    for decision in shadow_decisions:
        key = (decision.ticker, decision.direction, decision.utc_date)
        bucket = pool.get(key)
        if bucket:
            matched_fill = bucket.pop(0)  # FIFO within the same bucket
            if not bucket:
                del pool[key]
            pairs.append(PairedDecision(shadow=decision, real=matched_fill))
        else:
            pairs.append(PairedDecision(shadow=decision, real=None))

    # Collect remaining unmatched fills (human-initiated, no shadow counterpart).
    unmatched: list[RealFillRecord] = [
        fill for fills in pool.values() for fill in fills
    ]
    return pairs, unmatched


# ---------------------------------------------------------------------------
# Approval-rate slicing
# ---------------------------------------------------------------------------


def _slice_by(
    pairs: list[PairedDecision],
    key_fn: "Callable[[ShadowDecisionRecord], str]",  # noqa: F821
) -> list[ApprovalRateBucket]:
    """Group paired decisions by a key function and compute per-bucket rates."""
    from collections import defaultdict

    groups: dict[str, list[bool]] = defaultdict(list)
    for pair in pairs:
        k = key_fn(pair.shadow)
        groups[k].append(pair.was_approved)

    buckets: list[ApprovalRateBucket] = []
    for label in sorted(groups):
        approved_flags = groups[label]
        n_decisions = len(approved_flags)
        n_approved = sum(approved_flags)
        buckets.append(ApprovalRateBucket.build(label, n_decisions, n_approved))
    return buckets


def _conviction_band(record: ShadowDecisionRecord) -> str:
    """Map conviction score to a named band."""
    c = record.conviction
    if not math.isfinite(c):
        return "unknown"
    if c > 0.75:
        return "high"
    if c >= 0.5:
        return "mid"
    return "low"


# ---------------------------------------------------------------------------
# Fill-delay quantiles
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear interpolation percentile on a pre-sorted list (0–100)."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("empty list")
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_values[-1]
    frac = idx - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_compliance_telemetry(
    shadow_decisions: list[ShadowDecisionRecord],
    real_fills: list[RealFillRecord],
) -> ComplianceTelemetry:
    """Compute human-compliance telemetry from shadow decisions and real fills.

    This function is PURE — it reads the two input lists and returns a
    ComplianceTelemetry.  It does not write to any database, file, or external
    service.

    Parameters
    ----------
    shadow_decisions:
        Gate-approved (pre-HITL) decisions from the shadow ledger.
        Must not be ``None``; may be empty.
    real_fills:
        Human-approved + executed fills from the real book.
        Must not be ``None``; may be empty.

    Returns
    -------
    ComplianceTelemetry
        All rate fields are ``None`` when the sample is below ``MIN_SAMPLE``.
        ``thin_sample_warning`` is set when ``len(shadow_decisions) < MIN_SAMPLE``.

    Notes
    -----
    - Forward-only honest numbers: pairing is on (ticker, direction, date).
      A shadow decision rejected by the human is counted as rejected; we do
      not re-classify it.  The performance gap compares same-trade returns
      only (matched pairs with closed positions on both sides).
    - Thin-sample posture: any sub-bucket with fewer than ``MIN_SAMPLE``
      entries returns ``approval_rate = None`` — never a fabricated rate.
    - Finite-guard: NaN / inf in numeric fields are excluded from averages
      and flagged as ``"unknown"`` in the conviction band.
    """
    n_shadow = len(shadow_decisions)
    n_real = len(real_fills)
    thin = n_shadow < MIN_SAMPLE

    # Pair decisions to fills.
    pairs, unmatched_fills = _pair_decisions(shadow_decisions, real_fills)

    n_paired = sum(1 for p in pairs if p.was_approved)
    n_rejected = sum(1 for p in pairs if not p.was_approved)
    n_human_initiated = len(unmatched_fills)

    # Overall approval rate.
    if thin:
        overall_rate: Optional[float] = None
    else:
        overall_rate = n_paired / n_shadow if n_shadow > 0 else None

    # By signal type.
    by_signal_type = _slice_by(pairs, lambda r: r.signal_type or "unknown")

    # By direction.
    by_direction = _slice_by(pairs, lambda r: r.direction)

    # By conviction band.
    by_conviction_band = _slice_by(pairs, _conviction_band)

    # Fill delay distribution (only for approved pairs).
    delays: list[float] = []
    for pair in pairs:
        d = pair.fill_delay_seconds
        if d is not None:
            delays.append(d)
    delays.sort()

    if len(delays) >= MIN_SAMPLE:
        p50: Optional[float] = _percentile(delays, 50)
        p95: Optional[float] = _percentile(delays, 95)
    else:
        p50 = None
        p95 = None

    # Down-size frequency: approved pairs where human used < gate-proposed size.
    approved_pairs = [p for p in pairs if p.was_approved]
    size_ratios = [p.size_ratio for p in approved_pairs if p.size_ratio is not None]
    if len(approved_pairs) >= MIN_SAMPLE and size_ratios:
        down_size_freq: Optional[float] = sum(1 for r in size_ratios if r < 1.0) / len(
            approved_pairs
        )
    else:
        down_size_freq = None

    # Performance gap.
    perf_gap = PerformanceGap.from_pairs(pairs)

    return ComplianceTelemetry(
        n_shadow_decisions=n_shadow,
        n_real_fills=n_real,
        n_paired=n_paired,
        n_rejected=n_rejected,
        n_human_initiated=n_human_initiated,
        overall_approval_rate=overall_rate,
        by_signal_type=by_signal_type,
        by_direction=by_direction,
        by_conviction_band=by_conviction_band,
        fill_delay_seconds=delays,
        fill_delay_p50=p50,
        fill_delay_p95=p95,
        down_size_frequency=down_size_freq,
        performance_gap=perf_gap,
        thin_sample_warning=thin,
    )
