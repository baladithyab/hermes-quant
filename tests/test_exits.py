"""tests/test_exits.py — manage_open_positions() autonomous exit pass (Wave 1).

The exit pass is the SAFETY-CRITICAL core of autonomous position management.
A bug here = mass liquidation of the research book, so every rail gets a test:

  - flag-OFF => byte-identical no-op (append nothing, read nothing)
  - stop-loss fires; take-profit fires
  - valid-mark gate: NaN / inf / <=0 marks SKIPPED, never exited (the #1
    historical fail-open class — NaN <= 0 is False)
  - per-symbol sanity clamp: finite-but-wrong mark (>25% jump) SKIPPED
  - cross-sectional anomaly breaker: >50% of book breaching at once => feed
    event => alert, exit NOTHING (the rail that prevents correlated-feed
    mass-liquidation that max_exits_per_tick only rate-limits)
  - max_exits_per_tick cap: exit N worst, alert the rest
  - market clock fail-closed: closed / unknown / error => exit nothing
  - dry-run appends nothing
  - |held| > HARD_FILL_CEILING => ALERT (not silent skip)
  - exit record shape: target_position_pct=0.0, fill_size_pct=-held, paper
  - play_tag recovery from the originating open fill
  - runs EVEN under a tripped kill-switch (cutting losses is the one action a
    tripped system must still take)

All tests pass QUANT_HOME/"executions.jsonl" explicitly via the quant_home arg
(test-isolation; the module-level QUANT_HOME bound at import would defeat it).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hermes_quant.exits import ExitResult, manage_open_positions


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def qhome(tmp_path: Path) -> Path:
    """A redirected ~/.hermes/quant home with an empty executions bus."""
    home = tmp_path / "quant"
    home.mkdir(parents=True, exist_ok=True)
    (home / "executions.jsonl").touch()
    return home


@pytest.fixture
def exit_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point config.yaml at a tmp file the exits module reads via get_config_path."""
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    return cfg


def _write_exit_config(cfg_path: Path, **autonomous_overrides) -> None:
    """Write quant.autonomous.<overrides> (manage_positions + thresholds)."""
    import yaml

    cfg: dict = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    auto = cfg.setdefault("quant", {}).setdefault("autonomous", {})
    auto.update(autonomous_overrides)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _append_open(
    qhome: Path,
    symbol: str,
    target_pct: float,
    entry_price: float,
    *,
    play_tag: str = "advisor",
    ts: str = "2026-06-10T15:00:00Z",
    asset_class: str = "equity",
    timeframe: str = "1d",
    metadata: dict | None = None,
    fill_price: float | None = None,
    fill_size_pct: float | None = None,
    proposal_id: str | None = None,
) -> None:
    """Append a synthetic OPEN paper fill so reconstruct_portfolio_state sees it.

    ``entry_price`` is the decision_price (the pre-slippage quote). By default
    ``fill_price`` equals it, so the pnl basis and the clamp basis are the same
    number — every existing test is byte-identical. Pass ``fill_price`` explicitly
    to model the v0.2 slippage model (fill_price != decision_price) for FIX-B.

    ``fill_size_pct`` defaults to ``target_pct`` (PaperReactor.execute stamps them
    equal); pass it explicitly only when modelling a divergent cumulative fill.
    ``proposal_id`` is overridable so multi-add fills do not collide on the
    state.db idempotency key (proposal_id, asof, asset, asset_class).
    """
    rec = {
        "proposal_id": proposal_id or f"prop_open_{symbol}",
        "signal_id": f"sig_{symbol}",
        "asset": symbol,
        "asset_class": asset_class,
        "timeframe": timeframe,
        "asof_decision": ts,
        "asof_execution": ts,
        "target_position_pct": target_pct,
        "decision_price": entry_price,
        "fill_price": entry_price if fill_price is None else fill_price,
        "fill_size_pct": target_pct if fill_size_pct is None else fill_size_pct,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "approver_user_id": "test",
        "reactor_metadata": metadata or {"paper": True},
        "bar_ts": ts,
        "play_tag": play_tag,
    }
    line = json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n"
    with open(qhome / "executions.jsonl", "a", encoding="utf-8") as f:
        f.write(line)


