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


# --------------------------------------------------------------------------- #
# d83b — the ADR-0097 slippage-haircut rail must be tracked in _RAIL_FLAGS so
# the run-card's drift detection covers a mid-window disarm of the haircut.
# --------------------------------------------------------------------------- #
def test_slippage_haircut_tracked_in_rail_flags():
    """RED before d83b: _RAIL_FLAGS omitted SLIPPAGE_HAIRCUT, so a run-card review
    could not detect drift/disarm of the haircut rail while the snapshot still
    called the record honest/forward-only."""
    assert "HERMES_QUANT_SLIPPAGE_HAIRCUT" in H._RAIL_FLAGS, (
        "the haircut rail must be tracked so its drift/disarm is detectable"
    )


def test_run_card_detects_haircut_rail_drift(tmp_path, monkeypatch):
    """A haircut rail armed at t0 but DISARMED live -> rail_drift names it.
    RED before d83b: SLIPPAGE_HAIRCUT was not in _RAIL_FLAGS, so the drift dict
    (built only over _RAIL_FLAGS) never reported a haircut disarm."""
    home = tmp_path / "quant"
    _write_anchor(home, {"HERMES_QUANT_SLIPPAGE_HAIRCUT": "1"})
    monkeypatch.delenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", raising=False)  # disarmed live!
    card = H.write_run_card(tmp_path / "run", home=home)
    assert "rail_drift" in card
    assert "HERMES_QUANT_SLIPPAGE_HAIRCUT" in card["rail_drift"]
    assert card["rail_drift"]["HERMES_QUANT_SLIPPAGE_HAIRCUT"] == {"window": "1", "live": None}


# --------------------------------------------------------------------------- #
# 821d — the ag01 PORTFOLIO_VARIANCE_SIZING rail must be tracked in _RAIL_FLAGS
# so the run-card's window-vs-live drift detection covers a mid-window disarm of
# it (mirrors the d83b SLIPPAGE_HAIRCUT precedent). ag01 is not yet a required
# armed rail, but its drift must still be VISIBLE on a run-card.
# --------------------------------------------------------------------------- #
def test_variance_sizing_tracked_in_rail_flags():
    """RED before 821d: _RAIL_FLAGS omitted PORTFOLIO_VARIANCE_SIZING, so a
    run-card review could not detect drift/disarm of the ag01 rail."""
    assert "HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING" in H._RAIL_FLAGS, (
        "the ag01 variance-sizing rail must be tracked so its drift/disarm is detectable"
    )


def test_run_card_detects_variance_sizing_rail_drift(tmp_path, monkeypatch):
    """An ag01 rail armed at t0 but DISARMED live -> rail_drift names it.
    RED before 821d: PORTFOLIO_VARIANCE_SIZING was not in _RAIL_FLAGS, so the
    drift dict (built only over _RAIL_FLAGS) never reported its disarm."""
    home = tmp_path / "quant"
    _write_anchor(home, {"HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING": "1"})
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", raising=False)  # disarmed live!
    card = H.write_run_card(tmp_path / "run", home=home)
    assert "rail_drift" in card
    assert "HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING" in card["rail_drift"]
    assert card["rail_drift"]["HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING"] == {"window": "1", "live": None}


def test_gate0_start_lists_variance_sizing():
    """The gate0-start run-card snapshot must list the ag01 rail too (recommended,
    not required — ag01 is not yet an armed rail). RED before 821d: it was absent
    from both _RECOMMENDED and _SNAPSHOT_FLAGS, so a GATE-0 run-card never recorded
    the ag01 flag state at t0 and downstream drift could not be anchored."""
    spec = importlib.util.spec_from_file_location(
        "aegis_gate0_start",
        Path(__file__).resolve().parents[2] / "ops" / "scripts" / "aegis-gate0-start.py",
    )
    g0 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g0)
    assert "HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING" in g0._SNAPSHOT_FLAGS, (
        "gate0-start must snapshot the ag01 variance-sizing flag at t0"
    )
    # Recommended, not required (ag01 is not yet an armed rail).
    assert "HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING" not in g0._REQUIRED_ARMED, (
        "ag01 must NOT be a required-armed rail yet (no runtime/money-gate change)"
    )


