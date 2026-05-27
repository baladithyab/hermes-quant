"""Lock-in tests for the BMA single-contributor degenerate-confidence fix.

Anchor: 2026-05-26 production scan + MoA committee deliberation.

What broke: BMA `vote_share = |w*d*c| / Σ|w*d*c|` collapses to 1.0
when only ONE analyst contributes (single term in numerator and
denominator). Combined with the agreement_bonus path (a single voice
trivially "agrees" with itself), confidence_raw → 1.0, calibrated
confidence → ~1.0, and downstream gates see false unanimity.

In the 2026-05-26 EOD scan, 24 of 38 candidates went actionable at
confidence=1.00 driven entirely by lone Kronos votes — TA and
microstructure analysts had abstained correctly under
silence-by-default (ADR-0002) but the aggregator promoted the
single-source signal as fully unanimous.

Two-tier fix:
  1. Default-on `require_ensemble=True` silences candidates with
     n_contributing < 2.
  2. With require_ensemble=False (research/test only), single-
     contributor confidence falls back to the lone analyst's own
     confidence_raw, NOT vote_share's degenerate 1.0.
"""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx(asset: str = "TEST") -> MarketContext:
    """Minimal MarketContext for aggregator tests (bars not read)."""
    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="1d"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000.0, 1000.0],
        }
    )
    return MarketContext(
        asset=asset,
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=101.5,
        last_volume=1000.0,
        asof=pd.Timestamp("2026-05-26T20:00:00"),
    )


def _view(
    analyst: str,
    direction: int,
    raw_conf: float = 0.5,
    magnitude: float = 0.02,
) -> AnalystView:
    return AnalystView(
        analyst=analyst,
        direction=direction,  # type: ignore[arg-type]
        magnitude=magnitude,
        confidence=raw_conf,  # pre-calibrated
        confidence_raw=raw_conf,
        horizon="1d",
    )


# ---------------------------------------------------------------------------
# Default behavior: require_ensemble=True silences single-source candidates
# ---------------------------------------------------------------------------


class TestRequireEnsembleDefault:
    """The 2026-05-26 production fix: default-on silence for n_contributing<2.

    These tests pin the silence-by-default posture (AGENTS.md) at the
    aggregator level for single-source signals. Without this guard, the
    24-pick-at-conf-1.00 production incident would recur.
    """

    def test_single_kronos_view_silences(self):
        """Reproduces the production failure mode exactly."""
        agg = BMAAggregator()  # require_ensemble=True is default
        ctx = _ctx("PLTR")
        # Lone Kronos vote (TA + microstructure abstained correctly per ADR-0002).
        views = [_view("kronos", direction=-1, raw_conf=0.34, magnitude=0.036)]
        sig = agg.aggregate(views, ctx)
        # MUST silence — n_contributing=1, no ensemble.
        assert sig.direction == 0, (
            f"BMA must silence single-source candidates, got direction={sig.direction}"
        )
        assert sig.confidence == 0.0
        assert sig.magnitude == 0.0

    def test_silenced_signal_carries_no_metadata_for_gate(self):
        """A flat signal must look identical to the gate regardless of how
        many input views BMA was given — the gate keys on direction == 0."""
        agg = BMAAggregator()
        ctx = _ctx("PLTR")
        sig = agg.aggregate(
            [_view("kronos", direction=-1, raw_conf=0.85)], ctx
        )
        assert sig.direction == 0


# ---------------------------------------------------------------------------
# require_ensemble=False (research / test config): honest single-source
# ---------------------------------------------------------------------------


class TestRequireEnsembleOff:
    """When require_ensemble=False, the aggregator passes through the lone
    analyst's own confidence_raw — NOT vote_share's degenerate 1.0."""

    def test_single_view_uses_analyst_raw_confidence_not_vote_share(self):
        agg = BMAAggregator(require_ensemble=False)
        ctx = _ctx("PLTR")
        views = [_view("kronos", direction=-1, raw_conf=0.30)]
        sig = agg.aggregate(views, ctx)
        # Direction passes through.
        assert sig.direction == -1
        # confidence_raw must be the analyst's own (0.30), NOT vote_share's
        # degenerate 1.0 + agreement_bonus.
        assert sig.confidence_raw == pytest.approx(0.30, abs=1e-6)
        # Must NOT be ≥ 0.95 — that's the bug we're guarding against.
        assert sig.confidence_raw < 0.95

    def test_single_view_no_agreement_bonus(self):
        """A single voice cannot trigger the agreement bonus."""
        agg = BMAAggregator(require_ensemble=False, agreement_bonus=0.20)
        ctx = _ctx("PLTR")
        views = [_view("kronos", direction=1, raw_conf=0.50)]
        sig = agg.aggregate(views, ctx)
        # If the agreement bonus had fired, confidence_raw would be
        # 0.50 + 0.20 = 0.70 (or 1.0 + 0.20 clipped). The fix means
        # confidence_raw == 0.50 (the analyst's own).
        assert sig.confidence_raw == pytest.approx(0.50, abs=1e-6)