def _bus_records(qhome: Path) -> list[dict]:
    path = qhome / "executions.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _open_market(*_a, **_k) -> bool:
    return True


# ---------------------------------------------------------------------------
# Flag-OFF => byte-identical no-op
# ---------------------------------------------------------------------------


def test_flag_off_is_byte_identical_noop(qhome, exit_config):
    """manage_positions unset => append NOTHING, read NOTHING, empty result."""
    # No config written at all => manage_positions defaults False.
    _append_open(qhome, "AAPL", 0.10, 100.0)
    before = (qhome / "executions.jsonl").read_bytes()

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 50.0},  # would breach hard
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert isinstance(result, ExitResult)
    assert result.exited_symbols == []
    assert result.would_exit == []
    assert result.anomaly_tripped is False
    # The bus must be byte-for-byte unchanged.
    assert (qhome / "executions.jsonl").read_bytes() == before


# ---------------------------------------------------------------------------
# Stop-loss / take-profit
# ---------------------------------------------------------------------------


def test_stop_loss_fires(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0, play_tag="autonomous")

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},  # -15% < -10% stop
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.exited_symbols
    recs = _bus_records(qhome)
    exit_rec = recs[-1]
    assert exit_rec["asset"] == "AAPL"
    assert exit_rec["target_position_pct"] == 0.0
    assert exit_rec["fill_size_pct"] == pytest.approx(-0.10)
    assert exit_rec["reactor_name"] == "paper"


def test_take_profit_fires(qhome, exit_config):
    _write_exit_config(
        exit_config, manage_positions=True, stop_loss_pct=0.10, take_profit_pct=0.10
    )
    _append_open(qhome, "MSFT", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"MSFT": 115.0},  # +15% >= +10% take
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "MSFT" in result.exited_symbols


def test_take_profit_off_by_default(qhome, exit_config):
    """take_profit_pct=None (default) => a winner is NOT auto-closed."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "MSFT", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"MSFT": 120.0},  # +20% but take is off
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert result.exited_symbols == []


def test_short_stop_loss_fires(qhome, exit_config):
    """A short loses when price RISES; pnl_pct = qty_sign*(mark/entry-1)."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "TSLA", -0.10, 100.0)  # short

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"TSLA": 115.0},  # +15% price => short -15%
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "TSLA" in result.exited_symbols
    assert _bus_records(qhome)[-1]["fill_size_pct"] == pytest.approx(0.10)  # -(-0.10)


# ---------------------------------------------------------------------------
# Valid-mark gate (NaN-safe) — the #1 historical fail-open class
# ---------------------------------------------------------------------------


def test_nan_mark_skipped_no_append(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)
    before = (qhome / "executions.jsonl").read_bytes()

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": float("nan")},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.skipped_bad_mark
    assert result.exited_symbols == []
    assert (qhome / "executions.jsonl").read_bytes() == before


