"""tests/playbook/test_profile_scan.py — W3 asof profile-fit scanner pipeline.

The scan -> score -> select -> emit ONE-watchlist pipeline behind
``HERMES_QUANT_PROFILE_SCAN``. These tests RED-prove the load-bearing
invariants of the W3 workstream:

  * asof THREADS to the snapshot (no ``datetime.now`` leakage — every feature
    is as-of-honest against the universe artifact's own ``asof``).
  * ``--no-fetch`` builds TickerProfiles from the universe artifact's own
    fields alone (zero network, the genuinely-standalone aegis path).
  * the output is ONE ranked list, NOT the legacy ``{play: [...]}`` 5-bucket
    fan-out.
  * a missing universe artifact -> empty result (silence-by-default).
  * the new emit path is ``profile-fit.json`` and NEVER clobbers
    ``play-fit.json``.
  * deterministic NaN-safe ranking + one global cap.
  * default-OFF byte-identical: the flag-gated cron integration is inert when
    ``HERMES_QUANT_PROFILE_SCAN`` is unset; the standalone library/CLI entry is
    always runnable (running a tool by hand is the operator's choice).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hermes_quant.playbook import profile_scan

_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-profile-watchlist.py"
)


def _load_cli():
    """Import the standalone CLI shim as a module (it has a hyphenated name)."""
    spec = importlib.util.spec_from_file_location("quant_profile_watchlist_x", _CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _write_universe(path: Path, asof: str, symbols: list[dict]) -> None:
    payload = {
        "asof": asof,
        "count": len(symbols),
        "filters": {"asset_class": "us_equity", "max_price": 500.0, "min_price": 5.0},
        "symbols": symbols,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


_ASOF = "2026-06-18T10:15:55+00:00"


def _sym(symbol: str, **over) -> dict:
    base = {
        "symbol": symbol,
        "avg_dollar_volume_30d": 50_000_000.0,
        "last_close": 100.0,
        "tradable": True,
        "shortable": True,
        "exchange": "NASDAQ",
        "fractionable": True,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# build_profile_watchlist — the importable core
# --------------------------------------------------------------------------- #


def test_missing_universe_returns_empty_silence(tmp_path: Path):
    """A missing universe artifact -> empty result, no raise (silence-by-default)."""
    missing = tmp_path / "does-not-exist.json"
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        missing, asof=_ASOF, fetch=False, out_path=out
    )
    assert result["active"] == []
    assert result["asof"] == _ASOF
    # No file is clobbered; silence means we do not pretend to have a watchlist.


def test_no_fetch_builds_from_artifact_alone(tmp_path: Path):
    """--no-fetch builds TickerProfiles from the universe artifact's OWN fields.

    Zero network: market_cap/realized_vol/spread/quote_type abstain (None ->
    soft-rule miss), but the artifact's ADV/last_close/tradable seed the
    liquidity/price/tradable rails directly. A liquid, in-band, tradable name
    must be eligible without any yfinance call.
    """
    uni = tmp_path / "universe.json"
    _write_universe(
        uni,
        _ASOF,
        [
            _sym("AAA", avg_dollar_volume_30d=50_000_000.0, last_close=100.0),
            _sym("BBB", avg_dollar_volume_30d=80_000_000.0, last_close=42.0),
        ],
    )
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, out_path=out
    )
    syms = {row["symbol"] for row in result["active"]}
    assert syms == {"AAA", "BBB"}, f"both liquid names should be eligible, got {syms}"
    # No yfinance: market_cap rail abstains, profile still admits on the
    # artifact-seeded hard rails.


def test_output_is_one_list_not_five_buckets(tmp_path: Path):
    """The emitted state is ONE ranked list, NOT the {play: [...]} 5-bucket fan-out."""
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA"), _sym("BBB")])
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, out_path=out
    )
    # The result carries a flat 'active' list keyed by symbol — not a dict of
    # per-play lists.
    assert isinstance(result["active"], list)
    assert "plays" not in result  # no 5-bucket key
    for row in result["active"]:
        # Each active row is a single profile-fit ticker; it does NOT name a play.
        assert "play" not in row
        assert "symbol" in row

    # On-disk file mirrors the in-memory result.
    persisted = json.loads(out.read_text())
    assert isinstance(persisted["active"], list)
    assert "plays" not in persisted


def test_emit_path_never_clobbers_play_fit(tmp_path: Path):
    """profile_scan emits to a NEW path; play-fit.json is untouched."""
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA")])

    play_fit = tmp_path / "play-fit.json"
    sentinel = {"as_of": "SENTINEL", "plays": {"swing": []}}
    play_fit.write_text(json.dumps(sentinel))

    out = tmp_path / "profile-fit.json"
    profile_scan.build_profile_watchlist(uni, asof=_ASOF, fetch=False, out_path=out)

    # The new path was written, the legacy path is byte-identical untouched.
    assert out.exists()
    assert json.loads(play_fit.read_text()) == sentinel


def test_global_cap_keeps_top_n_ranked(tmp_path: Path):
    """ONE global cap (max_watchlist) trims to top-N by fit_score desc, deterministic."""
    uni = tmp_path / "universe.json"
    # Vary ADV so fit scores differ deterministically; all liquid + in-band.
    symbols = [
        _sym(f"S{i:02d}", avg_dollar_volume_30d=10_000_000.0 + i * 1_000_000.0)
        for i in range(10)
    ]
    _write_universe(uni, _ASOF, symbols)
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, max_watchlist=3, out_path=out
    )
    assert len(result["active"]) == 3, "global cap must trim to exactly max_watchlist"
    scores = [row["fit_score"] for row in result["active"]]
    assert scores == sorted(scores, reverse=True), "ranked by fit_score desc"


def test_ranking_is_nan_safe_and_symbol_tiebroken(tmp_path: Path):
    """Equal fit_scores tie-break by symbol asc; no NaN explosion in the sort."""
    uni = tmp_path / "universe.json"
    # All identical fields -> identical fit_score -> tie-break must be symbol asc.
    _write_universe(uni, _ASOF, [_sym("ZZZ"), _sym("AAA"), _sym("MMM")])
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, out_path=out
    )
    ordered = [row["symbol"] for row in result["active"]]
    assert ordered == ["AAA", "MMM", "ZZZ"], f"symbol-asc tie-break, got {ordered}"


def test_asof_threads_to_snapshot_no_datetime_now_leak(tmp_path: Path, monkeypatch):
    """The universe artifact's asof THREADS to compute_play_snapshot (no now() leak).

    With fetch=True, every snapshot must be built with the artifact's asof, not
    wall-clock now() — otherwise a backtest/replay leaks the future.
    """
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA"), _sym("BBB")])
    out = tmp_path / "profile-fit.json"

    seen_asofs: list = []

    def _fake_prewarm(symbols, asof=None, **kw):
        seen_asofs.append(asof)
        return {"prewarmed": len(symbols), "skipped": 0, "errors": 0, "elapsed_s": 0.0}

    def _fake_snapshot(symbol, asof=None):
        seen_asofs.append(asof)
        # Return a minimal equity-shaped snapshot.
        return {
            "symbol": symbol,
            "asof": asof.isoformat() if hasattr(asof, "isoformat") else str(asof),
            "quote_type": "EQUITY",
            "market_cap_usd": 5e9,
            "realized_vol_30d": 0.3,
        }

    monkeypatch.setattr(profile_scan, "prewarm_snapshot_cache", _fake_prewarm)
    monkeypatch.setattr(profile_scan, "compute_play_snapshot", _fake_snapshot)

    profile_scan.build_profile_watchlist(uni, asof=_ASOF, fetch=True, out_path=out)

    assert seen_asofs, "asof must be threaded to the snapshot/prewarm layer"
    # NONE of the threaded asofs may be None (None -> compute_play_snapshot
    # defaults to datetime.now(UTC), the lookahead leak this guards).
    for a in seen_asofs:
        assert a is not None, "asof leaked as None -> compute_play_snapshot uses now()"


def test_fetch_true_evicts_non_equity_via_quote_type(tmp_path: Path, monkeypatch):
    """With fetch=True, an enriched quote_type != EQUITY evicts the name.

    The fetch path overlays the yfinance quote_type onto the artifact-seeded
    snapshot; a non-equity (ETF/INDEX/...) must be evicted by the composite
    halt/penny/illiquid-trap (non_equity eviction).
    """
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("STOCK"), _sym("FUND")])
    out = tmp_path / "profile-fit.json"

    def _fake_prewarm(symbols, asof=None, **kw):
        return {"prewarmed": len(symbols), "skipped": 0, "errors": 0, "elapsed_s": 0.0}

    def _fake_snapshot(symbol, asof=None):
        qt = "ETF" if symbol == "FUND" else "EQUITY"
        return {
            "symbol": symbol,
            "asof": asof.isoformat() if hasattr(asof, "isoformat") else str(asof),
            "quote_type": qt,
            "market_cap_usd": 5e9,
            "realized_vol_30d": 0.3,
        }

    monkeypatch.setattr(profile_scan, "prewarm_snapshot_cache", _fake_prewarm)
    monkeypatch.setattr(profile_scan, "compute_play_snapshot", _fake_snapshot)

    result = profile_scan.build_profile_watchlist(uni, asof=_ASOF, fetch=True, out_path=out)
    syms = {row["symbol"] for row in result["active"]}
    assert "STOCK" in syms
    assert "FUND" not in syms, "non-equity quote_type must evict via the composite trap"


def test_active_rows_carry_horizon_set_and_fit_fields(tmp_path: Path):
    """Each active row carries the W2 horizon_set + fit_score + asof + flags."""
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA")])
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, out_path=out
    )
    assert result["active"], "AAA should be eligible"
    row = result["active"][0]
    assert row["symbol"] == "AAA"
    assert row["asof"] == _ASOF
    assert "fit_score" in row and isinstance(row["fit_score"], (int, float))
    assert "horizon_set" in row and isinstance(row["horizon_set"], list)
    # The default horizon set (flag-OFF) is 1D-30D (no 0D).
    assert "0D" not in row["horizon_set"]
    assert "30D" in row["horizon_set"]
    assert row["shortable"] is True


def test_pit_filter_runs_first_no_lookahead(tmp_path: Path):
    """filter_listed_at_asof runs FIRST; a not-yet-listed name is excluded.

    With a listing table + PIT forced on, a symbol that listed AFTER asof is
    dropped before scoring (survivorship/lookahead safety).
    """
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("OLD"), _sym("NEW")])
    out = tmp_path / "profile-fit.json"
    listing = {
        "OLD": {"listed_at": "2000-01-01"},
        "NEW": {"listed_at": "2099-01-01"},  # lists in the future -> excluded
    }
    result = profile_scan.build_profile_watchlist(
        uni,
        asof=_ASOF,
        fetch=False,
        out_path=out,
        listing_table=listing,
        force_pit=True,
    )
    syms = {row["symbol"] for row in result["active"]}
    assert syms == {"OLD"}, f"NEW listed in the future, must be PIT-excluded; got {syms}"


def test_penny_and_illiquid_traps_evict(tmp_path: Path):
    """The composite halt/penny/illiquid-trap evicts sub-floor names."""
    uni = tmp_path / "universe.json"
    _write_universe(
        uni,
        _ASOF,
        [
            _sym("GOOD", avg_dollar_volume_30d=50_000_000.0, last_close=100.0),
            _sym("PENNY", last_close=2.0),  # < 5.0 floor
            _sym("THIN", avg_dollar_volume_30d=500_000.0),  # < 2e6 ADV floor
            _sym("NOTRADE", tradable=False),  # fail-closed not-tradable
        ],
    )
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, out_path=out
    )
    syms = {row["symbol"] for row in result["active"]}
    assert "GOOD" in syms
    assert "PENNY" not in syms
    assert "THIN" not in syms
    assert "NOTRADE" not in syms


# --------------------------------------------------------------------------- #
# Default-OFF byte-identical guarantee
# --------------------------------------------------------------------------- #


def test_flag_constant_is_quoted_literal_default_off():
    """HERMES_QUANT_PROFILE_SCAN is a _FLAG constant with a quoted '0' default.

    The flag-inventory scanner only matches quoted-literal defaults; a computed
    default silently drops the flag from the inventory. The check is == '1'
    (fail-closed: any non-'1' value, including a typo, leaves the path OFF).
    """
    assert profile_scan._FLAG == "HERMES_QUANT_PROFILE_SCAN"
    # The scanner regex needs a literal "0" default in the source.
    src = Path(profile_scan.__file__).read_text()
    assert '"0"' in src or "'0'" in src


def test_profile_scan_enabled_reads_flag(monkeypatch):
    """_profile_scan_enabled() is fail-closed: only '1' enables it."""
    monkeypatch.delenv(profile_scan._FLAG, raising=False)
    assert profile_scan._profile_scan_enabled() is False
    monkeypatch.setenv(profile_scan._FLAG, "0")
    assert profile_scan._profile_scan_enabled() is False
    monkeypatch.setenv(profile_scan._FLAG, "true")  # typo / non-'1' -> OFF
    assert profile_scan._profile_scan_enabled() is False
    monkeypatch.setenv(profile_scan._FLAG, "1")
    assert profile_scan._profile_scan_enabled() is True


def test_library_entry_is_runnable_regardless_of_flag(tmp_path: Path, monkeypatch):
    """The standalone library entry runs even with the flag OFF (operator's choice).

    The flag gates the CRON/autonomous integration, not the hand-run tool. So
    build_profile_watchlist works whether or not HERMES_QUANT_PROFILE_SCAN is set.
    """
    monkeypatch.delenv(profile_scan._FLAG, raising=False)
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA")])
    out = tmp_path / "profile-fit.json"
    result = profile_scan.build_profile_watchlist(
        uni, asof=_ASOF, fetch=False, out_path=out
    )
    assert result["active"], "the standalone tool runs by hand regardless of flag"


def test_refuses_to_write_play_fit_path(tmp_path: Path):
    """The scanner hard-refuses to write play-fit.json (anti-clobber rail)."""
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA")])
    play_fit = tmp_path / "play-fit.json"
    with pytest.raises(ValueError, match="play-fit.json"):
        profile_scan.build_profile_watchlist(
            uni, asof=_ASOF, fetch=False, out_path=play_fit
        )


# --------------------------------------------------------------------------- #
# Standalone CLI — runs WITHOUT the cron stack
# --------------------------------------------------------------------------- #


def test_cli_no_fetch_writes_one_watchlist(tmp_path: Path, capsys):
    """The CLI builds + writes ONE profile-fit watchlist in --no-fetch mode."""
    cli = _load_cli()
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym("AAA"), _sym("BBB")])
    out = tmp_path / "profile-fit.json"

    rc = cli.main(
        ["--universe", str(uni), "--out", str(out), "--no-fetch", "--json"]
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload["active"], list)
    assert {r["symbol"] for r in payload["active"]} == {"AAA", "BBB"}
    # Persisted to the requested NEW path, not play-fit.json.
    assert out.exists()
    assert "plays" not in json.loads(out.read_text())


def test_cli_missing_universe_is_silent_exit_zero(tmp_path: Path, capsys):
    """A missing universe -> exit 0, no crash, no file (silence-by-default)."""
    cli = _load_cli()
    out = tmp_path / "profile-fit.json"
    rc = cli.main(
        ["--universe", str(tmp_path / "nope.json"), "--out", str(out), "--no-fetch"]
    )
    assert rc == 0
    assert not out.exists()  # nothing emitted for an empty universe


def test_cli_max_cap_threads_through(tmp_path: Path, capsys):
    """--max threads to the global cap."""
    cli = _load_cli()
    uni = tmp_path / "universe.json"
    _write_universe(uni, _ASOF, [_sym(f"S{i:02d}") for i in range(8)])
    out = tmp_path / "profile-fit.json"
    rc = cli.main(
        ["--universe", str(uni), "--out", str(out), "--no-fetch", "--max", "2", "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["active"]) == 2
    assert payload["max_watchlist"] == 2
