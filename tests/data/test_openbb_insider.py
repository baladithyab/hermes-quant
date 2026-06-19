"""Tests for hermes_quant.data.openbb_insider (aegis-ob3, ADR-0100).

Offline / deterministic — NO live network and NO openbb installed. Both
``OpenBBInsider`` and ``OpenBBInstitutional`` take an injected ``obb`` seam
(a fake exposing ``.equity.ownership.insider_trading`` /
``.equity.ownership.institutional``), so the cardinal no-lookahead,
default-OFF, and finite-guard tests run WITHOUT the ``openbb`` SDK importable.

Covers (per the ob3 seed):
  1. NO-LOOKAHEAD (cardinal, BOTH sources):
       - an insider row with filing_date > asof is DROPPED (the late Form-4
         filing must not leak); AT-asof included (boundary).
       - a 13-F row with filing_date > asof is DROPPED even when period_ending
         (quarter-end) <= asof (the 13-F is filed ~45d after quarter-end — the
         late-filing leak). AT-asof included.
  3. DEFAULT-OFF byte-identical: HERMES_QUANT_OPENBB unset -> no openbb import
     (poisoned-import sentinel proves the lazy import is not reached).
  4. BESIDE-not-REPLACE: OpenBBInsider maps to the SAME FilingEvidence shape
     as form4 (kind='filing'), with a DISTINCT source so it never collides on
     identity with a form4 record.
  5. FINITE-GUARD: a NaN/inf share count is dropped, never drives a view.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from hermes_quant.data.openbb_insider import (
    OPENBB_ENABLE_FLAG,
    OpenBBInsider,
    OpenBBInstitutional,
)
from hermes_quant.evidence.schema import FilingEvidence
from hermes_quant.protocol import DataProviderError


# ---------------------------------------------------------------------------
# Synthetic OpenBB ownership response fixtures.
#
# obb.equity.ownership.insider_trading(...) returns an OBBject whose
# .to_dataframe() yields one row per Form-4 transaction line, each carrying a
# `filing_date` (the EDGAR public moment — the asof anchor) and a
# `transaction_date` (the trade date — metadata only).
# obb.equity.ownership.institutional(...) returns 13-F holdings, each carrying a
# `filing_date` (the 13-F filing moment) and a `period_ending` (quarter-end).
# ---------------------------------------------------------------------------
class _FakeOBBject:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _FakeOwnershipObb:
    """Fake ``openbb.obb`` exposing equity.ownership.insider_trading + institutional."""

    def __init__(
        self,
        *,
        insider_df: pd.DataFrame | None = None,
        institutional_df: pd.DataFrame | None = None,
    ):
        self.insider_calls: list[dict] = []
        self.institutional_calls: list[dict] = []
        outer = self

        class _Ownership:
            def insider_trading(self, **kwargs: Any) -> _FakeOBBject:
                outer.insider_calls.append(kwargs)
                return _FakeOBBject(
                    insider_df if insider_df is not None else pd.DataFrame()
                )

            def institutional(self, **kwargs: Any) -> _FakeOBBject:
                outer.institutional_calls.append(kwargs)
                return _FakeOBBject(
                    institutional_df if institutional_df is not None else pd.DataFrame()
                )

        self.equity = SimpleNamespace(ownership=_Ownership())


def _insider_frame() -> pd.DataFrame:
    """A 3-transaction insider frame.

    The middle row is a Form-4 FILED 2026-05-31 — AFTER an asof of 2026-05-15 —
    even though its TRANSACTION date (2026-05-01) is well before asof. The
    no-lookahead filter must drop it by filing_date, NOT admit it by the trade
    date. The 2026-05-15 row is exactly AT asof and must be kept.
    """
    return pd.DataFrame(
        {
            "filing_date": pd.to_datetime(
                ["2026-05-02", "2026-05-31", "2026-05-15"]
            ),
            "transaction_date": pd.to_datetime(
                ["2026-04-30", "2026-05-01", "2026-05-13"]
            ),
            "securities_transacted": [10_000.0, 50_000.0, 8_000.0],
            "securities_owned": [100_000.0, 90_000.0, 108_000.0],
            "transaction_price": [180.0, 182.0, 181.0],
            "acquisition_or_disposal": ["A", "D", "A"],
            "accession_number": [
                "0000320193-26-000010",
                "0000320193-26-000011",
                "0000320193-26-000012",
            ],
            "symbol": ["AAPL", "AAPL", "AAPL"],
        }
    )


def _institutional_frame() -> pd.DataFrame:
    """A 3-holder 13-F frame.

    The middle row was FILED 2026-05-31 — AFTER a 2026-05-15 asof — for a
    quarter that ENDED 2026-03-31 (well before asof). The 13-F is filed ~45d
    after quarter-end; the no-lookahead filter must drop it by filing_date, NOT
    admit it by period_ending. The 2026-05-15 row is exactly AT asof and kept.
    """
    return pd.DataFrame(
        {
            "filing_date": pd.to_datetime(
                ["2026-05-10", "2026-05-31", "2026-05-15"]
            ),
            "period_ending": pd.to_datetime(
                ["2026-03-31", "2026-03-31", "2026-03-31"]
            ),
            "name": ["Vanguard", "BlackRock", "State Street"],
            "shares": [5_000_000.0, 6_000_000.0, 3_000_000.0],
            "value": [9.0e11, 1.0e12, 5.4e11],
            "change": [200_000.0, -300_000.0, 100_000.0],
        }
    )


def _ins(insider_df: pd.DataFrame | None = None) -> OpenBBInsider:
    return OpenBBInsider(
        obb=_FakeOwnershipObb(insider_df=insider_df), require_flag=False
    )


def _inst(institutional_df: pd.DataFrame | None = None) -> OpenBBInstitutional:
    return OpenBBInstitutional(
        obb=_FakeOwnershipObb(institutional_df=institutional_df), require_flag=False
    )


# ===========================================================================
# 1. NO-LOOKAHEAD (cardinal) — insider filing_date filter
# ===========================================================================
def test_insider_filing_date_after_asof_dropped_boundary_kept():
    """A row with filing_date > asof is DROPPED even when its transaction_date
    <= asof; a row filed exactly AT asof is KEPT.

    RED-proof: without the filing_date filter the 2026-05-31 row (filed after
    asof, traded 2026-05-01 <= asof) leaks — len would be 3, and the
    securities_transacted=50000/'D' disposal would distort the net flow.
    """
    prov = _ins(_insider_frame())
    as_of = pd.Timestamp("2026-05-15")

    out = prov.read_insider("AAPL", as_of=as_of)

    filed = set(pd.to_datetime(out["filing_date"]))
    assert pd.Timestamp("2026-05-31") not in filed  # late filing dropped
    assert pd.Timestamp("2026-05-15") in filed  # boundary kept
    assert pd.Timestamp("2026-05-02") in filed
    assert len(out) == 2


def test_insider_filter_is_on_filing_date_not_transaction_date():
    """The filter MUST be on filing_date, not the trade date.

    A transaction that occurred BEFORE asof but whose Form-4 was FILED after
    asof was NOT publicly knowable -> must be dropped. RED-proof: a
    transaction_date-based filter would ADMIT the 2026-05-31-filed row (traded
    2026-05-01 <= asof), leaking it.
    """
    prov = _ins(_insider_frame())
    out = prov.read_insider("AAPL", as_of=pd.Timestamp("2026-05-15"))
    # The leaked row's accession must be absent.
    assert "0000320193-26-000011" not in set(out["accession_number"])


# --- 13-F no-lookahead ---
def test_institutional_filing_date_after_asof_dropped_boundary_kept():
    """A 13-F row with filing_date > asof is DROPPED even when its period_ending
    (quarter-end) <= asof; the AT-asof row is KEPT.

    This is the 13-F late-filing hazard: filed ~45d after quarter-end. RED-proof:
    without the filing_date filter the BlackRock row (filed 2026-05-31, quarter
    ended 2026-03-31 <= asof) leaks — len would be 3 and the -300000 change
    would flip the net institutional flow.
    """
    prov = _inst(_institutional_frame())
    as_of = pd.Timestamp("2026-05-15")

    out = prov.read_institutional("AAPL", as_of=as_of)

    holders = set(out["holder"])
    assert "BlackRock" not in holders  # late 13-F dropped
    assert "Vanguard" in holders
    assert "State Street" in holders  # filed exactly at asof -> kept
    assert len(out) == 2


def test_institutional_does_not_filter_on_period_ending():
    """A 13-F whose quarter-end (period_ending) is AFTER asof but which was
    nonetheless filed BEFORE asof would be an impossible-future filing; the
    real hazard is the reverse — an old quarter filed late. We assert the kept
    rows are exactly those with filing_date <= asof regardless of period_ending
    (all three share the same 2026-03-31 quarter-end)."""
    prov = _inst(_institutional_frame())
    out = prov.read_institutional("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert (pd.to_datetime(out["filing_date"]) <= pd.Timestamp("2026-05-15")).all()


# ===========================================================================
# 2. LATEST-ONLY / asof-required
# ===========================================================================
def test_insider_read_requires_asof():
    """read_insider with as_of=None is rejected (latest-only otherwise)."""
    prov = _ins(_insider_frame())
    with pytest.raises(DataProviderError) as ei:
        prov.read_insider("AAPL", as_of=None)
    assert "as_of" in str(ei.value).lower() or "asof" in str(ei.value).lower()


def test_institutional_read_requires_asof():
    """read_institutional with as_of=None is rejected (latest-only otherwise)."""
    prov = _inst(_institutional_frame())
    with pytest.raises(DataProviderError) as ei:
        prov.read_institutional("AAPL", as_of=None)
    assert "as_of" in str(ei.value).lower() or "asof" in str(ei.value).lower()


# ===========================================================================
# 3. DEFAULT-OFF byte-identical (lazy import not triggered)
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

    ins = OpenBBInsider()  # require_flag defaults True
    inst = OpenBBInstitutional()

    for prov, call in (
        (ins, lambda: ins.read_insider("AAPL", as_of=pd.Timestamp("2026-05-15"))),
        (
            inst,
            lambda: inst.read_institutional("AAPL", as_of=pd.Timestamp("2026-05-15")),
        ),
    ):
        with pytest.raises(DataProviderError) as ei:
            call()
        msg = str(ei.value)
        assert "disabled" in msg.lower()
        assert OPENBB_ENABLE_FLAG in msg
        assert "not installed" not in msg.lower()
        assert "SENTINEL" not in msg


def test_flag_on_but_openbb_missing_raises_guidance(monkeypatch):
    """Flag ON + openbb not installed -> a guided DataProviderError, not a raw
    crash at module import time."""
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")
    monkeypatch.delitem(sys.modules, "openbb", raising=False)

    ins = OpenBBInsider()
    with pytest.raises(DataProviderError) as ei:
        ins.read_insider("AAPL", as_of=pd.Timestamp("2026-05-15"))
    msg = str(ei.value).lower()
    assert "openbb" in msg
    assert "install" in msg


# ===========================================================================
# 4. BESIDE-not-REPLACE — same FilingEvidence shape, distinct source/identity
# ===========================================================================
def test_insider_maps_to_filing_evidence_beside_form4():
    """OpenBBInsider.to_filing_evidence produces a valid FilingEvidence
    (kind='filing') — the SAME evidence shape form4 emits — anchored on
    filing_date (NOT transaction_date), with a source DISTINCT from form4's.

    This proves ob3 feeds the SAME evidence series BESIDE form4 without
    replacing it. RED-proof: a record anchored on transaction_date would have
    published_at != filing_date; a same-source tag would risk an id collision
    with form4.
    """
    from hermes_quant.evidence.adapters.form4 import _SOURCE as FORM4_SOURCE

    prov = _ins(_insider_frame())
    out = prov.read_insider("AAPL", as_of=pd.Timestamp("2026-05-15"))
    row = out.iloc[0]
    ev = prov.to_filing_evidence(row, ticker="AAPL")

    assert isinstance(ev, FilingEvidence)
    assert ev.kind == "filing"
    # published_at anchors on the FILING moment (the public moment), not the trade.
    assert ev.published_at.date() == pd.Timestamp(row["filing_date"]).date()
    assert ev.published_at.date() != pd.Timestamp(row["transaction_date"]).date()
    # DISTINCT source from form4 so identities never collide on the same series.
    assert ev.source != FORM4_SOURCE
    # filing ingest-lag floor is 0 -> available_at == published_at (causal-clean).
    assert ev.available_at == ev.published_at
    # Deterministic identity: same row -> same id (idempotent append).
    ev2 = prov.to_filing_evidence(row, ticker="AAPL")
    assert ev.id == ev2.id


# ===========================================================================
# 5. FINITE-GUARD
# ===========================================================================
def test_insider_nan_inf_share_count_row_dropped():
    """An insider row whose securities_transacted is NaN/inf is dropped.

    RED-proof: without the finite-guard the inf-share row survives and would
    poison the net-flow sum (inf defeats every comparison gate).
    """
    df = pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-10", "2026-05-12", "2026-05-13"]),
            "transaction_date": pd.to_datetime(
                ["2026-05-08", "2026-05-10", "2026-05-11"]
            ),
            "securities_transacted": [float("inf"), float("nan"), 8_000.0],
            "securities_owned": [100_000.0, 100_000.0, 108_000.0],
            "transaction_price": [180.0, 181.0, 181.0],
            "acquisition_or_disposal": ["A", "D", "A"],
            "accession_number": ["a", "b", "c"],
            "symbol": ["AAPL", "AAPL", "AAPL"],
        }
    )
    prov = _ins(df)
    out = prov.read_insider("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out) == 1  # only the finite 8000-share row survives
    assert np.isfinite(float(out.iloc[0]["securities_transacted"]))
    assert "c" in set(out["accession_number"])
    assert "a" not in set(out["accession_number"])


def test_institutional_nan_inf_shares_row_dropped():
    """A 13-F row whose shares count is NaN/inf is dropped."""
    df = pd.DataFrame(
        {
            "filing_date": pd.to_datetime(["2026-05-10", "2026-05-12"]),
            "period_ending": pd.to_datetime(["2026-03-31", "2026-03-31"]),
            "name": ["Vanguard", "BlackRock"],
            "shares": [float("inf"), 6_000_000.0],
            "value": [9.0e11, 1.0e12],
            "change": [200_000.0, -300_000.0],
        }
    )
    prov = _inst(df)
    out = prov.read_institutional("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out) == 1
    assert np.isfinite(float(out.iloc[0]["shares"]))
    assert "BlackRock" in set(out["holder"])


def test_empty_response_returns_empty_frame():
    """An empty OpenBB response yields an empty (correctly-columned) frame, not
    a raise — silence-by-default."""
    prov = _ins(None)
    out = prov.read_insider("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out) == 0
    assert "filing_date" in out.columns

    prov2 = _inst(None)
    out2 = prov2.read_institutional("AAPL", as_of=pd.Timestamp("2026-05-15"))
    assert len(out2) == 0
    assert "filing_date" in out2.columns
