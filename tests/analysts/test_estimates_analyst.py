"""Tests for hermes_quant.analysts.estimates.EstimatesAnalyst (aegis-ob2).

Offline / deterministic — NO live network and NO openbb installed. The
analyst reads an injected ``OpenBBEstimates`` provider (its own ``obb`` seam
is a fake), so the contract / default-OFF / finite-guard tests run WITHOUT
the ``openbb`` SDK being importable.

Covers (per the ob2 seed):
  3. CONTRACT: EstimatesAnalyst.analyze returns a valid AnalystView (right
     fields/types) when data exists; None when absent (abstain).
  4. DEFAULT-OFF byte-identical: HERMES_QUANT_ESTIMATES_ANALYST /
     HERMES_QUANT_OPENBB unset -> the analyst abstains (returns None) and
     never touches the provider (no openbb import). Flag-on -> contributes.
  5. FINITE-GUARD: a NaN/inf estimate never drives a view (abstain).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from hermes_quant.analysts.estimates import (
    ESTIMATES_ANALYST_FLAG,
    OPENBB_ENABLE_FLAG,
    EstimatesAnalyst,
)
from hermes_quant.protocol import AnalystView, MarketContext


# ---------------------------------------------------------------------------
# Fixtures: a stub OpenBBEstimates provider that returns a pre-built frame
# (the analyst depends only on read_estimates(ticker, as_of) -> DataFrame).
# ---------------------------------------------------------------------------
class _StubEstimatesProvider:
    """Minimal stand-in for OpenBBEstimates.read_estimates."""

    def __init__(self, frame: pd.DataFrame | None):
        self._frame = frame
        self.calls: list[dict] = []

    def read_estimates(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        self.calls.append({"ticker": ticker, "as_of": as_of})
        if self._frame is None:
            return pd.DataFrame()
        return self._frame.copy()


def _ctx(asset: str = "AAPL", asset_class: str = "equity") -> MarketContext:
    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-05-13", "2026-05-14", "2026-05-15"]),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [1e6, 1e6, 1e6],
        }
    )
    return MarketContext(
        asset=asset,
        timeframe="1d",
        asset_class=asset_class,
        exchange=None,
        bars=bars,
        last_close=102.5,
        last_volume=1e6,
        asof=pd.Timestamp("2026-05-15", tz="UTC"),
    )


def _rising_estimates() -> pd.DataFrame:
    """Forward EPS estimate REVISED UP across two recent publish dates
    (eps_avg 7.0 -> 7.3) -> bullish revision -> +1 direction."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-30", "2026-05-15"]),
            "eps_avg": [7.0, 7.3],
            "eps_prior": [6.9, 7.0],
            "revenue_avg": [4.10e11, 4.15e11],
            "analyst_count": [30, 31],
        }
    )


def _falling_estimates() -> pd.DataFrame:
    """Forward EPS estimate REVISED DOWN (7.5 -> 7.0) -> bearish -> -1."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-30", "2026-05-15"]),
            "eps_avg": [7.5, 7.0],
            "eps_prior": [7.6, 7.5],
            "revenue_avg": [4.20e11, 4.10e11],
            "analyst_count": [30, 31],
        }
    )


def _analyst(frame: pd.DataFrame | None) -> EstimatesAnalyst:
    return EstimatesAnalyst(provider=_StubEstimatesProvider(frame))


# ===========================================================================
# 4. DEFAULT-OFF byte-identical (flags unset -> abstain, provider untouched)
# ===========================================================================
def test_default_off_abstains_and_does_not_touch_provider(monkeypatch):
    """With HERMES_QUANT_ESTIMATES_ANALYST / HERMES_QUANT_OPENBB unset, the
    analyst returns None and NEVER calls the provider (no openbb import path).

    RED-proof: if analyze ran the scoring path it would call read_estimates
    (calls != []) and could return a view (not None).
    """
    monkeypatch.delenv(ESTIMATES_ANALYST_FLAG, raising=False)
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    stub = _StubEstimatesProvider(_rising_estimates())
    an = EstimatesAnalyst(provider=stub)
    out = an.analyze(_ctx())

    assert out is None
    assert stub.calls == []  # provider never reached -> no openbb import attempted
    assert an.enabled is False  # class-level enabled reflects the flag gate


def test_only_one_flag_set_still_off(monkeypatch):
    """Both flags are required. With only ESTIMATES_ANALYST set (OPENBB unset)
    the analyst still abstains."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    stub = _StubEstimatesProvider(_rising_estimates())
    an = EstimatesAnalyst(provider=stub)
    assert an.analyze(_ctx()) is None
    assert stub.calls == []


