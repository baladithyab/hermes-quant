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
    _VALID_ASSET_CLASSES,
    WatchlistEntry,
    add_to_watchlist,
    clear_watchlist,
    list_watchlist,
    materialize_profile_fit_entries,
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


# ---------------------------------------------------------------------------
# W4: horizon_set add-only field (mirrors agperc1 options_eligible pattern)
# ---------------------------------------------------------------------------


def test_horizon_set_defaults_none_and_round_trips_byte_identical():
    """ADD-ONLY: an entry with no horizon_set keeps the field None and the
    to_dict round-trip is byte-identical to the pre-W4 schema plus the new
    nullable key — exactly the agperc1 options_eligible contract."""
    entry = WatchlistEntry("AAPL", "equity", "1d")
    assert entry.horizon_set is None
    d = entry.to_dict()
    # The new key is present and null — never silently picks up a horizon set.
    assert d["horizon_set"] is None
    assert d == {
        "symbol": "AAPL",
        "asset_class": "equity",
        "timeframe": "1d",
        "options_eligible": False,
        "horizon_set": None,
    }


def test_horizon_set_round_trips_through_list_watchlist(tmp_config: Path):
    """A config watchlist row carrying horizon_set loads back with the list
    intact; an existing row WITHOUT the key loads back as None (byte-identical)."""
    tmp_config.write_text(
        "quant:\n"
        "  autonomous:\n"
        "    watchlist:\n"
        "      - symbol: AAPL\n"
        "        asset_class: equity\n"
        "        timeframe: 1d\n"
        "        horizon_set: [\"1D\", \"7D\", \"14D\", \"30D\"]\n"
        "      - symbol: MSFT\n"
        "        asset_class: equity\n"
        "        timeframe: 1d\n",
        encoding="utf-8",
    )
    entries = {e.symbol: e for e in list_watchlist(path=tmp_config)}
    assert entries["AAPL"].horizon_set == ["1D", "7D", "14D", "30D"]
    # Existing row with no horizon_set → None (byte-identical to pre-W4).
    assert entries["MSFT"].horizon_set is None


def test_existing_add_path_unchanged_horizon_set_none(tmp_config: Path):
    """The existing add/list path never writes a horizon_set (the operator
    watchlist add does not pick horizons) — default-OFF byte-identical."""
    add_to_watchlist("AAPL", "equity", path=tmp_config)
    [entry] = list_watchlist(path=tmp_config)
    assert entry.horizon_set is None
    # The persisted dict carries the nullable key but no list.
    import yaml

    cfg = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
    row = cfg["quant"]["autonomous"]["watchlist"][0]
    assert row.get("horizon_set") is None


# ---------------------------------------------------------------------------
# W4: profile-fit.json -> WatchlistEntry materialization adapter
# ---------------------------------------------------------------------------


def _profile_fit_payload() -> dict:
    """A profile-fit.json shape (the single watchlist W3 emits): active rows
    each carrying symbol / asset_class / options_eligible / shortable /
    horizon_set / fit_score / asof. NO per-play bucketing."""
    return {
        "asof": "2026-06-17T00:00:00+00:00",
        "active": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "options_eligible": True,
                "shortable": True,
                "horizon_set": ["1D", "7D", "14D", "30D"],
                "fit_score": 0.87,
                "asof": "2026-06-17T00:00:00+00:00",
            },
            {
                "symbol": "MSFT",
                "asset_class": "equity",
                "options_eligible": False,
                "shortable": False,
                "horizon_set": ["1D", "7D", "14D", "30D"],
                "fit_score": 0.71,
                "asof": "2026-06-17T00:00:00+00:00",
            },
        ],
    }


def test_materialize_profile_fit_rows_to_watchlist_entries():
    """The adapter materializes profile-fit active rows into config-watchlist
    WatchlistEntry objects carrying symbol/asset_class/options_eligible +
    horizon_set. The watchlist entry NEVER names a strategy."""
    entries = materialize_profile_fit_entries(_profile_fit_payload())
    by_sym = {e.symbol: e for e in entries}
    assert set(by_sym) == {"AAPL", "MSFT"}

    aapl = by_sym["AAPL"]
    assert aapl.asset_class == "equity"
    assert aapl.options_eligible is True
    assert aapl.horizon_set == ["1D", "7D", "14D", "30D"]
    # timeframe defaults from the asset_class map (equity -> 1d); the row never
    # pre-picks a strategy — only profile-fit + horizons.
    assert aapl.timeframe == "1d"

    msft = by_sym["MSFT"]
    assert msft.options_eligible is False
    assert msft.horizon_set == ["1D", "7D", "14D", "30D"]


def test_materialize_empty_when_no_active_rows():
    """Silence-by-default: a payload with no active rows yields no entries."""
    assert materialize_profile_fit_entries({"asof": "2026-06-17T00:00:00+00:00"}) == []
    assert materialize_profile_fit_entries({"active": []}) == []


def test_materialize_skips_rows_missing_required_keys():
    """A malformed active row (no symbol or no asset_class) is dropped, not
    crashed on — matches list_watchlist's defensive loader."""
    payload = {
        "active": [
            {"asset_class": "equity", "horizon_set": ["1D"]},  # no symbol
            {"symbol": "NOCLASS", "horizon_set": ["1D"]},  # no asset_class
            {"symbol": "GOOD", "asset_class": "equity", "horizon_set": ["1D"]},
        ]
    }
    entries = materialize_profile_fit_entries(payload)
    assert [e.symbol for e in entries] == ["GOOD"]


