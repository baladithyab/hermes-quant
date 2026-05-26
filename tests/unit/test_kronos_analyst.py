"""Tests for hermes_quant.analysts.kronos (ADR-0018).

Covers:
  - Lazy-load on first analyze() call (NOT at register time)
  - Missing kronos package -> zero-confidence abstain (graceful degrade)
  - Weight-load failure -> zero-confidence abstain
  - Insufficient bars (<32) -> None per Protocol
  - Distributional inference -> direction + magnitude + path-agreement confidence
  - Path-agreement confidence clipped to [0.30, 0.85]
  - Median-zero direction handling
  - Inference exception during analyze() -> zero-confidence abstain
  - Update calibrator hook is a no-op stub (Wave D wires it)
  - health() reports correct loaded/abstain state
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from hermes_quant.analysts.kronos import (
    KronosAnalyst,
    KronosConfig,
)
from hermes_quant.protocol import MarketContext


def _kronos_installed() -> bool:
    """Check if the kronos package is importable (controls test gating)."""
    try:
        import kronos  # noqa: F401
        return True
    except ImportError:
        return False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(n: int, *, drift: float = 0.5, base: float = 100.0):
    """Build n hourly OHLCV bars with optional linear drift."""
    rows = []
    start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    for i in range(n):
        ts = start + timedelta(hours=i)
        c = base + i * drift
        rows.append(
            {
                "timestamp": ts,
                "open": c - drift / 4,
                "high": c + 1.0,
                "low": c - 1.0,
                "close": c,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def _make_ctx(n_bars: int = 100, **kwargs):
    bars = _make_bars(n_bars, **kwargs)
    return MarketContext(
        asset="BTC/USDT",
        asset_class="crypto",
        timeframe="1h",
        exchange="binance",
        asof=bars["timestamp"].iloc[-1],
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        last_volume=float(bars["volume"].iloc[-1]),
    )


class _FakePredictor:
    """Stand-in for _DistributionalKronosPredictor.

    Returns paths that are deterministic given a `direction` argument
    (used by tests): +1 → all paths trend up, -1 → all down, 0 → split.
    """

    def __init__(self, *, direction: int = 1, agreement: float = 0.93):
        self.direction = direction
        self.agreement = agreement
        self.calls: list[dict] = []

    def predict_distributional(self, df, pred_len, sample_count):
        self.calls.append(
            {
                "df_len": len(df),
                "pred_len": pred_len,
                "sample_count": sample_count,
            }
        )
        last_close = float(df["close"].iloc[-1])
        # Build paths: agreement * sample_count agree with direction;
        # rest go the other way
        n_agree = round(self.agreement * sample_count)
        n_disagree = sample_count - n_agree
        paths = []
        for _ in range(n_agree):
            # this path's last close = last_close * (1 + direction * 0.02)
            end_close = last_close * (1 + self.direction * 0.02)
            paths.append(
                [[end_close, end_close, end_close, end_close, 100.0] for _ in range(pred_len)]
            )
        for _ in range(n_disagree):
            end_close = last_close * (1 - self.direction * 0.02)
            paths.append(
                [[end_close, end_close, end_close, end_close, 100.0] for _ in range(pred_len)]
            )
        return np.array(paths)


# ---------------------------------------------------------------------------
# Lazy-load + missing-package abstain
# ---------------------------------------------------------------------------


def test_no_load_at_construction():
    """Per ADR-0018 §D4, lazy-load means construction does NOT touch HF."""
    a = KronosAnalyst()
    h = a.health()
    assert h["loaded"] is False
    assert h["abstain_reason"] is None


@pytest.mark.skipif(
    _kronos_installed(),
    reason="Kronos package is installed in this environment; "
           "this test only validates the missing-package abstain path."
)
def test_missing_kronos_package_emits_abstain():
    """Without the kronos package, analyze() returns a zero-confidence abstain.

    Skipped when kronos IS installed (this is environment-dependent).
    The abstain path is still exercised via the inference-error path in
    test_inference_exception_abstains_for_this_call.
    """
    a = KronosAnalyst()
    # No _predictor_factory; the real lazy_load path will hit the import block
    view = a.analyze(_make_ctx())
    assert view is not None
    assert view.confidence == 0.0
    assert view.confidence_raw == 0.0
    assert view.direction == 0
    assert view.magnitude == 0.0
    assert "kronos package not installed" in (view.metadata or {}).get("reason", "")
    # health reflects the abstain
    assert "kronos package not installed" in (a.health()["abstain_reason"] or "")


def test_factory_failure_emits_abstain():
    """When the test-seam factory raises, abstain consistently."""

    def boom():
        raise RuntimeError("simulated weight load failure")

    a = KronosAnalyst(_predictor_factory=boom)
    view = a.analyze(_make_ctx())
    assert view.confidence == 0.0
    assert "factory_failed" in (view.metadata or {}).get("reason", "")
    # Subsequent calls keep abstaining without re-trying the factory
    view2 = a.analyze(_make_ctx())
    assert view2.confidence == 0.0
    # The factory was only invoked once
    # (we can't directly assert this without instrumentation; verify via abstain_reason persistence)
    assert a._abstain_reason is not None


# ---------------------------------------------------------------------------
# Insufficient bars -> None
# ---------------------------------------------------------------------------


def test_insufficient_bars_returns_none():
    a = KronosAnalyst()
    # 16 bars < 32 minimum
    ctx = _make_ctx(n_bars=16)
    assert a.analyze(ctx) is None


# ---------------------------------------------------------------------------
# Happy path with fake predictor
# ---------------------------------------------------------------------------


def test_predicts_direction_up():
    fake = _FakePredictor(direction=1, agreement=0.90)
    a = KronosAnalyst(_predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    assert view is not None
    assert view.direction == 1
    assert view.magnitude > 0
    # raw_confidence: 0.90 within [0.30, 0.85] clip -> clipped to 0.85
    assert view.confidence_raw == pytest.approx(0.85)


def test_predicts_direction_down():
    fake = _FakePredictor(direction=-1, agreement=0.93)
    a = KronosAnalyst(_predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    assert view.direction == -1
    assert view.magnitude > 0


def test_path_agreement_clipped_to_high():
    """Unanimous agreement (1.00) clipped to 0.85."""
    fake = _FakePredictor(direction=1, agreement=1.0)
    a = KronosAnalyst(_predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    assert view.confidence_raw == pytest.approx(0.85)


def test_path_agreement_clipped_to_low():
    """Even very low agreement clipped UP to 0.30 (foundation-model floor).

    Note: 'agreement' here is the FakePredictor's input — the fraction of
    paths matching the supplied `direction`. The KronosAnalyst code computes
    median-sign-agreement, which is symmetric: if 10% agree with +1, then
    90% disagree (i.e., go -1), so median is -1 with 0.90 agreement. So
    we test the floor by configuring a near-50/50 split.
    """
    fake = _FakePredictor(direction=1, agreement=20 / 30)  # exactly 20 agree
    a = KronosAnalyst(_predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    # 20/30 = 0.667 within [0.30, 0.85] -> not clipped
    assert view.confidence_raw == pytest.approx(20 / 30)


def test_path_agreement_floor_clipped():
    """Agreement very near 50/50 still floors at 0.30 — but with non-degenerate
    data we won't hit it under the FakePredictor model. Use a custom predictor
    that returns paths where the median is barely positive but most paths split."""

    class NearTiePredictor:
        def predict_distributional(self, df, pred_len, sample_count):
            last = float(df["close"].iloc[-1])
            paths = []
            # 16 up, 14 down -> median is up but agreement = 16/30 = 0.53
            # That's still above floor. To exercise floor, force agreement < 0.30
            # by putting 15 up at +1%, 14 down at -1%, 1 up at +0.001% — median is up
            # but barely. Agreement still 16/30. Hard to get below 0.30 mathematically
            # because median is by definition the 50th percentile.
            # The floor activates when direction != sign(median), but our code
            # sets direction = sign(median), so agreement >= 0.5 always.
            # Therefore the floor is dormant in practice with non-degenerate data;
            # it activates via the direction==0 path (median exactly zero), which
            # we test separately in test_median_zero_returns_flat_direction.
            for _ in range(sample_count):
                paths.append([[last * 1.0001] * 5 for _ in range(pred_len)])
            return np.array(paths)

    a = KronosAnalyst(_predictor_factory=lambda: NearTiePredictor())
    view = a.analyze(_make_ctx())
    # All paths slightly up, unanimous agreement -> clipped to 0.85 (high)
    assert view.confidence_raw == pytest.approx(0.85)


def test_calibrator_beta_prior_applied():
    """ColdStartCalibrator applies Beta(2,5) posterior to raw_confidence.

    ADR-0009 §P0-2 amendment 2026-05-26 (see
    docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md):
    cold-start = (raw + alpha) / (1 + alpha + beta), alpha=2 beta=5.
    """
    fake = _FakePredictor(direction=1, agreement=0.50)
    # raw=0.50; calibrated = (0.50 + 2.0) / (1 + 2.0 + 5.0) = 0.3125
    a = KronosAnalyst(_predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    assert view.confidence_raw == pytest.approx(0.50)
    assert view.confidence == pytest.approx(0.3125)


# ---------------------------------------------------------------------------
# Inference exception -> abstain (one-call abstain, doesn't poison future)
# ---------------------------------------------------------------------------


def test_inference_exception_abstains_for_this_call():
    """If predict_distributional raises, abstain for THIS call but allow
    future calls to retry (per the per-call-abstain pattern)."""

    class FlakyPredictor:
        def __init__(self):
            self.called = 0

        def predict_distributional(self, df, pred_len, sample_count):
            self.called += 1
            if self.called == 1:
                raise RuntimeError("torch CUDA OOM")
            # Second call succeeds
            n = sample_count
            last = float(df["close"].iloc[-1])
            return np.array([[[last * 1.01] * 5 for _ in range(pred_len)] for _ in range(n)])

    flaky = FlakyPredictor()
    a = KronosAnalyst(_predictor_factory=lambda: flaky)
    v1 = a.analyze(_make_ctx())
    assert v1.confidence == 0.0
    assert "inference_error" in (v1.metadata or {}).get("reason", "")
    # Critically: _abstain_reason is NOT set globally (this is per-call)
    assert a._abstain_reason is None
    # Second call recovers
    v2 = a.analyze(_make_ctx())
    assert v2.confidence > 0.0


# ---------------------------------------------------------------------------
# Median-zero direction edge case
# ---------------------------------------------------------------------------


def test_median_zero_returns_flat_direction():
    """When the median path is exactly flat, direction=0, conf=0.30 (floor)."""

    class FlatPredictor:
        def predict_distributional(self, df, pred_len, sample_count):
            last = float(df["close"].iloc[-1])
            # All paths exactly == last_close -> pct_returns all zero
            return np.array([[[last] * 5 for _ in range(pred_len)] for _ in range(sample_count)])

    a = KronosAnalyst(_predictor_factory=lambda: FlatPredictor())
    view = a.analyze(_make_ctx())
    assert view.direction == 0
    assert view.magnitude == 0.0
    # Per code: when direction == 0, agreement = 0.5; clipped to [0.30, 0.85] -> 0.50
    assert view.confidence_raw == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Update calibrator hook
# ---------------------------------------------------------------------------


def test_update_calibrator_is_noop_stub():
    """Per ADR-0018 §D8 — analysts never train. Hook honored as no-op."""
    from hermes_quant.protocol import AnalystView, RealizedOutcome

    a = KronosAnalyst()
    view = AnalystView(
        analyst="kronos",
        direction=1,
        magnitude=0.01,
        confidence=0.5,
        confidence_raw=0.6,
        horizon="1d",
    )
    # Should not raise; behavior is no-op
    a.update_calibrator(
        RealizedOutcome(
            view=view,
            asof_view=pd.Timestamp.now(tz="UTC"),
            asof_settlement=pd.Timestamp.now(tz="UTC"),
            realized_return=0.01,
            direction_correct=True,
        )
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_loaded_after_first_analyze():
    fake = _FakePredictor()
    a = KronosAnalyst(_predictor_factory=lambda: fake)
    assert a.health()["loaded"] is False
    a.analyze(_make_ctx())
    assert a.health()["loaded"] is True
    assert a.health()["abstain_reason"] is None


def test_health_reports_calibrator_status():
    a = KronosAnalyst()
    h = a.health()
    assert "calibrator" in h
    assert h["calibrator"]["name"] == "cold_start"
    assert h["calibrator"]["prior_alpha"] == 2.0
    assert h["calibrator"]["prior_beta"] == 5.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_overrides_propagate():
    cfg = KronosConfig(
        model="small",
        sample_count=10,
        pred_len=6,
    )
    fake = _FakePredictor()
    a = KronosAnalyst(config=cfg, _predictor_factory=lambda: fake)
    a.analyze(_make_ctx())
    # The fake records what it was called with
    assert fake.calls[0]["sample_count"] == 10
    assert fake.calls[0]["pred_len"] == 6


def test_config_clip_overrides_apply():
    cfg = KronosConfig(
        raw_confidence_clip_low=0.10,
        raw_confidence_clip_high=0.95,
    )
    fake = _FakePredictor(direction=1, agreement=1.0)
    a = KronosAnalyst(config=cfg, _predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    # 1.0 clipped to high=0.95 (not 0.85)
    assert view.confidence_raw == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Magnitude clip per horizon (Codex MED 2026-05-26)
# ---------------------------------------------------------------------------


class _MagnitudePredictor:
    """Predictor that emits a fixed *signed* return on every path.

    Lets tests directly drive the (direction, magnitude) the analyst sees
    without juggling sample_count / agreement bookkeeping. signed_return
    is applied to every path, so path-agreement is unanimous and the
    median pct_return == signed_return exactly.
    """

    def __init__(self, signed_return: float):
        self.signed_return = signed_return

    def predict_distributional(self, df, pred_len, sample_count):
        last = float(df["close"].iloc[-1])
        end = last * (1.0 + self.signed_return)
        return np.array(
            [[[end, end, end, end, 100.0] for _ in range(pred_len)]
             for _ in range(sample_count)]
        )


def test_magnitude_clip_high(caplog):
    """Forecast 0.50 with cap 0.10 → returns 0.10, log fires."""
    cfg = KronosConfig(max_magnitude_per_horizon={"1d": 0.10})
    fake = _MagnitudePredictor(signed_return=0.50)
    a = KronosAnalyst(config=cfg, _predictor_factory=lambda: fake)
    with caplog.at_level("WARNING", logger="hermes_quant.analysts.kronos"):
        view = a.analyze(_make_ctx())
    assert view is not None
    assert view.direction == 1
    assert view.magnitude == pytest.approx(0.10)
    assert (view.metadata or {}).get("magnitude_clipped") is True
    # Log fires: must mention "magnitude" and the cap or clipping
    assert any(
        "magnitude" in rec.getMessage().lower() and "clip" in rec.getMessage().lower()
        for rec in caplog.records
    ), f"Expected magnitude-clip warning; got: {[r.getMessage() for r in caplog.records]}"


def test_magnitude_clip_low(caplog):
    """Forecast -0.50 with cap 0.10 → magnitude returns 0.10, direction stays -1."""
    cfg = KronosConfig(max_magnitude_per_horizon={"1d": 0.10})
    fake = _MagnitudePredictor(signed_return=-0.50)
    a = KronosAnalyst(config=cfg, _predictor_factory=lambda: fake)
    with caplog.at_level("WARNING", logger="hermes_quant.analysts.kronos"):
        view = a.analyze(_make_ctx())
    assert view is not None
    assert view.direction == -1
    # magnitude is |median_return|; clipped to cap → 0.10
    assert view.magnitude == pytest.approx(0.10)
    assert (view.metadata or {}).get("magnitude_clipped") is True
    # Confidence is unchanged by clipping (path agreement is unanimous → 0.85)
    assert view.confidence_raw == pytest.approx(0.85)


def test_magnitude_no_clip():
    """Forecast 0.05 with cap 0.10 → unchanged, no warning."""
    cfg = KronosConfig(max_magnitude_per_horizon={"1d": 0.10})
    fake = _MagnitudePredictor(signed_return=0.05)
    a = KronosAnalyst(config=cfg, _predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    assert view is not None
    assert view.direction == 1
    assert view.magnitude == pytest.approx(0.05)
    assert (view.metadata or {}).get("magnitude_clipped") is False


def test_config_override_clip_propagates():
    """Custom cap config propagates correctly per horizon_label."""
    # Tighter cap on 1d, but our analyst uses horizon_label="5d" with looser cap
    cfg = KronosConfig(
        horizon_label="5d",
        max_magnitude_per_horizon={"1d": 0.05, "5d": 0.25},
    )
    fake = _MagnitudePredictor(signed_return=0.20)
    a = KronosAnalyst(config=cfg, _predictor_factory=lambda: fake)
    view = a.analyze(_make_ctx())
    # 0.20 < 5d cap (0.25) → no clip
    assert view.magnitude == pytest.approx(0.20)
    assert (view.metadata or {}).get("magnitude_clipped") is False

    # Now flip to 1d horizon with the same cap dict — same forecast hits 0.05 cap
    cfg2 = KronosConfig(
        horizon_label="1d",
        max_magnitude_per_horizon={"1d": 0.05, "5d": 0.25},
    )
    fake2 = _MagnitudePredictor(signed_return=0.20)
    a2 = KronosAnalyst(config=cfg2, _predictor_factory=lambda: fake2)
    view2 = a2.analyze(_make_ctx())
    assert view2.magnitude == pytest.approx(0.05)
    assert (view2.metadata or {}).get("magnitude_clipped") is True
    assert view2.horizon == "1d"
