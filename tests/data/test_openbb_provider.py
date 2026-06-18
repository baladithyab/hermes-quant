"""Tests for hermes_quant.data.openbb_provider (aegis-ob1, ADR-0100).

Offline / deterministic — NO live network and NO openbb installed. The
``OpenBBProvider`` takes an injected ``obb`` seam: a fake object exposing
``.equity.price.historical(...)`` (returns a synthetic date-indexed OHLCV
DataFrame) and ``.equity.price.quote(...)``. This exercises the mapping,
the cardinal no-lookahead as_of filter, the latest-only HARD-REJECT, the
canonical-shape contract, and the routing fall-through — all WITHOUT the
``openbb`` SDK being importable.

The no-lookahead, default-OFF, and routing tests MUST NOT depend on openbb
being installed (they monkeypatch / inject the seam).

Covers (per the seed):
  1. NO-LOOKAHEAD (cardinal): a row with timestamp > as_of is DROPPED.
  2. ROUTING fall-through: fetch_with_chain([transient_yf, openbb]) falls
     through yfinance -> openbb and returns openbb's bars.
  3. DEFAULT-OFF byte-identical: with HERMES_QUANT_OPENBB unset, constructing
     / registering does NOT import openbb (a poisoned import would raise if
     triggered, and is not).
  4. LATEST-ONLY REJECTED: the quote/latest-only path raises in an asof
     context (never returns a latest-only snapshot).
  5. CANONICAL SHAPE: fetch_bars output is exactly REQUIRED_COLUMNS, UTC,
     sorted, deduped (via validate_bars).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from hermes_quant.data.base import REQUIRED_COLUMNS, fetch_with_chain
from hermes_quant.data.openbb_provider import (
    OPENBB_ENABLE_FLAG,
    OpenBBProvider,
)
from hermes_quant.protocol import DataProvider, DataProviderError


# ---------------------------------------------------------------------------
# Synthetic OpenBB response fixtures.
#
# OpenBB equity.price.historical returns an OBBject whose .to_dataframe()
# yields a DATE-INDEXED frame with lowercase OHLCV columns. We model both the
# OBBject (via .to_dataframe) and the bare-DataFrame injection path.
# ---------------------------------------------------------------------------
def _historical_frame(dates: list[str]) -> pd.DataFrame:
    """Build a date-indexed OHLCV frame (the OpenBB historical shape)."""
    idx = pd.DatetimeIndex(pd.to_datetime(dates), name="date")
    n = len(dates)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1_000_000 + i for i in range(n)],
        },
        index=idx,
    )


class _FakeOBBject:
    """Minimal OBBject: exposes .to_dataframe() like the real Platform."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _FakeObb:
    """Fake `openbb.obb` client.

    ``equity.price.historical(...)`` returns a recorded OBBject;
    ``equity.price.quote(...)`` records that it was reached (it must NOT be).
    """

    def __init__(self, hist_df: pd.DataFrame):
        self.historical_calls: list[dict] = []
        self.quote_calls: list[dict] = []
        self.chain_calls: list[dict] = []
        provider = self

        class _Price:
            def historical(self, **kwargs: Any) -> _FakeOBBject:
                provider.historical_calls.append(kwargs)
                return _FakeOBBject(hist_df)

            def quote(self, **kwargs: Any) -> Any:
                provider.quote_calls.append(kwargs)
                return {"price": 999.0}  # latest-only snapshot — must never escape

        class _Options:
            def chains(self, **kwargs: Any) -> Any:
                provider.chain_calls.append(kwargs)
                return {"chain": "latest-only"}  # must never escape

        self.equity = SimpleNamespace(price=_Price())
        self.derivatives = SimpleNamespace(options=_Options())


def _provider(hist_df: pd.DataFrame) -> OpenBBProvider:
    """OpenBBProvider with an injected fake obb; require_flag=False so tests
    that don't care about the env don't have to set it."""
    return OpenBBProvider(obb=_FakeObb(hist_df), require_flag=False)


# ===========================================================================
# 1. NO-LOOKAHEAD (the cardinal test)
# ===========================================================================
def test_no_lookahead_future_bar_dropped():
    """A bar with timestamp > as_of MUST be filtered out before return.

    RED-proof: without the leaf as_of filter the 2026-05-31 row (which is
    AFTER the 2026-05-29 as_of) survives — the assertion on the max timestamp
    and the absence of 2026-05-31 both fail.
    """
    df = _historical_frame(["2026-05-27", "2026-05-28", "2026-05-29", "2026-05-31"])
    prov = _provider(df)
    as_of = pd.Timestamp("2026-05-29")

    out = prov.fetch_bars(
        "AAPL",
        "1d",
        pd.Timestamp("2026-05-01"),
        pd.Timestamp("2026-06-30"),
        as_of=as_of,
    )

    # The future bar (2026-05-31 > 2026-05-29 as_of) must be gone.
    assert out["timestamp"].max() <= pd.Timestamp("2026-05-29")
    assert pd.Timestamp("2026-05-31") not in set(out["timestamp"])
    # The in-window bars survive.
    assert pd.Timestamp("2026-05-29") in set(out["timestamp"])
    assert len(out) == 3


