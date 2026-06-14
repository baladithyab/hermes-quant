"""Unit tests for quant-playbook-weekly exit-rule logic (pure, no I/O).

The script lives at ~/.hermes/scripts/quant-playbook-weekly.py. We import it
via importlib.util so the tests don't depend on a particular sys.path setup.

Per ADR-0035 wave 3:
  - Swing exits: stop on >60d losing, take-profit on >3*ATR.
  - LEAPS thesis: close on broken revenue growth, balance-sheet risk, or
    -25% drawdown.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "quant-playbook-weekly.py"
if not SCRIPT_PATH.exists():
    # Fallback to ~/.hermes/scripts/ for local dev installations that
    # symlink scripts into the user home (legacy path).
    SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "quant-playbook-weekly.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT_PATH.exists(),
    reason=f"quant-playbook-weekly.py not found at {SCRIPT_PATH}",
)


@pytest.fixture(scope="module")
def mod():
    import sys
    spec = importlib.util.spec_from_file_location("qpw", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field-annotation resolution finds it.
    sys.modules["qpw"] = m
    spec.loader.exec_module(m)
    return m


# ---------------------- swing exit rules ----------------------

def test_swing_holds_short_winner(mod):
    d = mod.decide_swing(mod.SwingContext(days_held=10, pnl_pct=0.04, atr14_at_entry_pct=0.02))
    assert d.action == "HOLD"


def test_swing_stop_70d_losing(mod):
    """ADR-0035 §97: days_held>60 AND pnl_pct<0 -> close."""
    d = mod.decide_swing(mod.SwingContext(days_held=70, pnl_pct=-0.05, atr14_at_entry_pct=0.02))
    assert d.action == "CLOSE"
    assert "swing_stop" in d.reason


def test_swing_stop_does_not_fire_at_exactly_60d(mod):
    """Boundary: '>60' is strict."""
    d = mod.decide_swing(mod.SwingContext(days_held=60, pnl_pct=-0.10, atr14_at_entry_pct=0.02))
    assert d.action == "HOLD"


def test_swing_old_winner_holds(mod):
    """80d held but profitable — no stop."""
    d = mod.decide_swing(mod.SwingContext(days_held=80, pnl_pct=0.01, atr14_at_entry_pct=0.02))
    assert d.action == "HOLD"


def test_swing_take_profit_three_atr(mod):
    """ADR-0035 §98: pnl_pct > 3*ATR-14_at_entry -> close."""
    d = mod.decide_swing(mod.SwingContext(days_held=15, pnl_pct=0.10, atr14_at_entry_pct=0.02))
    # 3 * 0.02 = 0.06; 0.10 > 0.06
    assert d.action == "CLOSE"
    assert "swing_tp" in d.reason


def test_swing_no_atr_no_take_profit(mod):
    """If ATR data missing (0.0), TP rule cannot fire — strictly hold."""
    d = mod.decide_swing(mod.SwingContext(days_held=15, pnl_pct=0.50, atr14_at_entry_pct=0.0))
    assert d.action == "HOLD"


# ---------------------- leaps thesis-check ----------------------

def test_leaps_holds_when_thesis_intact(mod):
    d = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=0.18,
        debt_to_equity=0.7,
        drawdown_from_entry=0.05,
    ))
    assert d.action == "HOLD"


def test_leaps_close_on_revenue_growth_collapse(mod):
    d = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=0.02,  # < 0.05 threshold
        debt_to_equity=0.5,
        drawdown_from_entry=0.05,
    ))
    assert d.action == "CLOSE"
    assert "leaps_revgrowth" in d.reason


def test_leaps_close_on_debt(mod):
    d = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=0.20,
        debt_to_equity=2.5,  # > 2.0
        drawdown_from_entry=0.05,
    ))
    assert d.action == "CLOSE"
    assert "leaps_de" in d.reason


def test_leaps_close_on_drawdown(mod):
    d = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=0.20,
        debt_to_equity=0.5,
        drawdown_from_entry=0.30,  # > 0.25
    ))
    assert d.action == "CLOSE"
    assert "leaps_drawdown" in d.reason


def test_leaps_missing_fundamentals_does_not_close(mod):
    """Missing fundamentals (None) is not, by itself, a close signal."""
    d = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=None,
        debt_to_equity=None,
        drawdown_from_entry=0.05,
    ))
    assert d.action == "HOLD"


# ---------------------- play_tag inference ----------------------

def test_infer_play_tag_explicit(mod):
    execs = [{"asset": "AAPL", "side": "buy", "play_tag": "leaps"}]
    assert mod.infer_play_tag(execs, "AAPL") == "leaps"


def test_infer_play_tag_from_signal_id(mod):
    execs = [{"asset": "TSLA", "side": "buy", "signal_id": "sig-swing-TSLA-20260101"}]
    assert mod.infer_play_tag(execs, "TSLA") == "swing"


def test_infer_play_tag_default_swing(mod):
    """No clue at all -> default to swing (cautious)."""
    execs = [{"asset": "MSFT", "side": "buy"}]
    assert mod.infer_play_tag(execs, "MSFT") == "swing"


def test_infer_play_tag_no_match(mod):
    """Asset never in executions -> default swing."""
    execs = [{"asset": "AAPL", "side": "buy", "play_tag": "leaps"}]
    assert mod.infer_play_tag(execs, "MSFT") == "swing"


# ---------------------- cs17: live-record shape (no side/asof) ----------------------

def _live_exec_dict(
    asset: str = "AAPL",
    target_position_pct: float = 0.20,
    play_tag: str = "advisor",
    asof_execution: str = "2026-06-08T13:31:05+00:00",
) -> dict:
    """A record in the REAL live-producer shape: signed target_position_pct, no
    `side`, no `asof` (uses asof_execution), play_tag at the producer default."""
    return {
        "asset": asset,
        "asset_class": "equity",
        "target_position_pct": target_position_pct,
        "fill_size_pct": target_position_pct,
        "fill_price": 200.0,
        "decision_price": 200.0,
        "asof_execution": asof_execution,
        "reactor_name": "paper",
        "play_tag": play_tag,
    }


def test_rec_side_derives_buy_from_positive_target(mod):
    """cs17: a long target_position_pct (no `side` key) derives 'buy'."""
    assert mod._rec_side(_live_exec_dict(target_position_pct=0.20)) == "buy"


def test_rec_side_derives_sell_from_negative_target(mod):
    """cs17: a short target_position_pct derives 'sell'."""
    assert mod._rec_side(_live_exec_dict(target_position_pct=-0.20)) == "sell"


def test_rec_side_honors_explicit_legacy_side(mod):
    """cs17: a legacy record with an explicit `side` is honored verbatim."""
    assert mod._rec_side({"asset": "X", "side": "buy"}) == "buy"
    assert mod._rec_side({"asset": "X", "side": "sell"}) == "sell"


def test_live_shape_infer_play_tag_advisor_falls_through(mod):
    """cs17: a live record (no side, play_tag='advisor' sentinel) is treated as an
    opening leg AND the 'advisor' sentinel falls through to the swing default
    (it carries no playbook meaning)."""
    execs = [_live_exec_dict(play_tag="advisor")]
    assert mod.infer_play_tag(execs, "AAPL") == "swing"


def test_live_shape_find_entry_and_days_held(mod):
    """cs17: the live shape (no `side`, no `asof`) is found as the entry record, and
    days_held reads asof_execution (>0, not the ERROR/HOLD no-entry path)."""
    execs = [_live_exec_dict(asof_execution="2026-06-08T13:31:05+00:00")]
    entry = mod.find_entry_record(execs, "AAPL")
    assert entry is not None  # NOT the "no opening execution found" early-return

    now_dt = mod.datetime(2026, 6, 18, tzinfo=mod.UTC)
    days_held = mod.days_between_iso(
        entry.get("asof_execution") or entry.get("asof", ""), now_dt
    )
    # 2026-06-08T13:31:05 -> 2026-06-18T00:00 = 9 full calendar days. The key
    # assertion is that asof_execution was READ (days_held > 0, not the 0/ERROR
    # path that an empty asof would produce).
    assert days_held == 9
    assert days_held > 0


def test_live_shape_explicit_play_tag_still_wins(mod):
    """cs17: a live record with a REAL play_tag (not the advisor sentinel) is honored."""
    execs = [_live_exec_dict(play_tag="leaps")]
    assert mod.infer_play_tag(execs, "AAPL") == "leaps"
