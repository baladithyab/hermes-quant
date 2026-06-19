"""aegis-ageq2: the per-position stop sweep iterates the COMPOSITE (asset_class,
symbol) key and builds the WatchlistEntry / SymbolDecision from the tuple's
asset_class — NOT a hardcoded "equity".

For today's equity-only book the behavior must be byte-identical to the
symbol-keyed sweep (the equity entry still fires its stop). The composite key
just stops hardcoding "equity" so an options entry (added by agmon1) routes
correctly instead of being mislabeled equity.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.autonomous import TickResult, _run_per_position_stop_sweep


class _MarkFrame:
    def __init__(self, last_close):
        self.last_close = last_close


def test_sweep_accepts_composite_keyed_book_and_fires_equity(monkeypatch):
    """The sweep must accept a {(asset_class, symbol): frac} book and fire an
    equity stop, building the SymbolDecision with the asset_class FROM THE KEY."""
    import hermes_quant.autonomous as auto_mod
    import hermes_quant.perception as perc_mod
    import hermes_quant.risk.per_position_stop as pps_mod
    from hermes_quant.risk.per_position_stop import StopDecision

    monkeypatch.setattr(perc_mod, "build_perception_frame_live", lambda *a, **k: _MarkFrame(93.44))
    monkeypatch.setattr(auto_mod, "_establishing_avg_entry_price", lambda sym: 118.17)

    captured_entry = {}

    def fake_react(advisor_result, entry, fill_size_pct, **kwargs):
        captured_entry["asset_class"] = entry.asset_class
        captured_entry["symbol"] = entry.symbol
        return ("exec_stop", fill_size_pct)

    monkeypatch.setattr(auto_mod, "_react", fake_react)

    result = TickResult(asof="x", mode="autonomous", dry_run=False, watchlist_size=0)
    stopped = _run_per_position_stop_sweep(
        open_book={("equity", "ASTS"): 0.20},
        stop_pct=0.08,
        paper_zero_costs=False,
        result=result,
    )
    # The composite-key tuple ("equity", "ASTS") must be in `stopped` so the
    # watchlist-loop exemption + slot accounting work against the same key shape.
    assert ("equity", "ASTS") in stopped
    assert captured_entry["symbol"] == "ASTS"
    assert captured_entry["asset_class"] == "equity"  # FROM THE KEY, not hardcoded
    fired = [d for d in result.decisions if d.gate == "PER_POSITION_STOP_FIRED"]
    assert len(fired) == 1
    assert fired[0].asset_class == "equity"


def test_non_equity_entry_held_before_agmon1(monkeypatch):
    """Before agmon1 wires the options path, a us_option composite entry must be
    HELD (silence-by-default), never run through the equity stop primitive (which
    would mark an OCC symbol via build_perception_frame_live equity timeframe)."""
    import hermes_quant.autonomous as auto_mod
    import hermes_quant.perception as perc_mod

    react_calls = []
    monkeypatch.setattr(auto_mod, "_react", lambda *a, **k: react_calls.append(a) or ("x", 0.0))
    # If the sweep wrongly equity-marked the option, this would be consulted.
    monkeypatch.setattr(perc_mod, "build_perception_frame_live", lambda *a, **k: _MarkFrame(0.10))
    monkeypatch.setattr(auto_mod, "_establishing_avg_entry_price", lambda sym: 1.0)

    result = TickResult(asof="x", mode="autonomous", dry_run=False, watchlist_size=0)
    stopped = _run_per_position_stop_sweep(
        open_book={("us_option", "AAPL260116C00200000"): 0.05},
        stop_pct=0.08,
        paper_zero_costs=False,
        result=result,
    )
    assert react_calls == [], "a us_option entry must be HELD before agmon1 (no equity fire)"
    assert stopped == set()


# --------------------------------------------------------------------------- #
# byte-identical: the tick still builds an equity book that fires the same stop.
# --------------------------------------------------------------------------- #
def _seed_open_losing_position(qhome: Path):
    bus = qhome / "executions.jsonl"
    rec = {
        "proposal_id": "prop_seed_ASTS", "asset": "ASTS", "asset_class": "equity",
        "reactor_name": "paper", "account_id": "paper-default",
        "fill_size_pct": 0.20, "target_position_pct": 0.20,
        "fill_price": 118.17, "decision_price": 118.17,
        "asof_execution": "2026-06-04T15:35:36Z", "asof_decision": "2026-06-04T15:35:36+00:00",
        "bar_ts": "2026-06-03T04:00:00+00:00", "play_tag": "autonomous", "signal_id": "sig_asts",
    }
    bus.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return bus


@pytest.fixture
def isolate_quant_home(tmp_path, monkeypatch):
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr("hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "autonomous_kill_switch.json")
    return qhome


@pytest.fixture
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    return cfg


def _set_mode_autonomous(cfg_path: Path):
    import yaml
    cfg = {"quant": {"pdr": {"mode": "autonomous"}, "autonomous": {"max_per_tick_opens": 1}}}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_tick_equity_stop_still_fires_through_composite_book(isolate_config, isolate_quant_home, monkeypatch):
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setattr("hermes_quant.perception.build_perception_frame_live", lambda *a, **k: _MarkFrame(93.44))
    react_calls = []

    def fake_react(advisor_result, entry, fill_size_pct, **kwargs):
        react_calls.append((entry.symbol, fill_size_pct, advisor_result.get("reason")))
        return ("exec_stop_asts", fill_size_pct)

    monkeypatch.setattr("hermes_quant.autonomous._react", fake_react)
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert react_calls, "the equity stop must still fire through the composite-keyed book"
    assert react_calls[0][0] == "ASTS"
    assert react_calls[0][1] == pytest.approx(0.0, abs=1e-9)
    assert any(d.gate == "PER_POSITION_STOP_FIRED" for d in result.decisions)
