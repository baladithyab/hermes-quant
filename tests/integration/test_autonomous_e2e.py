"""Integration tests for the autonomous-mode tick orchestrator (ADR-0016).

Covers:
  - mode_mismatch when quant.pdr.mode != autonomous
  - dry-run safety (FIRE decisions don't actually React)
  - paper-only React happy path (FIRE + dry_run=False writes execution)
  - max_per_tick_opens cap
  - kill-switch trip prevents further fires
  - per-symbol error isolation
  - all four silence reasons surface in tick output
  - empty watchlist no-op
  - reset_kill_switch recovers
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from hermes_quant.autonomous import (
    reset_kill_switch,
    run_autonomous_cycle,
    tick,
    trip_kill_switch,
)
from hermes_quant.exits import manage_open_positions
from hermes_quant.react.paper import FillSizeInvariantError
from hermes_quant.watchlist import WatchlistEntry


def _append_open_position(
    qhome: Path,
    symbol: str,
    *,
    target_pct: float = 0.10,
    price: float = 100.0,
    ts: str = "2026-06-10T15:00:00Z",
) -> None:
    """Append a synthetic OPEN paper fill to the isolated executions bus so
    reconstruct_portfolio_state (the concurrency-rail / exit-pass source) sees it."""
    rec = {
        "proposal_id": f"prop_open_{symbol}",
        "signal_id": None,
        "asset": symbol,
        "asset_class": "equity",
        "timeframe": "1d",
        "asof_decision": ts,
        "asof_execution": ts,
        "target_position_pct": target_pct,
        "decision_price": price,
        "fill_price": price,
        "fill_size_pct": target_pct,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "approver_user_id": "test",
        "reactor_metadata": {"paper": True},
        "bar_ts": ts,
        "play_tag": "advisor",
    }
    path = qhome / "executions.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


@pytest.fixture
def isolate_quant_home(tmp_path, monkeypatch):
    """Redirect ~/.hermes/quant to a tmpdir so tests don't pollute real state."""
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "hermes_quant.autonomous.QUANT_HOME",
        qhome,
    )
    monkeypatch.setattr(
        "hermes_quant.autonomous.KILL_SWITCH_PATH",
        qhome / "autonomous_kill_switch.json",
    )
    return qhome


@pytest.fixture
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "hermes_quant.watchlist.get_config_path",
        lambda: cfg,
    )
    # autonomous module reads via watchlist's get_config_path, so this is enough
    return cfg


def _set_mode_autonomous(cfg_path: Path, *, max_per_tick_opens: int = 1):
    import yaml

    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("quant", {}).setdefault("pdr", {})["mode"] = "autonomous"
    cfg["quant"].setdefault("autonomous", {})["max_per_tick_opens"] = max_per_tick_opens
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _make_advisor_result(
    *,
    confidence=0.85,
    direction=1,
    magnitude=0.05,
    n_voices=2,
    risk_pass=True,
    kelly=0.05,
    atr_rel=0.05,
    lessons=None,
):
    return {
        "as_of": "2026-05-13T20:00:00Z",
        "decision_price": 100.0,
        "signal_id": "sig_test",
        "aggregated_signal": {
            "confidence": confidence,
            "direction": direction,
            "magnitude": magnitude,
        },
        "risk_gate": {
            "pass": risk_pass,
            "kelly_fraction": kelly,
            "reason": "ok" if risk_pass else "vetoed",
            "gated_reason": None if risk_pass else "vetoed",
        },
        "analyst_views": [
            {"analyst": f"A{i}", "metadata": {"atr_relative": atr_rel}} for i in range(n_voices)
        ],
        "lessons": lessons or [],
    }


# ---------------------------------------------------------------------------
# Mode gate
# ---------------------------------------------------------------------------


def test_tick_returns_mode_mismatch_when_advise(isolate_config, isolate_quant_home):
    # Default mode is 'advise' (no config written -> _read_pdr_mode returns advise)
    result = tick(dry_run=True)
    assert result.mode == "advise"
    assert result.errors == 1
    assert result.watchlist_size == 0
    assert result.decisions == []


