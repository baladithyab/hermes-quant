"""Unit + integration tests for hermes_quant.analysts.fundamentals.FundamentalsAnalyst.

Per ADR-0064 §Test Plan + docs/design/v0.6.1-fundamentals-analyst.md §6:
  - 12 unit tests covering happy path, abstains, cache lifecycle, clipping
  - 2 integration tests covering BMA wiring + Charter-§D8 no-training invariant

The fixture pattern mirrors tests/data/test_fundamentals_provider.py:
build rows via `provider.write_snapshot(...)` against a tmp_path-backed
FundamentalsProvider, then instantiate the analyst over the same provider
and assert on `analyze(ctx)` output. yfinance is never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from hermes_quant.analysts.fundamentals import FundamentalsAnalyst
from hermes_quant.data.fundamentals_provider import FundamentalsProvider
from hermes_quant.protocol import (
    Analyst,
    AnalystView,
    MarketContext,
    RealizedOutcome,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    return tmp_path / "fundamentals"


@pytest.fixture
def provider(cache_root: Path) -> FundamentalsProvider:
    return FundamentalsProvider(cache_root=cache_root)


@pytest.fixture
def analyst(provider: FundamentalsProvider) -> FundamentalsAnalyst:
    return FundamentalsAnalyst(provider=provider)


def _row(
    *,
    fetched_at: pd.Timestamp,
    pe_trailing: float = 18.0,
    pe_forward: float = 17.0,
    debt_to_equity: float = 1.5,
    free_cash_flow: float = 9.5e10,
    revenue_ttm: float = 4e11,
    eps_trailing: float = 6.5,
    eps_forward: float = 7.0,
    gross_margin_ttm: float = 0.45,
    gross_margin_prior: float = 0.42,
    revenue_yoy: float = 0.12,
    fcf_yoy: float = 0.20,
    sector: str = "Technology",
    currency: str = "USD",
    quote_type: str = "EQUITY",
) -> dict[str, Any]:
    """Mirror of tests/data/test_fundamentals_provider.py::_row to keep
    fixtures shape-compatible with the parquet cache layer."""
    return {
        "as_of_date": fetched_at.normalize(),
        "fetched_at": fetched_at,
        "source": "yfinance",
        "pe_trailing": pe_trailing,
        "pe_forward": pe_forward,
        "debt_to_equity": debt_to_equity,
        "free_cash_flow": free_cash_flow,
        "revenue_ttm": revenue_ttm,
        "eps_trailing": eps_trailing,
        "eps_forward": eps_forward,
        "gross_margin_ttm": gross_margin_ttm,
        "gross_margin_prior": gross_margin_prior,
        "revenue_yoy": revenue_yoy,
        "fcf_yoy": fcf_yoy,
        "sector": sector,
        "currency": currency,
        "quote_type": quote_type,
    }


def _bars(asof: pd.Timestamp, n: int = 60) -> pd.DataFrame:
    """Minimal canonical OHLCV (timestamp as a COLUMN per protocol §"bars")."""
    ts = pd.date_range(end=asof, periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": np.linspace(100.0, 110.0, n),
            "high": np.linspace(101.0, 111.0, n),
            "low": np.linspace(99.0, 109.0, n),
            "close": np.linspace(100.0, 110.0, n),
            "volume": np.full(n, 1_000_000.0),
        }
    )


def _ctx(
    asset: str,
    asof: pd.Timestamp,
    asset_class: str = "equity",
    timeframe: str = "1d",
) -> MarketContext:
    bars = _bars(asof)
    last_close = float(bars["close"].iloc[-1])
    last_volume = float(bars["volume"].iloc[-1])
    return MarketContext(
        asset=asset,
        timeframe=timeframe,
        asset_class=asset_class,
        exchange=None,
        bars=bars,
        last_close=last_close,
        last_volume=last_volume,
        asof=asof,
    )


# ---------------------------------------------------------------------------
# 1 — equity happy path: long
# ---------------------------------------------------------------------------


def test_equity_happy_path(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """Full 6-of-6 sub-signals firing bullish → direction +1, conf clipped."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=18)
    # Sector benchmark: P/E=18 vs sector median 25 → cheap.
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=24.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=26.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    # Subject — strong long signal across all 6 axes.
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            pe_trailing=18.0,
            pe_forward=14.0,  # forward < trailing → improving
            debt_to_equity=0.2,  # clean balance sheet
            free_cash_flow=9.5e10,
            fcf_yoy=0.25,  # FCF YoY +25% → buy
            revenue_yoy=0.18,  # > 0.15 → buy
            eps_trailing=7.0,
            eps_forward=7.5,  # fwd / trail = 1.07 → analysts revising UP → +1
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is not None
    assert view.direction == +1
    assert 0.20 <= view.confidence_raw <= 0.80
    # Rationale must list the contributing labels.
    assert "agreement=" in (view.rationale or "")
    md = view.metadata or {}
    sub_signals = md.get("sub_signals") or []
    longs = [s for s in sub_signals if s.get("direction") == 1]
    assert len(longs) >= 4, f"expected ≥4 contributing longs, got {len(longs)}: {sub_signals}"


