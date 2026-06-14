"""Unit tests for hermes_quant.data.fundamentals_provider.FundamentalsProvider.

Covers parquet cache mechanics (read/write/dedupe/atomic), sector-median
sibling cache, and staleness handling. yfinance network calls are NOT
exercised here — only the cache layer.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.data.fundamentals_provider import (
    DEFAULT_REPORTING_LAG_DAYS,
    REPORTING_LAG_ENV_FLAG,
    FundamentalsProvider,
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


def _row(
    *,
    fetched_at: pd.Timestamp,
    as_of_date: pd.Timestamp | None = None,
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
) -> dict:
    return {
        "as_of_date": (as_of_date or fetched_at).normalize(),
        "fetched_at": fetched_at,
        "report_date": report_date if report_date is not None else pd.NaT,
        "period_end": period_end if period_end is not None else pd.NaT,
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


def test_read_latest_respects_as_of_filter(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """as_of point-in-time semantics: rows with as_of_date > as_of are dropped.

    Pins the reporting-lag filter OFF: this test asserts the LEGACY
    ``as_of_date <= as_of`` predicate in isolation (rows carry NaT PIT
    columns, so default-ON would tighten via the as_of_date+lag fallback).
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
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


def test_refresh_sector_medians_aggregates(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cs41: this asserts the cold-path AGGREGATION (median arithmetic) via an
    # as_of=None live read (as_of defaults to now). A just-written median has
    # as_of_date=now.normalize(), so default-ON cs41 (now.normalize()+45d > now)
    # would go dark — that go-dark is asserted separately in
    # test_cs41_refresh_then_read_now_default_on_goes_dark. Pin the flag OFF here
    # so the aggregation value is observable (the as_of=None live path is not a
    # point-in-time backtest read; mirrors the cs12 pin at
    # test_read_latest_respects_as_of_filter).
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
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


def test_sector_median_skips_invalid_pe(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative / zero / >1000 P/E should not contribute to the median."""
    # cs41: flag OFF — see test_refresh_sector_medians_aggregates rationale (this
    # asserts the cold-path aggregation via an as_of=None live read).
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
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
# cs67: refresh() must NOT treat a FUTURE fetched_at as "fresh". refresh()
# computes age_h from read_latest(ticker) with as_of=None (the cold-refresh
# path the cs42(a)/cs53 read guards do NOT cover — those are scoped to the
# as_of-is-not-None point-in-time READ path). A cached row whose fetched_at is
# in the future relative to the cron wall-clock (NTP/clock-skew regression,
# container clock jump, a fabricated/corrupt parquet, or a backfill tool
# stamping a future timestamp) makes age_h NEGATIVE, so ``age_h < ttl_hours``
# is True and the ticker is returned 'skipped:fresh' PERMANENTLY — its
# fundamentals silently go stale and are never re-fetched until wall-clock
# advances past the future stamp. A future stamp answers NO to "is the cache
# fresh enough to SKIP re-fetching" -> force-refresh (treat max-stale).
# ---------------------------------------------------------------------------


def test_cs67_refresh_future_fetched_at_force_refreshes_not_skipped(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs67 RED->GREEN: a future-stamped fetched_at is NOT 'skipped:fresh'.

    A row stamped fetched_at = now + 2d yields age_h = -48.0, so the legacy
    ``age_h < ttl_hours`` (-48 < 24) is True and refresh() returns
    'skipped:fresh' — starved forever. After cs67 the future stamp is treated
    max-stale, so the ticker is force-refreshed ('ok' here, since the fetch is
    stubbed). The fetch is stubbed so this never touches the network.
    """
    pytest.importorskip(
        "yfinance", reason="refresh requires yfinance; skip when missing"
    )
    future = pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=2)
    provider.write_snapshot("AAPL", _row(fetched_at=future, pe_trailing=18.0))

    fetched: list[str] = []

    def _fake_fetch(ticker: str, asof: pd.Timestamp) -> dict:
        fetched.append(ticker)
        return _row(fetched_at=asof, pe_trailing=20.0)

    monkeypatch.setattr(provider, "_fetch_yfinance_snapshot", _fake_fetch)

    out = provider.refresh(["AAPL"])
    # RED: pre-cs67 the negative age_h defeats the staleness gate ->
    # 'skipped:fresh' (never re-fetched). GREEN: force-refresh -> 'ok'.
    assert out["AAPL"] == "ok"
    assert fetched == ["AAPL"], "the future-stamped ticker must be re-fetched"


def test_cs67_refresh_genuinely_fresh_past_stamp_still_skips_byte_identical(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs67 byte-identical: a genuinely-fresh PAST stamp still skips:fresh.

    A row fetched 1h ago (age_h = +1, within the 24h ttl) is the normal
    within-ttl case and must still be 'skipped:fresh' with NO fetch attempted —
    the cs67 clamp only fires for a NEGATIVE age (future stamp).
    """
    pytest.importorskip(
        "yfinance", reason="refresh requires yfinance; skip when missing"
    )
    fresh = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1)
    provider.write_snapshot("AAPL", _row(fetched_at=fresh, pe_trailing=18.0))

    fetched: list[str] = []

    def _fake_fetch(ticker: str, asof: pd.Timestamp) -> dict:
        fetched.append(ticker)
        return _row(fetched_at=asof, pe_trailing=20.0)

    monkeypatch.setattr(provider, "_fetch_yfinance_snapshot", _fake_fetch)

    out = provider.refresh(["AAPL"])
    assert out["AAPL"] == "skipped:fresh"
    assert fetched == [], "a genuinely-fresh past-stamped ticker must NOT be re-fetched"


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


# ---------------------------------------------------------------------------
# B34: reporting-lag-adjusted as_of (no-lookahead). Default-OFF behind
# HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG; ON only ever TIGHTENS visibility.
# ---------------------------------------------------------------------------


def _write_reportlag_row(
    p: FundamentalsProvider,
    *,
    ticker: str = "AAPL",
    as_of_date: pd.Timestamp,
    period_end: pd.Timestamp | None = None,
    report_date: pd.Timestamp | None = None,
) -> None:
    """Write one snapshot whose datum became knowable at report_date/period_end."""
    p.write_snapshot(
        ticker,
        _row(
            fetched_at=as_of_date + pd.Timedelta(hours=12),
            as_of_date=as_of_date,
            period_end=period_end,
            report_date=report_date,
            pe_trailing=18.0,
        ),
    )


def test_reporting_lag_default_constant_is_conservative() -> None:
    assert DEFAULT_REPORTING_LAG_DAYS == 45


def test_reporting_lag_flag_default_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """cs12: the reporting-lag filter is ON by default (no env var set).

    An ``as_of``-bounded read is a point-in-time read; default-ON closes the
    fundamental-lookahead leak without requiring an operator opt-in.
    """
    from hermes_quant.data.fundamentals_provider import _reporting_lag_flag_on

    monkeypatch.delenv(REPORTING_LAG_ENV_FLAG, raising=False)
    assert _reporting_lag_flag_on() is True
    # Explicit falsey values are the byte-identical OFF revert path.
    for off in ("0", "false", "False", "no", "off", " OFF ", ""):
        monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, off)
        assert _reporting_lag_flag_on() is False, off
    for on in ("1", "true", "yes", "on"):
        monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, on)
        assert _reporting_lag_flag_on() is True, on


def test_cs12_default_on_excludes_fundamental_lookahead_leak(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs12 RED->GREEN: the exact lookahead scenario, with NO flag set.

    A Q4 fundamental (period_end 2025-12-31) cached on 2026-01-14 is NOT
    publicly filed until ~mid-Feb (period_end + 45d = 2026-02-14). A backtest
    deciding at as_of 2026-01-15 must NOT see it. Pre-cs12 (flag OFF default)
    the legacy ``as_of_date <= as_of`` predicate alone RETURNED the row — a
    fundamental-lookahead leak. Default-ON excludes it until the lag horizon
    passes, then admits it.
    """
    monkeypatch.delenv(REPORTING_LAG_ENV_FLAG, raising=False)  # rely on default
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-01-14", tz="UTC"),  # cache day
        period_end=pd.Timestamp("2025-12-31", tz="UTC"),  # Q4 fiscal end
    )
    # Backtest decides Jan-15: period_end+45d = Feb-14 > Jan-15 -> EXCLUDED.
    assert provider.read_latest(
        "AAPL", as_of=pd.Timestamp("2026-01-15", tz="UTC")
    ) is None
    # Past the filing horizon (Feb-14) the datum is legitimately visible.
    snap = provider.read_latest("AAPL", as_of=pd.Timestamp("2026-02-20", tz="UTC"))
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(18.0)


def test_cs12_explicit_off_reverts_leak_byte_identical(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OFF revert path is byte-identical to pre-cs12: the leak returns.

    This is the documented kill switch — an operator who sets the flag falsey
    gets exactly the legacy ``as_of_date <= as_of`` behavior (leak and all).
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-01-14", tz="UTC"),
        period_end=pd.Timestamp("2025-12-31", tz="UTC"),
    )
    snap = provider.read_latest("AAPL", as_of=pd.Timestamp("2026-01-15", tz="UTC"))
    assert snap is not None  # legacy leak preserved on the explicit-OFF path
    assert snap["pe_trailing"] == pytest.approx(18.0)


def test_reporting_lag_columns_roundtrip(provider: FundamentalsProvider) -> None:
    """report_date / period_end are persisted and read back tz-aware."""
    pe = pd.Timestamp("2026-03-31", tz="UTC")
    rd = pd.Timestamp("2026-04-20", tz="UTC")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-25", tz="UTC"),
        period_end=pe,
        report_date=rd,
    )
    df = pd.read_parquet(provider.ticker_path("AAPL"))
    assert "report_date" in df.columns
    assert "period_end" in df.columns
    assert pd.Timestamp(df.iloc[0]["period_end"]) == pe
    assert pd.Timestamp(df.iloc[0]["report_date"]) == rd


