"""tests/shadow/test_compliance.py — ComplianceTelemetry unit tests.

ADR-0096 Gate 2 / ag02 lane.

Tests:
  - approval_rate computed correctly (matched pairs / total shadow decisions)
  - the performance gap reflects a known shadow-beats-real fixture
  - thin sample fails-closed (None on rates, thin_sample_warning=True)
  - by_signal_type / by_direction / by_conviction_band slicing
  - fill_delay_distribution and quantiles
  - down_size_frequency
  - n_human_initiated counts fills with no matching shadow decision
  - rejected decision counted when no real fill exists
  - NaN / inf conviction → "unknown" band, never crashes or fabricates
  - compute_compliance_telemetry returns ComplianceTelemetry type
  - to_dict round-trip: all expected keys present
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from hermes_quant.shadow.compliance import (
    MIN_SAMPLE,
    ApprovalRateBucket,
    ComplianceTelemetry,
    PerformanceGap,
    RealFillRecord,
    ShadowDecisionRecord,
    compute_compliance_telemetry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(days=1)


def _shadow(
    ticker: str = "AAPL",
    direction: str = "buy",
    asof: datetime = _T0,
    size_fraction: float = 0.10,
    signal_type: str = "semantic",
    conviction: float = 0.8,
    shadow_return: Optional[float] = None,
) -> ShadowDecisionRecord:
    return ShadowDecisionRecord(
        ticker=ticker,
        direction=direction,
        asof=asof,
        size_fraction=size_fraction,
        signal_type=signal_type,
        conviction=conviction,
        shadow_realized_return=shadow_return,
    )


def _fill(
    ticker: str = "AAPL",
    direction: str = "buy",
    fill_time: datetime = _T1,
    fill_price: Optional[float] = 150.0,
    fill_size_fraction: Optional[float] = 0.10,
    real_return: Optional[float] = None,
) -> RealFillRecord:
    return RealFillRecord(
        ticker=ticker,
        direction=direction,
        fill_time=fill_time,
        fill_price=fill_price,
        fill_size_fraction=fill_size_fraction,
        real_realized_return=real_return,
    )


def _full_shadow_set(
    n: int,
    direction: str = "buy",
    signal_type: str = "semantic",
    conviction: float = 0.8,
    shadow_return: Optional[float] = 0.02,
) -> list[ShadowDecisionRecord]:
    """Build n shadow decisions across distinct tickers to avoid key collision."""
    base = datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
    return [
        _shadow(
            ticker=f"SYM{i:04d}",
            direction=direction,
            asof=base + timedelta(minutes=i),
            signal_type=signal_type,
            conviction=conviction,
            shadow_return=shadow_return,
        )
        for i in range(n)
    ]


def _full_fill_set(
    shadows: list[ShadowDecisionRecord],
    fill_offset_seconds: float = 30.0,
    real_return: Optional[float] = 0.01,
    size_fraction: Optional[float] = 0.10,
) -> list[RealFillRecord]:
    """Build matching fills for every shadow decision in the list."""
    return [
        _fill(
            ticker=s.ticker,
            direction=s.direction,
            fill_time=s.asof + timedelta(seconds=fill_offset_seconds),
            real_return=real_return,
            fill_size_fraction=size_fraction,
        )
        for s in shadows
    ]


# ---------------------------------------------------------------------------
# Thin-sample: fails-closed
# ---------------------------------------------------------------------------


class TestThinSample:
    def test_empty_inputs_fails_closed(self):
        ct = compute_compliance_telemetry([], [])
        assert ct.overall_approval_rate is None
        assert ct.thin_sample_warning is True
        assert ct.fill_delay_p50 is None
        assert ct.fill_delay_p95 is None
        assert ct.down_size_frequency is None
        assert ct.performance_gap.mean_gap is None
        assert ct.performance_gap.shadow_win_rate is None

    def test_one_decision_is_thin(self):
        shadows = [_shadow()]
        fills = [_fill()]
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.thin_sample_warning is True
        assert ct.overall_approval_rate is None

    def test_min_sample_minus_1_is_thin(self):
        n = MIN_SAMPLE - 1
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.thin_sample_warning is True
        assert ct.overall_approval_rate is None

    def test_min_sample_exactly_is_not_thin(self):
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.thin_sample_warning is False
        assert ct.overall_approval_rate is not None


# ---------------------------------------------------------------------------
# Approval-rate correctness
# ---------------------------------------------------------------------------


class TestApprovalRate:
    def test_full_approval_rate_one_point(self):
        """With enough samples, 100% approval when all shadows have a fill."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.overall_approval_rate == pytest.approx(1.0)
        assert ct.n_paired == n
        assert ct.n_rejected == 0

    def test_zero_approval_rate(self):
        """No fills at all → 0% approval rate (all rejected)."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        ct = compute_compliance_telemetry(shadows, [])
        assert ct.overall_approval_rate == pytest.approx(0.0)
        assert ct.n_rejected == n
        assert ct.n_paired == 0

    def test_partial_approval_rate(self):
        """Half the decisions have a matching fill → 50% approval."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        # Only provide fills for the first half.
        fills = _full_fill_set(shadows[: n // 2])
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.overall_approval_rate == pytest.approx(0.5)
        assert ct.n_paired == n // 2
        assert ct.n_rejected == n - n // 2

    def test_direction_mismatch_does_not_pair(self):
        """A fill with opposite direction must NOT match the shadow decision."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, direction="buy")
        # Build fills with opposite direction — should not match.
        fills = [
            _fill(
                ticker=s.ticker,
                direction="sell",  # ← wrong direction
                fill_time=s.asof + timedelta(seconds=30),
            )
            for s in shadows
        ]
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.n_paired == 0
        assert ct.n_rejected == n
        # All fills are unmatched → human-initiated.
        assert ct.n_human_initiated == n

    def test_date_mismatch_does_not_pair(self):
        """A fill on a different calendar date must NOT match."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        # Build fills on the NEXT DAY.
        fills = [
            _fill(
                ticker=s.ticker,
                direction=s.direction,
                fill_time=s.asof + timedelta(days=2),
            )
            for s in shadows
        ]
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.n_paired == 0
        assert ct.n_human_initiated == n

    def test_human_initiated_counted(self):
        """Extra fills with no matching shadow are counted as human-initiated."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows)
        # Add 3 extra fills that have no shadow counterpart.
        extra = [
            _fill(ticker="ZZZ1", direction="buy", fill_time=_T1),
            _fill(ticker="ZZZ2", direction="sell", fill_time=_T1),
            _fill(ticker="ZZZ3", direction="buy", fill_time=_T1),
        ]
        ct = compute_compliance_telemetry(shadows, fills + extra)
        assert ct.n_human_initiated == 3
        assert ct.n_paired == n


# ---------------------------------------------------------------------------
# By-signal-type / by-direction / by-conviction-band slices
# ---------------------------------------------------------------------------


class TestApprovalRateBreakdown:
    def test_by_signal_type_correct_labels(self):
        n = MIN_SAMPLE
        # Half semantic, half trend_following.
        shadows_s = _full_shadow_set(n, signal_type="semantic")
        shadows_t = [
            _shadow(
                ticker=f"TRD{i:04d}",
                direction="buy",
                asof=_T0 + timedelta(minutes=i),
                signal_type="trend_following",
            )
            for i in range(n)
        ]
        # All semantics approved, none of the trend-following approved.
        fills = _full_fill_set(shadows_s)
        ct = compute_compliance_telemetry(shadows_s + shadows_t, fills)
        labels = {b.label for b in ct.by_signal_type}
        assert "semantic" in labels
        assert "trend_following" in labels

    def test_by_direction_buy_sell(self):
        n = MIN_SAMPLE
        shadows_buy = _full_shadow_set(n, direction="buy")
        shadows_sell = [
            _shadow(ticker=f"SL{i:04d}", direction="sell", asof=_T0 + timedelta(minutes=i))
            for i in range(n)
        ]
        fills_buy = _full_fill_set(shadows_buy)
        fills_sell = _full_fill_set(shadows_sell)
        ct = compute_compliance_telemetry(shadows_buy + shadows_sell, fills_buy + fills_sell)
        direction_labels = {b.label for b in ct.by_direction}
        assert "buy" in direction_labels
        assert "sell" in direction_labels
        for b in ct.by_direction:
            if b.label == "buy":
                assert b.approval_rate == pytest.approx(1.0)
            elif b.label == "sell":
                assert b.approval_rate == pytest.approx(1.0)

    def test_by_conviction_band_high(self):
        """High-conviction decisions (conviction > 0.75) → 'high' band."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, conviction=0.9)
        fills = _full_fill_set(shadows)
        ct = compute_compliance_telemetry(shadows, fills)
        labels = {b.label for b in ct.by_conviction_band}
        assert "high" in labels
        for b in ct.by_conviction_band:
            if b.label == "high":
                assert b.n_decisions == n

    def test_by_conviction_band_nan_is_unknown(self):
        """NaN conviction → 'unknown' band, no crash."""
        n = MIN_SAMPLE
        shadows = [
            _shadow(
                ticker=f"NAN{i:04d}",
                asof=_T0 + timedelta(minutes=i),
                conviction=float("nan"),
            )
            for i in range(n)
        ]
        fills = _full_fill_set(shadows)
        ct = compute_compliance_telemetry(shadows, fills)
        labels = {b.label for b in ct.by_conviction_band}
        assert "unknown" in labels

    def test_by_conviction_band_inf_is_unknown(self):
        """Inf conviction → 'unknown' band, no crash."""
        n = MIN_SAMPLE
        shadows = [
            _shadow(
                ticker=f"INF{i:04d}",
                asof=_T0 + timedelta(minutes=i),
                conviction=float("inf"),
            )
            for i in range(n)
        ]
        ct = compute_compliance_telemetry(shadows, [])
        labels = {b.label for b in ct.by_conviction_band}
        assert "unknown" in labels


# ---------------------------------------------------------------------------
# Performance gap: shadow-beats-real
# ---------------------------------------------------------------------------


class TestPerformanceGap:
    def test_shadow_beats_real_positive_gap(self):
        """Shadow return 2%, real return 1% → mean_gap ≈ 0.01, shadow_wins > 0."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, shadow_return=0.02)
        fills = _full_fill_set(shadows, real_return=0.01)
        ct = compute_compliance_telemetry(shadows, fills)
        gap = ct.performance_gap
        assert gap.n_pairs_with_returns == n
        assert gap.mean_gap == pytest.approx(0.01, abs=1e-9)
        assert gap.mean_shadow_return == pytest.approx(0.02, abs=1e-9)
        assert gap.mean_real_return == pytest.approx(0.01, abs=1e-9)
        assert gap.shadow_win_rate == pytest.approx(1.0)

    def test_real_beats_shadow_negative_gap(self):
        """Real return > shadow return → negative gap (human filtering added value)."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, shadow_return=0.005)
        fills = _full_fill_set(shadows, real_return=0.02)
        ct = compute_compliance_telemetry(shadows, fills)
        gap = ct.performance_gap
        assert gap.mean_gap is not None
        assert gap.mean_gap < 0
        assert gap.shadow_win_rate == pytest.approx(0.0)

    def test_gap_none_when_no_returns(self):
        """No closed positions → gap metrics are None."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, shadow_return=None)
        fills = _full_fill_set(shadows, real_return=None)
        ct = compute_compliance_telemetry(shadows, fills)
        gap = ct.performance_gap
        assert gap.n_pairs_with_returns == 0
        assert gap.mean_gap is None
        assert gap.shadow_win_rate is None

    def test_gap_thin_sample_no_rate(self):
        """Fewer than MIN_SAMPLE return-pairs → shadow_win_rate and mean_gap are None."""
        # Use fewer return-bearing pairs than MIN_SAMPLE.
        n = MIN_SAMPLE - 1
        shadows = _full_shadow_set(n, shadow_return=0.05)
        fills = _full_fill_set(shadows, real_return=0.01)
        ct = compute_compliance_telemetry(shadows, fills)
        gap = ct.performance_gap
        # n_pairs_with_returns may be n but < MIN_SAMPLE → rates are None.
        assert gap.mean_gap is None
        assert gap.shadow_win_rate is None

    def test_nan_shadow_return_excluded_from_gap(self):
        """NaN shadow_realized_return in one pair must not contaminate the gap."""
        n = MIN_SAMPLE
        # First pair has NaN shadow return → excluded from gap.
        shadows_nan = [
            _shadow(ticker="NANX", asof=_T0 + timedelta(minutes=0), shadow_return=float("nan"))
        ]
        # Remaining n pairs have valid returns.
        shadows_valid = _full_shadow_set(n, shadow_return=0.03)
        all_shadows = shadows_nan + shadows_valid
        fills_nan = [_fill(ticker="NANX", fill_time=_T0 + timedelta(minutes=0, seconds=30), real_return=0.01)]
        fills_valid = _full_fill_set(shadows_valid, real_return=0.01)
        ct = compute_compliance_telemetry(all_shadows, fills_nan + fills_valid)
        gap = ct.performance_gap
        # Gap should be computed from the valid pairs only.
        assert gap.mean_gap == pytest.approx(0.02, abs=1e-9)


# ---------------------------------------------------------------------------
# Fill delay distribution
# ---------------------------------------------------------------------------


class TestFillDelay:
    def test_fill_delay_computed(self):
        """Delays should be the time from decision to fill."""
        n = MIN_SAMPLE
        offset = 60.0  # 1 minute
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows, fill_offset_seconds=offset)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.fill_delay_p50 == pytest.approx(offset, abs=1.0)
        assert ct.fill_delay_p95 == pytest.approx(offset, abs=1.0)
        assert len(ct.fill_delay_seconds) == n

    def test_fill_delay_thin_sample_none(self):
        """Fewer than MIN_SAMPLE approved delays → p50/p95 are None."""
        n = MIN_SAMPLE - 1
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows, fill_offset_seconds=10.0)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.fill_delay_p50 is None
        assert ct.fill_delay_p95 is None

    def test_rejected_decisions_have_no_delay(self):
        """Rejected decisions (no fill) must not contribute to delay list."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        ct = compute_compliance_telemetry(shadows, [])
        assert ct.fill_delay_seconds == []
        assert ct.fill_delay_p50 is None

    def test_negative_delay_excluded(self):
        """A fill BEFORE the decision (fill_time < asof) must not produce a
        negative delay entry — it is excluded entirely.
        """
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        # Fill times are all 30 seconds BEFORE the decision.
        fills = [
            _fill(
                ticker=s.ticker,
                direction=s.direction,
                fill_time=s.asof - timedelta(seconds=30),  # ← before decision
            )
            for s in shadows
        ]
        ct = compute_compliance_telemetry(shadows, fills)
        # All pairs are "approved" (ticker/direction/date match), but delays < 0 → excluded.
        assert len(ct.fill_delay_seconds) == 0


# ---------------------------------------------------------------------------
# Down-size frequency
# ---------------------------------------------------------------------------


class TestDownSizeFrequency:
    def test_all_downsized(self):
        """Human uses half the proposed size → down_size_frequency = 1.0."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows, size_fraction=0.05)  # half of shadow 0.10
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.down_size_frequency == pytest.approx(1.0)

    def test_no_downsizing(self):
        """Human matches proposed size → down_size_frequency = 0.0."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows, size_fraction=0.10)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.down_size_frequency == pytest.approx(0.0)

    def test_down_size_thin_sample_none(self):
        n = MIN_SAMPLE - 1
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows, size_fraction=0.05)
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.down_size_frequency is None

    def test_upsizing_does_not_count_as_downsized(self):
        """Human uses MORE than the proposed size → size_ratio > 1 → not down-sized."""
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n)
        fills = _full_fill_set(shadows, size_fraction=0.20)  # double the 0.10 shadow
        ct = compute_compliance_telemetry(shadows, fills)
        assert ct.down_size_frequency == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Ticker normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_ticker_normalized_uppercase(self):
        s = _shadow(ticker="aapl")
        assert s.ticker == "AAPL"

    def test_unknown_direction_stored_as_unknown(self):
        s = _shadow(direction="sideways")
        assert s.direction == "unknown"

    def test_fill_invalid_price_sanitized(self):
        f = RealFillRecord(ticker="A", direction="buy", fill_time=_T0, fill_price=-10.0)
        assert f.fill_price is None

    def test_fill_nan_price_sanitized(self):
        f = RealFillRecord(ticker="A", direction="buy", fill_time=_T0, fill_price=float("nan"))
        assert f.fill_price is None

    def test_fill_inf_price_sanitized(self):
        f = RealFillRecord(ticker="A", direction="buy", fill_time=_T0, fill_price=float("inf"))
        assert f.fill_price is None

    def test_fill_negative_size_sanitized(self):
        f = RealFillRecord(ticker="A", direction="buy", fill_time=_T0, fill_size_fraction=-0.5)
        assert f.fill_size_fraction is None