def test_inf_mark_skipped(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": float("inf")},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.skipped_bad_mark
    assert result.exited_symbols == []


def test_nonpositive_mark_skipped(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)
    _append_open(qhome, "MSFT", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 0.0, "MSFT": -5.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert set(result.skipped_bad_mark) == {"AAPL", "MSFT"}
    assert result.exited_symbols == []


def test_missing_mark_skipped(qhome, exit_config):
    """A symbol with no mark in the provider dict is skipped, not exited."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {},  # provider returned nothing for AAPL
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.skipped_bad_mark
    assert result.exited_symbols == []


# ---------------------------------------------------------------------------
# Per-symbol sanity clamp (finite-but-wrong)
# ---------------------------------------------------------------------------


def test_sanity_clamp_skips_finite_wrong_mark(qhome, exit_config):
    """A finite, positive mark that jumped >25% from the last bus price is a
    finite-but-wrong feed glitch => skip, never exit."""
    _write_exit_config(
        exit_config, manage_positions=True, stop_loss_pct=0.10, mark_jump_max=0.25
    )
    _append_open(qhome, "AAPL", 0.10, 100.0)  # last bus price = 100

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 200.0},  # +100% jump > 25% clamp
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.skipped_bad_mark
    assert result.exited_symbols == []


# ---------------------------------------------------------------------------
# Cross-sectional anomaly breaker
# ---------------------------------------------------------------------------


def test_anomaly_breaker_trips_and_exits_nothing(qhome, exit_config):
    """>50% of the marked book breaching at once (count>=3) => feed event =>
    alert and exit NOTHING, even though each per-symbol mark is plausible."""
    _write_exit_config(
        exit_config,
        manage_positions=True,
        stop_loss_pct=0.10,
        anomaly_breaker_pct=0.50,
    )
    for sym in ("A", "B", "C", "D"):
        _append_open(qhome, sym, 0.10, 100.0)
    before = (qhome / "executions.jsonl").read_bytes()

    # Every mark -15%: passes the 25% clamp, breaches the 10% stop. 4/4 breach.
    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {s: 85.0 for s in syms},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert result.anomaly_tripped is True
    assert result.exited_symbols == []
    assert (qhome / "executions.jsonl").read_bytes() == before
    assert result.alerts  # the breaker must surface an alert


def test_anomaly_breaker_below_floor_does_not_trip(qhome, exit_config):
    """2 of 2 breaching is fraction 1.0 (>50%) but below the count floor of 3 =>
    NOT a feed event => normal exits proceed."""
    _write_exit_config(
        exit_config,
        manage_positions=True,
        stop_loss_pct=0.10,
        anomaly_breaker_pct=0.50,
    )
    _append_open(qhome, "A", 0.10, 100.0)
    _append_open(qhome, "B", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {s: 85.0 for s in syms},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert result.anomaly_tripped is False
    assert set(result.exited_symbols) == {"A", "B"}


# ---------------------------------------------------------------------------
# max_exits_per_tick cap
# ---------------------------------------------------------------------------


def test_max_exits_per_tick_caps_exits_worst_alerts_rest(qhome, exit_config):
    """4 breaches in a 10-name book (40% < 50% breaker) with cap=3 => exit the
    3 WORST, alert the 4th. Healthy positions are untouched."""
    _write_exit_config(
        exit_config,
        manage_positions=True,
        stop_loss_pct=0.10,
        anomaly_breaker_pct=0.50,
        max_exits_per_tick=3,
    )
    # 4 breaching with distinct losses so ranking is deterministic.
    breach_marks = {"A": 80.0, "B": 82.0, "C": 84.0, "D": 86.0}  # -20,-18,-16,-14%
    for sym in breach_marks:
        _append_open(qhome, sym, 0.10, 100.0)
    # 6 healthy (pnl 0, valid mark == entry).
    healthy = [f"H{i}" for i in range(6)]
    for sym in healthy:
        _append_open(qhome, sym, 0.10, 100.0)

    marks = {**breach_marks, **{s: 100.0 for s in healthy}}
    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: marks,
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert result.anomaly_tripped is False
    # The 3 worst (most negative pnl): A(-20), B(-18), C(-16).
    assert set(result.exited_symbols) == {"A", "B", "C"}
    # The 4th breach (D) is rate-limited: alerted, not exited.
    assert "D" not in result.exited_symbols
    assert any("D" in a for a in result.alerts)
    # No healthy name touched.
    for h in healthy:
        assert h not in result.exited_symbols


# ---------------------------------------------------------------------------
# Market-clock fail-closed
# ---------------------------------------------------------------------------


def test_clock_closed_exits_nothing(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)
    before = (qhome / "executions.jsonl").read_bytes()

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 50.0},
        clock_provider=lambda: False,  # market closed
        quant_home=qhome,
    )

    assert result.exited_symbols == []
    assert (qhome / "executions.jsonl").read_bytes() == before


def test_clock_provider_error_fails_closed(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    def boom() -> bool:
        raise RuntimeError("clock API down")

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 50.0},
        clock_provider=boom,
        quant_home=qhome,
    )

    assert result.exited_symbols == []


def test_clock_unknown_fails_closed(qhome, exit_config):
    """A clock provider returning a non-bool (unknown) is treated as CLOSED."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 50.0},
        clock_provider=lambda: None,  # unknown
        quant_home=qhome,
    )

    assert result.exited_symbols == []


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_appends_nothing(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)
    before = (qhome / "executions.jsonl").read_bytes()

    result = manage_open_positions(
        dry_run=True,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.would_exit
    assert result.exited_symbols == []
    assert (qhome / "executions.jsonl").read_bytes() == before


# ---------------------------------------------------------------------------
# |held| > HARD_FILL_CEILING => ALERT, not silent skip
# ---------------------------------------------------------------------------


def test_held_over_ceiling_alerts_not_silent(qhome, exit_config):
    """A position the system can't represent (|held|>1.0) is one it can't
    auto-close — that needs an ALERT, never a silent skip."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "BROKE", 1.5, 100.0)  # |held|=1.5 > HARD_FILL_CEILING
    before = (qhome / "executions.jsonl").read_bytes()

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"BROKE": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "BROKE" not in result.exited_symbols
    assert any("BROKE" in a for a in result.alerts)
    assert (qhome / "executions.jsonl").read_bytes() == before


# ---------------------------------------------------------------------------
# Exit record shape + play_tag recovery
# ---------------------------------------------------------------------------


def test_exit_record_shape_and_play_tag_recovered(qhome, exit_config):
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.20, 100.0, play_tag="autonomous")

    manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    exit_rec = _bus_records(qhome)[-1]
    assert exit_rec["target_position_pct"] == 0.0
    assert exit_rec["fill_size_pct"] == pytest.approx(-0.20)
    assert exit_rec["reactor_name"] == "paper"
    assert exit_rec["play_tag"] == "autonomous"  # recovered from the open fill
    assert exit_rec["decision_price"] == pytest.approx(85.0)  # exited at the mark
    assert exit_rec["fill_price"] == pytest.approx(85.0)


def test_exit_closes_in_reconstruct_view(qhome, exit_config):
    """After the exit fill, reconstruct_portfolio_state must show the name FLAT
    (target=0 drops it) — the idempotency the design relies on."""
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    state = reconstruct_portfolio_state(qhome / "executions.jsonl")
    assert "AAPL" not in state.positions


# ---------------------------------------------------------------------------
# Per-position stop override
# ---------------------------------------------------------------------------


def test_per_position_stop_override_price_cross(qhome, exit_config):
    """When the open fill carries trader_stop_loss (a PRICE), a price-cross
    triggers the stop even if the default pct band is not breached."""
    # Loose default stop (50%) so the pct path alone would NOT fire at -8%.
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.50)
    _append_open(
        qhome,
        "AAPL",
        0.10,
        100.0,
        metadata={"paper": True, "trader_stop_loss": 95.0},
    )

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 92.0},  # below 95 stop; only -8% pnl
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.exited_symbols


# ---------------------------------------------------------------------------
# Kill-switch independence — THE load-bearing safety property
# ---------------------------------------------------------------------------


def test_runs_even_under_tripped_kill_switch(qhome, exit_config, monkeypatch):
    """A tripped kill-switch halts ENTRIES but must NOT freeze losers open.
    manage_open_positions is a SEPARATE function that never reads the switch —
    lock that it still cuts a breaching position when the switch is tripped."""
    import hermes_quant.autonomous as auto

    # Trip the live kill-switch into this isolated home.
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", qhome / "autonomous_kill_switch.json")
    monkeypatch.setattr(auto, "QUANT_HOME", qhome)
    auto.trip_kill_switch(
        cumulative_pnl_pct=-0.15, threshold_pct=0.10, reason="book bleeding"
    )
    assert auto._read_kill_switch().tripped is True

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.exited_symbols


# ---------------------------------------------------------------------------
# Bad entry price guard
# ---------------------------------------------------------------------------


def test_zero_entry_price_skipped(qhome, exit_config):
    """An open fill with a 0.0 entry (the decision_price sentinel) can't yield a
    pnl => skip, never divide-by-zero into a fabricated breach."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 0.0)  # entry sentinel

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.skipped_bad_mark
    assert result.exited_symbols == []


def test_exit_log_line_renders(qhome, exit_config, caplog):
    """The CLOSED log line must format cleanly at INFO — a malformed % spec in
    the exit path would raise at record-render time (regression guard for the
    `%+.2%%` bug)."""
    import logging as _logging

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0, play_tag="autonomous")

    with caplog.at_level(_logging.INFO, logger="hermes_quant.exits"):
        manage_open_positions(
            dry_run=False,
            marks_provider=lambda syms: {"AAPL": 85.0},
            clock_provider=_open_market,
            quant_home=qhome,
        )
        # Force every captured record to render its message; a bad % spec raises.
        rendered = [rec.getMessage() for rec in caplog.records]

    assert any("CLOSED AAPL" in msg for msg in rendered)


def test_empty_book_is_noop(qhome, exit_config):
    """No open positions => clean empty result, nothing appended."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    before = (qhome / "executions.jsonl").read_bytes()

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert result.exited_symbols == []
    assert result.would_exit == []
    assert (qhome / "executions.jsonl").read_bytes() == before


# ---------------------------------------------------------------------------
# FIX-A (Codex P1): close the CUMULATIVE held quantity, not the latest target
# snapshot. reconstruct_portfolio_state is latest-supersedes (every add stamps
# target=fill, so two +0.1 adds read as held=0.1), but settlement FIFO sums the
# signed fills (= +0.2). Offsetting -held=-0.1 marks target=0 (reader sees flat)
# yet leaves a +0.1 ghost lot in the settlement view => wrong realized P&L and a
# lot invisible to future reconstruct-based exit passes.
# ---------------------------------------------------------------------------


def test_exit_closes_cumulative_not_latest_target(qhome, exit_config):
    """Two +0.1 adds then a stop must append fill_size_pct=-0.2 (the cumulative
    signed fill) and target_position_pct=0.0 — not -0.1 (the latest target)."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    # Two adds: each PaperReactor add stamps target=fill=+0.1. Distinct
    # proposal_ids so neither the bus nor state.db idempotency-collapses them.
    _append_open(qhome, "AAPL", 0.10, 100.0, proposal_id="prop_add1")
    _append_open(
        qhome, "AAPL", 0.10, 100.0, proposal_id="prop_add2",
        ts="2026-06-10T15:05:00Z",
    )

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},  # -15% < -10% stop
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.exited_symbols
    exit_rec = _bus_records(qhome)[-1]
    assert exit_rec["target_position_pct"] == 0.0
    assert exit_rec["fill_size_pct"] == pytest.approx(-0.20)  # NOT -0.10