def test_reporting_lag_flag_off_is_byte_identical(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag OFF: a row knowable only LATER is still visible (pre-B34 behavior).

    period_end 2026-03-31 + 45d lag = 2026-05-15 is AFTER as_of 2026-04-10, but
    with the flag explicitly OFF only the legacy ``as_of_date <= as_of``
    predicate applies. cs12 default-flipped the flag to ON, so OFF must now be
    requested with an explicit falsey value (the byte-identical revert path).
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-01", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
    )
    snap = provider.read_latest("AAPL", as_of=pd.Timestamp("2026-04-10", tz="UTC"))
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(18.0)


def test_reporting_lag_excludes_not_yet_reported_by_period_end(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE core B34 assertion (period_end fallback path).

    A fundamental whose period_end (2026-03-31) is BEFORE as_of (2026-04-10) but
    whose period_end + reporting_lag (45d -> 2026-05-15) is AFTER as_of is
    EXCLUDED — it was not yet reported as of 2026-04-10.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-01", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
    )
    assert provider.read_latest("AAPL", as_of=pd.Timestamp("2026-04-10", tz="UTC")) is None


def test_reporting_lag_excludes_not_yet_reported_by_report_date(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """report_date is preferred over period_end when both present.

    report_date 2026-04-20 + 45d = 2026-06-04 > as_of 2026-05-01 -> EXCLUDED.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-25", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
        report_date=pd.Timestamp("2026-04-20", tz="UTC"),
    )
    assert provider.read_latest("AAPL", as_of=pd.Timestamp("2026-05-01", tz="UTC")) is None


def test_reporting_lag_admits_once_lag_horizon_passed(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once as_of is past report_date + lag, the row becomes visible again."""
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-01", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
    )
    # period_end 2026-03-31 + 45d = 2026-05-15; read at 2026-06-01 -> visible.
    snap = provider.read_latest("AAPL", as_of=pd.Timestamp("2026-06-01", tz="UTC"))
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(18.0)