def test_tick_runs_when_mode_autonomous(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    result = tick(
        dry_run=True,
        symbols=[],  # empty watchlist
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.mode == "autonomous"
    assert result.watchlist_size == 0


# ---------------------------------------------------------------------------
# Empty watchlist
# ---------------------------------------------------------------------------


def test_tick_empty_watchlist_is_noop(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    result = tick(
        dry_run=True,
        symbols=[],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.fires == 0
    assert result.silences == 0
    assert result.errors == 0
    assert result.decisions == []


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------


def test_dry_run_does_not_react_even_on_fire(
    isolate_config,
    isolate_quant_home,
):
    """Even when the gate FIREs, dry_run=True must NOT call PaperReactor."""
    _set_mode_autonomous(isolate_config)

    react_calls = []

    def fake_advisor(**kw):
        return _make_advisor_result()  # gate-passing

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=lambda *a, **k: react_calls.append((a, k)) or "exec_xxx",
    ):
        result = tick(
            dry_run=True,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=fake_advisor,
        )

    assert result.fires == 1
    assert react_calls == []  # KEY: no React in dry-run
    assert result.decisions[0].execution_id is None  # unset in dry-run


def test_no_dry_run_calls_react_on_fire(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append((entry.symbol, kelly))
        return f"exec_{entry.symbol}"

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=fake_react,
    ):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert result.fires == 1
    assert react_calls == [("AAPL", 0.05)]
    assert result.decisions[0].execution_id == "exec_AAPL"


def test_fill_size_invariant_rejection_silences_not_errors(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=FillSizeInvariantError("fill size rejected"),
    ):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert result.fires == 0
    assert result.silences == 1
    assert result.errors == 0
    assert result.decisions[0].gate == "SILENCE_FILL_SIZE_INVARIANT"
    assert result.decisions[0].execution_id is None


# ---------------------------------------------------------------------------
# max_per_tick_opens cap
# ---------------------------------------------------------------------------


def test_max_per_tick_opens_caps_fires(
    isolate_config,
    isolate_quant_home,
):
    """First FIRE goes through; subsequent FIREs become SILENCE_PER_TICK_CAP."""
    _set_mode_autonomous(isolate_config)
    # Default max_per_tick_opens=1

    result = tick(
        dry_run=True,
        symbols=[
            WatchlistEntry("AAPL", "equity", "1d"),
            WatchlistEntry("MSFT", "equity", "1d"),
            WatchlistEntry("GOOG", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )

    # All three signals are gate-passing, but cap=1 so only 1 fires
    assert result.fires == 1
    assert result.silences == 2
    capped = [d for d in result.decisions if d.gate == "SILENCE_PER_TICK_CAP"]
    assert len(capped) == 2
    assert all(d.details.get("would_have_fired") for d in capped)


# ---------------------------------------------------------------------------
# Wave 2: same-tick re-open prevention (ULTRACODE-REVIEW Q5)
# ---------------------------------------------------------------------------


def test_exited_symbol_not_reopened_same_tick(isolate_config, isolate_quant_home):
    """A symbol flattened by the pre-entry exit pass MUST NOT be re-opened by the
    entry loop in the same tick. Passing exited_symbols=[X] makes the entry loop
    skip X entirely (the catastrophic stop-loss/re-fill churn loop the review
    flagged)."""
    _set_mode_autonomous(isolate_config)
    # AAPL is open at tick start; the exit pass just flattened it this tick.
    _append_open_position(isolate_quant_home, "AAPL", target_pct=0.10, price=100.0)

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            exited_symbols=["AAPL"],
        )

    # AAPL must NOT be re-fired.
    assert "AAPL" not in react_calls
    assert result.fires == 0
    aapl = [d for d in result.decisions if d.symbol == "AAPL"][0]
    assert aapl.gate == "SILENCE_EXITED_THIS_TICK"


def test_exited_symbol_frees_concurrency_slot(isolate_config, isolate_quant_home):
    """A symbol exited this tick must be DECREMENTED from the concurrency
    snapshot, not left consuming a slot. With max_concurrent=5 and a book of
    [A,B,C,D,E] where E was exited this tick, a NEW symbol F must be allowed to
    open (E freed the 5th slot)."""
    import yaml

    _set_mode_autonomous(isolate_config, max_per_tick_opens=10)
    cfg = yaml.safe_load(isolate_config.read_text(encoding="utf-8")) or {}
    cfg["quant"]["autonomous"]["max_concurrent_positions"] = 5
    isolate_config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    for sym in ("A", "B", "C", "D", "E"):
        _append_open_position(isolate_quant_home, sym, target_pct=0.10, price=100.0)

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("F", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            exited_symbols=["E"],  # E flattened this tick => slot freed
        )

    # Without the decrement, F would be SILENCE_CONCURRENT_CAP (book reads as 5).
    assert "F" in react_calls
    assert result.fires == 1


def test_exited_symbols_default_none_is_unchanged(isolate_config, isolate_quant_home):
    """exited_symbols defaulting to None must be byte-identical to today: a NEW
    symbol fires normally with no exit pass in play."""
    _set_mode_autonomous(isolate_config)

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert react_calls == ["AAPL"]
    assert result.fires == 1


# ---------------------------------------------------------------------------
# Wave 3: Q6 concurrent-cap fail-CLOSED on reconstruct error
# ---------------------------------------------------------------------------


def test_concurrent_cap_reconstruct_error_fails_closed_firing(
    isolate_config, isolate_quant_home, monkeypatch
):
    """A reconstruct ERROR means open-book headroom is UNKNOWN. On the firing
    path tick() must FAIL CLOSED — refuse to open new positions (an unreadable
    bus = 'can't prove the book has room'). Previously this failed OPEN to 0,
    re-opening the 41.6x-gross runaway window PR#82 closed (ULTRACODE-REVIEW Q6)."""
    _set_mode_autonomous(isolate_config)

    def _boom(*a, **k):
        raise OSError("bus unreadable")

    monkeypatch.setattr(
        "hermes_quant.portfolio.state.reconstruct_portfolio_state", _boom
    )

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert react_calls == []  # KEY: no open fired when headroom is unknown
    assert result.fires == 0
    aapl = [d for d in result.decisions if d.symbol == "AAPL"][0]
    assert aapl.gate == "SILENCE_CONCURRENT_CAP_UNKNOWN"
    assert aapl.details.get("would_have_fired") is True


def test_concurrent_cap_reconstruct_error_fails_closed_dry_run(
    isolate_config, isolate_quant_home, monkeypatch
):
    """Dry-run is a faithful preview of the firing path: when headroom is unknown
    the dry-run also reports the open as cap-unknown-silenced (the FAIL-CLOSED
    choice — a dry-run that reported 'would fire' would lie about what the real
    path does)."""
    _set_mode_autonomous(isolate_config)

    def _boom(*a, **k):
        raise OSError("bus unreadable")

    monkeypatch.setattr(
        "hermes_quant.portfolio.state.reconstruct_portfolio_state", _boom
    )

    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )

    assert result.fires == 0
    aapl = [d for d in result.decisions if d.symbol == "AAPL"][0]
    assert aapl.gate == "SILENCE_CONCURRENT_CAP_UNKNOWN"


def test_caps_seeding_reconstruct_error_fails_closed(
    isolate_config, isolate_quant_home, monkeypatch
):
    """Re-home of the brief gate's test_cap_init_failure_blocks_not_fires onto
    tick(): with HERMES_QUANT_PORTFOLIO_CAPS=1, if the cap-seeding reconstruct
    raises, tick() must FAIL CLOSED (block every open, surface no fire) — never
    crash the cron (tick's contract is 'raises nothing externally-visible') and
    never proceed uncapped (the 2026-06-02 runaway behavior)."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    _set_mode_autonomous(isolate_config, max_per_tick_opens=10)

    def _boom(*a, **k):
        raise RuntimeError("cap-seed bus read failed")

    monkeypatch.setattr(
        "hermes_quant.portfolio.state.reconstruct_portfolio_state", _boom
    )

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(  # must NOT raise
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert react_calls == []  # no uncapped fire
    assert result.fires == 0


def test_empty_book_still_fires_not_fail_closed(
    isolate_config, isolate_quant_home
):
    """A genuinely empty/missing bus (reconstruct returns empty cleanly, no
    exception) is the truth '0 open' — NOT a read error. The fail-closed change
    must NOT block fires here; only a reconstruct EXCEPTION fails closed."""
    _set_mode_autonomous(isolate_config)
    # No executions.jsonl written => reconstruct returns empty (no raise).

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert react_calls == ["AAPL"]
    assert result.fires == 1


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_trip_halts_entries_but_exits_still_run(
    isolate_config, isolate_quant_home
):
    """REVIEWED CHANGE (ULTRACODE-REVIEW Q2, the blocking finding): a tripped
    kill-switch must halt ENTRIES while EXITS still fire. Entries and exits have
    opposite desired behavior under a tripped switch — the switch trips precisely
    when the book is bleeding, which is exactly when stops must still cut losers.

    tick() (entries) still aborts before evaluating symbols — unchanged. The
    SEPARATE manage_open_positions() pass NEVER reads the kill-switch, so it still
    closes a breaching position even with the switch tripped. This test documents
    both halves of the contract (was: test_kill_switch_trip_halts_tick, which only
    asserted the entry-halt half)."""
    _set_mode_autonomous(isolate_config)
    # Enable the exit pass + put one breaching position in the isolated book.
    import yaml

    cfg = yaml.safe_load(isolate_config.read_text(encoding="utf-8")) or {}
    cfg["quant"]["autonomous"]["manage_positions"] = True
    cfg["quant"]["autonomous"]["stop_loss_pct"] = 0.10
    isolate_config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _append_open_position(isolate_quant_home, "AAPL", target_pct=0.10, price=100.0)

    trip_kill_switch(
        cumulative_pnl_pct=-0.15,
        threshold_pct=0.10,
        reason="manual_test_trip",
    )

    # ENTRIES halt — tick aborts before evaluating symbols (unchanged).
    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("MSFT", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.kill_switch_state.tripped is True
    assert result.fires == 0
    assert result.decisions == []

    # EXITS still run — the separate pass closes the breaching AAPL despite the
    # tripped switch.
    exit_result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},  # -15% < -10% stop
        clock_provider=lambda: True,
        quant_home=isolate_quant_home,
    )
    assert "AAPL" in exit_result.exited_symbols


def test_reset_kill_switch_resumes_normal_operation(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)
    trip_kill_switch(cumulative_pnl_pct=-0.15, threshold_pct=0.10, reason="test")
    cleared = reset_kill_switch()
    assert cleared is True

    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.kill_switch_state.tripped is False
    assert result.fires == 1


def test_reset_kill_switch_when_not_tripped_returns_false(
    isolate_config,
    isolate_quant_home,
):
    assert reset_kill_switch() is False


# ---------------------------------------------------------------------------
# Per-symbol error isolation
# ---------------------------------------------------------------------------


def test_advisor_failure_for_one_symbol_does_not_break_tick(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    def fake_advisor(*, symbol, **_kw):
        if symbol == "MSFT":
            raise RuntimeError("rate limit hit")
        return _make_advisor_result()

    result = tick(
        dry_run=True,
        symbols=[
            WatchlistEntry("AAPL", "equity", "1d"),
            WatchlistEntry("MSFT", "equity", "1d"),
            WatchlistEntry("GOOG", "equity", "1d"),
        ],
        advisor_recommend=fake_advisor,
    )

    assert result.errors == 1
    assert result.fires + result.silences == 2
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.gate == "ERROR"
    assert "rate limit" in (msft.error or "")


# ---------------------------------------------------------------------------
# All silence reasons appear in output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,expected_gate",
    [
        ("low_confidence", "SILENCE_LOW_CONFIDENCE"),
        ("low_urgency", "SILENCE_LOW_URGENCY"),
        ("insufficient_voices", "SILENCE_INSUFFICIENT_VOICES"),
        ("gated_by_advisor", "SILENCE_GATED_BY_ADVISOR"),
    ],
)
def test_all_silence_reasons_surface(
    isolate_config,
    isolate_quant_home,
    scenario,
    expected_gate,
):
    _set_mode_autonomous(isolate_config)

    def advisor_for_scenario(**kw):
        if scenario == "low_confidence":
            return _make_advisor_result(confidence=0.4)
        if scenario == "low_urgency":
            return _make_advisor_result(magnitude=0.001, atr_rel=0.10)
        if scenario == "insufficient_voices":
            return _make_advisor_result(n_voices=1)
        if scenario == "gated_by_advisor":
            return _make_advisor_result(risk_pass=False)
        raise AssertionError("unhandled scenario")

    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=advisor_for_scenario,
    )
    assert result.fires == 0
    assert result.silences == 1
    assert result.decisions[0].gate == expected_gate


# ---------------------------------------------------------------------------
# Tick output shape (operator-readable per ADR-0016 §D8)
# ---------------------------------------------------------------------------


def test_tick_output_to_dict_shape(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )

    out = result.to_dict()
    assert "asof" in out
    assert "mode" in out
    assert "dry_run" in out
    assert "watchlist_size" in out
    assert "decisions" in out
    assert "fires" in out
    assert "silences" in out
    assert "errors" in out
    assert "kill_switch" in out

    # Must be JSON-serializable
    serialized = json.dumps(out, default=str)
    assert serialized  # didn't raise

    # Each decision has required fields
    for d in out["decisions"]:
        assert "symbol" in d
        assert "gate" in d
        assert "details" in d


def test_fire_decision_includes_action_and_execution_id(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)
    with mock.patch(
        "hermes_quant.autonomous._react",
        return_value="exec_AAPL_001",
    ):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    fire = result.decisions[0]
    assert fire.gate == "FIRE"
    assert fire.action is not None
    assert fire.action["target_position_pct"] == 0.05
    assert fire.action["direction"] == 1
    assert fire.execution_id == "exec_AAPL_001"


# ---------------------------------------------------------------------------
# React error isolation
# ---------------------------------------------------------------------------


def test_react_failure_marks_decision_error_but_continues(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    def fake_react(advisor_result, entry, kelly, **kwargs):
        if entry.symbol == "AAPL":
            raise RuntimeError("paper bus full")
        return f"exec_{entry.symbol}"

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=fake_react,
    ):
        result = tick(
            dry_run=False,
            symbols=[
                WatchlistEntry("AAPL", "equity", "1d"),
            ],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert result.errors == 1
    assert result.decisions[0].gate == "ERROR"
    assert "paper bus full" in (result.decisions[0].error or "")


# ---------------------------------------------------------------------------
# ADR-0071 portfolio-aware Stage-2 sizing
# ---------------------------------------------------------------------------


def test_portfolio_caps_disabled_by_default_no_clip(
    isolate_config, isolate_quant_home, monkeypatch
):
    """Default behavior: HERMES_QUANT_PORTFOLIO_CAPS unset → no clipping."""
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    _set_mode_autonomous(isolate_config)

    fired_sizes = []

    def fake_react(advisor_result, entry, kelly, paper_zero_costs):
        fired_sizes.append(kelly)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[
                WatchlistEntry("AAPL", "equity", "1d"),
                WatchlistEntry("MSFT", "equity", "1d"),
            ],
            advisor_recommend=lambda **kw: _make_advisor_result(kelly=0.20),
        )

    # No clipping: every fire passes through full Kelly
    assert all(abs(s) == 0.20 for s in fired_sizes)
    # No SILENCE_PORTFOLIO_CAP decisions
    silenced_by_portfolio = [
        d for d in result.decisions if d.gate == "SILENCE_PORTFOLIO_CAP"
    ]
    assert silenced_by_portfolio == []


def test_portfolio_caps_enabled_clips_to_remaining_headroom(
    isolate_config, isolate_quant_home, monkeypatch
):
    """With caps enabled and 4 picks at 20%, default cash floor binds before net."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    _set_mode_autonomous(isolate_config, max_per_tick_opens=10)

    # Force the autonomous loop to start with empty book (no executions.jsonl)
    # — the isolate_quant_home fixture already redirects QUANT_HOME, but the
    # PortfolioState reconstruction reads from a hard-coded path. Patch it.
    from hermes_quant.portfolio.state import _DEFAULT_EXECUTIONS_PATH  # noqa: F401

    monkeypatch.setattr(
        "hermes_quant.portfolio.state._DEFAULT_EXECUTIONS_PATH",
        isolate_quant_home / "executions.jsonl",
    )

    fired_sizes = []

    def fake_react(advisor_result, entry, kelly, paper_zero_costs):
        fired_sizes.append((entry.symbol, kelly))
        return f"exec_{entry.symbol}"

    # Six picks at 20% each → demand = 120% gross, default caps allow ≤ 80% (cash floor)
    # Order: greedy first-come-first-served. Default kelly=0.20 (from _make_advisor_result).
    syms = [WatchlistEntry(f"S{i}", "equity", "1d") for i in range(6)]

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=syms,
            advisor_recommend=lambda **kw: _make_advisor_result(kelly=0.20),
        )

    # Total fired gross must not exceed 80% (1 - cash floor)
    total_gross = sum(abs(k) for _, k in fired_sizes)
    assert total_gross <= 0.80 + 1e-9, (
        f"total fired gross {total_gross} > 80% cap; fired: {fired_sizes}"
    )

    # At least the first pick should fire at full size (greedy)
    assert fired_sizes[0][1] == 0.20

    # Picks beyond cash budget should be silenced as SILENCE_PORTFOLIO_CAP
    portfolio_silences = [
        d for d in result.decisions if d.gate == "SILENCE_PORTFOLIO_CAP"
    ]
    assert len(portfolio_silences) >= 1
    assert all(
        d.details and d.details.get("would_have_fired") is True
        for d in portfolio_silences
    )


def test_portfolio_caps_dry_run_simulates_state_consumption(
    isolate_config, isolate_quant_home, monkeypatch
):
    """Dry-run also updates the running state so downstream picks see consumption."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    monkeypatch.setattr(
        "hermes_quant.portfolio.state._DEFAULT_EXECUTIONS_PATH",
        isolate_quant_home / "executions.jsonl",
    )
    _set_mode_autonomous(isolate_config, max_per_tick_opens=10)

    syms = [WatchlistEntry(f"S{i}", "equity", "1d") for i in range(6)]
    result = tick(
        dry_run=True,
        symbols=syms,
        advisor_recommend=lambda **kw: _make_advisor_result(kelly=0.20),
    )

    # Even in dry-run, the cap should have silenced some picks.
    portfolio_silences = [
        d for d in result.decisions if d.gate == "SILENCE_PORTFOLIO_CAP"
    ]
    assert len(portfolio_silences) >= 1


# ---------------------------------------------------------------------------
# Wave 4a: run_autonomous_cycle — the ONE importable seam the cron + the
# quant_autonomous_tick tool call. Runs manage_open_positions() BEFORE entries
# when manage_positions is enabled, passes exited_symbols into tick(), and the
# exit pass runs EVEN under a tripped kill-switch (cutting losses) while entries
# halt.
# ---------------------------------------------------------------------------


def _enable_manage_positions(cfg_path: Path, *, stop_loss_pct: float = 0.10) -> None:
    import yaml

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    auto = cfg.setdefault("quant", {}).setdefault("autonomous", {})
    auto["manage_positions"] = True
    auto["stop_loss_pct"] = stop_loss_pct
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_cycle_runs_exits_before_entries_and_blocks_reopen(
    isolate_config, isolate_quant_home
):
    """The cycle flattens a breaching position FIRST, then runs entries with that
    symbol in exited_symbols so the entry loop cannot re-open it the same cycle."""
    _set_mode_autonomous(isolate_config)
    _enable_manage_positions(isolate_config)
    # AAPL is open and breaching; it is also on the entry watchlist.
    _append_open_position(isolate_quant_home, "AAPL", target_pct=0.10, price=100.0)

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        out = run_autonomous_cycle(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            marks_provider=lambda syms: {"AAPL": 85.0},  # -15% < -10% stop
            clock_provider=lambda: True,
            quant_home=isolate_quant_home,
        )

    # Exit pass closed AAPL.
    assert "AAPL" in out.exit_result.exited_symbols
    # Entry loop did NOT re-open it the same cycle.
    assert "AAPL" not in react_calls
    assert out.tick_result.fires == 0
    aapl = [d for d in out.tick_result.decisions if d.symbol == "AAPL"][0]
    assert aapl.gate == "SILENCE_EXITED_THIS_TICK"


def test_cycle_exits_run_under_tripped_kill_switch_entries_halt(
    isolate_config, isolate_quant_home
):
    """THE load-bearing asymmetry: a tripped kill-switch must HALT ENTRIES but the
    exit pass must STILL FIRE (cutting losses). One seam, both behaviors."""
    _set_mode_autonomous(isolate_config)
    _enable_manage_positions(isolate_config)
    _append_open_position(isolate_quant_home, "AAPL", target_pct=0.10, price=100.0)
    trip_kill_switch(cumulative_pnl_pct=-0.15, threshold_pct=0.10, reason="bleeding")

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        out = run_autonomous_cycle(
            dry_run=False,
            symbols=[WatchlistEntry("MSFT", "equity", "1d")],  # a NEW entry candidate
            advisor_recommend=lambda **kw: _make_advisor_result(),
            marks_provider=lambda syms: {"AAPL": 85.0},
            clock_provider=lambda: True,
            quant_home=isolate_quant_home,
        )

    # EXITS still ran despite the tripped switch.
    assert "AAPL" in out.exit_result.exited_symbols
    # ENTRIES halted: tick early-returns on the tripped switch, no React.
    assert react_calls == []
    assert out.tick_result.fires == 0
    assert out.tick_result.kill_switch_state.tripped is True


def test_cycle_manage_positions_off_skips_exit_pass(
    isolate_config, isolate_quant_home
):
    """manage_positions unset (default) => the exit pass is a byte-identical
    no-op and the cycle is just a plain tick() (no exited_symbols)."""
    _set_mode_autonomous(isolate_config)
    # NOTE: manage_positions NOT enabled.
    _append_open_position(isolate_quant_home, "AAPL", target_pct=0.10, price=100.0)
    before = (isolate_quant_home / "executions.jsonl").read_bytes()

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        out = run_autonomous_cycle(
            dry_run=False,
            symbols=[WatchlistEntry("MSFT", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            marks_provider=lambda syms: {"AAPL": 50.0},  # would breach hard if run
            clock_provider=lambda: True,
            quant_home=isolate_quant_home,
        )

    # Exit pass did nothing (flag off): no exits, bus untouched by the exit leg.
    assert out.exit_result.exited_symbols == []
    assert (isolate_quant_home / "executions.jsonl").read_bytes() == before
    # Entry path is unaffected — MSFT fires normally.
    assert react_calls == ["MSFT"]
    assert out.tick_result.fires == 1


def test_cycle_dry_run_honored_end_to_end(isolate_config, isolate_quant_home):
    """dry_run=True must thread to BOTH the exit pass (would_exit, no append) and
    the entry tick (no React)."""
    _set_mode_autonomous(isolate_config)
    _enable_manage_positions(isolate_config)
    _append_open_position(isolate_quant_home, "AAPL", target_pct=0.10, price=100.0)
    before = (isolate_quant_home / "executions.jsonl").read_bytes()

    react_calls = []

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=lambda *a, **k: react_calls.append(a) or "x",
    ):
        out = run_autonomous_cycle(
            dry_run=True,
            symbols=[WatchlistEntry("MSFT", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            marks_provider=lambda syms: {"AAPL": 85.0},
            clock_provider=lambda: True,
            quant_home=isolate_quant_home,
        )

    assert "AAPL" in out.exit_result.would_exit
    assert out.exit_result.exited_symbols == []
    assert react_calls == []  # entry dry-run: no React
    assert (isolate_quant_home / "executions.jsonl").read_bytes() == before


# ---------------------------------------------------------------------------
# Wave 5a: explicit mode_override replaces the cron's _read_pdr_mode monkeypatch
# ---------------------------------------------------------------------------


def test_mode_override_runs_pipeline_without_config_mode(
    isolate_config, isolate_quant_home
):
    """The cron runs the PDR pipeline even though config.yaml does NOT set
    quant.pdr.mode=autonomous. Previously it monkey-patched auto._read_pdr_mode
    at process scope (brittle + leaks to every importer). tick(mode_override=...)
    is the explicit, scoped replacement: pass 'autonomous' and the pipeline runs;
    config is left untouched (still 'advise')."""
    # NOTE: config mode deliberately NOT set to autonomous (default advise).

    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            mode_override="autonomous",
        )

    assert result.mode == "autonomous"
    assert result.fires == 1
    assert react_calls == ["AAPL"]


def test_mode_override_none_reads_config_byte_identical(
    isolate_config, isolate_quant_home
):
    """mode_override=None (default) is byte-identical to today: read config. With
    config unset (advise), the tick mode-mismatches exactly as before."""
    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.mode == "advise"
    assert result.errors == 1
    assert result.decisions == []


def test_cycle_threads_mode_override(isolate_config, isolate_quant_home):
    """run_autonomous_cycle forwards mode_override to tick() so the cron can call
    the ONE seam without touching config or monkey-patching."""
    react_calls = []

    def fake_react(advisor_result, entry, kelly, **kwargs):
        react_calls.append(entry.symbol)
        return f"exec_{entry.symbol}"

    with mock.patch("hermes_quant.autonomous._react", side_effect=fake_react):
        out = run_autonomous_cycle(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
            marks_provider=lambda syms: {},
            clock_provider=lambda: True,
            quant_home=isolate_quant_home,
            mode_override="autonomous",
        )

    assert out.tick_result.mode == "autonomous"
    assert out.tick_result.fires == 1
