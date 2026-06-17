"""Tests for aegis-run-snapshot — the daily perf snapshot must be read-only + honest."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "aegis-run-snapshot.py"


def _load():
    spec = importlib.util.spec_from_file_location("aegis_run_snapshot", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aegis_run_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load()


def _seed_bus(bus: Path):
    """One ASTS long opened then closed at a loss -> 1 settled round-trip, realized < 0."""
    entry = {
        "proposal_id": "e1", "asset": "ASTS", "asset_class": "equity",
        "reactor_name": "paper", "account_id": "paper-default",
        "fill_size_pct": 0.20, "fill_price": 100.0,
        "asof_execution": "2026-06-04T15:35:36Z", "signal_id": "s",
    }
    exit_ = dict(entry)
    exit_.update({"proposal_id": "x1", "fill_size_pct": -0.20, "fill_price": 90.0,
                  "asof_execution": "2026-06-05T15:35:36Z"})
    bus.write_text(json.dumps(entry) + "\n" + json.dumps(exit_) + "\n", encoding="utf-8")


def test_snapshot_reports_realized_loss_and_winrate(tmp_path):
    home = tmp_path / "quant"
    home.mkdir()
    bus = home / "executions.jsonl"
    _seed_bus(bus)
    snap = H.compute_snapshot(home, bus)
    assert snap["n_settled_roundtrips"] == 1
    assert snap["win_rate"] == 0.0  # the only trip lost
    assert snap["realized_pnl_frac_nav"] < 0  # honest: a loss is negative


def test_snapshot_is_read_only(tmp_path):
    home = tmp_path / "quant"
    home.mkdir()
    bus = home / "executions.jsonl"
    _seed_bus(bus)
    before = bus.read_bytes()
    H.compute_snapshot(home, bus)
    assert bus.read_bytes() == before, "snapshot must NOT mutate the bus"


def test_missing_bus_is_graceful(tmp_path):
    home = tmp_path / "quant"
    home.mkdir()
    snap = H.compute_snapshot(home, home / "nope.jsonl")
    assert "error" in snap  # graceful, no crash


def test_run_card_records_live_env_when_no_anchor(tmp_path, monkeypatch):
    """No GATE-0 anchor -> fall back to the live env, flagged as un-anchored."""
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.delenv("HERMES_QUANT_ACCOUNT_LOCK", raising=False)
    run_dir = tmp_path / "run"
    card = H.write_run_card(run_dir, home=tmp_path / "no_quant_here")
    assert card["live_env_flags"]["HERMES_QUANT_PER_POSITION_STOP"] == "1"
    assert card["live_env_flags"]["HERMES_QUANT_ACCOUNT_LOCK"] is None
    assert card["window_armed_flags"] is None  # no anchor
    assert "NO GATE-0 anchor" in card["armed_source"]
    assert (run_dir / "run-card.json").exists()


def _write_anchor(home, armed_flags):
    """Write a GATE-0 anchor the run-card reads (home is the quant dir)."""
    import json as _json

    home.mkdir(parents=True, exist_ok=True)
    (home / "clean_window_start.json").write_text(
        _json.dumps({"t0": "2026-06-17T22:49:07+00:00", "armed_flags": armed_flags}),
        encoding="utf-8",
    )


def test_run_card_uses_gate0_window_flags_not_process_env(tmp_path, monkeypatch):
    """The run-card's armed answer comes from the GATE-0 anchor (the window), NOT the
    snapshot process env — so a snapshot run OUTSIDE the armed wrapper still records the
    window as armed (the footgun the operator flagged)."""
    home = tmp_path / "quant"
    _write_anchor(home, {"HERMES_QUANT_PER_POSITION_STOP": "1", "HERMES_QUANT_ACCOUNT_LOCK": "1"})
    # Process env is BARE (simulates running the snapshot outside the wrapper).
    for f in H._RAIL_FLAGS:
        monkeypatch.delenv(f, raising=False)
    card = H.write_run_card(tmp_path / "run", home=home)
    assert card["armed_source"] == "gate0_anchor"
    assert card["window_armed_flags"]["HERMES_QUANT_PER_POSITION_STOP"] == "1"
    assert card["clean_window_t0"] == "2026-06-17T22:49:07+00:00"
    # The window shows armed even though the live env is bare -> the footgun is fixed.


def test_run_card_detects_rail_drift(tmp_path, monkeypatch):
    """A rail armed at t0 but DISARMED in the live env -> rail_drift warning (a real hazard)."""
    home = tmp_path / "quant"
    _write_anchor(home, {"HERMES_QUANT_PER_POSITION_STOP": "1"})
    monkeypatch.delenv("HERMES_QUANT_PER_POSITION_STOP", raising=False)  # disarmed live!
    card = H.write_run_card(tmp_path / "run", home=home)
    assert "rail_drift" in card
    assert "HERMES_QUANT_PER_POSITION_STOP" in card["rail_drift"]
    assert card["rail_drift"]["HERMES_QUANT_PER_POSITION_STOP"] == {"window": "1", "live": None}


def test_run_card_no_drift_when_env_matches_window(tmp_path, monkeypatch):
    home = tmp_path / "quant"
    _write_anchor(home, {"HERMES_QUANT_PER_POSITION_STOP": "1"})
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")  # matches window
    card = H.write_run_card(tmp_path / "run", home=home)
    assert "rail_drift" not in card


def test_main_appends_perf_line(tmp_path):
    home = tmp_path / "quant"
    home.mkdir()
    bus = home / "executions.jsonl"
    _seed_bus(bus)
    rc = H.main(["--run-id", "t", "--home", str(home), "--bus", str(bus)])
    assert rc == 0
    perf = home / "aegis-runs" / "t" / "perf.jsonl"
    lines = [json.loads(x) for x in perf.read_text().splitlines() if x.strip()]
    assert len(lines) == 1 and lines[0]["n_settled_roundtrips"] == 1
    # a second call appends, not overwrites
    H.main(["--run-id", "t", "--home", str(home), "--bus", str(bus)])
    assert len(perf.read_text().splitlines()) == 2