def test_reporting_lag_missing_pit_columns_falls_back_to_as_of_date(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No report_date AND no period_end -> fall back to as_of_date + lag.

    This only ever TIGHTENS: as_of_date 2026-04-01 + 45d = 2026-05-16 > as_of
    2026-04-10 -> EXCLUDED. A missing backfill never loosens visibility.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-01", tz="UTC"),
        period_end=None,
        report_date=None,
    )
    assert provider.read_latest("AAPL", as_of=pd.Timestamp("2026-04-10", tz="UTC")) is None
    assert (
        provider.read_latest("AAPL", as_of=pd.Timestamp("2026-06-01", tz="UTC"))
        is not None
    )


def test_reporting_lag_never_admits_beyond_off_path(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ON is a strict subset of OFF: a row OFF excludes is never admitted ON.

    as_of_date (2026-05-01) is AFTER as_of (2026-04-10), so OFF already drops it
    on the legacy predicate; ON must also drop it.
    """
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-05-01", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
        report_date=pd.Timestamp("2026-04-01", tz="UTC"),
    )
    as_of = pd.Timestamp("2026-04-10", tz="UTC")
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    assert provider.read_latest("AAPL", as_of=as_of) is None  # OFF
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    assert provider.read_latest("AAPL", as_of=as_of) is None  # ON


def test_reporting_lag_custom_lag_days(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A larger configured reporting_lag_days tightens further."""
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    p = FundamentalsProvider(cache_root=cache_root, reporting_lag_days=90)
    _write_reportlag_row(
        p,
        as_of_date=pd.Timestamp("2026-04-01", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
    )
    # period_end + 90d = 2026-06-29. Default 45d would admit at 2026-06-01; with
    # 90d it stays EXCLUDED there but is admitted once past 2026-06-29.
    assert p.read_latest("AAPL", as_of=pd.Timestamp("2026-06-01", tz="UTC")) is None
    assert (
        p.read_latest("AAPL", as_of=pd.Timestamp("2026-07-01", tz="UTC")) is not None
    )


def test_reporting_lag_no_op_without_as_of(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """as_of=None is a live 'most recent' read — the lag filter does not apply."""
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "1")
    _write_reportlag_row(
        provider,
        as_of_date=pd.Timestamp("2026-04-01", tz="UTC"),
        period_end=pd.Timestamp("2026-03-31", tz="UTC"),
    )
    snap = provider.read_latest("AAPL")  # no as_of -> latest row regardless of lag
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# cs41: read_sector_median_pe reporting-lag symmetry with read_latest.
# The pe_relative DENOMINATOR (sector median) must obey the same no-lookahead
# lag as the NUMERATOR (read_latest). Previously read_sector_median_pe filtered
# only ``as_of_date <= as_of`` and never applied _apply_reporting_lag_filter, so
# a backtest sector median could embed not-yet-public constituent fundamentals.
# ---------------------------------------------------------------------------


def _write_sector_median_row(
    p: FundamentalsProvider,
    *,
    sector: str = "Tech",
    as_of_date: pd.Timestamp,
    fetched_at: pd.Timestamp | None = None,
    median_pe: float = 20.0,
    n: int = 5,
) -> None:
    """Write one sector-median snapshot with an explicit as_of_date / fetched_at."""
    p.write_sector_median(
        sector,
        {
            "as_of_date": as_of_date,
            "fetched_at": fetched_at if fetched_at is not None else as_of_date,
            "sector": sector,
            "median_pe_trailing": median_pe,
            "n_constituents": n,
        },
    )


def test_cs41_sector_median_applies_reporting_lag_symmetric(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs41 RED->GREEN (default-ON): the sector median obeys the reporting lag.

    A median dated only ~10d before the read as_of (within the 45d not-yet-public
    window) must NOT be visible — its constituents' fundamentals were not all
    public yet. Pre-cs41 read_sector_median_pe filtered only ``as_of_date <=
    as_of`` and RETURNED it (lookahead). After cs41 the as_of_date+45d fallback
    drops it. A median dated >=45d before as_of (but freshly re-snapshotted so it
    passes the 30d hard-staleness) is correctly ADMITTED.
    """
    monkeypatch.delenv(REPORTING_LAG_ENV_FLAG, raising=False)  # rely on default-ON
    as_of = pd.Timestamp("2026-05-01", tz="UTC")
    # Within-lag-window median: as_of_date 10d before read -> as_of_date+45d > as_of.
    _write_sector_median_row(
        provider,
        as_of_date=as_of - pd.Timedelta(days=10),
        fetched_at=as_of - pd.Timedelta(days=10) + pd.Timedelta(hours=12),
        median_pe=22.0,
    )
    # RED: pre-cs41 returns 22.0; GREEN after cs41: None (lag drops it).
    assert provider.read_sector_median_pe("Tech", as_of=as_of) is None

    # Admit case: a fresh re-snapshot stamped at an OLD as_of_date (>=45d before
    # as_of) but fetched only ~5d ago (age < 30d hard-staleness passes; the
    # as_of_date+45d <= as_of lag passes too) -> median IS returned.
    p2 = FundamentalsProvider(cache_root=provider.cache_root / "admit")
    _write_sector_median_row(
        p2,
        as_of_date=as_of - pd.Timedelta(days=45),
        fetched_at=as_of - pd.Timedelta(days=5),
        median_pe=19.0,
    )
    assert p2.read_sector_median_pe("Tech", as_of=as_of) == pytest.approx(19.0)


def test_cs41_sector_median_flag_off_byte_identical(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs41 byte-identical revert: flag OFF returns the within-window median.

    The exact near-today median the default-ON path drops is RETURNED with the
    flag explicitly OFF — the pre-cs41 ``as_of_date <= as_of`` read path is
    preserved exactly (the cs12 kill switch covers this read too).
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    as_of = pd.Timestamp("2026-05-01", tz="UTC")
    _write_sector_median_row(
        provider,
        as_of_date=as_of - pd.Timedelta(days=10),
        fetched_at=as_of - pd.Timedelta(days=10) + pd.Timedelta(hours=12),
        median_pe=22.0,
    )
    assert provider.read_sector_median_pe("Tech", as_of=as_of) == pytest.approx(22.0)


def test_cs41_refresh_then_read_now_default_on_goes_dark(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs41 documents the default-ON change for the as_of=None / now read.

    A just-refreshed sector median (as_of_date = now.normalize()) read at the
    implicit as_of=now is DROPPED under default-ON (now.normalize()+45d > now ->
    conservative go-dark) and RETURNED under the flag OFF revert. This documents
    why the existing as_of=None aggregation tests must pin the flag OFF.
    """
    provider.write_snapshot("AAA", _row(fetched_at=pd.Timestamp.now(tz="UTC"), pe_trailing=10.0, sector="Tech"))
    provider.write_snapshot("BBB", _row(fetched_at=pd.Timestamp.now(tz="UTC"), pe_trailing=20.0, sector="Tech"))
    provider.write_snapshot("CCC", _row(fetched_at=pd.Timestamp.now(tz="UTC"), pe_trailing=30.0, sector="Tech"))
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    provider.refresh_sector_medians(["AAA", "BBB", "CCC"])  # write under OFF; read path is what matters
    # Default-ON: as_of=now read goes dark (now.normalize()+45d > now).
    monkeypatch.delenv(REPORTING_LAG_ENV_FLAG, raising=False)
    assert provider.read_sector_median_pe("Tech") is None
    # Flag OFF revert: the median is returned.
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    assert provider.read_sector_median_pe("Tech") == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# cs53: read_sector_median_pe must drop a row whose fetched_at is strictly
# AFTER as_of — the SYMMETRIC future-fetch guard read_latest got in cs42(a) but
# read_sector_median_pe never did. as_of_date is day-normalized at write, so a
# same-day intraday-future fetched_at (or a fabricated future timestamp)
# silently passes the ``as_of_date <= as_of`` snapshot filter, then yields a
# NEGATIVE age_days that defeats the ``age_days > SECTOR_MEDIAN_STALE_HARD_DAYS``
# staleness gate (negative is never > 30) -> the future-fetched DENOMINATOR
# (sector median) is accepted. cs41 insists the pe_relative denominator obey the
# same no-lookahead discipline as the read_latest numerator.
# ---------------------------------------------------------------------------


def test_cs53_sector_median_future_fetched_at_excluded_at_as_of(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs53 RED->GREEN: a sector median fetched in the future relative to as_of is dropped.

    Flag OFF isolates the fetched_at guard from the reporting-lag filter (else
    the as_of_date+45d fallback would also drop the row, masking the bug). The
    median's as_of_date = D 00:00 (normalized) passes ``as_of_date <= as_of`` at
    as_of = D 00:00, but fetched_at = D+12h is strictly AFTER as_of -> it was
    fetched in the FUTURE. age_days = (D - (D+12h)).days = -1, so the staleness
    gate (age_days > 30) is False and pre-cs53 the future-fetched median is
    RETURNED. After cs53 the fetched_at guard drops it -> None.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    d = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    _write_sector_median_row(
        provider,
        as_of_date=d,
        fetched_at=d + pd.Timedelta(hours=12),
        median_pe=22.0,
    )
    # RED: pre-cs53 returns 22.0 (negative age defeats the staleness gate);
    # GREEN: future fetched_at dropped -> None.
    assert provider.read_sector_median_pe("Tech", as_of=d) is None


def test_cs53_sector_median_fetched_at_le_as_of_byte_identical(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs53 byte-identical: the guard only drops STRICTLY-future fetches.

    fetched_at = D-1h is before the read as_of = D+1d, so a median legitimately
    fetched before the point-in-time read is unchanged — still returned (and the
    age_days < 30 staleness gate still passes).
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    d = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    _write_sector_median_row(
        provider,
        as_of_date=d,
        fetched_at=d - pd.Timedelta(hours=1),
        median_pe=22.0,
    )
    assert provider.read_sector_median_pe(
        "Tech", as_of=d + pd.Timedelta(days=1)
    ) == pytest.approx(22.0)


def test_cs53_sector_median_no_as_of_latest_path_untouched(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs53 scope: the as_of=None live read is unaffected by the fetched_at guard.

    The guard lives on the as_of-bounded path only (mirroring cs42(a) in
    read_latest). With as_of=None the read defaults asof_ts to now(UTC); a median
    fetched ~1h ago is the normal live case and is still returned.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    now = pd.Timestamp.now(tz="UTC")
    _write_sector_median_row(
        provider,
        as_of_date=now.normalize(),
        fetched_at=now - pd.Timedelta(hours=1),
        median_pe=22.0,
    )
    assert provider.read_sector_median_pe("Tech") == pytest.approx(22.0)


# ---------------------------------------------------------------------------
# cs59: write_sector_median dedupe must PRESERVE the historical point-in-time
# median — the WRITE-side symmetry of cs42(b) (which hardened write_snapshot).
# The sector median is the pe_relative DENOMINATOR; a past-as-of read
# (read_sector_median_pe, cs53 fetched_at<=as_of) must return the SAME value
# across re-fetches, else a backtest replayed after refresh_sector_medians runs
# again sees a mutated denominator. Pre-cs59 write_sector_median did a blind
# keep-latest-fetched_at dedupe (no _same_day guard), so a cross-day backfill of
# an already-written as_of_date overwrote the historical PIT row.
# ---------------------------------------------------------------------------


def test_cs59_sector_median_backfill_does_not_rewrite_historical_pit(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs59 RED->GREEN: a cross-day sector-median backfill preserves the historical PIT.

    Original PIT snapshot: as_of_date 2026-05-01, fetched_at 2026-05-01 - 1h
    (knowable at the read as_of), median 20.0. A point-in-time read at
    as_of = 2026-05-01 + 1h returns 20.0. A backfill re-fetch carrying the SAME
    old as_of_date but a CROSS-DAY-later fetched_at (2026-05-03) with median 99.0
    must NOT win. Pre-cs59 the blind keep-latest-fetched_at dedupe overwrote the
    row with 99.0, and the cs53 fetched_at<=as_of read guard then dropped the
    future-fetched survivor -> the same past-as-of read flips 20.0 -> None
    (history destroyed). After cs59 the same-day-correct PIT row (20.0) survives
    and the past-as-of read is stable across the re-fetch.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    pit = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    read_at = pit + pd.Timedelta(hours=1)
    _write_sector_median_row(
        provider,
        as_of_date=pit,
        fetched_at=pit - pd.Timedelta(hours=1),
        median_pe=20.0,
    )
    before = provider.read_sector_median_pe("Tech", as_of=read_at)
    assert before == pytest.approx(20.0)

    # Cross-day backfill of the SAME old as_of_date with a much later fetched_at.
    _write_sector_median_row(
        provider,
        as_of_date=pit,
        fetched_at=pit + pd.Timedelta(days=2),
        median_pe=99.0,
    )
    # RED: pre-cs59 the historical row is overwritten (99.0) -> the cs53 read
    # guard drops the future-fetched survivor -> past-as-of read becomes None.
    # GREEN: the same-day PIT median (20.0) survives -> the read is stable.
    after = provider.read_sector_median_pe("Tech", as_of=read_at)
    assert after == pytest.approx(20.0)

    # Exactly one row for that as_of_date, carrying the original PIT median.
    df = pd.read_parquet(provider.sector_median_path("Tech"))
    same_date = df[df["as_of_date"] == pit]
    assert len(same_date) == 1
    assert same_date.iloc[0]["median_pe_trailing"] == pytest.approx(20.0)


def test_cs59_sector_median_same_day_correction_still_wins(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs59 scope: a SAME-DAY correction (later fetched_at, same calendar day) wins.

    The cs59 guard only protects cross-day backfills; a legitimate intraday
    revision (a newer fetched_at on the same UTC calendar day as the as_of_date)
    is still the authoritative value, exactly as cs42(b) preserves on the
    per-ticker write side.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    pit = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    _write_sector_median_row(
        provider, as_of_date=pit, fetched_at=pit + pd.Timedelta(hours=2), median_pe=20.0
    )
    _write_sector_median_row(
        provider, as_of_date=pit, fetched_at=pit + pd.Timedelta(hours=10), median_pe=21.0
    )
    df = pd.read_parquet(provider.sector_median_path("Tech"))
    same_date = df[df["as_of_date"] == pit]
    assert len(same_date) == 1
    assert same_date.iloc[0]["median_pe_trailing"] == pytest.approx(21.0)


def test_cs59_sector_median_first_write_byte_identical(
    provider: FundamentalsProvider,
) -> None:
    """cs59 scope: a first-write of a NEW as_of_date is byte-identical.

    The guard only fires on a re-write of an existing as_of_date. A single write
    (even a cross-day fetched_at) lands as one clean row with the temporary
    _same_day ranking column dropped — no schema leak.
    """
    pit = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    _write_sector_median_row(
        provider, as_of_date=pit, fetched_at=pit + pd.Timedelta(days=2), median_pe=33.0
    )
    df = pd.read_parquet(provider.sector_median_path("Tech"))
    assert len(df) == 1
    assert df.iloc[0]["median_pe_trailing"] == pytest.approx(33.0)
    assert "_same_day" not in df.columns


# ---------------------------------------------------------------------------
# cs42(a): read_latest must drop a row whose fetched_at is strictly AFTER as_of.
# as_of_date is day-normalized at write, so a same-day intraday-future fetched_at
# (or a fabricated future timestamp) silently passes the ``as_of_date <= as_of``
# snapshot filter and yields a negative age that defeats the staleness gate.
# ---------------------------------------------------------------------------


def test_cs42_future_fetched_at_excluded_at_as_of(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs42(a) RED->GREEN: a row fetched in the future relative to as_of is dropped.

    Flag OFF isolates the fetched_at guard from the reporting-lag filter (else
    the lag would also drop the row). as_of_date = D 00:00 (normalized) passes
    ``as_of_date <= as_of`` at as_of = D 00:00, but fetched_at = D+12h is
    strictly after as_of -> the row was fetched in the FUTURE. Pre-cs42 it was
    returned (negative age); after cs42 it is None.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    d = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    provider.write_snapshot(
        "AAPL", _row(fetched_at=d + pd.Timedelta(hours=12), as_of_date=d, pe_trailing=18.0)
    )
    # RED: pre-cs42 returns the row; GREEN: future fetched_at dropped -> None.
    assert provider.read_latest("AAPL", as_of=d) is None


def test_cs42_fetched_at_le_as_of_byte_identical(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs42(a) byte-identical: the guard only drops STRICTLY-future fetches.

    fetched_at = D-1h is before the read as_of = D+1d, so the normal live case
    (a row fetched before the point-in-time read) is unchanged — still returned.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    d = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    provider.write_snapshot(
        "AAPL", _row(fetched_at=d - pd.Timedelta(hours=1), as_of_date=d, pe_trailing=18.0)
    )
    snap = provider.read_latest("AAPL", as_of=d + pd.Timedelta(days=1))
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(18.0)


def test_cs42_no_as_of_latest_path_untouched(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs42(a) scope: the as_of=None latest-read path is unaffected by the guard.

    refresh / refresh_sector_medians read with as_of=None; the fetched_at guard
    lives inside ``if as_of is not None`` so a future-fetched row is still
    returned by the latest path.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    d = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    provider.write_snapshot(
        "AAPL", _row(fetched_at=d + pd.Timedelta(hours=12), as_of_date=d, pe_trailing=18.0)
    )
    snap = provider.read_latest("AAPL")  # no as_of -> latest path
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# cs42(b): write_snapshot dedupe must PRESERVE the historical point-in-time
# value. A cross-day backfill (fetched_at on a later calendar day than the
# row's as_of_date) must NOT overwrite a row already recorded same-day-correct
# for that as_of_date — else a re-fetch rewrites a historical PIT read.
# ---------------------------------------------------------------------------


def test_cs42_backfill_does_not_rewrite_historical_pit(
    provider: FundamentalsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs42(b) RED->GREEN: a cross-day backfill does not rewrite a historical PIT row.

    Original PIT snapshot: as_of_date 2026-05-01, fetched_at 2026-05-01T12:00,
    pe 10.0 (recorded same-day -> point-in-time-correct). A backfill re-fetch
    carrying the SAME old as_of_date but TODAY's fetched_at (2026-06-13T12:00,
    cross-day) with pe 99.0 must NOT win. Pre-cs42 the blind keep-latest-
    fetched_at dedupe returned 99.0 (history rewritten); after cs42 the same-day
    PIT row (10.0) is preserved.
    """
    monkeypatch.setenv(REPORTING_LAG_ENV_FLAG, "0")
    pit_date = pd.Timestamp("2026-05-01T00:00:00", tz="UTC")
    provider.write_snapshot(
        "AAPL",
        _row(fetched_at=pit_date + pd.Timedelta(hours=12), as_of_date=pit_date, pe_trailing=10.0),
    )
    # Cross-day backfill of the SAME old as_of_date with a much later fetched_at.
    provider.write_snapshot(
        "AAPL",
        _row(
            fetched_at=pd.Timestamp("2026-06-13T12:00:00", tz="UTC"),
            as_of_date=pit_date,
            pe_trailing=99.0,
        ),
    )
    # RED: pre-cs42 returns 99.0; GREEN: the historical PIT value (10.0) survives.
    snap = provider.read_latest("AAPL", as_of=pd.Timestamp("2026-05-01T23:59:00", tz="UTC"))
    assert snap is not None
    assert snap["pe_trailing"] == pytest.approx(10.0)
    # Exactly one row for that as_of_date, carrying the original PIT pe.
    df = pd.read_parquet(provider.ticker_path("AAPL"))
    same_date = df[df["as_of_date"] == pit_date]
    assert len(same_date) == 1
    assert same_date.iloc[0]["pe_trailing"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# cs61: corrupt-parquet read-error branch must QUARANTINE the existing file
# (recoverable .corrupt sidecar + loud warning) instead of silently truncating
# the entire historical point-in-time series to the single new row. Same
# write-side PIT-history-mutation class as cs42(b) / cs59, but triggered by a
# READ ERROR (transient / torn / genuinely-corrupt read) rather than a
# re-fetch, and a SILENT fail-open data-loss. The fix is fail-CLOSED: never
# silently destroy history.
# ---------------------------------------------------------------------------


def _corrupt(path: Path) -> None:
    """Overwrite a parquet file with bytes that make pd.read_parquet raise."""
    path.write_bytes(b"PAR1\x00\x00not-a-valid-parquet-footer")


def test_cs61_write_snapshot_corrupt_parquet_quarantines_not_truncates(
    provider: FundamentalsProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """cs61 RED->GREEN (per-ticker): a corrupt read quarantines, never truncates.

    RED (pre-cs61): the read-error branch reset ``existing`` to an EMPTY frame
    and atomic-wrote the single new row over the file — the ENTIRE historical
    PIT series (3 distinct as_of_dates here) was irrecoverably destroyed, with
    only a logger.warning. GREEN: the corrupt bytes are MOVED to a recoverable
    ``.corrupt`` sidecar BEFORE starting fresh, a loud warning fires, and the
    write still makes forward progress (the new row lands).
    """
    base = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    for i in range(3):  # 3 historical PIT snapshots, distinct as_of_dates
        provider.write_snapshot(
            "AAPL", _row(fetched_at=base + pd.Timedelta(days=31 * i), pe_trailing=10.0 + i)
        )
    path = provider.ticker_path("AAPL")
    assert len(pd.read_parquet(path)) == 3

    # Simulate a torn/corrupt read, then append a new snapshot. The on-disk
    # bytes at read-failure time are the corrupt bytes; the quarantine must
    # preserve EXACTLY those bytes (so a human / repair job can recover whatever
    # of the history is salvageable), not silently delete them.
    _corrupt(path)
    corrupt_bytes = path.read_bytes()
    with caplog.at_level("WARNING"):
        provider.write_snapshot(
            "AAPL", _row(fetched_at=base + pd.Timedelta(days=200), pe_trailing=99.0)
        )

    # The corrupt bytes were QUARANTINED (recoverable), not deleted.
    sidecars = [p for p in path.parent.iterdir() if p.name.endswith(".corrupt")]
    assert len(sidecars) == 1, "exactly one quarantine sidecar expected"
    assert sidecars[0].read_bytes() == corrupt_bytes, "corrupt file must be recoverable"
    # A LOUD warning fired (fail-loud, not silent).
    assert any(
        "QUARANTINED" in r.getMessage() and "AAPL" in r.getMessage()
        for r in caplog.records
    )
    # Forward progress: the new row landed (history of the corrupt file is in the
    # sidecar, not silently merged — that is the documented quarantine tradeoff).
    live = pd.read_parquet(path)
    assert len(live) == 1
    assert live.iloc[0]["pe_trailing"] == pytest.approx(99.0)


def test_cs61_write_snapshot_normal_read_is_byte_identical(
    provider: FundamentalsProvider,
) -> None:
    """cs61 byte-identical: a READABLE existing parquet appends + preserves history.

    The quarantine branch only fires when ``pd.read_parquet`` RAISES. A normal
    (non-corrupt) read is unchanged: the new row is appended, all prior PIT rows
    survive, and NO ``.corrupt`` sidecar is created.
    """
    base = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    for i in range(3):
        provider.write_snapshot(
            "AAPL", _row(fetched_at=base + pd.Timedelta(days=31 * i), pe_trailing=10.0 + i)
        )
    provider.write_snapshot(
        "AAPL", _row(fetched_at=base + pd.Timedelta(days=200), pe_trailing=12.0)
    )
    path = provider.ticker_path("AAPL")
    df = pd.read_parquet(path)
    assert len(df) == 4, "all 3 historical rows + the new one survive"
    assert [p for p in path.parent.iterdir() if p.name.endswith(".corrupt")] == []


def test_cs61_write_sector_median_corrupt_parquet_quarantines_not_truncates(
    provider: FundamentalsProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """cs61 RED->GREEN (sector median): symmetric with write_snapshot.

    The sector median is the pe_relative DENOMINATOR. Pre-cs61 a corrupt read
    here silently overwrote the entire historical median series with the single
    new row — and without even a warning (the branch had a bare ``except`` with
    no log). GREEN: quarantine the corrupt bytes to a recoverable sidecar and
    fire a loud warning.
    """
    base = pd.Timestamp("2026-01-01", tz="UTC")
    for i in range(3):
        _write_sector_median_row(
            provider, as_of_date=base + pd.Timedelta(days=31 * i), median_pe=18.0 + i
        )
    path = provider.sector_median_path("Tech")
    assert len(pd.read_parquet(path)) == 3

    _corrupt(path)
    corrupt_bytes = path.read_bytes()
    with caplog.at_level("WARNING"):
        _write_sector_median_row(
            provider, as_of_date=base + pd.Timedelta(days=200), median_pe=22.0
        )

    sidecars = [p for p in path.parent.iterdir() if p.name.endswith(".corrupt")]
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == corrupt_bytes, "corrupt median file recoverable"
    assert any(
        "QUARANTINED" in r.getMessage() and "Tech" in r.getMessage()
        for r in caplog.records
    )
    live = pd.read_parquet(path)
    assert len(live) == 1
    assert live.iloc[0]["median_pe_trailing"] == pytest.approx(22.0)


def test_cs61_write_sector_median_normal_read_is_byte_identical(
    provider: FundamentalsProvider,
) -> None:
    """cs61 byte-identical: a readable sector-median parquet appends + preserves."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    for i in range(3):
        _write_sector_median_row(
            provider, as_of_date=base + pd.Timedelta(days=31 * i), median_pe=18.0 + i
        )
    _write_sector_median_row(
        provider, as_of_date=base + pd.Timedelta(days=200), median_pe=21.0
    )
    path = provider.sector_median_path("Tech")
    assert len(pd.read_parquet(path)) == 4
    assert [p for p in path.parent.iterdir() if p.name.endswith(".corrupt")] == []


def test_cs61_repeated_corrupt_reads_do_not_clobber_prior_quarantine(
    provider: FundamentalsProvider,
) -> None:
    """cs61: two corrupt reads of the same path produce two distinct sidecars.

    The sidecar name carries a uniqueness counter so a second quarantine within
    the same second never overwrites the first — both corrupt generations stay
    recoverable.
    """
    base = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    provider.write_snapshot("AAPL", _row(fetched_at=base, pe_trailing=10.0))
    path = provider.ticker_path("AAPL")

    _corrupt(path)
    provider.write_snapshot("AAPL", _row(fetched_at=base + pd.Timedelta(days=1), pe_trailing=11.0))
    _corrupt(path)
    provider.write_snapshot("AAPL", _row(fetched_at=base + pd.Timedelta(days=2), pe_trailing=12.0))

    sidecars = [p for p in path.parent.iterdir() if p.name.endswith(".corrupt")]
    assert len(sidecars) == 2, "each corrupt read gets its own recoverable sidecar"
