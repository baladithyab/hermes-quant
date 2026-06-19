"""Tests for hermes_quant.data.openbb_fundamentals (aegis-ob2, ADR-0100).

Offline / deterministic — NO live network and NO openbb installed. Both
``OpenBBFundamentals`` and ``OpenBBEstimates`` take an injected ``obb`` seam
(a fake object exposing ``.equity.fundamental.*`` / ``.equity.estimates.*``),
so the cardinal no-lookahead, latest-only-reject, default-OFF, and
finite-guard tests run WITHOUT the ``openbb`` SDK being importable.

Covers (per the ob2 seed):
  1. NO-LOOKAHEAD (cardinal, BOTH sources):
       - a fundamentals row with filing_date > asof is DROPPED even when
         period_ending <= asof (a late restatement must not leak);
       - an estimate published > asof is DROPPED;
       - a row exactly AT asof is INCLUDED (boundary).
  2. LATEST-ONLY REJECTED: the yfinance/consensus latest-only path raises in
     an asof context (never returns a latest-only snapshot).
  4. DEFAULT-OFF byte-identical: HERMES_QUANT_OPENBB unset -> no openbb
     import (poisoned-import sentinel proves the lazy import is not reached).
  5. FINITE-GUARD: a NaN/inf fundamental / estimate value is dropped/handled,
     never drives a view.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from hermes_quant.data.openbb_fundamentals import (
    OPENBB_ENABLE_FLAG,
    OpenBBEstimates,
    OpenBBFundamentals,
)
from hermes_quant.protocol import DataProviderError


# ---------------------------------------------------------------------------
# Synthetic OpenBB response fixtures.
#
# obb.equity.fundamental.metrics(...) (and balance/income) returns an OBBject
# whose .to_dataframe() yields a frame with one row per fiscal period. The
# point-in-time honesty columns are `period_ending` and `filing_date` (FMP
# exposes `accepted_date` / `filing_date`; we tolerate either).
# obb.equity.estimates.historical(...) returns forward analyst estimates, each
# row stamped with a `date` (the as-of publish date of the estimate).
# ---------------------------------------------------------------------------
class _FakeOBBject:
    """Minimal OBBject: exposes .to_dataframe() like the real Platform."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _FakeFundamentalObb:
    """Fake ``openbb.obb`` exposing equity.fundamental.* + estimates.*."""

    def __init__(
        self,
        *,
        metrics_df: pd.DataFrame | None = None,
        estimates_df: pd.DataFrame | None = None,
    ):
        self.metrics_calls: list[dict] = []
        self.estimates_calls: list[dict] = []
        self.quote_calls: list[dict] = []
        outer = self

        class _Fundamental:
            def metrics(self, **kwargs: Any) -> _FakeOBBject:
                outer.metrics_calls.append(kwargs)
                return _FakeOBBject(metrics_df if metrics_df is not None else pd.DataFrame())

        class _Estimates:
            def historical(self, **kwargs: Any) -> _FakeOBBject:
                outer.estimates_calls.append(kwargs)
                return _FakeOBBject(
                    estimates_df if estimates_df is not None else pd.DataFrame()
                )

            def consensus(self, **kwargs: Any) -> Any:
                # Latest-only consensus snapshot (NO as-of honesty) — must never escape.
                outer.quote_calls.append(kwargs)
                return {"target_consensus": 999.0}

        self.equity = SimpleNamespace(
            fundamental=_Fundamental(),
            estimates=_Estimates(),
        )


def _metrics_frame() -> pd.DataFrame:
    """A 3-period fundamentals frame.

    The middle period (Q4 2025) has a RESTATEMENT filed 2026-05-31 — AFTER an
    as_of of 2026-05-15 — even though its period_ending (2025-12-31) is well
    before asof. The no-lookahead filter must drop the restatement row by
    filing_date, NOT admit it by period_ending.
    """
    return pd.DataFrame(
        {
            "period_ending": pd.to_datetime(
                ["2025-09-30", "2025-12-31", "2026-03-31"]
            ),
            "filing_date": pd.to_datetime(
                ["2025-11-01", "2026-05-31", "2026-05-10"]
            ),
            "pe_trailing": [18.0, 19.0, 17.0],
            "pe_forward": [17.0, 18.0, 16.0],
            "debt_to_equity": [1.5, 1.6, 1.4],
            "free_cash_flow": [9.5e10, 9.6e10, 9.8e10],
            "revenue_ttm": [4.0e11, 4.1e11, 4.2e11],
            "eps_trailing": [6.5, 6.6, 6.7],
            "eps_forward": [7.0, 7.1, 7.2],
            "revenue_yoy": [0.12, 0.13, 0.14],
            "fcf_yoy": [0.20, 0.21, 0.22],
            "sector": ["Technology", "Technology", "Technology"],
            "currency": ["USD", "USD", "USD"],
            "quote_type": ["EQUITY", "EQUITY", "EQUITY"],
        }
    )


