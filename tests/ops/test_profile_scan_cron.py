"""tests/ops/test_profile_scan_cron.py — W6 profile-fit scanner cron seam.

The watchlist-evolve cron (``ops/scripts/quant-watchlist-evolve.py``) gains a
``HERMES_QUANT_PROFILE_SCAN``-gated branch. Behind that flag:

  * OFF (the default) — the cron runs the EXISTING 5-bucket ``evolve_watchlist``
    path verbatim over ``play-fit.json``. ``build_profile_watchlist`` (W3) is
    NEVER imported or called, and NO ``profile-fit.json`` is written. This is
    the byte-identical-OFF guarantee: with the flag unset, ``main()`` reaches
    ``evolve_watchlist`` exactly as today.
  * ON — the cron calls ``build_profile_watchlist`` (the W3 importable core)
    with the universe path + the universe artifact's asof, which emits a SINGLE
    ``profile-fit.json`` (the autonomous-consumed watchlist). The 5-bucket
    ``evolve_watchlist`` is NOT run.

These tests are hermetic: ``build_profile_watchlist`` and ``evolve_watchlist``
are monkeypatched so no real ~/.hermes write, yfinance fetch, or W3 import
happens. The flag-inventory drift gate (separate file) enforces that the 3 new
flags appear once their defining modules (profile_scan.py / autonomous.py /
horizons.py) merge into the tree.

POSTURE: the new branch is ADD-ONLY behind a default-OFF flag. The cron's
existing prewarm / stale-universe / catalyst-onboard / budget-guard machinery
is untouched on the OFF path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_cron_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-watchlist-evolve.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_watchlist_evolve", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # neutralize the script's execv guard
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod


@pytest.fixture(scope="module")
def cron():
    return _load_cron_module()


def _write_universe(tmp_path: Path, asof: str = "2026-06-18T10:15:55+00:00") -> Path:
    """A minimal asof-stamped universe artifact in the cron's expected shape."""
    p = tmp_path / "alpaca-daily.json"
    p.write_text(
        json.dumps(
            {
                "asof": asof,
                "count": 2,
                "symbols": [
                    {"symbol": "AAPL", "avg_dollar_volume_30d": 1e10,
                     "last_close": 200.0, "tradable": True, "shortable": True},
                    {"symbol": "MSFT", "avg_dollar_volume_30d": 9e9,
                     "last_close": 410.0, "tradable": True, "shortable": True},
                ],
            }
        )
    )
    return p


def _neuter_io(cron, monkeypatch, tmp_path):
    """Make main() hermetic: no prewarm, no stale-universe abort, no real
    evolve write. Points the universe at a fresh tmp artifact via a home
    redirect so the cron's ``Path.home() / .hermes / ...`` resolves into
    tmp_path."""
    universe = _write_universe(tmp_path)
    monkeypatch.setattr(cron.Path, "home", classmethod(lambda cls: tmp_path), raising=False)
    dest = tmp_path / ".hermes" / "quant" / "universe"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "alpaca-daily.json").write_text(universe.read_text())
    # Disable prewarm so the OFF path is purely evolve_watchlist.
    monkeypatch.setattr(cron, "prewarm_snapshot_cache", None, raising=False)
    return tmp_path / ".hermes" / "quant" / "universe" / "alpaca-daily.json"


# ---------------------------------------------------------------------------
# Flag-reading seam: the cron reads HERMES_QUANT_PROFILE_SCAN, default-OFF.
# ---------------------------------------------------------------------------


def test_profile_scan_flag_constant(cron):
    """The cron binds the flag to the canonical name so the seam wires to W3."""
    assert cron._PROFILE_SCAN_FLAG == "HERMES_QUANT_PROFILE_SCAN"