# ---------------------------------------------------------------------------
# Multi-contributor unaffected: existing behavior preserved
# ---------------------------------------------------------------------------


class TestMultiContributorUnchanged:
    """The fix MUST NOT change behavior when n_contributing >= 2.

    Two converging analysts should still produce confidence > 0.5 and the
    agreement bonus should still apply. Two dissenting analysts should
    still produce a vote_share-based moderate confidence.
    """

    def test_two_unanimous_views_get_agreement_bonus(self):
        agg = BMAAggregator(agreement_bonus=0.10)
        ctx = _ctx("MRNA")
        views = [
            _view("classical_ta", direction=-1, raw_conf=0.6),
            _view("kronos", direction=-1, raw_conf=0.7),
        ]
        sig = agg.aggregate(views, ctx)
        assert sig.direction == -1
        # Unanimous on shared direction → vote_share == 1.0 + bonus.
        # confidence_raw = clip(1.0 + 0.10) = 1.0
        assert sig.confidence_raw == pytest.approx(1.0, abs=1e-6)
        # Calibrated may be lower depending on calibrator state, but the
        # invariant we care about is direction != 0 and confidence_raw is
        # NOT degenerate-1.0-from-single-source.
        assert sig.metadata["n_contributing"] >= 2

    def test_two_dissenting_views_moderate_confidence(self):
        # Build two strong-conf opposing views with similar weights so the
        # composite picks one direction but the OTHER still appears as a
        # contributor (not silenced as flat). vote_share will then be
        # less than 1.0 and the agreement bonus will NOT fire.
        agg = BMAAggregator(agreement_bonus=0.10)
        ctx = _ctx("META")
        views = [
            _view("classical_ta", direction=1, raw_conf=0.6, magnitude=0.04),
            _view("kronos", direction=-1, raw_conf=0.5, magnitude=0.02),
        ]
        sig = agg.aggregate(views, ctx)
        # If composite still emerges, confidence_raw should be moderate
        # (vote_share between 0 and 1, no agreement bonus).
        if sig.direction != 0:
            assert sig.confidence_raw < 1.0
            # Both views were considered (n_views=2). The number that
            # AGREED with composite_direction (n_contributing) may be 1
            # in the dissent case — that's expected because the LOSING
            # side contributes its weight to the loss, not the win.
            assert sig.metadata.get("n_views") == 2
        else:
            # Cancellation → flat — also a valid honest outcome.
            assert sig.confidence == 0.0


# ---------------------------------------------------------------------------
# Regression marker: the exact production failure mode
# ---------------------------------------------------------------------------


class TestProductionRegressionMarker:
    """This test exists to lock the 2026-05-26 incident behavior:
    24 EOD picks at confidence=1.00 driven by lone Kronos votes.
    If a future refactor reintroduces the degenerate path, this test
    fails with a message that points at the exact bug."""

    def test_lone_low_confidence_kronos_must_not_become_unanimous(self):
        """The headline production failure: Kronos at raw_conf=0.34 (low
        conviction) was being aggregated to confidence ~= 1.00 because
        BMA's vote_share collapses to 1.0 with one contributor and the
        agreement bonus then pushed it over 1.0 (clipped). This must not
        happen — either the candidate silences (default) OR the
        confidence reflects Kronos's actual 0.34 (require_ensemble=False)."""
        ctx = _ctx("PLTR")
        weak_kronos = [_view("kronos", direction=-1, raw_conf=0.34, magnitude=0.036)]

        # Default path: must silence.
        agg_default = BMAAggregator()
        sig_default = agg_default.aggregate(weak_kronos, ctx)
        assert sig_default.direction == 0, (
            "REGRESSION: lone weak Kronos signal must silence under "
            "require_ensemble=True (default). Got direction=%d, conf=%.3f."
            % (sig_default.direction, sig_default.confidence)
        )

        # Pass-through path: must reflect Kronos's actual confidence.
        agg_passthrough = BMAAggregator(require_ensemble=False)
        sig_passthrough = agg_passthrough.aggregate(weak_kronos, ctx)
        assert sig_passthrough.confidence_raw < 0.95, (
            "REGRESSION: lone Kronos at raw_conf=0.34 must not produce "
            "BMA confidence_raw >= 0.95. Got %.3f — this is the "
            "vote_share collapses-to-1.0 + agreement_bonus bug."
            % sig_passthrough.confidence_raw
        )
        assert sig_passthrough.confidence_raw == pytest.approx(0.34, abs=0.01)