def test_both_flags_on_contributes(monkeypatch):
    """Both flags ON -> the analyst runs and reaches the provider."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    stub = _StubEstimatesProvider(_rising_estimates())
    an = EstimatesAnalyst(provider=stub)
    out = an.analyze(_ctx())

    assert out is not None
    assert stub.calls and stub.calls[0]["ticker"] == "AAPL"
    # as_of is forwarded for the point-in-time read.
    assert stub.calls[0]["as_of"] is not None


# ===========================================================================
# 3. CONTRACT — valid AnalystView when data exists; None when absent
# ===========================================================================
def test_contract_returns_valid_analyst_view(monkeypatch):
    """A bullish revision yields a well-formed AnalystView (+1, calibrated
    confidence in [0,1], right field types)."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(_rising_estimates())
    out = an.analyze(_ctx())

    assert isinstance(out, AnalystView)
    assert out.analyst == "estimates"
    assert out.direction == 1  # eps revised up -> bullish
    assert isinstance(out.confidence, float)
    assert 0.0 <= out.confidence <= 1.0
    assert isinstance(out.confidence_raw, float)
    assert isinstance(out.magnitude, float)
    assert out.magnitude >= 0.0
    assert isinstance(out.horizon, str)


def test_contract_bearish_revision_direction(monkeypatch):
    """A downward revision yields direction == -1."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(_falling_estimates())
    out = an.analyze(_ctx())
    assert isinstance(out, AnalystView)
    assert out.direction == -1


def test_contract_absent_data_abstains(monkeypatch):
    """No estimates -> None (Protocol-clean abstain, not a zero-conf view)."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(None)  # provider returns an empty frame
    assert an.analyze(_ctx()) is None


def test_contract_single_estimate_no_revision_abstains(monkeypatch):
    """Only one estimate row (no prior to measure a revision against) -> None."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    one = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-15"]),
            "eps_avg": [7.3],
            "eps_prior": [7.0],
            "revenue_avg": [4.15e11],
            "analyst_count": [31],
        }
    )
    an = _analyst(one)
    # A single estimate with NO prior revision signal -> abstain (silence).
    assert an.analyze(_ctx()) is None


def test_non_equity_abstains(monkeypatch):
    """A crypto / non-equity asset abstains (estimates are equity-only)."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(_rising_estimates())
    assert an.analyze(_ctx(asset="BTC/USDT", asset_class="crypto")) is None


# ===========================================================================
# 5. FINITE-GUARD — a NaN/inf estimate never drives a view
# ===========================================================================
def test_finite_guard_nan_estimate_abstains(monkeypatch):
    """The latest estimate eps_avg is NaN -> abstain (never a view).

    RED-proof: without the finite-guard the NaN revision ratio would produce a
    direction / confidence and a (poisoned) view.
    """
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-30", "2026-05-15"]),
            "eps_avg": [7.0, float("nan")],  # latest poisoned
            "eps_prior": [6.9, 7.0],
            "revenue_avg": [4.10e11, 4.15e11],
            "analyst_count": [30, 31],
        }
    )
    an = _analyst(df)
    assert an.analyze(_ctx()) is None


def test_finite_guard_inf_estimate_abstains(monkeypatch):
    """An inf eps_avg -> abstain (inf defeats every `<=` gate)."""
    monkeypatch.setenv(ESTIMATES_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-30", "2026-05-15"]),
            "eps_avg": [7.0, float("inf")],
            "eps_prior": [6.9, 7.0],
            "revenue_avg": [4.10e11, 4.15e11],
            "analyst_count": [30, 31],
        }
    )
    an = _analyst(df)
    assert an.analyze(_ctx()) is None