def test_no_lookahead_tzaware_asof():
    """A tz-AWARE as_of compares cleanly against the tz-naive validated frame."""
    df = _historical_frame(["2026-05-27", "2026-05-28", "2026-05-30"])
    prov = _provider(df)
    as_of = pd.Timestamp("2026-05-28", tz="America/New_York")

    out = prov.fetch_bars(
        "AAPL", "1d", pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-30"), as_of=as_of
    )
    # 2026-05-30 is after the as_of -> dropped; comparison must not raise.
    assert pd.Timestamp("2026-05-30") not in set(out["timestamp"])


# ===========================================================================
# 2. ROUTING fall-through via fetch_with_chain
# ===========================================================================
class _TransientYF:
    """A yfinance-shaped provider that always raises a transient error."""

    name = "yfinance"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self):
        self.calls = 0

    def fetch_bars(self, *a: Any, **k: Any) -> pd.DataFrame:
        self.calls += 1
        raise DataProviderError("yfinance transient (429)")

    def fetch_latest(self, *a: Any, **k: Any) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def health(self) -> dict:  # pragma: no cover
        return {"provider": self.name}


def test_routing_falls_through_yf_to_openbb():
    """fetch_with_chain([transient_yf, openbb]) falls through to openbb.

    RED-proof: if openbb were NOT reached (e.g. not in the chain, or its
    fetch raised because the flag gate fired), the chain would exhaust and
    raise DataProviderError('all providers failed') instead of returning
    openbb's bars.
    """
    df = _historical_frame(["2026-05-27", "2026-05-28", "2026-05-29"])
    yf = _TransientYF()
    openbb = _provider(df)

    out = fetch_with_chain(
        [yf, openbb],
        "AAPL",
        "1d",
        pd.Timestamp("2026-05-01"),
        pd.Timestamp("2026-06-30"),
        max_retries=0,  # don't waste time on backoff in the test
    )

    assert yf.calls == 1  # yfinance was tried first and failed
    assert openbb._n_fetches == 1  # the chain reached openbb
    assert list(out.columns) == REQUIRED_COLUMNS
    assert len(out) == 3


# ===========================================================================
# 3. DEFAULT-OFF byte-identical (lazy import not triggered)
# ===========================================================================
def test_default_off_does_not_import_openbb(monkeypatch):
    """With HERMES_QUANT_OPENBB unset, constructing/registering must NOT
    import openbb. A fetch attempt fails-closed at the flag gate BEFORE the
    lazy import is reached.

    RED-proof: poison the `openbb` import so ANY attempt raises ImportError
    with a sentinel. Constructing the provider + the vendor singleton must
    NOT trigger it. A flag-off fetch must raise the FLAG error (disabled),
    NOT the poisoned-import error — proving the import was never reached.
    """
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    # Poison the import: if openbb is imported, raise a sentinel.
    class _Poison:
        def __getattr__(self, name):  # pragma: no cover - any access explodes
            raise ImportError("SENTINEL: openbb import was triggered")

    monkeypatch.setitem(sys.modules, "openbb", _Poison())

    # Construction (real flag-gated provider, no injected seam) — no import.
    prov = OpenBBProvider()  # require_flag defaults True
    # Vendor singleton construction — no import.
    from hermes_quant.data import vendor_routing

    monkeypatch.setattr(vendor_routing, "_OPENBB", None, raising=False)
    inst = vendor_routing._get_openbb()
    assert inst is not None  # built without importing openbb

    # A flag-off fetch fails closed at the FLAG gate, never reaching the import.
    # The discriminator: the flag-off message says "disabled"/"to enable"
    # (UNIQUE to the gate). The install-guidance message ("not installed") is
    # what surfaces if the gate is removed and the poisoned import IS reached
    # — so asserting on "disabled" RED-fails when the gate is bypassed.
    with pytest.raises(DataProviderError) as ei:
        prov.fetch_bars(
            "AAPL", "1d", pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-01")
        )
    msg = str(ei.value)
    assert "disabled" in msg.lower()
    assert OPENBB_ENABLE_FLAG in msg
    assert "not installed" not in msg.lower()  # the install path was NOT reached
    assert "SENTINEL" not in msg  # the poisoned import was NOT reached


