"""Unit tests for hermes_quant.universe.alpaca_scanner.

All Alpaca clients are mocked. No network calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_quant.universe.alpaca_scanner import scan_universe

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


@dataclass
class FakeAsset:
    symbol: str
    exchange: str = "NASDAQ"
    tradable: bool = True
    fractionable: bool = True
    shortable: bool = True


@dataclass
class FakeBar:
    close: float
    volume: float


def make_bars(close: float, volume: float, n: int = 25) -> list[FakeBar]:
    return [FakeBar(close=close, volume=volume) for _ in range(n)]


def make_assets() -> list[FakeAsset]:
    """Six assets that exercise every filter branch."""
    return [
        FakeAsset(symbol="AAPL"),  # passes everything
        FakeAsset(symbol="MSFT"),  # passes everything
        FakeAsset(symbol="PENNY"),  # last_close too low ($2)
        FakeAsset(symbol="EXPENSIVE"),  # last_close too high ($800)
        FakeAsset(symbol="THIN"),  # passes price, fails dollar-volume
        FakeAsset(symbol="UNTRADABLE", tradable=False),  # dropped at asset stage
        FakeAsset(symbol="WHOLEONLY", fractionable=False),  # dropped at asset stage
    ]


# Bars keyed by symbol.
BARS_BY_SYMBOL: dict[str, list[FakeBar]] = {
    "AAPL": make_bars(close=200.0, volume=80_000_000),  # ADV ~ $16B
    "MSFT": make_bars(close=400.0, volume=20_000_000),  # ADV ~ $8B
    "PENNY": make_bars(close=2.0, volume=10_000_000),  # ADV $20M but price too low
    "EXPENSIVE": make_bars(close=800.0, volume=1_000_000),  # ADV $800M but price too high
    "THIN": make_bars(close=50.0, volume=10_000),  # ADV $500k — too thin
    # UNTRADABLE / WHOLEONLY should never reach the bars stage; if they do,
    # we still return data so we can prove the asset-level filter dropped them.
    "UNTRADABLE": make_bars(close=100.0, volume=5_000_000),
    "WHOLEONLY": make_bars(close=100.0, volume=5_000_000),
}


def _build_clients(assets: list[FakeAsset]) -> tuple[MagicMock, MagicMock]:
    """Return (trading_client, data_client) mocks wired to the fixture data."""
    trading = MagicMock()
    trading.get_all_assets.return_value = assets

    data = MagicMock()

    def fake_get_stock_bars(req):
        syms = req.symbol_or_symbols
        if isinstance(syms, str):
            syms = [syms]
        out = MagicMock()
        out.data = {s: BARS_BY_SYMBOL[s] for s in syms if s in BARS_BY_SYMBOL}
        return out

    data.get_stock_bars.side_effect = fake_get_stock_bars
    return trading, data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run(tmp_path: Path, **overrides) -> dict:
    trading, data = _build_clients(make_assets())
    out = tmp_path / "universe.json"
    payload = scan_universe(
        output_path=out,
        trading_client=trading,
        data_client=data,
        **overrides,
    )
    assert out.exists(), "scan_universe must write JSON to output_path"
    on_disk = json.loads(out.read_text())
    assert on_disk == payload, "atomic write must contain the returned payload"
    return payload


def test_price_filter_excludes_too_low_and_too_high(tmp_path: Path):
    payload = _run(tmp_path)
    syms = {r["symbol"] for r in payload["symbols"]}
    assert "AAPL" in syms
    assert "MSFT" in syms
    assert "PENNY" not in syms, "price-too-low must be excluded"
    assert "EXPENSIVE" not in syms, "price-too-high must be excluded"


def test_dollar_volume_filter_excludes_thin_names(tmp_path: Path):
    payload = _run(tmp_path)
    syms = {r["symbol"] for r in payload["symbols"]}
    assert "THIN" not in syms, "thin ADV must be excluded"
    # Sanity: AAPL stays because it's far above min ADV.
    assert "AAPL" in syms


def test_non_tradable_assets_dropped(tmp_path: Path):
    payload = _run(tmp_path)
    syms = {r["symbol"] for r in payload["symbols"]}
    assert "UNTRADABLE" not in syms
    # Every surviving row must report tradable=True.
    assert all(r["tradable"] for r in payload["symbols"])


def test_fractionable_false_assets_dropped(tmp_path: Path):
    payload = _run(tmp_path)
    syms = {r["symbol"] for r in payload["symbols"]}
    assert "WHOLEONLY" not in syms
    assert all(r["fractionable"] for r in payload["symbols"])


def test_output_is_valid_json_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Atomic-write contract: tempfile + os.replace, no leftover *.tmp files."""
    out = tmp_path / "deep" / "nested" / "universe.json"  # parent must be created
    trading, data = _build_clients(make_assets())

    payload = scan_universe(output_path=out, trading_client=trading, data_client=data)

    # JSON validity
    parsed = json.loads(out.read_text())
    assert parsed["count"] == len(parsed["symbols"])
    assert parsed["count"] == payload["count"]

    # Required envelope fields
    assert "asof" in parsed
    assert "filters" in parsed
    assert parsed["filters"]["min_price"] == 5.0
    assert parsed["filters"]["max_price"] == 500.0

    # Per-symbol schema
    for row in parsed["symbols"]:
        for k in (
            "symbol",
            "exchange",
            "last_close",
            "avg_dollar_volume_30d",
            "tradable",
            "shortable",
            "fractionable",
        ):
            assert k in row, f"missing field {k} in row {row}"

    # No leftover tempfile in the output dir
    leftovers = list(out.parent.glob("*.tmp"))
    assert leftovers == [], f"tempfiles leaked: {leftovers}"


def test_results_sorted_descending_by_dollar_volume(tmp_path: Path):
    payload = _run(tmp_path)
    advs = [r["avg_dollar_volume_30d"] for r in payload["symbols"]]
    assert advs == sorted(advs, reverse=True)


def test_max_symbols_caps_output(tmp_path: Path):
    payload = _run(tmp_path, max_symbols=1)
    assert len(payload["symbols"]) == 1
    # Highest ADV in our fixture is AAPL.
    assert payload["symbols"][0]["symbol"] == "AAPL"


def test_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for k in (
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    with pytest.raises(RuntimeError, match="ALPACA_API_KEY and ALPACA_API_SECRET"):
        scan_universe(output_path=tmp_path / "should-not-exist.json")
