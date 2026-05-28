"""Backtest ablations for FundamentalsAnalyst (ADR-0064 §Test Plan).

Two ablations specified by the ADR:

  B1: SPY 2020-01-01 → 2025-12-31, hermes-quant w/ FundamentalsAnalyst
      vs. without — measure Sharpe / max-DD delta.
  B2: Synthetic stress: feed COVID-March-2020-shaped fundamentals
      (negative FCF, Rev YoY < -10%, EPS miss) — assert direction == -1
      on impacted names.

These are heavyweight backtests that need the universe-bar cache and
yfinance history. The full implementation lands behind
`HERMES_QUANT_RUN_BACKTEST=1` in the next subagent; here we keep a
runnable harness skeleton with clear xfail/skip semantics so the test
plan row is checked in but does not stall CI.

Per the v0.6.1 charter: backtest ablations are a *release-gate* check,
not a per-PR check.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from hermes_quant.analysts.fundamentals import FundamentalsAnalyst
from hermes_quant.data.fundamentals_provider import FundamentalsProvider


_RUN_FLAG = "HERMES_QUANT_RUN_BACKTEST"


def _run_full_backtests() -> bool:
    return os.environ.get(_RUN_FLAG, "0") == "1"


@pytest.mark.skipif(
    not _run_full_backtests(),
    reason=(
        "B1 SPY 2020-2025 ablation needs full bar cache + yfinance history; "
        f"set {_RUN_FLAG}=1 (release-gate, not per-PR)."
    ),
)
def test_b1_spy_ablation_sharpe_nonzero() -> None:  # pragma: no cover - release-gate
    """B1: with-vs-without FundamentalsAnalyst over SPY 2020-2025.

    Acceptance: |Sharpe(with) − Sharpe(without)| > 0.0 — i.e. the analyst
    actually changes portfolio behavior. (No directional Sharpe target —
    that's a v0.7 question once the calibrator has trained on real fills.)
    """
    raise NotImplementedError(
        "B1 ablation harness lands in a follow-up subagent; "
        "scaffold present so the test plan row is reachable from CI."
    )


def test_b2_covid_stress_synthetic_short_bias(tmp_path) -> None:
    """B2: synthetic COVID-March-2020 fundamentals → analyst tilts short.

    This one CAN run in unit-test scope because we hand-build the snapshot:
    no network, no historical bars, only the analyst's reaction function.
    """
    cache_root = tmp_path / "fundamentals"
    provider = FundamentalsProvider(cache_root=cache_root)
    asof = pd.Timestamp("2020-03-31T20:00:00", tz="UTC")
    fetched = asof - pd.Timedelta(hours=2)

    # Sector benchmark (Tech) calibrated to early-2020 levels.
    provider.write_snapshot(
        "AAA",
        {
            "as_of_date": fetched.normalize(),
            "fetched_at": fetched,
            "source": "yfinance",
            "pe_trailing": 22.0,
            "pe_forward": 22.0,
            "debt_to_equity": 1.0,
            "free_cash_flow": 1e9,
            "revenue_ttm": 5e10,
            "eps_trailing": 4.0,
            "eps_forward": 4.0,
            "gross_margin_ttm": 0.40,
            "gross_margin_prior": 0.40,
            "revenue_yoy": 0.05,
            "fcf_yoy": 0.05,
            "sector": "Industrials",
            "currency": "USD",
            "quote_type": "EQUITY",
        },
    )
    provider.write_snapshot(
        "BBB",
        {
            "as_of_date": fetched.normalize(),
            "fetched_at": fetched,
            "source": "yfinance",
            "pe_trailing": 24.0,
            "pe_forward": 24.0,
            "debt_to_equity": 1.0,
            "free_cash_flow": 1.5e9,
            "revenue_ttm": 6e10,
            "eps_trailing": 4.0,
            "eps_forward": 4.0,
            "gross_margin_ttm": 0.40,
            "gross_margin_prior": 0.40,
            "revenue_yoy": 0.05,
            "fcf_yoy": 0.05,
            "sector": "Industrials",
            "currency": "USD",
            "quote_type": "EQUITY",
        },
    )
    provider.refresh_sector_medians(["AAA", "BBB"])

    # Subject: a representative travel-and-leisure name in March 2020.
    # Negative FCF, suspended guidance (forward EPS = trailing/2 stand-in),
    # revenue YoY collapse, P/E suddenly rich because price stickier than
    # earnings on the way down.
    provider.write_snapshot(
        "AAL",
        {
            "as_of_date": fetched.normalize(),
            "fetched_at": fetched,
            "source": "yfinance",
            "pe_trailing": 35.0,  # > 1.30 × sector median 23 → rich
            "pe_forward": 45.0,  # > trailing → deteriorating outlook
            "debt_to_equity": 6.0,  # covenant risk
            "free_cash_flow": -2e9,  # cash burning hard
            "revenue_ttm": 4e10,
            "eps_trailing": 1.0,
            "eps_forward": 2.0,  # ratio 0.5 → severe miss
            "gross_margin_ttm": 0.20,
            "gross_margin_prior": 0.30,
            "revenue_yoy": -0.35,  # < -0.10 → declining
            "fcf_yoy": -0.80,
            "sector": "Industrials",
            "currency": "USD",
            "quote_type": "EQUITY",
        },
    )

    analyst = FundamentalsAnalyst(provider=provider)
    # Build a minimal MarketContext (the analyst doesn't read bars,
    # but the dataclass needs them).
    import numpy as np

    ts = pd.date_range(end=asof, periods=30, freq="1D", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": np.full(30, 12.0),
            "high": np.full(30, 12.5),
            "low": np.full(30, 11.5),
            "close": np.full(30, 12.0),
            "volume": np.full(30, 1e7),
        }
    )
    from hermes_quant.protocol import MarketContext

    ctx = MarketContext(
        asset="AAL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=12.0,
        last_volume=1e7,
        asof=asof,
    )
    view = analyst.analyze(ctx)
    assert view is not None, "B2 stress: analyst silenced when it should have spoken"
    assert view.direction == -1, (
        f"B2 stress: expected short bias on COVID-shaped fundamentals, "
        f"got direction={view.direction}"
    )