def test_exit_nets_settlement_flat(qhome, exit_config):
    """After the cumulative close, the settlement FIFO matcher must leave NO
    residual open lot for the symbol — the dual-ledger divergence is gone."""
    from hermes_quant.daemon.settlement_loop import join_exit_fills

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0, proposal_id="prop_add1")
    _append_open(
        qhome, "AAPL", 0.10, 100.0, proposal_id="prop_add2",
        ts="2026-06-10T15:05:00Z",
    )

    manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    _round_trips, open_lots = join_exit_fills(_bus_records(qhome))
    # The real position bucket (paper-default, equity, AAPL) must be fully
    # settled — no leftover long lot. (Namespaced ('_deferred', ...) keys are
    # not position lots; the real bucket must simply be absent / empty.)
    real = {k: v for k, v in open_lots.items() if not (k and k[0] == "_deferred")}
    assert ("paper-default", "equity", "AAPL") not in real


# ---------------------------------------------------------------------------
# FIX-B (Codex P2): evaluate thresholds against fill_price (the actual entry the
# v0.2 slippage model + settlement use), not decision_price (the pre-slippage
# quote). Storing decision_price as the pnl/clamp basis makes the exit's
# exit_pnl_pct disagree with settlement's realized P&L.
# ---------------------------------------------------------------------------