def test_profile_scan_disabled_by_default(cron, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_PROFILE_SCAN", raising=False)
    assert cron._profile_scan_enabled() is False


def test_profile_scan_enabled_only_on_literal_one(cron, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_PROFILE_SCAN", "1")
    assert cron._profile_scan_enabled() is True
    # Fail-closed: any value other than "1" is OFF.
    for v in ("0", "true", "", "yes", "2"):
        monkeypatch.setenv("HERMES_QUANT_PROFILE_SCAN", v)
        assert cron._profile_scan_enabled() is False


# ---------------------------------------------------------------------------
# aegis-ra-home2 (ADR-0092 home-decouple residue): the profile-fit watchlist
# output path must honor HERMES_QUANT_HOME / HERMES_HOME via hermes_quant.home,
# not a raw Path.home()/".hermes"/"quant" literal. RED-proof: pre-fix the helper
# returned Path.home() / ".hermes" / "quant" / "watchlist" / "profile-fit.json"
# which ignored both env overrides (it honored only a Path.home monkeypatch).
# ---------------------------------------------------------------------------


def test_profile_watchlist_path_honors_hermes_quant_home(cron, monkeypatch, tmp_path):
    """An injected HERMES_QUANT_HOME redirects the resolved profile-fit.json
    target into the injected quant root, NOT ~/.hermes."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    inj = tmp_path / "injected_quant_root"
    monkeypatch.setenv("HERMES_QUANT_HOME", str(inj))

    resolved = cron._resolve_profile_watchlist_path()
    assert resolved == inj / "watchlist" / "profile-fit.json"
    assert (Path.home() / ".hermes") not in resolved.parents


def test_profile_watchlist_path_honors_hermes_home(cron, monkeypatch, tmp_path):
    """HERMES_HOME points at the hermes home; the quant root (and the watchlist
    output) is <HERMES_HOME>/quant/watchlist/profile-fit.json."""
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    hhome = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hhome))

    resolved = cron._resolve_profile_watchlist_path()
    assert resolved == hhome / "quant" / "watchlist" / "profile-fit.json"


def test_profile_watchlist_path_byte_identical_without_env(cron, monkeypatch):
    """Parity: no env -> EXACTLY the legacy ~/.hermes/quant/watchlist literal."""
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert cron._resolve_profile_watchlist_path() == (
        Path.home() / ".hermes" / "quant" / "watchlist" / "profile-fit.json"
    )


# ---------------------------------------------------------------------------
# OFF (default) — byte-identical: evolve_watchlist runs; build_profile_watchlist
# is never imported/called; no profile-fit.json is written.
# ---------------------------------------------------------------------------


def test_off_runs_evolve_and_never_calls_profile_scan(cron, monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_QUANT_PROFILE_SCAN", raising=False)
    _neuter_io(cron, monkeypatch, tmp_path)

    called = {"evolve": 0, "profile": 0}

    def _fake_evolve(**kw):
        called["evolve"] += 1
        return {"events_written": 0, "as_of": "2026-06-18", "per_play": {}}

    def _boom_profile(*a, **k):
        called["profile"] += 1
        raise AssertionError("OFF path must NOT call build_profile_watchlist")

    monkeypatch.setattr(cron, "evolve_watchlist", _fake_evolve)
    monkeypatch.setattr(cron, "_run_profile_scan", _boom_profile)
    monkeypatch.setattr(sys, "argv", ["quant-watchlist-evolve.py"])

    rc = cron.main()
    assert rc == 0
    assert called["evolve"] == 1  # the existing 5-bucket path ran
    assert called["profile"] == 0  # the new path was never entered


# ---------------------------------------------------------------------------
# ON — build_profile_watchlist is called with the universe path + asof; the
# profile-fit.json is emitted; the 5-bucket evolve is NOT run.
# ---------------------------------------------------------------------------


def test_on_calls_build_profile_watchlist_and_skips_evolve(cron, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_QUANT_PROFILE_SCAN", "1")
    universe_path = _neuter_io(cron, monkeypatch, tmp_path)

    captured = {}
    out = tmp_path / ".hermes" / "quant" / "watchlist" / "profile-fit.json"

    def _fake_build(universe_path, asof, *, fetch=True, max_watchlist=50,
                    listing_table=None, out_path=None, force_pit=None):
        captured["universe_path"] = Path(universe_path)
        captured["asof"] = asof
        captured["fetch"] = fetch
        captured["out_path"] = out_path
        # Mirror profile_scan.build_profile_watchlist's REAL emitted contract:
        # {"asof", "active":[row,...], "max_watchlist", "n_scanned", "n_eligible"}.
        # There is NO "n_active" / "out_path" key — the cron must derive the
        # active count from len(active) and surface the path it requested.
        target = Path(out_path) if out_path is not None else out
        target.parent.mkdir(parents=True, exist_ok=True)
        result = {
            "asof": asof,
            "active": [
                {"symbol": "AAPL", "asset_class": "us_equity", "options_eligible": True,
                 "shortable": True, "horizon_set": ["1D", "7D", "14D", "30D"],
                 "fit_score": 0.91, "asof": asof},
                {"symbol": "MSFT", "asset_class": "us_equity", "options_eligible": True,
                 "shortable": True, "horizon_set": ["1D", "7D", "14D", "30D"],
                 "fit_score": 0.88, "asof": asof},
            ],
            "max_watchlist": max_watchlist,
            "n_scanned": 2,
            "n_eligible": 2,
        }
        target.write_text(json.dumps(result))
        return result

    def _boom_evolve(**kw):
        raise AssertionError("ON path must NOT run the 5-bucket evolve_watchlist")

    # build_profile_watchlist is imported lazily inside _run_profile_scan; patch
    # the module attribute the cron exposes for injection.
    monkeypatch.setattr(cron, "_build_profile_watchlist", _fake_build, raising=False)
    monkeypatch.setattr(cron, "evolve_watchlist", _boom_evolve)
    monkeypatch.setattr(sys, "argv", ["quant-watchlist-evolve.py"])

    rc = cron.main()
    assert rc == 0
    assert captured["universe_path"] == universe_path
    # asof is threaded from the universe artifact (no datetime.now leakage).
    assert captured["asof"] == "2026-06-18T10:15:55+00:00"
    # The cron passes the resolved target path it requested (not a guess).
    assert captured["out_path"] == out
    # profile-fit.json was emitted (the autonomous-consumed single watchlist).
    pf = tmp_path / ".hermes" / "quant" / "watchlist" / "profile-fit.json"
    assert pf.exists()
    parsed = json.loads(pf.read_text())
    assert parsed["active"][0]["symbol"] == "AAPL"
    assert "horizon_set" in parsed["active"][0]
    # Crucially: the new path NEVER touches play-fit.json's evolve.
    assert not (tmp_path / ".hermes" / "quant" / "watchlist" / "play-fit.json").exists()

    # Breadcrumb truth: against the REAL contract the cron derives the active
    # count from len(active) (2 here) and prints the path it actually requested.
    # The pre-fix cron read summary["n_active"] (absent -> 0) so it printed
    # nothing; this asserts the breadcrumb is now both EMITTED and CORRECT.
    captured_out = capsys.readouterr().out
    assert "profile-fit scan" in captured_out
    assert "2 active" in captured_out
    assert str(out) in captured_out


def test_on_does_not_clobber_play_fit_json(cron, monkeypatch, tmp_path, capsys):
    """The new profile-fit.json path is a NEW file; play-fit.json (5-bucket) is
    left exactly as it was — the OFF default still owns play-fit.json. An EMPTY
    active list stays silent-by-default (no breadcrumb)."""
    monkeypatch.setenv("HERMES_QUANT_PROFILE_SCAN", "1")
    _neuter_io(cron, monkeypatch, tmp_path)
    wl = tmp_path / ".hermes" / "quant" / "watchlist"
    wl.mkdir(parents=True, exist_ok=True)
    play_fit = wl / "play-fit.json"
    play_fit.write_text('{"sentinel": "untouched 5-bucket state"}')

    def _fake_build(universe_path, asof, **k):
        # REAL contract, empty watchlist (silence-by-default): NO "n_active",
        # NO "out_path" — just the asof + an empty active list + counts.
        (wl / "profile-fit.json").write_text('{"asof": "x", "active": []}')
        return {
            "asof": asof,
            "active": [],
            "max_watchlist": 50,
            "n_scanned": 0,
            "n_eligible": 0,
        }

    monkeypatch.setattr(cron, "_build_profile_watchlist", _fake_build, raising=False)
    monkeypatch.setattr(cron, "evolve_watchlist",
                        lambda **k: pytest.fail("evolve must not run when ON"))
    monkeypatch.setattr(sys, "argv", ["quant-watchlist-evolve.py"])

    cron.main()
    # play-fit.json is byte-identical to what we wrote (untouched).
    assert json.loads(play_fit.read_text()) == {"sentinel": "untouched 5-bucket state"}
    # Empty active list -> silence-by-default: NO breadcrumb on stdout.
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Stale-universe / silence posture is preserved on the ON path too.
# ---------------------------------------------------------------------------


def test_on_missing_universe_is_silent(cron, monkeypatch, tmp_path):
    """No universe artifact → the cron is silent and runs nothing (silence-by-
    default), regardless of the flag."""
    monkeypatch.setenv("HERMES_QUANT_PROFILE_SCAN", "1")
    monkeypatch.setattr(cron.Path, "home", classmethod(lambda cls: tmp_path), raising=False)
    monkeypatch.setattr(cron, "prewarm_snapshot_cache", None, raising=False)

    def _boom_build(*a, **k):
        raise AssertionError("missing universe must not call build_profile_watchlist")

    monkeypatch.setattr(cron, "_build_profile_watchlist", _boom_build, raising=False)
    monkeypatch.setattr(cron, "evolve_watchlist",
                        lambda **k: {"events_written": 0, "as_of": "x", "per_play": {}})
    monkeypatch.setattr(sys, "argv", ["quant-watchlist-evolve.py"])
    rc = cron.main()
    # Silent exit; no crash. (No universe file → 5-bucket evolve sees nothing.)
    assert rc == 0


# ---------------------------------------------------------------------------
# Flag-inventory composition (W6 regen requirement).
#
# The flag-inventory scanner scans hermes_quant/ for quoted-literal-default
# flag reads. The 3 new flags are CONSTANTS in the W3/W5 modules
# (profile_scan.py / autonomous.py / horizons.py); in this isolated worktree
# those modules are not present, so a regen here is a no-op (no drift). What
# W6 must guarantee is that the regen MECHANISM captures the 3 flags once their
# defining modules join the tree. We prove that hermetically by pointing the
# scanner at a temp hermes_quant/ that contains minimal flag-reading stubs in
# exactly the W3/W5 idiom (a `_FLAG = "HERMES_QUANT_..."` constant read with a
# quoted-literal "0" default), and asserting all 3 are captured. The stubs are
# throwaway test fixtures — NOT shipped source (no duplication of W3/W5).
# ---------------------------------------------------------------------------


def _load_inventory_scanner():
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-flag-inventory.py"
    spec = importlib.util.spec_from_file_location("quant_flag_inventory_w6", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inventory_scanner_captures_three_new_flags_from_w3_w5_idiom(monkeypatch, tmp_path):
    """When the W3/W5 modules are present, the scanner picks up all 3 flags
    written in the canonical `_FLAG = "HERMES_QUANT_..."` + `environ.get(_FLAG,
    "0")` idiom. This is the regen contract W6 must compose with."""
    inv = _load_inventory_scanner()
    fake_src = tmp_path / "hermes_quant" / "playbook"
    fake_src.mkdir(parents=True)
    # profile_scan.py → HERMES_QUANT_PROFILE_SCAN (W3)
    (fake_src / "profile_scan.py").write_text(
        'import os\n'
        '_PROFILE_SCAN_FLAG = "HERMES_QUANT_PROFILE_SCAN"\n'
        'def _on():\n'
        '    return os.environ.get(_PROFILE_SCAN_FLAG, "0") == "1"\n'
    )
    # horizons.py → HERMES_QUANT_ZERO_DTE (W5)
    (fake_src / "horizons.py").write_text(
        'import os\n'
        '_ZERO_DTE_FLAG = "HERMES_QUANT_ZERO_DTE"\n'
        'def _zero_dte_on():\n'
        '    return os.environ.get(_ZERO_DTE_FLAG, "0") == "1"\n'
    )
    # autonomous.py → HERMES_QUANT_MULTI_HORIZON_TICK (W5)
    (tmp_path / "hermes_quant" / "autonomous.py").write_text(
        'import os\n'
        '_MULTI_HORIZON_TICK_FLAG = "HERMES_QUANT_MULTI_HORIZON_TICK"\n'
        'def _mh_on():\n'
        '    return os.environ.get(_MULTI_HORIZON_TICK_FLAG, "0") == "1"\n'
    )

    # scan() does f.relative_to(REPO) for the source loc; repoint both so the
    # temp tree resolves cleanly.
    monkeypatch.setattr(inv, "REPO", tmp_path)
    monkeypatch.setattr(inv, "SRC", tmp_path / "hermes_quant")
    flags = inv.scan()
    for f in (
        "HERMES_QUANT_PROFILE_SCAN",
        "HERMES_QUANT_MULTI_HORIZON_TICK",
        "HERMES_QUANT_ZERO_DTE",
    ):
        assert f in flags, f"scanner missed {f} from the canonical W3/W5 idiom"
        # All three are default-OFF rails with the quoted-literal "0" default.
        assert flags[f][0] == "0", f"{f} default must scan as '0' (default-OFF)"


def test_inventory_not_stale_after_w6_edits(monkeypatch):
    """My W6 cron edits live in ops/scripts/ (outside the hermes_quant/ scan),
    so the committed FLAG-INVENTORY.md stays current — the W6 change introduces
    NO flag drift of its own. (The 3 flags are added at merge once W3/W5's
    modules join the tree and `--write` is re-run.)"""
    inv = _load_inventory_scanner()
    flags = inv.scan()
    expected = inv.render(flags)
    committed = inv.DOC.read_text() if inv.DOC.exists() else ""
    assert committed == expected, (
        "FLAG-INVENTORY.md drifted after W6 edits — regenerate: "
        "python ops/scripts/quant-flag-inventory.py --write"
    )
