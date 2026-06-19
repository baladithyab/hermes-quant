"""Tests for hermes_quant.analysts.insider.InsiderAnalyst (aegis-ob3).

Offline / deterministic — NO live network and NO openbb installed. The analyst
reads injected ``OpenBBInsider`` / ``OpenBBInstitutional`` providers (stubs), so
the contract / default-OFF / finite-guard tests run WITHOUT the ``openbb`` SDK.

Covers (per the ob3 seed):
  2. CONTRACT: InsiderAnalyst.analyze returns a valid AnalystView when data
     exists; None when absent (abstain).
  3. DEFAULT-OFF byte-identical: HERMES_QUANT_INSIDER_ANALYST /
     HERMES_QUANT_OPENBB unset -> the analyst abstains (returns None) and never
     touches the providers (no openbb import). Flag-on -> contributes.
  4. BESIDE-not-REPLACE: form4.py is untouched at HEAD (a separate test asserts
     this); this analyst is a SEPARATE consumer that does not modify form4.
  5. FINITE-GUARD: a NaN/inf share count never drives a view (abstain).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from hermes_quant.analysts.insider import (
    INSIDER_ANALYST_FLAG,
    OPENBB_ENABLE_FLAG,
    InsiderAnalyst,
)
from hermes_quant.protocol import AnalystView, MarketContext


# ---------------------------------------------------------------------------
# Stub providers: depend only on read_insider / read_institutional(ticker, as_of).
# ---------------------------------------------------------------------------
class _StubInsiderProvider:
    def __init__(self, frame: pd.DataFrame | None):
        self._frame = frame
        self.calls: list[dict] = []

    def read_insider(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        self.calls.append({"ticker": ticker, "as_of": as_of})
        return pd.DataFrame() if self._frame is None else self._frame.copy()


class _StubInstitutionalProvider:
    def __init__(self, frame: pd.DataFrame | None):
        self._frame = frame
        self.calls: list[dict] = []

    def read_institutional(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        self.calls.append({"ticker": ticker, "as_of": as_of})
        return pd.DataFrame() if self._frame is None else self._frame.copy()


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


def _net_buy_insider() -> pd.DataFrame:
    """Insiders NET BUYING (two acquisitions, one small disposal) -> bullish +1."""
    return pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-02", "2026-05-10", "2026-05-12"]),
            "transaction_date": pd.to_datetime(
                ["2026-04-30", "2026-05-08", "2026-05-10"]
            ),
            "securities_transacted": [40_000.0, 30_000.0, 5_000.0],
            "securities_owned": [100_000.0, 130_000.0, 125_000.0],
            "transaction_price": [180.0, 181.0, 182.0],
            "acquisition_or_disposal": ["A", "A", "D"],
            "accession_number": ["a", "b", "c"],
            "symbol": ["AAPL", "AAPL", "AAPL"],
        }
    )


def _net_sell_insider() -> pd.DataFrame:
    """Insiders NET SELLING -> bearish -1."""
    return pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-02", "2026-05-10"]),
            "transaction_date": pd.to_datetime(["2026-04-30", "2026-05-08"]),
            "securities_transacted": [5_000.0, 50_000.0],
            "securities_owned": [100_000.0, 50_000.0],
            "transaction_price": [180.0, 181.0],
            "acquisition_or_disposal": ["A", "D"],
            "accession_number": ["a", "b"],
            "symbol": ["AAPL", "AAPL"],
        }
    )


def _rising_institutional() -> pd.DataFrame:
    """Institutions ADDING (net positive change) -> bullish."""
    return pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-10", "2026-05-12"]),
            "period_ending": pd.to_datetime(["2026-03-31", "2026-03-31"]),
            "name": ["Vanguard", "BlackRock"],
            "shares": [5_000_000.0, 6_000_000.0],
            "value": [9.0e11, 1.0e12],
            "change": [400_000.0, 300_000.0],
        }
    )


def _analyst(
    insider: pd.DataFrame | None, institutional: pd.DataFrame | None
) -> InsiderAnalyst:
    return InsiderAnalyst(
        insider_provider=_StubInsiderProvider(insider),
        institutional_provider=_StubInstitutionalProvider(institutional),
    )


# ===========================================================================
# 3. DEFAULT-OFF byte-identical (flags unset -> abstain, providers untouched)
# ===========================================================================
def test_default_off_abstains_and_does_not_touch_providers(monkeypatch):
    """With both flags unset, analyze returns None and NEVER calls the providers
    (no openbb import path).

    RED-proof: if analyze ran the scoring path it would call read_insider /
    read_institutional (calls != []) and could return a view.
    """
    monkeypatch.delenv(INSIDER_ANALYST_FLAG, raising=False)
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    ins_stub = _StubInsiderProvider(_net_buy_insider())
    inst_stub = _StubInstitutionalProvider(_rising_institutional())
    an = InsiderAnalyst(insider_provider=ins_stub, institutional_provider=inst_stub)
    out = an.analyze(_ctx())

    assert out is None
    assert ins_stub.calls == []
    assert inst_stub.calls == []
    assert an.enabled is False


def test_only_one_flag_set_still_off(monkeypatch):
    """Both flags are required. ANALYST set but OPENBB unset -> still off."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    ins_stub = _StubInsiderProvider(_net_buy_insider())
    inst_stub = _StubInstitutionalProvider(_rising_institutional())
    an = InsiderAnalyst(insider_provider=ins_stub, institutional_provider=inst_stub)
    assert an.analyze(_ctx()) is None
    assert ins_stub.calls == []
    assert inst_stub.calls == []