def test_materialize_rejects_unknown_horizon_label():
    """Validation: a horizon label not in the known W2 rungs is rejected.
    Money-software fail-closed — an unknown rung must not silently flow to the
    decision layer's DTE resolver."""
    payload = {
        "active": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "horizon_set": ["1D", "BOGUS"],
            }
        ]
    }
    with pytest.raises(ValueError, match="horizon"):
        materialize_profile_fit_entries(payload)


def test_materialize_accepts_known_rungs_including_0d():
    """The canonical W2 rungs are 0D/1D/7D/14D/30D; all are accepted labels
    (0D membership is itself flag-gated upstream, but the LABEL is valid)."""
    payload = {
        "active": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "horizon_set": ["0D", "1D", "7D", "14D", "30D"],
            }
        ]
    }
    [entry] = materialize_profile_fit_entries(payload)
    assert entry.horizon_set == ["0D", "1D", "7D", "14D", "30D"]


def test_materialize_known_rungs_injectable_composes_with_w2():
    """W4 must COMPOSE with W2's HORIZONS keys without duplicating its module:
    the adapter accepts an injectable known-rung set so the caller (the cron
    integration) can pass horizons.HORIZONS.keys() verbatim."""
    payload = {
        "active": [
            {"symbol": "AAPL", "asset_class": "equity", "horizon_set": ["W1"]}
        ]
    }
    # With a custom rung set, "W1" is valid.
    [entry] = materialize_profile_fit_entries(payload, known_rungs={"W1", "W2"})
    assert entry.horizon_set == ["W1"]
    # And the default canonical set would reject it.
    with pytest.raises(ValueError, match="horizon"):
        materialize_profile_fit_entries(payload)


# ---------------------------------------------------------------------------
# rt05 — asset_class W3->W4 seam: the materialized entry's asset_class MUST be
# a canonical watchlist class, never the Alpaca universe filter token.
# ---------------------------------------------------------------------------


def test_materialize_real_producer_value_yields_canonical_asset_class(tmp_path: Path):
    """rt05 integration: feed the payload the REAL producer (profile_scan)
    emits and assert every materialized entry's asset_class is a class the
    consumer accepts (``_VALID_ASSET_CLASSES``).

    The materialize tests above hand-rolled ``"equity"`` in their fixture, which
    masked that the live producer emits the Alpaca universe filter token. This
    test runs the actual producer so the seam is validated end-to-end.
    """
    from hermes_quant.playbook import profile_scan

    uni = tmp_path / "universe.json"
    payload = {
        "asof": "2026-06-18T10:15:55+00:00",
        "count": 1,
        "filters": {"asset_class": "us_equity", "max_price": 500.0, "min_price": 5.0},
        "symbols": [
            {
                "symbol": "AAA",
                "avg_dollar_volume_30d": 50_000_000.0,
                "last_close": 100.0,
                "tradable": True,
                "shortable": True,
            }
        ],
    }
    uni.parent.mkdir(parents=True, exist_ok=True)
    uni.write_text(__import__("json").dumps(payload), encoding="utf-8")
    out = tmp_path / "profile-fit.json"
    produced = profile_scan.build_profile_watchlist(
        uni, asof="2026-06-18T10:15:55+00:00", fetch=False, out_path=out
    )
    assert produced["active"], "AAA should be eligible"

    entries = materialize_profile_fit_entries(produced)
    assert entries, "the real producer payload must materialize at least one entry"
    for e in entries:
        assert e.asset_class in _VALID_ASSET_CLASSES, (
            f"materialized asset_class {e.asset_class!r} is not a valid watchlist "
            f"class {sorted(_VALID_ASSET_CLASSES)}"
        )


def test_materialize_rejects_invalid_asset_class():
    """rt05 defense-in-depth: the seam fail-CLOSED rejects an unknown
    asset_class rather than silently passing it.

    Mirrors the existing horizon_set rung validation: an asset_class outside
    ``_VALID_ASSET_CLASSES`` (e.g. the Alpaca universe filter token
    ``us_equity``) must raise so it can never silently flow to the decision
    layer's asset-class routing.
    """
    payload = {
        "active": [
            {
                "symbol": "AAPL",
                "asset_class": "us_equity",  # the universe filter token, not canonical
                "horizon_set": ["1D"],
            }
        ]
    }
    with pytest.raises(ValueError, match="asset_class"):
        materialize_profile_fit_entries(payload)


def test_materialize_rejects_arbitrary_unknown_asset_class():
    """A made-up asset_class is also fail-closed rejected (not just us_equity)."""
    payload = {
        "active": [
            {"symbol": "X", "asset_class": "bogus_class", "horizon_set": ["1D"]}
        ]
    }
    with pytest.raises(ValueError, match="asset_class"):
        materialize_profile_fit_entries(payload)


def test_materialize_accepts_all_canonical_asset_classes():
    """Every canonical class round-trips (no over-tight guard)."""
    payload = {
        "active": [
            {"symbol": f"S_{ac}", "asset_class": ac, "horizon_set": ["1D"]}
            for ac in sorted(_VALID_ASSET_CLASSES)
        ]
    }
    entries = materialize_profile_fit_entries(payload)
    assert {e.asset_class for e in entries} == _VALID_ASSET_CLASSES