def _estimates_frame() -> pd.DataFrame:
    """Forward analyst estimates, each stamped with its publish `date`.

    The last row is published 2026-05-31 — AFTER a 2026-05-15 asof — and must
    be dropped. The 2026-05-15 row is exactly AT asof and must be kept.
    """
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-30", "2026-05-15", "2026-05-31"]),
            "eps_avg": [7.0, 7.3, 7.6],
            "eps_prior": [6.8, 7.0, 7.3],
            "revenue_avg": [4.10e11, 4.15e11, 4.20e11],
            "analyst_count": [30, 31, 32],
        }
    )


def _fund(metrics_df: pd.DataFrame | None = None) -> OpenBBFundamentals:
    return OpenBBFundamentals(
        obb=_FakeFundamentalObb(metrics_df=metrics_df), require_flag=False
    )


def _est(estimates_df: pd.DataFrame | None = None) -> OpenBBEstimates:
    return OpenBBEstimates(
        obb=_FakeFundamentalObb(estimates_df=estimates_df), require_flag=False
    )


# ===========================================================================
# 1. NO-LOOKAHEAD (cardinal) — fundamentals filing_date filter
# ===========================================================================
def test_fundamentals_filing_date_after_asof_dropped():
    """A row with filing_date > asof is DROPPED even when period_ending <= asof.

    RED-proof: without the filing_date filter the Q4-2025 restatement (filed
    2026-05-31, period_ending 2025-12-31 <= asof) leaks — its pe_trailing=19.0
    would surface and len would be 2 instead of 1.
    """
    prov = _fund(_metrics_frame())
    as_of = pd.Timestamp("2026-05-15")

    out = prov.read_fundamentals("AAPL", as_of=as_of)

    # The late-filed restatement (period_ending 2025-12-31, filing 2026-05-31)
    # must be gone — a filing known only AFTER asof is a leak.
    assert pd.Timestamp("2025-12-31") not in set(
        pd.to_datetime(out["period_ending"])
    )
    # The Q3-2025 row (filed 2025-11-01 <= asof) survives.
    assert pd.Timestamp("2025-09-30") in set(pd.to_datetime(out["period_ending"]))
    # The Q1-2026 row (period_ending 2026-03-31 <= asof, filed 2026-05-10 <= asof) survives.
    assert pd.Timestamp("2026-03-31") in set(pd.to_datetime(out["period_ending"]))
    assert len(out) == 2


def test_fundamentals_period_ending_after_asof_dropped():
    """period_ending > asof is also dropped (the AND half of the predicate)."""
    df = _metrics_frame()
    prov = _fund(df)
    # asof before the Q1-2026 period end -> that future-period row must drop too.
    as_of = pd.Timestamp("2026-01-15")

    out = prov.read_fundamentals("AAPL", as_of=as_of)
    # Q1-2026 (period_ending 2026-03-31 > asof) dropped; Q4-2025 (filing
    # 2026-05-31 > asof) dropped; only Q3-2025 (both <= asof) survives.
    assert set(pd.to_datetime(out["period_ending"])) == {pd.Timestamp("2025-09-30")}
    assert len(out) == 1