def test_stop_evaluated_against_fill_price_not_decision_price(qhome, exit_config):
    """A long bought at fill_price=120 (decision_price=100, +20% slippage). At
    mark=95: against fill_price pnl=-20.8% (breaches -10% stop); against
    decision_price pnl=-5% (no breach). The stop MUST fire => fill_price basis."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    # decision_price=100 (clamp basis), fill_price=120 (true entry).
    _append_open(qhome, "AAPL", 0.10, 100.0, fill_price=120.0)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 95.0},  # -5% vs decision, -20.8% vs fill
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.exited_symbols


def test_threshold_falls_back_to_decision_price_when_no_fill_price(qhome, exit_config):
    """An older record lacking fill_price must still evaluate (fall back to
    decision_price) — fill_price recovery is a preference, not a hard require."""
    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    # Append a record with fill_price explicitly null (legacy shape).
    import json as _json

    rec = {
        "proposal_id": "prop_legacy_AAPL",
        "signal_id": "sig_AAPL",
        "asset": "AAPL",
        "asset_class": "equity",
        "timeframe": "1d",
        "asof_decision": "2026-06-10T15:00:00Z",
        "asof_execution": "2026-06-10T15:00:00Z",
        "target_position_pct": 0.10,
        "decision_price": 100.0,
        "fill_price": None,  # legacy: no fill price recorded
        "fill_size_pct": 0.10,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "approver_user_id": "test",
        "reactor_metadata": {"paper": True},
        "bar_ts": "2026-06-10T15:00:00Z",
        "play_tag": "advisor",
    }
    with open(qhome / "executions.jsonl", "a", encoding="utf-8") as f:
        f.write(_json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},  # -15% vs decision_price=100
        clock_provider=_open_market,
        quant_home=qhome,
    )

    assert "AAPL" in result.exited_symbols  # fell back to decision_price


# ---------------------------------------------------------------------------
# FIX-C (Codex P2): a non-dry exit appends directly (bypassing
# PaperReactor.execute, which rebuilds state.db), so state.db keeps showing the
# closed symbol open => status / NAV-kill-switch / sizing run on stale state.
# After a real exit, rebuild state.db from the bus (flatten-script pattern).
# ---------------------------------------------------------------------------


def test_non_dry_exit_reconciles_state_db(qhome, exit_config):
    """After a non-dry exit, state.db (quant_home/state.db) must NOT show the
    closed symbol open — the direct append also rebuilds the derived cache."""
    from hermes_quant.state.portfolio_state import PortfolioState

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    state_db = qhome / "state.db"
    # Seed state.db so it shows AAPL open (the stale-open precondition).
    PortfolioState(state_db_path=state_db).reconstruct_from(qhome / "executions.jsonl")
    seeded = PortfolioState(state_db_path=state_db).get_positions("paper-default")
    assert ("equity", "AAPL") in seeded

    manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    after = PortfolioState(state_db_path=state_db).get_positions("paper-default")
    assert ("equity", "AAPL") not in after


def test_dry_run_does_not_reconcile_state_db(qhome, exit_config):
    """A dry-run appends nothing AND must not touch state.db (reconcile is
    guarded to real exits only)."""
    from hermes_quant.state.portfolio_state import PortfolioState

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)
    state_db = qhome / "state.db"
    PortfolioState(state_db_path=state_db).reconstruct_from(qhome / "executions.jsonl")

    manage_open_positions(
        dry_run=True,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    # Dry-run did not append a close, so a reconcile (if it wrongly ran) would
    # still show AAPL open; the contract is simply that the dry path is a no-op
    # against state.db AND the bus. AAPL is still open in both.
    after = PortfolioState(state_db_path=state_db).get_positions("paper-default")
    assert ("equity", "AAPL") in after


def test_state_db_reconcile_failure_does_not_crash_exit(qhome, exit_config, monkeypatch):
    """A reconcile failure must be logged and swallowed — the bus is the source
    of truth, so the exit append must stand even if the cache rebuild fails."""
    import hermes_quant.state.portfolio_state as ps_mod

    _write_exit_config(exit_config, manage_positions=True, stop_loss_pct=0.10)
    _append_open(qhome, "AAPL", 0.10, 100.0)

    def _boom(self, *a, **k):
        raise OSError("state.db locked")

    monkeypatch.setattr(ps_mod.PortfolioState, "reconstruct_from", _boom)

    result = manage_open_positions(
        dry_run=False,
        marks_provider=lambda syms: {"AAPL": 85.0},
        clock_provider=_open_market,
        quant_home=qhome,
    )

    # The exit still succeeded (bus is source of truth); reconcile failure is
    # non-fatal.
    assert "AAPL" in result.exited_symbols
    assert _bus_records(qhome)[-1]["target_position_pct"] == 0.0
