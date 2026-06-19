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
    report_date: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
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
    fixtures shape-compatible with the parquet cache layer.

    cs12: every snapshot carries a PUBLIC point-in-time stamp. These tests'
    ctx asof is 2026-05-15; the default ``period_end = 2025-12-31`` is a Q4
    that was publicly filed well before asof (period_end + 45d reporting lag
    = 2026-02-14 < 2026-05-15). Under the cs12 default-ON reporting-lag
    filter the analyst's ``read_latest(ticker, as_of=asof)`` therefore ADMITS
    this already-public quarter — exactly the live no-lookahead behavior:
    a filed quarter is visible, a not-yet-filed one would be excluded. The
    stamp keeps the filter ACTIVE (the tests do NOT pin the flag OFF)."""
    return {
        "as_of_date": fetched_at.normalize(),
        "fetched_at": fetched_at,
        "report_date": report_date if report_date is not None else pd.NaT,
        "period_end": (
            period_end
            if period_end is not None
            else pd.Timestamp("2025-12-31", tz="UTC")
        ),
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
# 6b — option FAMILY abstain (cs45): both the generic 'option' token AND the
#      live 'us_option' stamp (react.multileg.py:569) must abstain. An OCC-21
#      option symbol has no '/' and does not end '=X', so a bare `== "option"`
#      check let 'us_option' fall through the symbol heuristics → 'equity' →
#      analyze() fetched/scored fundamentals for a contract symbol as a stock
#      (the ac1 contract-layer divergence, here in the analyst). The recognizer
#      keys on the option FAMILY (pdr_core.is_option_asset_class) so both tokens
#      abstain. LATENT today (options route the multi-leg recipe path, not the
#      equity advisor analyst loop), live the instant an option MarketContext
#      reaches the analyst pool. FundamentalsAnalyst is an ADR-0004 gate input.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("opt_class", ["option", "us_option"])
def test_option_family_abstain_returns_none(
    provider: FundamentalsProvider,
    analyst: FundamentalsAnalyst,
    opt_class: str,
) -> None:
    """cs45 RED→GREEN: an option MarketContext abstains (Protocol-clean None).

    Pre-fix, 'us_option' (the live stamp) classified 'equity' and analyze()
    proceeded to read the provider for the OCC-21 contract symbol as a stock.
    The OCC-21 symbol below has no '/' and no '=X', so the symbol heuristics
    alone would route it to 'equity' — the abstain MUST come from the
    asset_class option-family gate, not the heuristics.
    """
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    occ_symbol = "AAPL  260116C00150000"  # OCC-21: no '/', no '=X'

    # Classifier-level invariant: option family → 'unknown' (abstain).
    assert (
        FundamentalsAnalyst._classify_symbol_universe(occ_symbol, opt_class)
        == "unknown"
    )
    # The non-option classes stay byte-identical (no collateral change).
    assert FundamentalsAnalyst._classify_symbol_universe("AAPL", "equity") == "equity"
    assert FundamentalsAnalyst._classify_symbol_universe("SPY", "etf") == "etf"
    assert (
        FundamentalsAnalyst._classify_symbol_universe("BTC/USDT", "crypto") == "crypto"
    )
    assert FundamentalsAnalyst._classify_symbol_universe("EURUSD=X", "fx") == "fx"

    # End-to-end abstain, and the provider is never touched (sentinel: no
    # parquet ever materializes — same proof shape as the crypto abstain).
    view = analyst.analyze(_ctx(occ_symbol, asof, asset_class=opt_class))
    assert view is None
    yf_dir = provider.yfinance_dir
    assert not yf_dir.exists() or not any(yf_dir.iterdir()), (
        "option abstain must not trigger a provider read/write"
    )


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
# 9 — staleness: a GENUINELY-stale datum (period basis > cadence limit) abstains
# ---------------------------------------------------------------------------


def test_cache_staleness_hard_limit_abstains(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """cs40: staleness keys off the datum's fiscal basis, not fetched_at.

    A row whose period_end is older than the cadence-aware limit (> ~1
    quarter + reporting lag) is genuinely stale fundamentals and abstains —
    regardless of when it was cached. Here period_end = 2024-06-30 vs asof
    2026-05-15 (~685d) is far past any quarterly cadence, so abstain.
    """
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)  # cron just ran -> fetched_at fresh
    old_period = pd.Timestamp("2024-06-30", tz="UTC")  # ~685d ago, genuinely stale
    # A full bullish slate WITH a sector benchmark: every gate EXCEPT staleness
    # would admit a view, so abstention here pins the genuinely-stale rejection.
    provider.write_snapshot(
        "AAA", _row(fetched_at=fetched, period_end=old_period, pe_trailing=30.0, sector="Tech")
    )
    provider.write_snapshot(
        "BBB", _row(fetched_at=fetched, period_end=old_period, pe_trailing=30.0, sector="Tech")
    )
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            period_end=old_period,
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
    assert view is None
    # Hard-staleness is a structural abstain, not an exception path.
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 9b — cs40 RED: lag-admitted row with OLD fetched_at must NOT be darkened
# ---------------------------------------------------------------------------


def test_cs40_lag_admitted_old_fetched_at_still_emits(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """cs40 regression: the no-lookahead backtest replay path.

    A Q4 datum (period_end 2025-12-31, knowable 2026-02-14 under the 45d
    reporting lag) cached ONCE near period_end (fetched_at 2025-12-31), then
    replayed at asof 2026-03-01 (> knowable). The provider lag filter ADMITS
    it (genuinely public). Pre-cs40 the analyst's 7d *fetched_at* staleness
    gate rejected it (fetched_at ~60d old) -> the analyst went DARK on every
    sparse backtest cache. Post-cs40 staleness keys off the datum's fiscal
    period basis, so a recently-public quarter is ADMITTED.
    """
    period_end = pd.Timestamp("2025-12-31", tz="UTC")
    fetched_at = pd.Timestamp("2025-12-31T20:00:00", tz="UTC")  # cached once
    asof = pd.Timestamp("2026-03-01T16:00:00", tz="UTC")  # > knowable 2026-02-14

    # sector benchmark (cheap relative P/E so pe_relative can also fire)
    provider.write_snapshot(
        "AAA", _row(fetched_at=fetched_at, period_end=period_end, pe_trailing=30.0, sector="Tech")
    )
    provider.write_snapshot(
        "BBB", _row(fetched_at=fetched_at, period_end=period_end, pe_trailing=30.0, sector="Tech")
    )
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched_at,
            period_end=period_end,
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
    # Sanity: the provider lag filter genuinely admits this row (it is public).
    assert provider.read_latest("AAPL", as_of=asof) is not None
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is not None, "cs40: lag-admitted public quarter must NOT be darkened by fetched_at age"
    assert view.direction == +1
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 9c — cs40: live/no-period-basis row keeps the fetched_at cron-liveness gate
# ---------------------------------------------------------------------------


def test_cs40_no_period_basis_keeps_fetched_at_liveness_gate(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """cs40 safety: rows with NO fiscal basis (old-schema / pre-B34 caches)
    still use the fetched_at cron-liveness gate, so a dead cron that stopped
    writing > the fetched_at limit ago still darkens the analyst. This keeps
    the original D5 operational-liveness behavior on the fallback path and
    does not loosen anything for rows that never carried a period basis."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    # No report_date AND no period_end -> fall back to fetched_at age gate.
    stale_fetch = asof - pd.Timedelta(days=8)
    provider.write_snapshot(
        "AAPL",
        _row(fetched_at=stale_fetch, report_date=pd.NaT, period_end=pd.NaT, sector="Tech"),
    )
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is None
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 9d — cs77 RED: a FUTURE-dated datum basis (period_end/report_date > asof)
#      must ABSTAIN — the datum-age gate had no lower bound, so a negative
#      datum_age_days (basis in the future) silently passed `> HARD_LIMIT`
#      and a not-yet-knowable fundamental was scored as a current datum.
# ---------------------------------------------------------------------------