def test_flag_on_but_openbb_missing_raises_guidance(monkeypatch):
    """Flag ON + openbb not installed -> a clear ImportError-with-guidance
    (DataProviderError), NOT a raw crash at module import time."""
    monkeypatch.setenv(OPENBB_ENABLE_FLAG, "1")
    monkeypatch.delitem(sys.modules, "openbb", raising=False)

    # Force the real lazy-import path (no injected seam) and ensure import fails.
    class _NoOpenbb:
        def find_module(self, *a, **k):
            return None

    # If openbb genuinely isn't installed, the import raises ImportError. Assert
    # the provider maps it to a guided DataProviderError.
    prov = OpenBBProvider()  # require_flag True, flag on
    with pytest.raises(DataProviderError) as ei:
        prov.fetch_bars(
            "AAPL", "1d", pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-01")
        )
    msg = str(ei.value).lower()
    assert "openbb" in msg
    assert "install" in msg


# ===========================================================================
# 4. LATEST-ONLY REJECTED
# ===========================================================================
def test_latest_only_quote_hard_rejected():
    """The latest-only quote path raises in an asof context — never returns a
    point-in-time-unsafe snapshot.

    RED-proof: if fetch_quote returned the snapshot (or wired quote into
    fetch_bars), this would not raise.
    """
    df = _historical_frame(["2026-05-27", "2026-05-28", "2026-05-29"])
    prov = _provider(df)

    with pytest.raises(DataProviderError) as ei:
        prov.fetch_quote("AAPL", as_of=pd.Timestamp("2026-05-29"))
    assert "latest-only" in str(ei.value).lower()

    # And fetch_bars must NOT have touched the quote endpoint.
    prov.fetch_bars(
        "AAPL", "1d", pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-30")
    )
    assert prov._obb.quote_calls == []  # quote never reached via fetch_bars


@pytest.mark.parametrize("provider", ["cboe", "yfinance", "CBOE"])
def test_latest_only_options_chain_hard_rejected(provider):
    """d2ef: CBOE/yfinance options chains are latest-only and must not be touched."""
    df = _historical_frame(["2026-05-27", "2026-05-28", "2026-05-29"])
    prov = _provider(df)

    with pytest.raises(DataProviderError) as ei:
        prov.fetch_options_chain("AAPL", as_of=pd.Timestamp("2026-05-29"), provider=provider)

    msg = str(ei.value).lower()
    assert "latest-only" in msg
    assert "options-chain" in msg or "options chain" in msg
    assert prov._obb.chain_calls == []


def test_options_chain_without_asof_hard_rejected():
    """An asof-less chain read is latest-only semantics, even before provider choice."""
    df = _historical_frame(["2026-05-27", "2026-05-28", "2026-05-29"])
    prov = _provider(df)

    with pytest.raises(DataProviderError) as ei:
        prov.fetch_options_chain("AAPL", provider="intrinio")

    assert "latest-only" in str(ei.value).lower()
    assert prov._obb.chain_calls == []


# ===========================================================================
# 5. CANONICAL SHAPE
# ===========================================================================
def test_canonical_shape_columns_utc_sorted_deduped():
    """fetch_bars output has exactly REQUIRED_COLUMNS, UTC tz-naive, sorted
    ascending, deduped (lean on validate_bars)."""
    # Out-of-order + a duplicate timestamp (validate_bars keeps last + sorts).
    df = _historical_frame(["2026-05-29", "2026-05-27", "2026-05-28", "2026-05-28"])
    prov = _provider(df)

    out = prov.fetch_bars(
        "AAPL", "1d", pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-30")
    )

    assert list(out.columns) == REQUIRED_COLUMNS
    # tz-naive UTC (validate_bars normalizes)
    assert out["timestamp"].dt.tz is None
    # sorted ascending
    assert out["timestamp"].is_monotonic_increasing
    # deduped: 4 input rows with one dup timestamp -> 3 unique timestamps
    assert out["timestamp"].is_unique
    assert len(out) == 3


def test_satisfies_dataprovider_protocol():
    """OpenBBProvider conforms to the runtime_checkable DataProvider Protocol."""
    prov = OpenBBProvider(require_flag=False)
    assert isinstance(prov, DataProvider)
    assert prov.name == "openbb"
    assert "equity" in prov.asset_classes


def test_registered_as_second_ohlcv_tier():
    """ADR-0100: openbb sits in VENDOR_LIST as the 2nd OHLCV tier behind
    yfinance (ahead of ccxt) and is routable for fetch_bars."""
    from hermes_quant.data import vendor_routing

    vl = vendor_routing.VENDOR_LIST
    assert vl.index("openbb") == vl.index("yfinance") + 1
    assert vl.index("openbb") < vl.index("ccxt")
    # routable
    fn = vendor_routing.route_to_vendor("fetch_bars", "openbb")
    assert callable(fn)
