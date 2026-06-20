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
import json
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


# ---------------------------------------------------------------------------
# cx2 [P1]: HOME-SCOPED BOOK. The settled-book loader MUST read the --home's own
# executions.jsonl, NOT the process-default EXECUTION_BUS_PATH (a DIFFERENT home).
# These two tests DO NOT monkeypatch _settle_paper_round_trips_in_window — they
# write a REAL executions.jsonl under the tmp --home and exercise the FIFO matcher
# end-to-end, so they catch the missing executions_path thread (test-vacuity in
# the monkeypatched tests above).
# ---------------------------------------------------------------------------

_QUANT_EXECUTIONS = "quant/executions.jsonl"


def _exec_record(asset: str, fill_size_pct: float, fill_price: float, asof_exec: str) -> dict:
    """Minimal real-bus ExecutionRecord (PaperReactor._record_to_dict shape), paper-default.

    Mirrors tests/governance/test_promotion_ar125_settlement_derived._make_exec_record
    (the canonical FIFO-matcher fixture). asset_class=us_option so RoundTrip.is_options
    is True (GATE-2 does not filter on it, but keeps the book options-honest)."""
    return {
        "proposal_id": f"p-{asset}-{asof_exec[:10]}",
        "signal_id": f"s-{asset}-{asof_exec[:10]}",
        "asset": asset,
        "asset_class": "us_option",
        "timeframe": "1d",
        "asof_decision": asof_exec,
        "asof_execution": asof_exec,
        "target_position_pct": fill_size_pct,
        "decision_price": fill_price,
        "fill_price": fill_price,
        "fill_size_pct": fill_size_pct,
        "reactor_name": "paper",
        "human_in_the_loop": False,
        "approver_user_id": None,
        "reactor_metadata": {"account_id": "paper-default"},
        "bar_ts": asof_exec,
        "play_tag": None,
        "schema_version": None,
    }


def _write_real_clearing_book(home: Path, t0: datetime) -> None:
    """Write a REAL executions.jsonl under ``home/quant/`` that clears GATE-2."""
    _write_real_clearing_book_at(home / _QUANT_EXECUTIONS, t0)