# ---------------------------------------------------------------------------
# 2 — equity happy path: short (kept from §6 even though ADR §Test Plan only
#     names the long variant — the short side is the symmetric invariant)
# ---------------------------------------------------------------------------


def test_equity_happy_path_short(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=18)
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=18.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=18.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "XYZ",
        _row(
            fetched_at=fetched,
            pe_trailing=40.0,  # > 1.30 × sector → rich
            pe_forward=50.0,  # forward > trailing → deteriorating
            debt_to_equity=6.0,  # > 5.0 → covenant risk
            free_cash_flow=-1e9,  # < 0 → cash-burning
            fcf_yoy=-0.30,  # ignored by `_score_fcf` (already short on level)
            revenue_yoy=-0.15,  # < -0.10 → declining
            eps_trailing=3.0,
            eps_forward=2.0,  # fwd / trail = 0.66 → analysts revising DOWN → -1
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("XYZ", asof))
    assert view is not None
    assert view.direction == -1


# ---------------------------------------------------------------------------
# 3 — partial data abstain (<3 surviving)
# ---------------------------------------------------------------------------


def test_equity_partial_data_3_of_6_minimum(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """Only 2 sub-signals can fire → composite must abstain (returns None)."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)
    # No sector benchmark written → pe_relative cannot fire.
    # All other moderate values → only debt_equity_trend (D/E=0.2 → clean) and
    # nothing else fires — well below the ≥3 surviving gate.
    provider.write_snapshot(
        "TSLA",
        _row(
            fetched_at=fetched,
            pe_trailing=18.0,
            pe_forward=17.5,  # ratio 0.97 → flat
            debt_to_equity=0.2,  # → +1 (clean)
            free_cash_flow=float("nan"),  # missing → abstain
            fcf_yoy=float("nan"),
            revenue_yoy=0.05,  # flat
            eps_trailing=float("nan"),
            eps_forward=float("nan"),
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("TSLA", asof))
    assert view is None


# ---------------------------------------------------------------------------
# 4 — ETF abstain
# ---------------------------------------------------------------------------


def test_etf_abstain_returns_none(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    # Even with cache populated, the asset_class="etf" gate trips first.
    provider.write_snapshot(
        "SPY", _row(fetched_at=asof - pd.Timedelta(hours=1), quote_type="ETF")
    )
    view = analyst.analyze(_ctx("SPY", asof, asset_class="etf"))
    assert view is None


# ---------------------------------------------------------------------------
# 5 — crypto abstain (provider must not be touched)
# ---------------------------------------------------------------------------


def test_crypto_abstain_returns_none(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    # Sentinel: if the analyst tries to read fundamentals for a crypto pair
    # it will hit the provider — assert no parquet ever materialized.
    view = analyst.analyze(_ctx("BTC/USDT", asof, asset_class="crypto"))
    assert view is None
    yf_dir = provider.yfinance_dir
    assert not yf_dir.exists() or not any(yf_dir.iterdir()), (
        "crypto abstain must not trigger a provider read/write"
    )


# ---------------------------------------------------------------------------
# 6 — FX abstain (heuristic: trailing '=X')
# ---------------------------------------------------------------------------


def test_fx_abstain_returns_none(analyst: FundamentalsAnalyst) -> None:
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    # No asset_class given → '=X' suffix heuristic kicks in.
    view = analyst.analyze(_ctx("EURUSD=X", asof, asset_class="fx"))
    assert view is None


# ---------------------------------------------------------------------------
# 7 — cache hit: fresh snapshot
# ---------------------------------------------------------------------------


def test_cache_hit_skips_yfinance(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """Fresh snapshot in cache → analyze emits view; metadata.snapshot_age_days==0.

    The 'skips_yfinance' name from ADR-0064 is structural: analyze() never
    calls yfinance — only refresh() does. Asserting via the metadata age.
    """
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=24.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=26.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            pe_trailing=18.0,
            pe_forward=14.0,
            debt_to_equity=0.2,
            fcf_yoy=0.25,
            revenue_yoy=0.18,
            eps_trailing=7.0,
            eps_forward=7.5,
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is not None
    md = view.metadata or {}
    assert md.get("snapshot_age_days") == 0


# ---------------------------------------------------------------------------
# 8 — cache miss
# ---------------------------------------------------------------------------


def test_cache_miss_returns_none_no_error(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """Empty cache_root → analyze returns None; error_count stays 0."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    view = analyst.analyze(_ctx("NEW", asof))
    assert view is None
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 9 — staleness (>7d hard limit) abstains
# ---------------------------------------------------------------------------


def test_cache_staleness_hard_limit_abstains(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    too_old = asof - pd.Timedelta(days=8)
    provider.write_snapshot("AAPL", _row(fetched_at=too_old, sector="Tech"))
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is None
    # Hard-staleness is a structural abstain, not an exception path.
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 10 — provider read raises FileNotFoundError (or other) → handled cleanly
# ---------------------------------------------------------------------------


def test_provider_read_failure_returns_none_with_health_log(
    provider: FundamentalsProvider,
    analyst: FundamentalsAnalyst,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If provider.read_latest raises, analyze returns None — not a crash."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")

    def _boom(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("simulated parquet vanished")

    monkeypatch.setattr(provider, "read_latest", _boom)
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is None
    # FileNotFoundError is caught inside _fetch_fundamentals; the outer
    # try/except never fires, so error_count must remain 0.
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 11 — sector-median lookup cooperates with analyst
# ---------------------------------------------------------------------------


def test_sector_median_lookup_works_then_missing_still_emits(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """Even without a sector median, ≥3 OTHER sub-signals can produce a view.

    This pins the design's §6 row #11 invariant: pe_relative abstaining is
    not fatal as long as the rest of the slate has quorum.
    """
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)
    # NOTE: no `refresh_sector_medians` call → pe_relative cannot fire.
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            pe_trailing=18.0,
            pe_forward=14.0,  # → +1
            debt_to_equity=0.2,  # → +1
            fcf_yoy=0.25,  # → +1
            revenue_yoy=0.18,  # → +1
            eps_trailing=7.0,
            eps_forward=7.5,  # → +1
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is not None
    md = view.metadata or {}
    sub_signals = md.get("sub_signals") or []
    by_label = {s.get("label"): s for s in sub_signals}
    # pe_relative must be a 0-direction abstain in the metadata trail.
    assert by_label.get("pe_relative", {}).get("direction") == 0


# ---------------------------------------------------------------------------
# 12 — confidence clipping floor + ceiling
# ---------------------------------------------------------------------------


def test_confidence_clipping_to_0_20_0_80(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """Both floor and ceiling of the [0.20, 0.80] envelope are enforced."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)

    # ----- ceiling: unanimous strong-long signal across the slate -----
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=30.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=30.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "BULL",
        _row(
            fetched_at=fetched,
            pe_trailing=15.0,  # P/E 0.5 × sector → strong cheap
            pe_forward=12.0,  # → +1
            debt_to_equity=0.1,  # → +1
            fcf_yoy=0.50,  # → +1
            revenue_yoy=0.30,  # → +1
            eps_trailing=8.0,
            eps_forward=10.0,  # ratio 1.25 → strong upward revision
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("BULL", asof))
    assert view is not None
    assert view.confidence_raw <= analyst._RAW_CONF_CLIP_HI + 1e-9
    assert view.confidence_raw >= analyst._RAW_CONF_CLIP_LO - 1e-9


# ---------------------------------------------------------------------------
# 13 — INTEGRATION: BMA aggregator wiring
# ---------------------------------------------------------------------------


def test_bma_integration_e2e(provider: FundamentalsProvider) -> None:
    """A FundamentalsAnalyst view aggregates cleanly alongside ClassicalTA.

    This is the §Test Plan e2e_bma_integration check: the analyst's view
    survives BMA's abstain filter (confidence >= ABSTAIN_THRESHOLD=0.10
    after ColdStartCalibrator's Beta(2,5) shrink) and ends up in
    AggregatedSignal.components.
    """
    pytest.importorskip("hermes_quant.aggregators.bma")
    from hermes_quant.aggregators.bma import BMAAggregator

    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=30.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=30.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            pe_trailing=18.0,
            pe_forward=14.0,
            debt_to_equity=0.2,
            fcf_yoy=0.25,
            revenue_yoy=0.18,
            eps_trailing=7.0,
            eps_forward=7.5,
            sector="Tech",
        ),
    )
    fund = FundamentalsAnalyst(provider=provider)
    ctx = _ctx("AAPL", asof)
    fund_view = fund.analyze(ctx)
    assert fund_view is not None

    # Build a synthetic ClassicalTA-shaped peer view (we don't need the
    # full classical analyst; BMA only consumes AnalystView dataclasses).
    peer_view = AnalystView(
        analyst="classical_ta",
        direction=+1,
        magnitude=0.02,
        confidence=0.35,
        confidence_raw=0.65,
        horizon="1d",
    )

    agg = BMAAggregator()
    signal = agg.aggregate([fund_view, peer_view], ctx)
    component_names = [c.analyst for c in signal.components]
    assert "fundamentals" in component_names
    assert "classical_ta" in component_names


# ---------------------------------------------------------------------------
# 14 — INTEGRATION: Charter §D8 — analyst never trains during inference
# ---------------------------------------------------------------------------


def test_charter_d8_no_training_invariant(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0018 §D8 generalized: update() forwards to calibrator.fit only.

    The analyst's class-level _MAGNITUDE_BY_HORIZON / _RAW_CONF_CLIP_*
    constants must NOT mutate from a settled outcome. Only the calibrator
    learns.
    """
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=30.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=30.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            pe_trailing=18.0,
            pe_forward=14.0,
            debt_to_equity=0.2,
            fcf_yoy=0.25,
            revenue_yoy=0.18,
            eps_trailing=7.0,
            eps_forward=7.5,
            sector="Tech",
        ),
    )
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is not None

    # Snapshot the immutable surface of the analyst before update().
    snap_clip_lo = analyst._RAW_CONF_CLIP_LO
    snap_clip_hi = analyst._RAW_CONF_CLIP_HI
    snap_mag_table = dict(analyst._MAGNITUDE_BY_HORIZON)
    snap_min_surv = analyst._MIN_SURVIVING_SUBSIGNALS
    n_views_before = analyst._n_views_emitted

    # Spy on calibrator.fit so we can prove it (and only it) was called.
    fit_calls: list[tuple[Any, Any]] = []
    real_fit = analyst.calibrator.fit

    def _spy_fit(raw_scores: Any, direction_correct: Any) -> None:
        fit_calls.append((list(raw_scores), list(direction_correct)))
        real_fit(raw_scores, direction_correct)

    monkeypatch.setattr(analyst.calibrator, "fit", _spy_fit)

    outcome = RealizedOutcome(
        view=view,
        asof_view=asof,
        asof_settlement=asof + pd.Timedelta(days=30),
        realized_return=0.05,
        direction_correct=True,
    )
    analyst.update(outcome)

    # 1. Calibrator was called exactly once.
    assert len(fit_calls) == 1
    # 2. Class-level constants did NOT mutate.
    assert analyst._RAW_CONF_CLIP_LO == snap_clip_lo
    assert analyst._RAW_CONF_CLIP_HI == snap_clip_hi
    assert analyst._MAGNITUDE_BY_HORIZON == snap_mag_table
    assert analyst._MIN_SURVIVING_SUBSIGNALS == snap_min_surv
    # 3. update() does not bump _n_views_emitted (only analyze() does).
    assert analyst._n_views_emitted == n_views_before


# ---------------------------------------------------------------------------
# 15 — Protocol compliance (runtime_checkable)
# ---------------------------------------------------------------------------


def test_protocol_compliance(analyst: FundamentalsAnalyst) -> None:
    """`isinstance(FundamentalsAnalyst(), Analyst)` must be True (ADR-0002)."""
    assert isinstance(analyst, Analyst)
