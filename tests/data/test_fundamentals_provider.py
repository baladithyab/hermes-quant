"""Unit tests for hermes_quant.data.fundamentals_provider.FundamentalsProvider.

Covers parquet cache mechanics (read/write/dedupe/atomic), sector-median
sibling cache, and staleness handling. yfinance network calls are NOT
exercised here — only the cache layer.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hermes_quant.data.fundamentals_provider import FundamentalsProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    return tmp_path / "fundamentals"


@pytest.fixture
def provider(cache_root: Path) -> FundamentalsProvider:
    return FundamentalsProvider(cache_root=cache_root)


def _row(
    *,
    fetched_at: pd.Timestamp,
    as_of_date: pd.Timestamp | None = None,
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
) -> dict:
    return {
        "as_of_date": (as_of_date or fetched_at).normalize(),
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


# ---------------------------------------------------------------------------
# Read / write basics
# ---------------------------------------------------------------------------


def test_read_latest_missing_ticker_returns_none(provider: FundamentalsProvider) -> None:
    assert provider.read_latest("NOPE") is None


def test_write_then_read_roundtrip(provider: FundamentalsProvider) -> None:
    now = pd.Timestamp.now(tz="UTC")
    provider.write_snapshot("AAPL", _row(fetched_at=now, pe_trailing=22.0))
    snap = provider.read_latest("AAPL")
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(22.0)
    assert snap["sector"] == "Technology"


def test_write_appends_and_dedupes_on_as_of_date(
    provider: FundamentalsProvider,
) -> None:
    base = pd.Timestamp("2026-05-01T12:00:00", tz="UTC")
    provider.write_snapshot("AAPL", _row(fetched_at=base, pe_trailing=10.0))
    # Newer fetched_at on the same as_of_date should override.
    provider.write_snapshot(
        "AAPL", _row(fetched_at=base + pd.Timedelta(hours=2), pe_trailing=11.0)
    )
    df = pd.read_parquet(provider.ticker_path("AAPL"))
    assert len(df) == 1
    assert df.iloc[0]["pe_trailing"] == pytest.approx(11.0)


def test_write_keeps_distinct_as_of_dates(
    provider: FundamentalsProvider,
) -> None:
    d1 = pd.Timestamp("2026-05-01T12:00:00", tz="UTC")
    d2 = pd.Timestamp("2026-05-02T12:00:00", tz="UTC")
    provider.write_snapshot("AAPL", _row(fetched_at=d1, pe_trailing=10.0))
    provider.write_snapshot("AAPL", _row(fetched_at=d2, pe_trailing=12.0))
    df = pd.read_parquet(provider.ticker_path("AAPL"))
    assert len(df) == 2
    # Latest read is the most recent fetched_at.
    snap = provider.read_latest("AAPL")
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(12.0)


def test_read_latest_respects_as_of_filter(provider: FundamentalsProvider) -> None:
    """as_of point-in-time semantics: rows with as_of_date > as_of are dropped."""
    d1 = pd.Timestamp("2026-05-01T12:00:00", tz="UTC")
    d2 = pd.Timestamp("2026-05-15T12:00:00", tz="UTC")
    provider.write_snapshot("AAPL", _row(fetched_at=d1, pe_trailing=10.0))
    provider.write_snapshot("AAPL", _row(fetched_at=d2, pe_trailing=12.0))
    snap = provider.read_latest("AAPL", as_of=pd.Timestamp("2026-05-10T00:00:00", tz="UTC"))
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Sector-median cache
# ---------------------------------------------------------------------------


def test_sector_median_missing_returns_none(provider: FundamentalsProvider) -> None:
    assert provider.read_sector_median_pe("Technology") is None
    assert provider.read_sector_median_pe(None) is None
    assert provider.read_sector_median_pe("") is None
    assert provider.read_sector_median_pe("unknown") is None


def test_refresh_sector_medians_aggregates(provider: FundamentalsProvider) -> None:
    now = pd.Timestamp.now(tz="UTC")
    provider.write_snapshot("AAA", _row(fetched_at=now, pe_trailing=10.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=now, pe_trailing=20.0, sector="Tech"))
    provider.write_snapshot("CCC", _row(fetched_at=now, pe_trailing=30.0, sector="Tech"))
    out = provider.refresh_sector_medians(["AAA", "BBB", "CCC"])
    assert "Tech" in out
    assert out["Tech"]["n"] == 3
    assert out["Tech"]["median_pe"] == pytest.approx(20.0)
    median = provider.read_sector_median_pe("Tech")
    assert median == pytest.approx(20.0)


def test_sector_median_skips_invalid_pe(provider: FundamentalsProvider) -> None:
    """Negative / zero / >1000 P/E should not contribute to the median."""
    now = pd.Timestamp.now(tz="UTC")
    provider.write_snapshot("AAA", _row(fetched_at=now, pe_trailing=10.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=now, pe_trailing=-5.0, sector="Tech"))
    provider.write_snapshot("CCC", _row(fetched_at=now, pe_trailing=99999.0, sector="Tech"))
    provider.write_snapshot("DDD", _row(fetched_at=now, pe_trailing=20.0, sector="Tech"))
    provider.refresh_sector_medians(["AAA", "BBB", "CCC", "DDD"])
    median = provider.read_sector_median_pe("Tech")
    # Only AAA (10) and DDD (20) are valid.
    assert median == pytest.approx(15.0)


def test_sector_median_hard_staleness_returns_none(
    cache_root: Path,
) -> None:
    """A sector_median row older than 30 days should NOT be returned."""
    p = FundamentalsProvider(cache_root=cache_root)
    old = pd.Timestamp("2026-01-01T00:00:00", tz="UTC")
    p.write_sector_median(
        "Tech",
        {
            "as_of_date": old,
            "fetched_at": old,
            "sector": "Tech",
            "median_pe_trailing": 18.0,
            "n_constituents": 5,
        },
    )
    asof = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")  # 120d later
    assert p.read_sector_median_pe("Tech", as_of=asof) is None


# ---------------------------------------------------------------------------
# Refresh fail-soft path
# ---------------------------------------------------------------------------


def test_refresh_skipped_when_yfinance_missing(
    provider: FundamentalsProvider, monkeypatch
) -> None:
    """If yfinance import fails, refresh returns 'skipped:no_yf' for every ticker."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "yfinance":
            raise ImportError("yfinance not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    out = provider.refresh(["AAPL", "MSFT"])
    assert out == {"AAPL": "skipped:no_yf", "MSFT": "skipped:no_yf"}


def test_refresh_skipped_when_within_ttl(
    provider: FundamentalsProvider, monkeypatch
) -> None:
    """If a recent snapshot exists, refresh skips the ticker."""
    pytest.importorskip(
        "yfinance", reason="refresh requires yfinance; skip when missing"
    )
    fresh = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1)
    provider.write_snapshot("AAPL", _row(fetched_at=fresh))
    out = provider.refresh(["AAPL"])
    assert out["AAPL"] == "skipped:fresh"


# ---------------------------------------------------------------------------
# Atomic-write resilience
# ---------------------------------------------------------------------------


def test_write_atomic_rename_no_partial_files(
    provider: FundamentalsProvider,
) -> None:
    """After a successful write there should be no .tmp / hidden files."""
    now = pd.Timestamp.now(tz="UTC")
    provider.write_snapshot("AAPL", _row(fetched_at=now))
    leftovers = [
        p for p in provider.yfinance_dir.iterdir() if p.name.endswith(".tmp")
    ]
    assert leftovers == []
