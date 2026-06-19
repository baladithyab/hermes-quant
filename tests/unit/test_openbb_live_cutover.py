"""tests/unit/test_openbb_live_cutover.py — c7a9 + 2f33 (ADR-0100 consumer cutovers).

c7a9 — OpenBB OHLCV into the LIVE advisor provider chain
--------------------------------------------------------
``OpenBBProvider`` is registered in ``vendor_routing.VENDOR_LIST`` but the live
fetch path (``advisor._get_default_provider`` -> a bare ``YFinanceProvider``,
consumed by both ``recommend`` and ``recommend_multi_horizon``) never consulted
openbb. Behind a NEW default-OFF flag ``HERMES_QUANT_OPENBB_LIVE`` (distinct
from ``HERMES_QUANT_OPENBB`` which gates the provider SDK), ``_get_default_provider``
now returns a chained provider that consults the openbb tier as a 2nd/fallback
source (yfinance PRIMARY). Default-OFF => byte-identical bare ``YFinanceProvider``.

Rails verified:
  * Default-OFF: flag unset -> bare YFinanceProvider (byte-identical type/identity).
  * Flag-ON, yfinance OK -> openbb NOT consulted (yfinance is primary).
  * Flag-ON, yfinance fails (transient) -> openbb consulted as fallback tier.
  * No-lookahead: the ``as_of`` cutoff is threaded into BOTH tiers' fetch_bars.

2f33 — EstimatesAnalyst / InsiderAnalyst as live committee inputs
-----------------------------------------------------------------
Both analysts exist (estimates.py / insider.py) gated default-OFF behind their
own per-analyst flag AND ``HERMES_QUANT_OPENBB``, but were NOT registered in the
advisor committee builders. Option (a): register them behind their existing
default-OFF flags so an operator CAN enable them, mirroring FundamentalsAnalyst.

Rails verified:
  * Both flags off -> roster byte-identical (no estimates/insider voice).
  * Per-analyst flag + OPENBB on -> the analyst joins the roster.
  * The canonical recommend() inline roster wires both flags too.
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from hermes_quant.protocol import DataProviderError


# ---------------------------------------------------------------------------
# c7a9 — _get_default_provider OpenBB live cutover
# ---------------------------------------------------------------------------


def _bars(n: int = 3, start: str = "2024-01-02") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.5] * n,
            "volume": [1_000_000.0] * n,
        }
    )


def test_default_off_is_bare_yfinance(monkeypatch):
    """Flag unset -> _get_default_provider returns a bare YFinanceProvider.

    Byte-identical to the pre-cutover live path: same concrete type, no wrapper.
    """
    monkeypatch.delenv("HERMES_QUANT_OPENBB_LIVE", raising=False)
    from hermes_quant.advisor import _get_default_provider
    from hermes_quant.data.yfinance_provider import YFinanceProvider

    prov = _get_default_provider("equity")
    assert type(prov) is YFinanceProvider


def test_default_off_etf_is_bare_yfinance(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_OPENBB_LIVE", raising=False)
    from hermes_quant.advisor import _get_default_provider
    from hermes_quant.data.yfinance_provider import YFinanceProvider

    assert type(_get_default_provider("etf")) is YFinanceProvider


def test_flag_on_returns_chained_provider_consulting_openbb(monkeypatch):
    """Flag ON -> provider is a chain whose fallback tier is openbb.

    When the PRIMARY (yfinance) fails transiently, the chain falls through to
    the openbb tier and returns its bars. RED before the cutover: the bare
    YFinanceProvider has no openbb fallback, so a yfinance failure propagates.
    """
    monkeypatch.setenv("HERMES_QUANT_OPENBB_LIVE", "1")
    from hermes_quant.advisor import _get_default_provider

    prov = _get_default_provider("equity")

    # Substitute deterministic in-memory tiers so no network/SDK is touched.
    yf_calls: list[dict] = []
    obb_calls: list[dict] = []

    class _FailingYF:
        name = "yfinance"

        def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
            yf_calls.append({"asset": asset, "as_of": as_of})
            raise DataProviderError("yfinance transient failure")

    class _OkOpenBB:
        name = "openbb"

        def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
            obb_calls.append({"asset": asset, "as_of": as_of})
            return _bars()

    # The chained provider must expose its tier list so we can inject doubles.
    assert hasattr(prov, "_providers"), (
        "flag-ON provider must be a chain exposing its tier list"
    )
    prov._providers = [_FailingYF(), _OkOpenBB()]

    asof = pd.Timestamp("2024-01-05", tz="UTC")
    out = prov.fetch_bars("AAPL", "1d", pd.Timestamp("2024-01-01"), asof, as_of=asof)

    assert len(out) == 3  # openbb tier supplied the bars after yfinance failed
    assert obb_calls, "openbb tier was NOT consulted on yfinance failure"
    # No-lookahead: the as_of cutoff reached the openbb tier verbatim.
    assert obb_calls[0]["as_of"] == asof


def test_flag_on_yfinance_primary_short_circuits_openbb(monkeypatch):
    """Flag ON but yfinance succeeds -> openbb is NEVER consulted (yfinance primary)."""
    monkeypatch.setenv("HERMES_QUANT_OPENBB_LIVE", "1")
    from hermes_quant.advisor import _get_default_provider

    prov = _get_default_provider("equity")
    obb_calls: list = []

    class _OkYF:
        name = "yfinance"

        def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
            return _bars()

    class _OpenBB:
        name = "openbb"

        def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
            obb_calls.append(asset)
            return _bars()

    prov._providers = [_OkYF(), _OpenBB()]
    out = prov.fetch_bars("AAPL", "1d", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-05"))
    assert len(out) == 3
    assert obb_calls == [], "openbb consulted even though yfinance (primary) succeeded"


def test_unsupported_asset_class_still_raises(monkeypatch):
    """The cutover does not loosen the asset-class guard (crypto still NotImplemented)."""
    monkeypatch.setenv("HERMES_QUANT_OPENBB_LIVE", "1")
    from hermes_quant.advisor import _get_default_provider

    with pytest.raises(NotImplementedError):
        _get_default_provider("crypto")


# ---------------------------------------------------------------------------
# 2f33 — EstimatesAnalyst / InsiderAnalyst committee registration
# ---------------------------------------------------------------------------


def test_estimates_insider_excluded_when_flags_off(monkeypatch):
    """Both per-analyst flags off -> roster byte-identical (no estimates/insider)."""
    monkeypatch.delenv("HERMES_QUANT_ESTIMATES_ANALYST", raising=False)
    monkeypatch.delenv("HERMES_QUANT_INSIDER_ANALYST", raising=False)
    monkeypatch.delenv("HERMES_QUANT_OPENBB", raising=False)
    from hermes_quant.advisor import _build_default_analysts

    names = [getattr(a, "name", "?") for a in _build_default_analysts()]
    assert "estimates" not in names
    assert "insider" not in names


def test_estimates_included_when_flag_on(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_ESTIMATES_ANALYST", "1")
    monkeypatch.setenv("HERMES_QUANT_OPENBB", "1")
    from hermes_quant.advisor import _build_default_analysts

    names = [getattr(a, "name", "?") for a in _build_default_analysts()]
    assert "estimates" in names


def test_insider_included_when_flag_on(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_INSIDER_ANALYST", "1")
    monkeypatch.setenv("HERMES_QUANT_OPENBB", "1")
    from hermes_quant.advisor import _build_default_analysts

    names = [getattr(a, "name", "?") for a in _build_default_analysts()]
    assert "insider" in names


def test_recommend_inline_roster_wires_both_flags():
    """The canonical recommend() inline roster must reach BOTH new flags so
    operator recommendations + ablations through recommend() exercise them
    (mirrors the OvernightDrift precedent — flags must reach both rosters)."""
    import hermes_quant.advisor as adv

    src = inspect.getsource(adv.recommend)
    assert "HERMES_QUANT_ESTIMATES_ANALYST" in src
    assert "EstimatesAnalyst" in src
    assert "HERMES_QUANT_INSIDER_ANALYST" in src
    assert "InsiderAnalyst" in src