def test_cs77_future_basis_datum_abstains(
    provider: FundamentalsProvider,
    analyst: FundamentalsAnalyst,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cs77: a fundamentals datum with a FUTURE fiscal basis is not knowable.

    A corrupt / hand-built / mis-stamped ``period_end`` dated AFTER ``asof``
    makes ``datum_age_days = (asof - basis).days`` NEGATIVE. The old upper-only
    clause ``if datum_age_days > _STALENESS_DATUM_DAYS_HARD_LIMIT`` then reads
    ``-121 > 190`` (False) and the gate is BYPASSED — a not-yet-knowable datum
    is scored as a current one. Same fail-OPEN-on-future-timestamp class as
    cs42a/cs53/cs67/cs68/cs69/cs75.

    The provider's cs12 reporting-lag filter is the OTHER guard (it would drop
    a future basis: effective_knowable = basis + 45d > asof), but it is
    flag-gated. With ``HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG=0`` the filter is
    a no-op and the future-basis row reaches the analyst's datum-age gate — the
    sole guard on that path. The analyst's own gate must independently abstain.
    """
    monkeypatch.setenv("HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG", "0")
    asof = pd.Timestamp("2026-03-01T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)  # fresh -> fetched_at fallback never darkens
    future_period = pd.Timestamp("2026-06-30", tz="UTC")  # basis > asof, age = -121d

    # Full bullish slate + sector benchmark: every gate EXCEPT the datum-age
    # lower bound would admit a view, so abstention pins the future-basis reject.
    provider.write_snapshot(
        "AAA", _row(fetched_at=fetched, period_end=future_period, pe_trailing=30.0, sector="Tech")
    )
    provider.write_snapshot(
        "BBB", _row(fetched_at=fetched, period_end=future_period, pe_trailing=30.0, sector="Tech")
    )
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            period_end=future_period,
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
    # Sanity: flag OFF -> lag filter is a no-op -> the future-basis row survives
    # the read and routes to the analyst datum-age gate (the only remaining guard).
    assert provider.read_latest("AAPL", as_of=asof) is not None
    view = analyst.analyze(_ctx("AAPL", asof))
    assert view is None, "cs77: a future-dated (not-yet-knowable) datum must abstain"
    # Future-basis abstain is a structural drop, not an exception path.
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 9e — cs77 companion: a genuinely-CURRENT datum (age in [0, HARD]) still emits
#      — the bounded membership test is byte-identical on the in-range path.
# ---------------------------------------------------------------------------


def test_cs77_current_datum_still_emits(
    provider: FundamentalsProvider,
    analyst: FundamentalsAnalyst,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cs77 byte-identical guard: a current datum (period_end 60d before asof,
    age in [0, 190]) is admitted exactly as before. Same flag-OFF isolation
    and same slate as the RED test, differing only in the fiscal basis."""
    monkeypatch.setenv("HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG", "0")
    asof = pd.Timestamp("2026-03-01T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)
    current_period = asof - pd.Timedelta(days=60)  # age 60, in [0, 190]

    provider.write_snapshot(
        "AAA", _row(fetched_at=fetched, period_end=current_period, pe_trailing=30.0, sector="Tech")
    )
    provider.write_snapshot(
        "BBB", _row(fetched_at=fetched, period_end=current_period, pe_trailing=30.0, sector="Tech")
    )
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=fetched,
            period_end=current_period,
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
    assert view is not None, "cs77: a current in-range datum must still emit"
    assert view.direction == +1
    assert analyst.health()["error_count"] == 0


# ---------------------------------------------------------------------------
# 9f — cs78 RED: the basis-LESS cron-liveness fallback (no report_date AND no
#      period_end) had an upper-only fetched_at gate, so a FUTURE fetched_at
#      (negative age) or a NaT/missing fetched_at (nan age) silently passed
#      ``age_days > _STALENESS_FETCHED_AT_DAYS_HARD_LIMIT`` (`-10 > 7` False,
#      `nan > 7` False) and a not-yet-/un-knowable fetch-time row was scored
#      as fresh — the symmetric sibling of the cs77 datum-axis fail-open.
#
#      cs78 is defense-in-depth: the analyst's live read passes as_of=ctx.asof
#      (non-None), where the provider's cs42a drops a future/NaT fetched_at on
#      the as_of-bounded read (`df[df["fetched_at"] <= asof_ts]`), so this gate
#      is not independently live-reachable. These tests exercise the analyst's
#      own gate directly via _fetch_fundamentals with a hand-built basis-less
#      Series (the cron-liveness fallback path) to pin the LAST unbounded
#      sibling gate — closing the future-timestamp/staleness family on the
#      cron-liveness axis as cs77 did on the datum axis.
# ---------------------------------------------------------------------------


def _basis_less_row(*, fetched_at: pd.Timestamp) -> pd.Series:
    """A snapshot with NO fiscal basis (report_date AND period_end NaT), so
    _datum_basis returns None and _fetch_fundamentals routes to the fetched_at
    cron-liveness fallback gate. Equity quote_type so the cs45/cs47 non-equity
    post-check does not pre-empt the staleness gate under audit."""
    return pd.Series(
        {
            "report_date": pd.NaT,
            "period_end": pd.NaT,
            "fetched_at": fetched_at,
            "quote_type": "EQUITY",
            "pe_trailing": 18.0,
            "sector": "Technology",
        }
    )


def test_cs78_future_fetched_at_basis_less_abstains(
    analyst: FundamentalsAnalyst,
) -> None:
    """cs78 RED: a basis-less row with a FUTURE fetched_at must abstain.

    asof - fetched_at is NEGATIVE (here -10d). The old upper-only clause
    ``age_days > 7`` reads ``-10 > 7`` (False) -> gate BYPASSED -> the row is
    returned as fresh (fail-OPEN, same class as cs42a/cs53/cs67/cs68/cs75/cs77).
    Post-cs78 the bounded membership test ``not (0 <= age_days <= 7)`` rejects a
    negative age -> _fetch_fundamentals returns None.
    """
    asof = pd.Timestamp("2026-03-01T16:00:00", tz="UTC")
    future_fetch = asof + pd.Timedelta(days=10)  # age = -10d
    snap = _basis_less_row(fetched_at=future_fetch)

    # Sanity: this row genuinely has no fiscal basis -> the fallback path runs.
    assert FundamentalsAnalyst._datum_basis(snap) is None

    # Drive the analyst gate directly: provider.read_latest returns the
    # basis-less Series verbatim; cs42a is bypassed here precisely so the
    # analyst's own (last) gate is the one under test.
    analyst.provider.read_latest = lambda *_a, **_k: snap  # type: ignore[assignment]
    result = analyst._fetch_fundamentals("AAPL", asof)
    assert result is None, (
        "cs78: a basis-less row with a FUTURE fetched_at (negative age) must "
        "abstain — it was fetched in the future and is not yet knowable"
    )


def test_cs78_nat_fetched_at_basis_less_abstains(
    analyst: FundamentalsAnalyst,
) -> None:
    """cs78 RED: a basis-less row with a NaT/missing fetched_at must abstain.

    ``(asof - NaT).days`` is ``nan``; the old clause ``nan > 7`` is False ->
    gate BYPASSED -> an unknowable-fetch-time row scored as fresh (fail-OPEN).
    Post-cs78 ``not (0 <= nan <= 7)`` is True (nan comparisons are False) ->
    abstain. NaT survives the fetched_at parse (pd.Timestamp(NaT) -> NaT, and
    NaT.tz_localize('UTC') -> NaT, no raise), so it reaches the age gate rather
    than the KeyError/ValueError/TypeError fallback.
    """
    asof = pd.Timestamp("2026-03-01T16:00:00", tz="UTC")
    snap = _basis_less_row(fetched_at=pd.NaT)
    assert FundamentalsAnalyst._datum_basis(snap) is None

    analyst.provider.read_latest = lambda *_a, **_k: snap  # type: ignore[assignment]
    result = analyst._fetch_fundamentals("AAPL", asof)
    assert result is None, (
        "cs78: a basis-less row with a NaT fetched_at (nan age) must abstain — "
        "an unknowable fetch time cannot satisfy the cron-liveness gate"
    )


def test_cs78_fresh_basis_less_still_admitted(
    analyst: FundamentalsAnalyst,
) -> None:
    """cs78 byte-identical guard: a basis-less row with a genuinely-fresh
    fetched_at (age in [0, 7]) is admitted EXACTLY as before. The inclusive
    ``<=`` preserves the old strictly-greater upper boundary, and a stale
    (> 7d) fetched_at still darkens — so cs78 only ADDS the lower bound and
    does not loosen the cron-liveness gate cs40 9c pins."""
    asof = pd.Timestamp("2026-03-01T16:00:00", tz="UTC")

    fresh = _basis_less_row(fetched_at=asof - pd.Timedelta(days=3))  # age 3, in [0,7]
    analyst.provider.read_latest = lambda *_a, **_k: fresh  # type: ignore[assignment]
    assert analyst._fetch_fundamentals("AAPL", asof) is not None, (
        "cs78: a fresh basis-less row (age in [0, 7]) must still be admitted"
    )

    # Upper bound preserved: a > 7d basis-less fetched_at still abstains.
    stale = _basis_less_row(fetched_at=asof - pd.Timedelta(days=8))  # age 8, > 7
    analyst.provider.read_latest = lambda *_a, **_k: stale  # type: ignore[assignment]
    assert analyst._fetch_fundamentals("AAPL", asof) is None, (
        "cs78: a stale basis-less row (age > 7) must still abstain (upper "
        "bound unchanged)"
    )


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


class _OpenBBFundamentalsStub:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls: list[tuple[str, pd.Timestamp]] = []

    def read_fundamentals(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        assert as_of is not None
        self.calls.append((ticker, as_of))
        return self.frame.copy()


def _openbb_fundamentals_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "period_ending": pd.to_datetime(["2025-12-31", "2026-03-31"]),
            "filing_date": pd.to_datetime(["2026-02-15", "2026-05-10"]),
            "pe_trailing": [18.0, 17.0],
            "pe_forward": [16.0, 15.0],
            "debt_to_equity": [0.4, 0.3],
            "free_cash_flow": [9.0e10, 9.8e10],
            "revenue_ttm": [4.1e11, 4.2e11],
            "eps_trailing": [6.6, 6.7],
            "eps_forward": [7.1, 7.2],
            "revenue_yoy": [0.16, 0.18],
            "fcf_yoy": [0.21, 0.22],
            "sector": ["Technology", "Technology"],
            "currency": ["USD", "USD"],
            "quote_type": ["EQUITY", "EQUITY"],
        }
    )