def test_both_flags_on_contributes(monkeypatch):
    """Both flags ON -> the analyst runs and reaches the providers, forwarding
    the as_of for a point-in-time read."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    ins_stub = _StubInsiderProvider(_net_buy_insider())
    inst_stub = _StubInstitutionalProvider(_rising_institutional())
    an = InsiderAnalyst(insider_provider=ins_stub, institutional_provider=inst_stub)
    out = an.analyze(_ctx())

    assert out is not None
    assert ins_stub.calls and ins_stub.calls[0]["ticker"] == "AAPL"
    assert ins_stub.calls[0]["as_of"] is not None
    assert inst_stub.calls and inst_stub.calls[0]["as_of"] is not None


# ===========================================================================
# 2. CONTRACT — valid AnalystView when data exists; None when absent
# ===========================================================================
def test_contract_returns_valid_analyst_view(monkeypatch):
    """Net insider buying + rising institutional -> a well-formed bullish view."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(_net_buy_insider(), _rising_institutional())
    out = an.analyze(_ctx())

    assert isinstance(out, AnalystView)
    assert out.analyst == "insider"
    assert out.direction == 1  # net buying + adding institutions -> bullish
    assert isinstance(out.confidence, float)
    assert 0.0 <= out.confidence <= 1.0
    assert isinstance(out.confidence_raw, float)
    assert isinstance(out.magnitude, float)
    assert out.magnitude >= 0.0
    assert isinstance(out.horizon, str)


def test_contract_net_selling_direction(monkeypatch):
    """Net insider selling (no institutional data) -> direction == -1."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(_net_sell_insider(), None)
    out = an.analyze(_ctx())
    assert isinstance(out, AnalystView)
    assert out.direction == -1


def test_contract_absent_data_abstains(monkeypatch):
    """No ownership data from either source -> None (Protocol-clean abstain)."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(None, None)
    assert an.analyze(_ctx()) is None


def test_non_equity_abstains(monkeypatch):
    """A crypto / non-equity asset abstains (ownership is equity-only)."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    an = _analyst(_net_buy_insider(), _rising_institutional())
    assert an.analyze(_ctx(asset="BTC/USDT", asset_class="crypto")) is None


def test_balanced_insider_flow_abstains(monkeypatch):
    """A balanced buy/sell cluster (|net|/gross below the imbalance threshold)
    with no institutional data -> abstain (silence-by-default)."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    balanced = pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-02", "2026-05-10"]),
            "transaction_date": pd.to_datetime(["2026-04-30", "2026-05-08"]),
            "securities_transacted": [10_000.0, 10_000.0],
            "securities_owned": [100_000.0, 90_000.0],
            "transaction_price": [180.0, 181.0],
            "acquisition_or_disposal": ["A", "D"],  # perfectly balanced -> |net|=0
            "accession_number": ["a", "b"],
            "symbol": ["AAPL", "AAPL"],
        }
    )
    an = _analyst(balanced, None)
    assert an.analyze(_ctx()) is None