def test_fundamentals_boundary_at_asof_included():
    """A row whose filing_date is EXACTLY at asof is INCLUDED (boundary <=)."""
    df = pd.DataFrame(
        {
            "period_ending": pd.to_datetime(["2026-03-31"]),
            "filing_date": pd.to_datetime(["2026-05-15"]),  # exactly at asof
            "pe_trailing": [17.0],
            "pe_forward": [16.0],
            "debt_to_equity": [1.4],
            "free_cash_flow": [9.8e10],
            "revenue_ttm": [4.2e11],
            "eps_trailing": [6.7],
            "eps_forward": [7.2],
            "revenue_yoy": [0.14],
            "fcf_yoy": [0.22],
            "sector": ["Technology"],
            "currency": ["USD"],
            "quote_type": ["EQUITY"],
        }
    )
    prov = _fund(df)
    out = prov.read_fundamentals("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out) == 1
    assert pd.Timestamp("2026-03-31") in set(pd.to_datetime(out["period_ending"]))


def test_estimates_published_after_asof_dropped_boundary_kept():
    """An estimate published > asof is DROPPED; published == asof is KEPT.

    RED-proof: without the publish-date filter the 2026-05-31 estimate
    (eps_avg 7.6) leaks; the boundary 2026-05-15 row must remain.
    """
    prov = _est(_estimates_frame())
    as_of = pd.Timestamp("2026-05-15")

    out = prov.read_estimates("AAPL", as_of=as_of)

    pub = set(pd.to_datetime(out["date"]))
    assert pd.Timestamp("2026-05-31") not in pub  # future publish dropped
    assert pd.Timestamp("2026-05-15") in pub  # boundary kept
    assert pd.Timestamp("2026-04-30") in pub
    assert len(out) == 2


# ===========================================================================
# 2. LATEST-ONLY REJECTED
# ===========================================================================
def test_estimates_latest_only_consensus_hard_rejected():
    """The latest-only consensus path raises in an asof context — never
    returns a point-in-time-unsafe snapshot.

    RED-proof: if read_consensus returned the snapshot this would not raise.
    """
    prov = _est(_estimates_frame())
    with pytest.raises(DataProviderError) as ei:
        prov.read_consensus("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert "latest-only" in str(ei.value).lower()

    # And read_estimates must NOT have touched the consensus endpoint.
    prov.read_estimates("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert prov._obb.quote_calls == []


def test_estimates_read_requires_asof():
    """read_estimates with as_of=None is rejected (no point-in-time anchor =
    latest-only semantics). An asof-less estimate read would silently return a
    not-yet-public snapshot."""
    prov = _est(_estimates_frame())
    with pytest.raises(DataProviderError) as ei:
        prov.read_estimates("AAPL", as_of=None)
    assert "as_of" in str(ei.value).lower() or "asof" in str(ei.value).lower()


def test_fundamentals_read_requires_asof():
    """read_fundamentals with as_of=None is rejected (latest-only otherwise)."""
    prov = _fund(_metrics_frame())
    with pytest.raises(DataProviderError) as ei:
        prov.read_fundamentals("AAPL", as_of=None)
    assert "as_of" in str(ei.value).lower() or "asof" in str(ei.value).lower()


# ===========================================================================
# 4. DEFAULT-OFF byte-identical (lazy import not triggered)
# ===========================================================================
def test_default_off_does_not_import_openbb(monkeypatch):
    """With HERMES_QUANT_OPENBB unset, constructing must NOT import openbb. A
    fetch attempt fails-closed at the flag gate BEFORE the lazy import.

    RED-proof: poison the `openbb` import. Constructing the providers must NOT
    trigger it; a flag-off read raises the FLAG error ("disabled"), NOT the
    poisoned-import error — proving the import was never reached.
    """
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    class _Poison:
        def __getattr__(self, name):  # pragma: no cover - any access explodes
            raise ImportError("SENTINEL: openbb import was triggered")

    monkeypatch.setitem(sys.modules, "openbb", _Poison())

    fund = OpenBBFundamentals()  # require_flag defaults True
    est = OpenBBEstimates()

    for prov, call in (
        (fund, lambda: fund.read_fundamentals("AAPL", as_of=pd.Timestamp("2026-05-15"))),
        (est, lambda: est.read_estimates("AAPL", as_of=pd.Timestamp("2026-05-15"))),
    ):
        with pytest.raises(DataProviderError) as ei:
            call()
        msg = str(ei.value)
        assert "disabled" in msg.lower()
        assert OPENBB_ENABLE_FLAG in msg
        assert "not installed" not in msg.lower()
        assert "SENTINEL" not in msg


def test_flag_on_but_openbb_missing_raises_guidance(monkeypatch):
    """Flag ON + openbb not installed -> a guided DataProviderError, not a
    raw crash at module import time."""
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")
    monkeypatch.delitem(sys.modules, "openbb", raising=False)

    fund = OpenBBFundamentals()
    with pytest.raises(DataProviderError) as ei:
        fund.read_fundamentals("AAPL", as_of=pd.Timestamp("2026-05-15"))
    msg = str(ei.value).lower()
    assert "openbb" in msg
    assert "install" in msg


# ===========================================================================
# 5. FINITE-GUARD
# ===========================================================================
def test_fundamentals_nan_inf_values_coerced_not_admitted():
    """A NaN/inf fundamental value is coerced to NaN (never a finite poison).

    RED-proof: without the finite-guard the inf pe_trailing survives as inf
    and would later defeat every `<=` sanity gate. We assert no inf escapes.
    """
    df = pd.DataFrame(
        {
            "period_ending": pd.to_datetime(["2026-03-31"]),
            "filing_date": pd.to_datetime(["2026-05-10"]),
            "pe_trailing": [float("inf")],  # poison
            "pe_forward": [float("nan")],
            "debt_to_equity": [1.4],
            "free_cash_flow": [9.8e10],
            "revenue_ttm": [4.2e11],
            "eps_trailing": [6.7],
            "eps_forward": [7.2],
            "revenue_yoy": [0.14],
            "fcf_yoy": [0.22],
            "sector": ["Technology"],
            "currency": ["USD"],
            "quote_type": ["EQUITY"],
        }
    )
    prov = _fund(df)
    out = prov.read_fundamentals("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out) == 1
    pe = out.iloc[0]["pe_trailing"]
    assert not np.isfinite(float(pe))  # inf -> NaN (not a finite poison)


def test_estimates_nan_inf_row_dropped():
    """An estimate row whose key numeric is NaN/inf is dropped (never drives a
    view).

    RED-proof: without the finite-guard the inf eps_avg row survives.
    """
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-10", "2026-05-12"]),
            "eps_avg": [float("inf"), 7.3],  # first row poisoned
            "eps_prior": [7.0, 7.0],
            "revenue_avg": [4.10e11, 4.15e11],
            "analyst_count": [30, 31],
        }
    )
    prov = _est(df)
    out = prov.read_estimates("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out) == 1
    assert np.isfinite(float(out.iloc[0]["eps_avg"]))
    assert pd.Timestamp("2026-05-10") not in set(pd.to_datetime(out["date"]))