# --------------------------------------------------------------------------- #
# dbcd — AG-OPT-EV-1: the snapshot must SURFACE the ADR-0029 options-evidence gate
# (N_options>=30 over >=30 calendar days). READ-ONLY/additive; changes no live gate.
# --------------------------------------------------------------------------- #
def _seed_option_bus(bus: Path, n: int, *, t0_day: int = 4):
    """n settled us_option round-trips, one entry+exit pair per day after 2026-06-{t0_day}.

    Each pair opens day D and closes day D+1 at a WIN (premium decayed: 2.0 -> 1.0 on a
    short, realized_return > 0). Distinct assets so FIFO does not cross-match.
    """
    lines = []
    for i in range(n):
        day = t0_day + i
        entry = {
            "proposal_id": f"e{i}", "asset": f"OPT{i}", "asset_class": "us_option",
            "reactor_name": "paper", "account_id": "paper-default",
            "fill_size_pct": -0.05, "fill_price": 2.0,  # short premium (credit)
            "asof_execution": f"2026-06-{day:02d}T15:35:36Z", "signal_id": "s",
        }
        exit_ = dict(entry)
        exit_.update({
            "proposal_id": f"x{i}", "fill_size_pct": 0.05, "fill_price": 1.0,
            "asof_execution": f"2026-06-{day + 1:02d}T15:35:36Z",
        })
        lines.append(json.dumps(entry))
        lines.append(json.dumps(exit_))
    bus.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_gate0_anchor(home: Path, t0: str = "2026-06-01T00:00:00+00:00"):
    home.mkdir(parents=True, exist_ok=True)
    (home / "clean_window_start.json").write_text(
        json.dumps({"t0": t0, "armed_flags": {}}), encoding="utf-8"
    )


def test_snapshot_surfaces_options_evidence_red_below_30(tmp_path):
    """N_options < 30 => the snapshot reports the AG-OPT-EV-1 gate as RED, not-yet-evidenced.

    RED before dbcd: compute_snapshot did not emit an ``options_evidence`` section at all
    (the gate was process prose, not code), so KeyError on snap['options_evidence']."""
    home = tmp_path / "quant"
    home.mkdir()
    bus = home / "executions.jsonl"
    _seed_gate0_anchor(home)
    _seed_option_bus(bus, 5)
    snap = H.compute_snapshot(home, bus)
    oe = snap["options_evidence"]
    assert oe["n_options"] == 5
    assert oe["n_threshold_met"] is False
    assert oe["verdict"] == "RED"


def test_snapshot_options_evidence_excludes_equity(tmp_path):
    """The equity round-trip (the original _seed_bus) is NOT counted as an options outcome."""
    home = tmp_path / "quant"
    home.mkdir()
    bus = home / "executions.jsonl"
    _seed_gate0_anchor(home)
    _seed_bus(bus)  # one EQUITY round-trip
    snap = H.compute_snapshot(home, bus)
    assert snap["n_settled_roundtrips"] == 1  # the equity trip still counts overall
    assert snap["options_evidence"]["n_options"] == 0  # but NOT as options evidence
    assert snap["options_evidence"]["verdict"] == "RED"


def test_snapshot_options_evidence_green_at_30_over_30_days(tmp_path):
    """>=30 options outcomes spanning >=30 calendar days => GREEN.

    RED before dbcd: no options_evidence section existed."""
    home = tmp_path / "quant"
    home.mkdir()
    bus = home / "executions.jsonl"
    _seed_gate0_anchor(home)
    # 31 daily option round-trips closing 2026-06-05 .. 2026-07-05 => >=30d span, N=31.
    lines = []
    for i in range(31):
        # close dates spread one per day from 2026-06-05 onward
        from datetime import datetime as _dt, timedelta as _td
        close = _dt(2026, 6, 5, 15, 35, 36) + _td(days=i)
        openn = close - _td(days=1)
        entry = {
            "proposal_id": f"e{i}", "asset": f"OPT{i}", "asset_class": "us_option",
            "reactor_name": "paper", "account_id": "paper-default",
            "fill_size_pct": -0.05, "fill_price": 2.0,
            "asof_execution": openn.strftime("%Y-%m-%dT%H:%M:%SZ"), "signal_id": "s",
        }
        exit_ = dict(entry)
        exit_.update({
            "proposal_id": f"x{i}", "fill_size_pct": 0.05, "fill_price": 1.0,
            "asof_execution": close.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        lines.append(json.dumps(entry))
        lines.append(json.dumps(exit_))
    bus.write_text("\n".join(lines) + "\n", encoding="utf-8")
    snap = H.compute_snapshot(home, bus)
    oe = snap["options_evidence"]
    assert oe["n_options"] == 31, oe
    assert oe["calendar_days"] >= 30.0, oe
    assert oe["verdict"] == "GREEN", oe


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