# ===========================================================================
# 5. FINITE-GUARD — a NaN/inf share count never drives (or suppresses) a view
# ===========================================================================
def test_finite_guard_insider_poison_row_excluded_legit_signal_survives(monkeypatch):
    """A poisoned (inf/nan) insider row is EXCLUDED from the net-flow math so
    the legitimate finite acquisitions still produce a CORRECT bullish view.

    This exercises the analyst's OWN per-row finite-guard (the stub provider
    returns the raw frame WITHOUT the provider-level coercion). RED-proof:
    without the per-row `np.isfinite(qty)` skip, the inf disposal poisons the
    gross/net sums to NaN, the NaN imbalance fails the threshold gate, and the
    analyst WRONGLY abstains (returns None) — suppressing the real net-buy
    signal. The guard makes the inf invisible to the arithmetic.
    """
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    mixed = pd.DataFrame(
        {
            "filing_date": pd.to_datetime(
                ["2026-05-02", "2026-05-08", "2026-05-10"]
            ),
            "transaction_date": pd.to_datetime(
                ["2026-04-30", "2026-05-06", "2026-05-08"]
            ),
            # Two legitimate acquisitions (net buy) + one POISONED inf disposal.
            "securities_transacted": [40_000.0, 30_000.0, float("inf")],
            "securities_owned": [100_000.0, 130_000.0, 130_000.0],
            "transaction_price": [180.0, 181.0, 182.0],
            "acquisition_or_disposal": ["A", "A", "D"],
            "accession_number": ["a", "b", "poison"],
            "symbol": ["AAPL", "AAPL", "AAPL"],
        }
    )
    an = _analyst(mixed, None)
    out = an.analyze(_ctx())
    # The finite acquisitions dominate -> a valid bullish view; the inf row is
    # excluded (never poisons the sum into NaN).
    assert isinstance(out, AnalystView)
    assert out.direction == 1


def test_finite_guard_institutional_poison_row_does_not_flip_direction(monkeypatch):
    """An inf institutional CHANGE row is EXCLUDED from the net-change sum so a
    poisoned positive inf cannot flip a genuinely BEARISH net-reduction view.

    The legit holder is REDUCING (-900k) -> bearish -1. The poison is a +inf
    add. RED-proof: without the per-row `np.isfinite(chg)` skip the +inf
    dominates the net_change sum -> direction sign flips to +1 (the
    `min(strength, 1.0)` launders inf into a finite 1.0, so it does NOT abstain
    — it silently emits a WRONG bullish view). The per-row skip keeps the
    bearish legit signal intact.
    """
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    mixed = pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-10", "2026-05-12"]),
            "period_ending": pd.to_datetime(["2026-03-31", "2026-03-31"]),
            "name": ["Vanguard", "Poison LP"],
            "shares": [5_000_000.0, 1_000_000.0],
            # Legit NEGATIVE change (institutions reducing -> bearish) + inf poison.
            "change": [-900_000.0, float("inf")],
            "value": [9.0e11, 1.8e11],
        }
    )
    an = _analyst(None, mixed)
    out = an.analyze(_ctx())
    assert isinstance(out, AnalystView)
    assert out.direction == -1  # poison must NOT flip the bearish net-reduction


def test_finite_guard_all_poisoned_abstains(monkeypatch):
    """If EVERY ownership row is non-finite (no finite signal anywhere), the
    analyst abstains (no poisoned view, no crash) — silence-by-default."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    poisoned_insider = pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-02", "2026-05-10"]),
            "transaction_date": pd.to_datetime(["2026-04-30", "2026-05-08"]),
            "securities_transacted": [float("inf"), float("nan")],
            "securities_owned": [100_000.0, 90_000.0],
            "transaction_price": [180.0, 181.0],
            "acquisition_or_disposal": ["A", "D"],
            "accession_number": ["a", "b"],
            "symbol": ["AAPL", "AAPL"],
        }
    )
    an = _analyst(poisoned_insider, None)
    assert an.analyze(_ctx()) is None


# ===========================================================================
# update() — calibrator only, never raises (Protocol)
# ===========================================================================
def test_update_feeds_calibrator_only(monkeypatch):
    """update() forwards a realized outcome to the calibrator without raising."""
    monkeypatch.setenv(INSIDER_ANALYST_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    from hermes_quant.protocol import RealizedOutcome

    an = _analyst(_net_buy_insider(), _rising_institutional())
    view = an.analyze(_ctx())
    assert view is not None
    outcome = RealizedOutcome(
        view=view,
        asof_view=pd.Timestamp("2026-05-15", tz="UTC"),
        asof_settlement=pd.Timestamp("2026-06-15", tz="UTC"),
        realized_return=0.03,
        direction_correct=True,
    )
    an.update(outcome)  # must not raise