# ---------------------------------------------------------------------------
# to_dict round-trip
# ---------------------------------------------------------------------------


class TestToDictRoundTrip:
    def test_to_dict_all_keys_present(self):
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, shadow_return=0.02)
        fills = _full_fill_set(shadows, real_return=0.01)
        ct = compute_compliance_telemetry(shadows, fills)
        d = ct.to_dict()
        expected_keys = {
            "n_shadow_decisions",
            "n_real_fills",
            "n_paired",
            "n_rejected",
            "n_human_initiated",
            "overall_approval_rate",
            "by_signal_type",
            "by_direction",
            "by_conviction_band",
            "fill_delay_seconds",
            "fill_delay_p50",
            "fill_delay_p95",
            "down_size_frequency",
            "performance_gap",
            "thin_sample_warning",
        }
        assert expected_keys <= set(d.keys())

    def test_to_dict_performance_gap_keys(self):
        n = MIN_SAMPLE
        shadows = _full_shadow_set(n, shadow_return=0.02)
        fills = _full_fill_set(shadows, real_return=0.01)
        ct = compute_compliance_telemetry(shadows, fills)
        d = ct.to_dict()
        gap_keys = {
            "n_pairs_with_returns",
            "mean_shadow_return",
            "mean_real_return",
            "mean_gap",
            "shadow_win_rate",
        }
        assert gap_keys <= set(d["performance_gap"].keys())

    def test_returns_compliance_telemetry_instance(self):
        ct = compute_compliance_telemetry([], [])
        assert isinstance(ct, ComplianceTelemetry)