def test_openbb_fundamentals_fallback_on_cache_miss(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2f33: FundamentalsAnalyst must be able to consume the OpenBB provider path.

    RED before the consumer cutover: OpenBBFundamentals existed, but the analyst
    never called it, so arming OpenBB left fundamentals cache misses dark.
    """
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    openbb = _OpenBBFundamentalsStub(_openbb_fundamentals_frame())
    analyst = FundamentalsAnalyst(provider=provider, openbb_provider=openbb)
    monkeypatch.setattr(
        provider,
        "read_latest",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("cache miss")),
    )

    snap = analyst._fetch_fundamentals("AAPL", asof)

    assert snap is not None
    assert openbb.calls == [("AAPL", asof)]
    assert snap["source"] == "openbb"
    assert snap["period_end"] == pd.Timestamp("2026-03-31", tz="UTC")
    assert snap["report_date"] == pd.Timestamp("2026-05-10", tz="UTC")


def test_openbb_fundamentals_default_off_without_injected_provider(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset HERMES_QUANT_OPENBB preserves the old cache-miss abstain path."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    analyst = FundamentalsAnalyst(provider=provider)
    monkeypatch.delenv("HERMES_QUANT_OPENBB", raising=False)
    monkeypatch.setattr(
        provider,
        "read_latest",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("cache miss")),
    )

    assert analyst._fetch_fundamentals("AAPL", asof) is None


def test_openbb_fundamentals_fallback_on_stale_cache(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    stale = pd.Series(
        _row(
            fetched_at=asof - pd.Timedelta(days=250),
            period_end=asof - pd.Timedelta(days=250),
        )
    )
    openbb = _OpenBBFundamentalsStub(_openbb_fundamentals_frame())
    analyst = FundamentalsAnalyst(provider=provider, openbb_provider=openbb)
    monkeypatch.setattr(provider, "read_latest", lambda *_a, **_k: stale)

    snap = analyst._fetch_fundamentals("AAPL", asof)

    assert snap is not None
    assert snap["source"] == "openbb"
    assert openbb.calls == [("AAPL", asof)]


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


# ---------------------------------------------------------------------------
# 16 — non-equity quote_type abstain (cs47): the post-fetch quote_type gate
#      abstained ONLY on 'ETF'. The provider writes ANY yfinance quoteType
#      verbatim (fundamentals_provider.py:742 -> str(info.get("quoteType")
#      or "")), so a cached snapshot whose quote_type is MUTUALFUND / INDEX /
#      CURRENCY / CRYPTOCURRENCY (the rest of the canonical non-equity set that
#      scorers.py already enumerates twice) reached the analyst when the symbol
#      heuristics classified it 'equity' (no '/', no '=X', no asset_class).
#      It was then SCORED with equity-specific fundamentals (P/E, D/E, FCF, …)
#      as if it were a stock — a perception-layer category error feeding an
#      ADR-0004 gate input. The fix STRICTLY WIDENS the abstain set to the full
#      canonical non-equity quote_type vocabulary (defense-in-depth,
#      silence-by-default); EQUITY stays byte-identical (still scored).
# ---------------------------------------------------------------------------


def _strong_long_row(fetched: pd.Timestamp, quote_type: str) -> dict[str, Any]:
    """A snapshot that WOULD fire a strong 6-of-6 long view if scored as equity.

    Mirrors test_equity_happy_path's subject so the ONLY thing that can stop a
    view is the quote_type gate — proving the non-equity snapshot is otherwise
    fully scoreable equity-shaped data."""
    return _row(
        fetched_at=fetched,
        pe_trailing=18.0,
        pe_forward=14.0,  # forward < trailing → improving
        debt_to_equity=0.2,  # clean balance sheet
        free_cash_flow=9.5e10,
        fcf_yoy=0.25,  # FCF YoY +25% → buy
        revenue_yoy=0.18,  # > 0.15 → buy
        eps_trailing=7.0,
        eps_forward=7.5,  # fwd / trail = 1.07 → +1
        sector="Tech",
        quote_type=quote_type,
    )


@pytest.mark.parametrize(
    "quote_type", ["MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY"]
)
def test_non_equity_quote_type_abstains_post_fetch(
    provider: FundamentalsProvider,
    analyst: FundamentalsAnalyst,
    quote_type: str,
) -> None:
    """cs47 RED→GREEN: a cached non-equity quote_type must abstain post-fetch.

    No asset_class is given on the ctx, so the universe heuristics classify the
    plain ticker as 'equity' and the snapshot is fetched. Pre-fix the post-fetch
    gate only matched 'ETF', so a MUTUALFUND/INDEX/CURRENCY/CRYPTOCURRENCY
    snapshot was scored as a stock and produced a view. Post-fix the analyst
    abstains (Protocol-clean None) for the whole non-equity set."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=18)
    # Sector benchmark so pe_relative can fire (P/E=18 vs sector ~25 → cheap).
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=24.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=26.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    # Subject: equity-shaped strong-long fundamentals but a non-equity quote_type.
    provider.write_snapshot("FOO", _strong_long_row(fetched, quote_type=quote_type))

    # ctx asset_class=None → '/'-less, '=X'-less ticker classifies 'equity'.
    view = analyst.analyze(_ctx("FOO", asof, asset_class=None))
    assert view is None, (
        f"quote_type={quote_type!r} is non-equity; the analyst must abstain "
        f"(Protocol-clean None), not score it as a stock — got {view!r}"
    )


def test_equity_quote_type_still_scored_byte_identical(
    provider: FundamentalsProvider, analyst: FundamentalsAnalyst
) -> None:
    """cs47 invariant: an EQUITY quote_type snapshot still scores unchanged.

    The widened abstain set must NOT darken legitimate equities — the same
    strong-long row that test_equity_happy_path admits must still produce a
    +1 view when quote_type='EQUITY'."""
    asof = pd.Timestamp("2026-05-15T16:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=18)
    provider.write_snapshot("AAA", _row(fetched_at=fetched, pe_trailing=24.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=fetched, pe_trailing=26.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB"])
    provider.write_snapshot("FOO", _strong_long_row(fetched, quote_type="EQUITY"))

    view = analyst.analyze(_ctx("FOO", asof, asset_class=None))
    assert view is not None
    assert view.direction == +1
