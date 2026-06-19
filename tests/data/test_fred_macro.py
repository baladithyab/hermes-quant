"""Tests for hermes_quant.data.fred_macro (aegis-ob4, ADR-0100).

Offline / deterministic — NO live network and NO openbb installed. The
``FredMacroProvider`` takes an injected ``fetcher`` (direct FRED REST route) or
an injected ``obb`` seam (OpenBB economy route), so the cardinal no-lookahead,
window-pinned, default-OFF, fail-closed, and finite-guard tests run WITHOUT the
``openbb`` SDK being importable and WITHOUT a network.

Covers (per the ob4 seed):
  1. NO-LOOKAHEAD WINDOW (cardinal):
       - an observation with observation-date > asof is EXCLUDED;
       - for a LAGGED series (CPI), an observation whose VALUE-date <= asof but
         whose RELEASE-date > asof is EXCLUDED (the release-lag leak);
       - a row exactly AT asof (both axes) is INCLUDED (boundary);
       - the documented lookback window pins the lower bound.
  2. DEFAULT-OFF byte-identical: HERMES_QUANT_FRED_MACRO / HERMES_QUANT_OPENBB
     unset -> no openbb import and no FRED HTTP call (poisoned-import +
     poisoned-fetcher sentinels prove neither is reached).
  3. FAIL-CLOSED: flag on but FRED_API_KEY absent (and no obb route) -> a clear
     DataProviderError, never a fabricated series.
  4. FINITE-GUARD: a NaN/inf macro value is dropped.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from hermes_quant.data.fred_macro import (
    FRED_MACRO_ENABLE_FLAG,
    OPENBB_ENABLE_FLAG,
    FredMacroProvider,
)
from hermes_quant.protocol import DataProviderError


# ---------------------------------------------------------------------------
# Synthetic FRED REST payload helpers.
#
# The FRED series/observations endpoint returns:
#   {"observations": [{"date": "...", "value": "...",
#                      "realtime_start": "...", "realtime_end": "..."}, ...]}
# where `date` is the OBSERVATION (period) date and `realtime_start` is the
# RELEASE/VINTAGE date (when that value first became publicly knowable).
# ---------------------------------------------------------------------------
def _payload(rows: list[dict]) -> bytes:
    return json.dumps({"observations": rows}).encode("utf-8")


def _fetcher_for(payload: bytes):
    """Return an injectable fetcher(url, timeout) that yields a fixed payload
    and records the URL it was called with (to assert the asof params)."""
    calls: list[str] = []

    def _fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        return payload

    _fetch.calls = calls  # type: ignore[attr-defined]
    return _fetch


def _provider(payload: bytes) -> FredMacroProvider:
    """Direct-FRED-route provider with an injected fetcher + key (no env, no flag)."""
    return FredMacroProvider(
        fetcher=_fetcher_for(payload), api_key="TEST_KEY", require_flag=False
    )


# ===========================================================================
# 1. NO-LOOKAHEAD WINDOW (cardinal)
# ===========================================================================
def test_observation_date_after_asof_excluded():
    """An observation whose observation-date > asof is EXCLUDED, ISOLATING the
    date<=asof clause from the release_date<=asof clause.

    wave4-review FIX (was non-isolating): the prior fixture set realtime_start==date
    on every row, so the release_date filter ALONE caught the future print — removing
    the date<=asof clause left the suite green. To pin the date<=asof clause
    INDEPENDENTLY, the poison row has a FUTURE observation date (2026-06-20) but a PAST
    release date (2026-06-10 <= asof) — so ONLY the date<=asof clause can drop it.

    RED-proof: remove `& (mapped["date"] <= cutoff)` -> this poison row survives (its
    release_date 2026-06-10 <= asof passes the release half) -> len 3 not 2.
    """
    rows = [
        {"date": "2026-06-10", "value": "13.5", "realtime_start": "2026-06-10"},
        {"date": "2026-06-12", "value": "14.0", "realtime_start": "2026-06-12"},
        # Future OBSERVATION date but PAST release date -> only the date<=asof clause
        # can exclude it (the release_date<=asof clause passes). Isolates the clause.
        {"date": "2026-06-20", "value": "15.0", "realtime_start": "2026-06-10"},
    ]
    prov = _provider(_payload(rows))
    out = prov.read_series("VIXCLS", as_of=pd.Timestamp("2026-06-15"))

    got = set(pd.to_datetime(out["date"]))
    assert pd.Timestamp("2026-06-20") not in got, (
        "a future-OBSERVATION-date row must be dropped by the date<=asof clause even "
        "when its release_date is in the past"
    )
    assert got == {pd.Timestamp("2026-06-10"), pd.Timestamp("2026-06-12")}
    assert len(out) == 2


def test_lagged_series_release_after_asof_excluded():
    """CARDINAL: a CPI observation whose VALUE-date <= asof but whose
    RELEASE-date > asof is EXCLUDED (the release-lag leak).

    The May CPI value (observation date 2026-05-01) is NOT released until
    mid-June (realtime_start 2026-06-11). A read at asof 2026-06-01 must NOT
    see it — even though the value's observation date (2026-05-01) is <= asof.

    RED-proof: removing the release-date half of the filter (keep only
    date<=asof) admits the not-yet-released May print — len 2 not 1, and the
    May value 320.1 leaks.
    """
    rows = [
        # April CPI: observed 2026-04-01, released 2026-05-12 (both <= asof).
        {"date": "2026-04-01", "value": "319.0", "realtime_start": "2026-05-12"},
        # May CPI: observed 2026-05-01 (<= asof) but RELEASED 2026-06-11 (> asof).
        {"date": "2026-05-01", "value": "320.1", "realtime_start": "2026-06-11"},
    ]
    prov = _provider(_payload(rows))
    as_of = pd.Timestamp("2026-06-01")

    out = prov.read_series("CPIAUCSL", as_of=as_of)

    # The May print (released AFTER asof) must be gone.
    assert pd.Timestamp("2026-05-01") not in set(pd.to_datetime(out["date"]))
    assert 320.1 not in set(out["value"].tolist())
    # The April print (released 2026-05-12 <= asof) survives.
    assert set(pd.to_datetime(out["date"])) == {pd.Timestamp("2026-04-01")}
    assert len(out) == 1


def test_boundary_both_axes_at_asof_included():
    """A row whose observation-date AND release-date are EXACTLY at asof is
    INCLUDED (boundary <=)."""
    rows = [
        {"date": "2026-06-15", "value": "5.25", "realtime_start": "2026-06-15"},
    ]
    prov = _provider(_payload(rows))
    out = prov.read_series("FEDFUNDS", as_of=pd.Timestamp("2026-06-15"))
    assert len(out) == 1
    assert pd.Timestamp("2026-06-15") in set(pd.to_datetime(out["date"]))
    assert float(out.iloc[0]["value"]) == 5.25


def test_lookback_window_lower_bound_pinned():
    """An observation older than the documented lookback window is EXCLUDED.

    RED-proof: a value dated well before asof - lookback_days must drop; only
    in-window rows survive.
    """
    rows = [
        {"date": "2020-01-01", "value": "1.0", "realtime_start": "2020-01-01"},  # too old
        {"date": "2026-05-01", "value": "5.0", "realtime_start": "2026-05-01"},  # in window
    ]
    prov = _provider(_payload(rows))
    out = prov.read_series(
        "DGS10", as_of=pd.Timestamp("2026-06-15"), lookback_days=365
    )
    assert set(pd.to_datetime(out["date"])) == {pd.Timestamp("2026-05-01")}
    assert len(out) == 1


def test_release_date_params_pin_the_request():
    """The constructed FRED URL pins realtime_end=asof (ALFRED vintage read)."""
    fetch = _fetcher_for(_payload([]))
    prov = FredMacroProvider(fetcher=fetch, api_key="TEST_KEY", require_flag=False)
    prov.read_series("CPIAUCSL", as_of=pd.Timestamp("2026-06-15"), lookback_days=730)
    assert fetch.calls, "fetcher should have been called"
    url = fetch.calls[0]
    assert "realtime_end=2026-06-15" in url
    assert "observation_end=2026-06-15" in url
    # The key is in the URL (FRED requires it) but never logged by us.
    assert "series_id=CPIAUCSL" in url


def test_asof_none_hard_rejected():
    """An asof-less macro read is latest-only semantics -> HARD-REJECTED."""
    prov = _provider(_payload([]))
    with pytest.raises(DataProviderError) as ei:
        prov.read_series("FEDFUNDS", as_of=None)
    assert "as_of" in str(ei.value).lower()


# ===========================================================================
# 2. DEFAULT-OFF byte-identical (no openbb import, no FRED HTTP call)
# ===========================================================================
def test_default_off_no_import_no_fetch(monkeypatch):
    """With both flags unset, a read fails closed at the macro-flag gate BEFORE
    any openbb import OR any FRED HTTP call.

    RED-proof: poison BOTH the openbb import and the fetcher. A flag-off read
    raises the FLAG error ("disabled"), NOT the poison sentinels — proving
    neither the import nor the fetch was reached.
    """
    monkeypatch.delenv(FRED_MACRO_ENABLE_FLAG, raising=False)
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    class _Poison:
        def __getattr__(self, name):  # pragma: no cover - any access explodes
            raise ImportError("SENTINEL: openbb import was triggered")

    monkeypatch.setitem(sys.modules, "openbb", _Poison())

    def _poison_fetch(url, timeout):  # pragma: no cover - must not be reached
        raise AssertionError("SENTINEL: FRED HTTP fetch was triggered")

    prov = FredMacroProvider(fetcher=_poison_fetch, api_key="TEST_KEY")  # require_flag default True
    with pytest.raises(DataProviderError) as ei:
        prov.read_series("FEDFUNDS", as_of=pd.Timestamp("2026-06-15"))
    msg = str(ei.value)
    assert "disabled" in msg.lower()
    assert FRED_MACRO_ENABLE_FLAG in msg
    assert "SENTINEL" not in msg
    assert "not installed" not in msg.lower()


# ===========================================================================
# 3. FAIL-CLOSED (flag on, key absent, no obb route)
# ===========================================================================
def test_flag_on_key_absent_fails_closed(monkeypatch):
    """Macro flag ON + FRED_API_KEY absent + no obb route -> clear error, never
    a fabricated series.

    RED-proof: a fail-OPEN implementation would return an empty (or synthetic)
    frame; we require it to RAISE so the caller cannot read silence as
    'no signal'.
    """
    monkeypatch.setenv(FRED_MACRO_ENABLE_FLAG, "1")
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    prov = FredMacroProvider()  # no fetcher, no key, no obb, require_flag default True
    with pytest.raises(DataProviderError) as ei:
        prov.read_series("FEDFUNDS", as_of=pd.Timestamp("2026-06-15"))
    msg = str(ei.value).lower()
    assert "fred_api_key" in msg or "fred api" in msg
    assert "fabricate" in msg or "cannot read" in msg


def test_dead_feed_raises_not_silent():
    """A dead/erroring FRED feed RAISES (never silently returns empty).

    A macro series quietly returning empty would be read as 'no signal' — a
    fail-open lie about the rate environment. RED-proof: the erroring fetcher
    must surface as a DataProviderError, not an empty frame.
    """
    def _boom(url, timeout):
        raise OSError("connection reset")

    prov = FredMacroProvider(fetcher=_boom, api_key="TEST_KEY", require_flag=False)
    with pytest.raises(DataProviderError):
        prov.read_series("FEDFUNDS", as_of=pd.Timestamp("2026-06-15"))


# ===========================================================================
# 4. FINITE-GUARD
# ===========================================================================
def test_nan_inf_value_dropped():
    """A NaN/inf macro value (or FRED '.' sentinel) is DROPPED.

    RED-proof: without the finite-guard the inf row survives and would defeat
    every downstream `<=` sanity gate. The '.' missing-sentinel and an inf
    value must both drop; the one finite row survives.
    """
    rows = [
        {"date": "2026-06-10", "value": ".", "realtime_start": "2026-06-10"},      # FRED missing
        {"date": "2026-06-11", "value": "inf", "realtime_start": "2026-06-11"},    # poison
        {"date": "2026-06-12", "value": "14.0", "realtime_start": "2026-06-12"},   # good
    ]
    prov = _provider(_payload(rows))
    out = prov.read_series("VIXCLS", as_of=pd.Timestamp("2026-06-15"))
    assert len(out) == 1
    assert float(out.iloc[0]["value"]) == 14.0
    assert np.isfinite(out["value"]).all()


# ===========================================================================
# OpenBB economy route (obb seam) — asof-honest, flag-gated
# ===========================================================================
class _FakeOBBject:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _FakeEconomyObb:
    """Fake openbb.obb exposing economy.fred_series."""

    def __init__(self, df: pd.DataFrame):
        self.calls: list[dict] = []
        outer = self

        class _Economy:
            def fred_series(self, **kwargs: Any) -> _FakeOBBject:
                outer.calls.append(kwargs)
                return _FakeOBBject(df)

        self.economy = _Economy()


def test_obb_route_requires_openbb_flag(monkeypatch):
    """An injected obb seam is only used when HERMES_QUANT_OPENBB is on; with the
    OpenBB flag off the obb route is NOT taken (falls through to key-required
    direct route)."""
    monkeypatch.setenv(FRED_MACRO_ENABLE_FLAG, "1")
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    df = pd.DataFrame(
        {"date": ["2026-06-12"], "value": [5.0], "realtime_start": ["2026-06-12"]}
    )
    # obb injected but OpenBB flag OFF + no key -> must fail closed (no obb call).
    prov = FredMacroProvider(obb=_FakeEconomyObb(df))
    with pytest.raises(DataProviderError) as ei:
        prov.read_series("FEDFUNDS", as_of=pd.Timestamp("2026-06-15"))
    assert "fred_api_key" in str(ei.value).lower() or "fabricate" in str(ei.value).lower()


def test_obb_route_used_when_both_flags_on(monkeypatch):
    """With both flags on and an obb seam injected, the obb economy route is
    used and the no-lookahead filter still applies."""
    monkeypatch.setenv(FRED_MACRO_ENABLE_FLAG, "1")
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")

    df = pd.DataFrame(
        {
            "date": ["2026-06-12", "2026-06-20"],  # second is future
            "value": [5.0, 6.0],
            "realtime_start": ["2026-06-12", "2026-06-20"],
        }
    )
    fake = _FakeEconomyObb(df)
    prov = FredMacroProvider(obb=fake)
    out = prov.read_series("FEDFUNDS", as_of=pd.Timestamp("2026-06-15"))
    assert fake.calls, "obb economy route should have been called"
    assert set(pd.to_datetime(out["date"])) == {pd.Timestamp("2026-06-12")}
    assert len(out) == 1
