"""bf76b: the GATE-2 options-origination marker WRITER (aegis-gate2-eval.py).

bf76 landed the READER (read_options_unlocked); bf76b is the writer the eval cron
runs. Acceptance (the seed's own words): a GATE-2-clearing book UNLOCKS, a thin book
stays LOCKED — verified END-TO-END through the REAL gate math
(compute_gate_metrics + evaluate_gate) and the REAL reader (read_options_unlocked),
NOT a hand-written marker. The settled-book LOADER is the only seam monkeypatched
(building 50 real executions through the FIFO matcher is a fixture cost with no extra
coverage — the loader is tested in promotion's own suite); the gate math, the marker
shape, and the reader are all exercised for real.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.eval.clean_window import read_options_unlocked, write_clean_window_start

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "aegis-gate2-eval.py"


def _load_writer():
    spec = importlib.util.spec_from_file_location("aegis_gate2_eval_x", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _SRT:
    """A SettledRoundTrip-shaped stand-in carrying ONLY the 3 fields RoundTrip reads
    (asof_exit / realized_return / asset_class). Faithful: the real SettledRoundTrip
    has all three (verified — settlement_loop.py)."""

    def __init__(self, asof_exit: datetime, realized_return: float, asset_class: str = "us_option"):
        self.asof_exit = asof_exit
        self.realized_return = realized_return
        self.asset_class = asset_class


def _clearing_book(t0: datetime) -> list[_SRT]:
    """54 options round-trips over 64 days, 66% winners at +3% / losers at -1%,
    losers never adjacent -> N>=50, calendar_days>=60, win_rate>=0.50,
    profit_factor>=1.3, max_drawdown<=3% (single -1% draws), max_consec_losses=1.
    Engineered to clear the FULL GATE-2 metric suite (verified against
    compute_gate_metrics + evaluate_gate(2))."""
    trips: list[_SRT] = []
    for i in range(54):
        day = t0 + timedelta(days=1 + (i * 66) // 54)  # spread across ~64 days
        is_win = (i % 3) != 2  # lose every 3rd -> 66% win, losers never adjacent (dd<3%)
        trips.append(_SRT(asof_exit=day, realized_return=0.03 if is_win else -0.01))
    return trips


def _thin_book(t0: datetime) -> list[_SRT]:
    """Only 5 trips over a few days -> N<50 and days<60 -> GATE-2 NOT cleared."""
    return [_SRT(asof_exit=t0 + timedelta(days=i + 1), realized_return=0.04) for i in range(5)]


def _seed_t0(home: Path, t0: datetime) -> None:
    write_clean_window_start(home, t0, armed_flags={"HERMES_QUANT_PER_POSITION_STOP": "1"})


def test_clearing_book_unlocks(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    now = datetime(2026, 6, 18, tzinfo=UTC)
    t0 = now - timedelta(days=65)
    _seed_t0(home, t0)

    writer = _load_writer()
    # Monkeypatch ONLY the settled-book loader (the executions-FIFO source); the gate
    # math + marker + reader stay real.
    monkeypatch.setattr(
        "hermes_quant.governance.promotion._settle_paper_round_trips_in_window",
        lambda window_start, asof, **kw: _clearing_book(t0),
    )
    rc = writer.main(["--home", str(home)])
    assert rc == 0
    # The REAL reader now reports UNLOCKED (the end-to-end contract).
    assert read_options_unlocked(home) is True


def test_thin_book_stays_locked(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    now = datetime(2026, 6, 18, tzinfo=UTC)
    t0 = now - timedelta(days=10)
    _seed_t0(home, t0)

    writer = _load_writer()
    monkeypatch.setattr(
        "hermes_quant.governance.promotion._settle_paper_round_trips_in_window",
        lambda window_start, asof, **kw: _thin_book(t0),
    )
    rc = writer.main(["--home", str(home)])
    assert rc == 0
    assert read_options_unlocked(home) is False  # thin -> LOCKED


def test_no_anchor_stays_locked_fail_closed(tmp_path: Path):
    """No GATE-0 t0 anchor => fail-CLOSED LOCKED (never unlock without a clean window)."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    writer = _load_writer()
    payload = writer.evaluate_gate2(home)
    assert payload["gate2_cleared"] is False
    assert "no_clean_window_anchor" in payload["reason"]
    # And the on-disk reader agrees (no marker written yet => absent => LOCKED).
    assert read_options_unlocked(home) is False


def test_unreadable_book_fails_closed(tmp_path: Path, monkeypatch):
    """A settled-book read error => LOCKED (never unlock on a degraded read)."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    now = datetime(2026, 6, 18, tzinfo=UTC)
    _seed_t0(home, now - timedelta(days=65))

    writer = _load_writer()

    def _boom(window_start, asof, **kw):
        raise OSError("bus unreadable")

    monkeypatch.setattr(
        "hermes_quant.governance.promotion._settle_paper_round_trips_in_window", _boom
    )
    payload = writer.evaluate_gate2(home)
    assert payload["gate2_cleared"] is False
    assert "settled_book_read_failed" in payload["reason"]
