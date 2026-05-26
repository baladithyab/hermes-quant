"""Tests for hermes_quant.watchlist (ADR-0016 §D5).

Covers add/remove/list, validation, idempotency, atomic-rename, and
flock concurrent-write safety.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from hermes_quant.watchlist import (
    WatchlistEntry,
    add_to_watchlist,
    clear_watchlist,
    list_watchlist,
    remove_from_watchlist,
)


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


# ---------------------------------------------------------------------------
# Empty / missing config
# ---------------------------------------------------------------------------


def test_list_empty_when_config_missing(tmp_config: Path):
    assert list_watchlist(path=tmp_config) == []


def test_list_empty_when_no_watchlist_key(tmp_config: Path):
    tmp_config.write_text("quant:\n  pdr:\n    mode: advise\n", encoding="utf-8")
    assert list_watchlist(path=tmp_config) == []


def test_list_empty_when_config_corrupt(tmp_config: Path):
    tmp_config.write_text("not: yaml: at all: ::: !!", encoding="utf-8")
    assert list_watchlist(path=tmp_config) == []


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------


def test_add_creates_config_when_absent(tmp_config: Path):
    entry = add_to_watchlist("AAPL", "equity", path=tmp_config)
    assert entry == WatchlistEntry("AAPL", "equity", "1d")
    assert tmp_config.exists()
    assert list_watchlist(path=tmp_config) == [entry]


def test_add_uses_default_timeframe_per_asset_class(tmp_config: Path):
    entry = add_to_watchlist("BTC/USDT", "crypto", path=tmp_config)
    assert entry.timeframe == "1h"


def test_add_explicit_timeframe(tmp_config: Path):
    entry = add_to_watchlist("AAPL", "equity", "5m", path=tmp_config)
    assert entry.timeframe == "5m"


def test_add_rejects_invalid_asset_class(tmp_config: Path):
    with pytest.raises(ValueError, match="asset_class"):
        add_to_watchlist("AAPL", "stocks", path=tmp_config)


def test_add_rejects_invalid_timeframe(tmp_config: Path):
    with pytest.raises(ValueError, match="timeframe"):
        add_to_watchlist("AAPL", "equity", "999h", path=tmp_config)


def test_add_rejects_empty_symbol(tmp_config: Path):
    with pytest.raises(ValueError, match="symbol"):
        add_to_watchlist("   ", "equity", path=tmp_config)


def test_add_idempotent_on_symbol_and_asset_class(tmp_config: Path):
    """Re-adding (symbol, asset_class) replaces, doesn't append."""
    add_to_watchlist("AAPL", "equity", "1d", path=tmp_config)
    add_to_watchlist("AAPL", "equity", "5m", path=tmp_config)
    entries = list_watchlist(path=tmp_config)
    assert len(entries) == 1
    assert entries[0].timeframe == "5m"


def test_add_distinct_when_asset_class_differs(tmp_config: Path):
    """Same symbol + different asset_class is a distinct entry."""
    add_to_watchlist("FB", "equity", path=tmp_config)
    # made-up scenario: same ticker symbol on a different asset_class
    add_to_watchlist("FB", "etf", path=tmp_config)
    entries = list_watchlist(path=tmp_config)
    assert len(entries) == 2


def test_add_preserves_unrelated_config_keys(tmp_config: Path):
    """Adding to watchlist must not clobber other config sections."""
    tmp_config.write_text(
        "homepage: https://example.com\n"
        "quant:\n"
        "  pdr:\n"
        "    mode: advise\n"
        "  risk:\n"
        "    profile: moderate\n",
        encoding="utf-8",
    )
    add_to_watchlist("AAPL", "equity", path=tmp_config)

    import yaml

    cfg = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
    assert cfg["homepage"] == "https://example.com"
    assert cfg["quant"]["pdr"]["mode"] == "advise"
    assert cfg["quant"]["risk"]["profile"] == "moderate"
    assert len(cfg["quant"]["autonomous"]["watchlist"]) == 1


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def test_remove_existing_returns_true(tmp_config: Path):
    add_to_watchlist("AAPL", "equity", path=tmp_config)
    assert remove_from_watchlist("AAPL", path=tmp_config) is True
    assert list_watchlist(path=tmp_config) == []


def test_remove_nonexistent_returns_false(tmp_config: Path):
    add_to_watchlist("AAPL", "equity", path=tmp_config)
    assert remove_from_watchlist("MSFT", path=tmp_config) is False
    assert len(list_watchlist(path=tmp_config)) == 1


def test_remove_with_asset_class_filter(tmp_config: Path):
    add_to_watchlist("FB", "equity", path=tmp_config)
    add_to_watchlist("FB", "etf", path=tmp_config)
    removed = remove_from_watchlist("FB", asset_class="equity", path=tmp_config)
    assert removed is True
    entries = list_watchlist(path=tmp_config)
    assert len(entries) == 1
    assert entries[0].asset_class == "etf"


def test_clear_returns_count(tmp_config: Path):
    add_to_watchlist("AAPL", "equity", path=tmp_config)
    add_to_watchlist("MSFT", "equity", path=tmp_config)
    n = clear_watchlist(path=tmp_config)
    assert n == 2
    assert list_watchlist(path=tmp_config) == []


# ---------------------------------------------------------------------------
# Atomic-rename + concurrent-write
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_state_on_disk(tmp_config: Path, monkeypatch):
    """If the write process is interrupted between fsync and rename, the
    original config is preserved (no partial write)."""
    # Seed
    add_to_watchlist("AAPL", "equity", path=tmp_config)
    original = tmp_config.read_text(encoding="utf-8")

    # Monkeypatch os.replace to raise mid-write
    real_replace = os.replace
    call_count = {"n": 0}

    def boom(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated crash")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        add_to_watchlist("MSFT", "equity", path=tmp_config)

    # Original content preserved
    assert tmp_config.read_text(encoding="utf-8") == original


def test_concurrent_adds_serialize(tmp_config: Path):
    """Multi-thread adds must not corrupt the watchlist (flock + RLock)."""
    add_to_watchlist("AAPL", "equity", path=tmp_config)

    barrier = threading.Barrier(8)
    errors = []

    def worker(idx: int):
        try:
            barrier.wait()
            add_to_watchlist(f"SYM{idx}", "equity", path=tmp_config)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    entries = list_watchlist(path=tmp_config)
    symbols = {e.symbol for e in entries}
    # AAPL + 8 SYM* = 9
    assert len(entries) == 9
    assert symbols == {"AAPL"} | {f"SYM{i}" for i in range(8)}
