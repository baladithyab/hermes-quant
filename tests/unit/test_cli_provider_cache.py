"""Tests for backtest CLI provider/cache wiring (V03-7)."""
from __future__ import annotations

import argparse

import pandas as pd
import pytest

from hermes_quant.cli import _fetch_bars_via_provider, setup_argparse


def test_backtest_parser_accepts_provider_cache_flags():
    p = argparse.ArgumentParser()
    setup_argparse(p)
    args = p.parse_args([
        "backtest",
        "--symbol", "BTC/USDT",
        "--asset-class", "crypto",
        "--provider", "ccxt:kraken",
        "--cache-root", "/tmp/hq-cache",
        "--walk-forward",
        "--n-splits", "3",
    ])
    assert args.provider == "ccxt:kraken"
    assert args.cache_root == "/tmp/hq-cache"
    assert args.walk_forward
    assert args.n_splits == 3


def test_fetch_provider_rejects_unknown_provider():
    with pytest.raises(ValueError):
        _fetch_bars_via_provider(
            symbol="BTC/USDT",
            asset_class="crypto",
            timeframe="1h",
            start=None,
            end=None,
            provider_spec="bogus",
        )


def test_fetch_provider_asset_class_mismatch_returns_none():
    out = _fetch_bars_via_provider(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        start=None,
        end=None,
        provider_spec="ccxt:kraken",
    )
    assert out is None
