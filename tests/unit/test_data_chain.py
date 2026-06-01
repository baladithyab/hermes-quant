"""Tests for hermes_quant.data.chain.build_provider_chain (B22, R-B22).

Default-OFF rail: with HERMES_QUANT_DATA_FALLBACK unset, the equity chain is a
single YFinanceProvider (byte-identical to today's single-provider path). With
the flag set AND a key present, AlphaVantage is appended LAST. Fail-closed: no
key -> AV silently dropped. Crypto is ccxt-only.

Offline/deterministic — no network. CcxtProvider construction is avoided in the
default crypto path by NOT exercising it here beyond type assertions where the
ccxt SDK is available; we monkeypatch env flags only.
"""

from __future__ import annotations

import pytest

from hermes_quant.data.chain import (
    build_provider_chain,
    data_fallback_enabled,
)
from hermes_quant.data.yfinance_provider import YFinanceProvider


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_DATA_FALLBACK", raising=False)
    assert data_fallback_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DATA_FALLBACK", "1")
    assert data_fallback_enabled() is True


def test_equity_default_off_is_single_yfinance(monkeypatch):
    """Flag OFF -> exactly one provider (yfinance), regardless of AV key."""
    monkeypatch.delenv("HERMES_QUANT_DATA_FALLBACK", raising=False)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "KEY")  # present but ignored when OFF
    chain = build_provider_chain("equity")
    assert len(chain) == 1
    assert isinstance(chain[0], YFinanceProvider)


def test_etf_default_off_is_single_yfinance(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_DATA_FALLBACK", raising=False)
    chain = build_provider_chain("etf")
    assert len(chain) == 1
    assert isinstance(chain[0], YFinanceProvider)


def test_equity_flag_on_with_key_appends_alphavantage_last(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DATA_FALLBACK", "1")
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "KEY")
    chain = build_provider_chain("equity")
    assert len(chain) == 2
    assert isinstance(chain[0], YFinanceProvider)
    assert chain[-1].name == "alphavantage"  # AV is LAST (last-resort)


def test_equity_flag_on_without_key_drops_alphavantage(monkeypatch):
    """Fail-closed: no AV key -> AV tier silently dropped, chain still works."""
    monkeypatch.setenv("HERMES_QUANT_DATA_FALLBACK", "1")
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    chain = build_provider_chain("equity")
    assert len(chain) == 1
    assert isinstance(chain[0], YFinanceProvider)


def test_unknown_asset_class_raises():
    with pytest.raises(ValueError, match="unknown asset_class"):
        build_provider_chain("forex")