def _write_real_clearing_book_at(bus: Path, t0: datetime) -> None:
    """Write a REAL clearing executions.jsonl at the EXACT path ``bus``.

    54 round trips over ~64 days, 66% winners (+3% via 100->103) / losers (-1% via
    100->99), losers never adjacent. Same return profile as the monkeypatched
    ``_clearing_book`` above, but produced through the real FIFO matcher
    (settlement_loop.join_exit_fills): each trip is a BUY fill (open) + SELL fill
    (close), realized_return = (exit-entry)/entry with zero fees."""
    bus.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for i in range(54):
        exit_day = t0 + timedelta(days=1 + (i * 66) // 54)  # spread across ~64 days
        entry_day = exit_day - timedelta(hours=12)  # entry strictly before exit, same trip
        is_win = (i % 3) != 2  # lose every 3rd -> 66% win, losers never adjacent
        exit_price = 103.0 if is_win else 99.0  # +3% / -1% off 100.0
        asset = f"OPT{i}"
        records.append(_exec_record(asset, +0.01, 100.0, entry_day.isoformat()))
        records.append(_exec_record(asset, -0.01, exit_price, exit_day.isoformat()))
    with bus.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_clearing_book_under_home_unlocks_not_process_default(tmp_path: Path, monkeypatch):
    """cx2: the verdict reflects the --home's OWN book, NOT the process-default bus.

    RED before the fix: evaluate_gate2 calls _settle_paper_round_trips_in_window
    WITHOUT executions_path -> the function defaults to EXECUTION_BUS_PATH (here a
    DIFFERENT, EMPTY decoy home) -> reads zero round-trips -> thin -> LOCKED, even
    though the --home's own book clears GATE-2. After the fix (executions_path threaded
    to home/quant/executions.jsonl) the verdict is UNLOCKED."""
    home = tmp_path / "operator_a"
    home.mkdir(parents=True)
    decoy_home = tmp_path / "operator_b"  # the process-default points HERE (empty)
    decoy_bus = decoy_home / _QUANT_EXECUTIONS
    decoy_bus.parent.mkdir(parents=True, exist_ok=True)
    decoy_bus.write_text("")  # empty -> process-default read yields zero trips

    now = datetime(2026, 6, 18, tzinfo=UTC)
    t0 = now - timedelta(days=65)
    _seed_t0(home, t0)
    _write_real_clearing_book(home, t0)

    # Point the PROCESS-DEFAULT bus at operator_b (the WRONG home). The loader reads
    # EXECUTION_BUS_PATH at call time (imports it inside the function), so this binds.
    monkeypatch.setattr(
        "hermes_quant.daemon.signal_bus.EXECUTION_BUS_PATH", decoy_bus, raising=True
    )

    writer = _load_writer()
    payload = writer.evaluate_gate2(home, asof=now)

    # The verdict MUST reflect operator_a's own clearing book (n==54, UNLOCKED),
    # not the empty operator_b default (which would be n==0, LOCKED).
    assert payload["n"] == 54, f"read the wrong home's book: {payload}"
    assert payload["gate2_cleared"] is True, f"home-scoped clearing book did not unlock: {payload}"

    rc = writer.main(["--home", str(home)])
    assert rc == 0
    assert read_options_unlocked(home) is True
    # The decoy home was never unlocked.
    assert read_options_unlocked(decoy_home) is False


def test_empty_home_book_stays_locked_even_if_process_default_clears(tmp_path: Path, monkeypatch):
    """cx2 inverse: an EMPTY --home book stays LOCKED even when the process-default
    bus (a DIFFERENT home) holds a clearing book — never unlock home A off home B's book."""
    home = tmp_path / "operator_a"  # empty book
    home.mkdir(parents=True)
    (home / _QUANT_EXECUTIONS).parent.mkdir(parents=True, exist_ok=True)
    (home / _QUANT_EXECUTIONS).write_text("")

    decoy_home = tmp_path / "operator_b"  # holds a clearing book at the process default
    decoy_home.mkdir(parents=True)

    now = datetime(2026, 6, 18, tzinfo=UTC)
    t0 = now - timedelta(days=65)
    _seed_t0(home, t0)
    _write_real_clearing_book(decoy_home, t0)

    monkeypatch.setattr(
        "hermes_quant.daemon.signal_bus.EXECUTION_BUS_PATH",
        decoy_home / _QUANT_EXECUTIONS,
        raising=True,
    )

    writer = _load_writer()
    payload = writer.evaluate_gate2(home, asof=now)
    assert payload["n"] == 0, f"read the WRONG (process-default) home's book: {payload}"
    assert payload["gate2_cleared"] is False, f"unlocked off the wrong home's book: {payload}"


def test_env_hermes_quant_home_book_read_matches_tick_write(tmp_path: Path, monkeypatch):
    """cx2-followup (p6-cx-wave-review): under HERMES_QUANT_HOME-only with home=None
    (the env-driven path the cron uses), evaluate_gate2 must read the book the autonomous
    tick WROTE — quant_home()/executions.jsonl (HERMES_QUANT_HOME-first), NOT the
    HERMES_HOME-only ~/.hermes path. Pre-fix the local _home_path diverged: the tick wrote
    $HERMES_QUANT_HOME/executions.jsonl while gate2-eval read ~/.hermes (a stale book there
    could UNLOCK the wrong home). RED-proof: revert to _home_path(None)/quant/... and this
    reads zero trips (LOCKED) despite the HQH book clearing."""
    hqh = tmp_path / "hqh_quant_root"  # quant_home() returns this directly (no /quant)
    hqh.mkdir(parents=True)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(hqh))
    monkeypatch.delenv("HERMES_HOME", raising=False)

    now = datetime(2026, 6, 18, tzinfo=UTC)
    t0 = now - timedelta(days=65)
    # GATE-0 anchor lives under the quant root the writer reads for read_clean_window_start;
    # pass home=hqh-as-hermes is wrong here — the env path uses home=None, and the anchor is
    # read via clean_window _home_path(None) (HERMES_HOME or ~/.hermes). Seed the anchor where
    # read_clean_window_start(None) looks, and the BOOK where quant_home() looks.
    from hermes_quant.eval.clean_window import _home_path as _cw_home
    _seed_t0(_cw_home(None), t0)  # anchor at the clean_window-resolved home
    # write the real clearing book at quant_home()/executions.jsonl (= hqh/executions.jsonl)
    _write_real_clearing_book_at(hqh / "executions.jsonl", t0)

    writer = _load_writer()
    payload = writer.evaluate_gate2(None, asof=now)  # home=None => env-driven (the cron path)
    assert payload["n"] == 54, f"env HERMES_QUANT_HOME book not read (tick-write vs gate-read divergence): {payload}"
    assert payload["gate2_cleared"] is True
