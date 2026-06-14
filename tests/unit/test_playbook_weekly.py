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


# ---------------------- cs20: sign-aware P&L / drawdown ----------------------

def test_pnl_drawdown_long_byte_identical(mod):
    """REGRESSION GUARD: a long (qty>=0) keeps the EXACT pre-cs20 formula.

    pnl_pct = (mark-avg_entry)/avg_entry ; drawdown = max(0,(avg_entry-mark)/avg_entry).
    """
    avg_entry, mark = 200.0, 250.0
    pnl, dd = mod.compute_pnl_drawdown(avg_entry, mark, qty=100.0)
    assert pnl == pytest.approx((mark - avg_entry) / avg_entry)  # +0.25
    assert dd == pytest.approx(max(0.0, (avg_entry - mark) / avg_entry))  # 0.0
    # losing long: price fell below entry
    pnl2, dd2 = mod.compute_pnl_drawdown(200.0, 150.0, qty=100.0)
    assert pnl2 == pytest.approx(-0.25)
    assert dd2 == pytest.approx(0.25)


def test_pnl_drawdown_long_flat_qty_treated_as_long(mod):
    """qty==0 (degenerate) takes the long branch — byte-identical to original."""
    pnl, dd = mod.compute_pnl_drawdown(200.0, 250.0, qty=0.0)
    assert pnl == pytest.approx(0.25)
    assert dd == pytest.approx(0.0)


def test_pnl_drawdown_short_losing_flips_to_loss_and_drawdown(mod):
    """A LOSING short (mark>avg_entry, price rose against it) must show pnl<0 AND a
    positive drawdown so the LEAPS -25% close and the >60d loss-stop CAN fire.

    Under the buggy long-only formula this short showed pnl=+0.25, dd=0.0 — the
    -25% LEAPS close and the loss-stop never fired on a real losing short.
    """
    pnl, dd = mod.compute_pnl_drawdown(avg_entry=200.0, mark=250.0, qty=-100.0)
    assert pnl == pytest.approx(-0.25)  # NOT +0.25
    assert dd == pytest.approx(0.25)    # NOT 0.0 -> LEAPS -25% fires


def test_pnl_drawdown_short_winning_flips_to_profit(mod):
    """A WINNING short (mark<avg_entry, price fell) must show pnl>0 so the >60d
    loss-stop does NOT wrong-fire on a winner, and drawdown stays 0."""
    pnl, dd = mod.compute_pnl_drawdown(avg_entry=200.0, mark=150.0, qty=-100.0)
    assert pnl == pytest.approx(0.25)  # NOT -0.25 (which would wrong-fire the stop)
    assert dd == pytest.approx(0.0)


def test_pnl_drawdown_zero_avg_entry_holds(mod):
    """Non-positive avg_entry (bad data) yields (0,0) for both signs -> rules HOLD."""
    assert mod.compute_pnl_drawdown(0.0, 100.0, qty=100.0) == (0.0, 0.0)
    assert mod.compute_pnl_drawdown(0.0, 100.0, qty=-100.0) == (0.0, 0.0)


def test_short_losing_drives_leaps_drawdown_close(mod):
    """End-to-end through the decision layer: a losing short's flipped drawdown
    crosses the LEAPS -25% threshold and decide_leaps CLOSES."""
    _, dd = mod.compute_pnl_drawdown(avg_entry=200.0, mark=270.0, qty=-100.0)  # +35% adverse
    d = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=0.20, debt_to_equity=0.5, drawdown_from_entry=dd,
    ))
    assert d.action == "CLOSE"
    assert "leaps_drawdown" in d.reason


def test_short_winner_take_profit_fires_under_flipped_pnl(mod):
    """A short that has profited (mark fell) crosses the 3*ATR take-profit under the
    flipped pnl_pct, so decide_swing CLOSES on the TP branch (a short hitting +3*ATR
    profit should take profit)."""
    pnl, _ = mod.compute_pnl_drawdown(avg_entry=200.0, mark=180.0, qty=-100.0)  # +10% profit
    d = mod.decide_swing(mod.SwingContext(days_held=15, pnl_pct=pnl, atr14_at_entry_pct=0.02))
    # 3*0.02 = 0.06 ; pnl=+0.10 > 0.06
    assert d.action == "CLOSE"
    assert "swing_tp" in d.reason


def test_short_loser_60d_stop_fires_under_flipped_pnl(mod):
    """An old (>60d) losing short shows pnl<0 under the flip, so the >60d loss-stop
    CLOSES (it never could under the buggy +pnl)."""
    pnl, _ = mod.compute_pnl_drawdown(avg_entry=200.0, mark=240.0, qty=-100.0)  # -20% loss
    d = mod.decide_swing(mod.SwingContext(days_held=70, pnl_pct=pnl, atr14_at_entry_pct=0.02))
    assert d.action == "CLOSE"
    assert "swing_stop" in d.reason


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


# ---------------------- cs26: sign-directed opening-leg lookup ----------------------
#
# The live book is short-dominated (cs14 emits Position.qty<0). A SHORT's OPENING
# leg derives _rec_side=="sell" (cs17), but find_entry_record / infer_play_tag
# hardcode =="buy" -> a held short returns None -> run_weekly logs action="ERROR"
# and `continue`s, so the cs20 math + decide_swing/decide_leaps CLOSE path is
# UNREACHABLE for any short. cs26 threads the held position's sign into both
# helpers so the opening leg is matched by direction (long->buy, short->sell).
# The LONG path stays byte-identical; legacy explicit-`side` records still match.


def test_find_entry_record_short_unfound_without_sign(mod):
    """Documents the bug AND the byte-identical default: a 2-arg lookup (default
    position_qty=0.0 -> desired_side 'buy') never matches a short's sell opening
    leg. Stays green pre- and post-fix (a long lookup must not match a short)."""
    execs = [_live_exec_dict(target_position_pct=-0.20)]
    assert mod.find_entry_record(execs, "AAPL") is None


def test_find_entry_record_short_found_with_negative_qty(mod):
    """KEYSTONE: passing the held SHORT sign finds the sell opening leg. RED today
    (the unpatched 2-arg helper rejects a 3rd positional arg / drops the short)."""
    execs = [_live_exec_dict(target_position_pct=-0.20)]
    entry = mod.find_entry_record(execs, "AAPL", -100.0)
    assert entry is not None
    assert mod._rec_side(entry) == "sell"


def test_infer_play_tag_short_with_negative_qty(mod):
    """A short's opening leg (sell) is now read, so an explicit play_tag wins."""
    execs = [_live_exec_dict(target_position_pct=-0.20, play_tag="leaps")]
    assert mod.infer_play_tag(execs, "AAPL", -100.0) == "leaps"


def test_infer_play_tag_short_advisor_falls_through(mod):
    """The 'advisor' sentinel falls through to swing even though the short's sell
    leg is now seen as the opening leg (it carries no playbook meaning)."""
    execs = [_live_exec_dict(target_position_pct=-0.20, play_tag="advisor")]
    assert mod.infer_play_tag(execs, "AAPL", -100.0) == "swing"


def test_short_entry_reaches_cs20_close_path_end_to_end(mod):
    """END-TO-END: the previously-unreachable short-exit chain. A held SHORT now
    finds its entry -> days_held>0 via asof_execution -> the cs20 sign-aware
    compute_pnl_drawdown yields an adverse drawdown -> decide_leaps CLOSES.

    This is the logical join of run_weekly's :491->523->535->551 chain that was
    dead behind the entry-is-None ERROR/continue for every short."""
    execs = [_live_exec_dict(
        target_position_pct=-0.20,
        asof_execution="2026-06-08T13:31:05+00:00",
    )]
    entry = mod.find_entry_record(execs, "AAPL", -100.0)
    assert entry is not None  # NOT the "no opening execution found" ERROR path

    now_dt = mod.datetime(2026, 6, 18, tzinfo=mod.UTC)
    days_held = mod.days_between_iso(
        entry.get("asof_execution") or entry.get("asof", ""), now_dt
    )
    assert days_held == 9
    assert days_held > 0

    # Losing short: mark rose 200 -> 270 against it (+35% adverse).
    pnl, dd = mod.compute_pnl_drawdown(200.0, 270.0, -100.0)
    assert pnl == pytest.approx(-0.35)
    assert dd == pytest.approx(0.35)
    decision = mod.decide_leaps(mod.LeapsContext(
        revenue_growth_yoy=0.20, debt_to_equity=0.5, drawdown_from_entry=dd,
    ))
    assert decision.action == "CLOSE"
    assert "leaps_drawdown" in decision.reason


def test_long_opening_leg_unchanged_with_positive_qty(mod):
    """LONG REGRESSION: the 3-arg call with a positive qty is byte-identical to the
    2-arg default for BOTH helpers (desired_side 'buy' either way)."""
    execs = [_live_exec_dict(target_position_pct=0.20)]
    assert mod.find_entry_record(execs, "AAPL", 100.0) == mod.find_entry_record(execs, "AAPL")
    assert mod.find_entry_record(execs, "AAPL", 100.0) is not None
    assert mod.infer_play_tag(execs, "AAPL", 100.0) == mod.infer_play_tag(execs, "AAPL")
    assert mod.infer_play_tag(execs, "AAPL", 100.0) == "swing"


def test_legacy_explicit_short_side_now_matches(mod):
    """A legacy record with an explicit side='sell' is honored by _rec_side and now
    matches as the short opening leg when the held sign is negative."""
    execs = [{"asset": "X", "side": "sell", "play_tag": "leaps"}]
    assert mod.find_entry_record(execs, "X", -50.0) is not None
    assert mod.infer_play_tag(execs, "X", -50.0) == "leaps"


def test_reshorted_asset_picks_held_sign_leg(mod):
    """AMBIGUITY: an asset that was long then flipped short carries both a buy and a
    sell leg. The opening leg is the one matching the CURRENT held sign — a held
    short (-qty) picks the sell, a held long (+qty) picks the buy."""
    execs = [
        _live_exec_dict(target_position_pct=0.20),   # earlier long opening (buy)
        _live_exec_dict(target_position_pct=-0.20),  # later short opening (sell)
    ]
    short_entry = mod.find_entry_record(execs, "AAPL", -100.0)
    assert short_entry is not None
    assert mod._rec_side(short_entry) == "sell"

    long_entry = mod.find_entry_record(execs, "AAPL", 100.0)
    assert long_entry is not None
    assert mod._rec_side(long_entry) == "buy"


# ---- cs27/cs28: establishing-leg (multi-fill / re-open) ----
#
# Root question (cs27 P1 + cs28 P2 are ONE increment): WHICH execution record is
# the entry of a multi-fill position? The loader keeps the LATEST target per asset
# (portfolio_loader.py:245-255, no flip/partial guard on the absolute path) so the
# held SIGN is sign(latest target_position_pct). But find_entry_record reads the
# FIRST file-order match (oldest leg -> days_held source) while infer_play_tag reads
# the reversed()/NEWEST match (play source) -> three readers anchor to three legs.
#
# The fix defines the OPEN BOUNDARY = the establishing leg = the first fill of the
# CURRENT held sign AFTER the last flat (target==0) or flip (sign change). days_held
# AND the play tag both anchor to THAT leg, so all readers agree with the loader's
# held position. A flat fill is _live_exec_dict(target_position_pct=0.0) (derives
# _rec_side==""). now_dt is pinned to 2026-06-14 UTC.


def test_reopened_short_days_held_from_current_run(mod):
    """cs27 (RED today, GREEN after): a short re-opened across a flat must read
    days_held off the CURRENT run's opening leg (06-08, ~6d), NOT the long-dead
    01-05 leg (~159d). The buggy find_entry_record returns the FIRST file-order sell
    (01-05) -> 159-160d -> the armed 60d swing stop wrong-fires on a fresh re-open.
    """
    execs = [
        _live_exec_dict(asset="BA", target_position_pct=-0.20,
                        asof_execution="2026-01-05T00:00:00+00:00"),  # short opened
        _live_exec_dict(asset="BA", target_position_pct=0.0,
                        asof_execution="2026-03-01T00:00:00+00:00"),  # FLAT (closed)
        _live_exec_dict(asset="BA", target_position_pct=-0.20,
                        asof_execution="2026-06-08T00:00:00+00:00"),  # RE-OPENED (current)
    ]
    entry = mod.find_entry_record(execs, "BA", -100.0)
    assert entry is not None
    assert entry.get("asof_execution").startswith("2026-06-08")

    now_dt = mod.datetime(2026, 6, 14, tzinfo=mod.UTC)
    days_held = mod.days_between_iso(
        entry.get("asof_execution") or entry.get("asof", ""), now_dt
    )
    # 2026-06-08T00:00 -> 2026-06-14T00:00 = 6 days (the buggy 01-05 leg -> 159).
    assert days_held == 6
    assert days_held <= 60


def test_reopened_short_swing_stop_does_not_wrongfire(mod):
    """cs27 (RED today, GREEN after): with days_held anchored to the establishing
    leg the armed swing stop HOLDs a fresh re-opened (underwater) short. Cross-check
    the weapon is real: the SAME stop CLOSES at the buggy 160d age."""
    execs = [
        _live_exec_dict(asset="BA", target_position_pct=-0.20,
                        asof_execution="2026-01-05T00:00:00+00:00"),
        _live_exec_dict(asset="BA", target_position_pct=0.0,
                        asof_execution="2026-03-01T00:00:00+00:00"),
        _live_exec_dict(asset="BA", target_position_pct=-0.20,
                        asof_execution="2026-06-08T00:00:00+00:00"),
    ]
    entry = mod.find_entry_record(execs, "BA", -100.0)
    now_dt = mod.datetime(2026, 6, 14, tzinfo=mod.UTC)
    days_held = mod.days_between_iso(
        entry.get("asof_execution") or entry.get("asof", ""), now_dt
    )
    d = mod.decide_swing(mod.SwingContext(
        days_held=days_held, pnl_pct=-0.05, atr14_at_entry_pct=0.02,
    ))
    assert d.action == "HOLD"
    # The stop is a real weapon — it fires at the (buggy) inflated age.
    assert mod.decide_swing(mod.SwingContext(
        days_held=160, pnl_pct=-0.05, atr14_at_entry_pct=0.02,
    )).action == "CLOSE"


def test_multifill_short_add_readers_agree_on_first_of_run(mod):
    """cs28 (RED today, GREEN after): a same-sign add (no flat between) -> all readers
    anchor to the FIRST add of the current run (02-01). days_held off 02-01; the play
    tag off 02-01 ('leaps'). RED today: infer_play_tag's reversed() picks the 06-09
    newest add -> 'swing'; find_entry_record picks 02-01 already (oldest) so the play
    and the age legs DISAGREE."""
    execs = [
        _live_exec_dict(asset="AVGO", target_position_pct=-0.10, play_tag="leaps",
                        asof_execution="2026-02-01T00:00:00+00:00"),  # opened short
        _live_exec_dict(asset="AVGO", target_position_pct=-0.20, play_tag="swing",
                        asof_execution="2026-06-09T00:00:00+00:00"),  # added (current)
    ]
    entry = mod.find_entry_record(execs, "AVGO", -100.0)
    play = mod.infer_play_tag(execs, "AVGO", -100.0)
    assert entry is not None
    assert entry.get("asof_execution").startswith("2026-02-01")
    assert play == "leaps"


def test_single_fill_long_establishing_leg_byte_identical(mod):
    """cs27/cs28 REGRESSION: a single-fill long is byte-identical pre/post fix. One
    buy -> no flat, no flip -> the establishing leg IS that buy (== today's first
    match). days_held reads asof_execution; play falls through to swing."""
    execs = [_live_exec_dict(target_position_pct=0.20, play_tag="advisor",
                             asof_execution="2026-06-08T13:31:05+00:00")]
    assert mod.find_entry_record(execs, "AAPL", 100.0) == mod.find_entry_record(execs, "AAPL")
    entry = mod.find_entry_record(execs, "AAPL", 100.0)
    assert entry is not None
    now_dt = mod.datetime(2026, 6, 18, tzinfo=mod.UTC)
    days_held = mod.days_between_iso(
        entry.get("asof_execution") or entry.get("asof", ""), now_dt
    )
    assert days_held == 9
    assert mod.infer_play_tag(execs, "AAPL", 100.0) == "swing"


def test_flip_then_short_establishing_is_post_flip_leg(mod):
    """cs27/cs28 (flip-boundary compat): an asset long then flipped short. The held
    SHORT's establishing leg is the post-flip sell (06-10); the held LONG's is the
    pre-flip buy (05-01). A flip resets the run (like a flat)."""
    execs = [
        _live_exec_dict(asset="AAPL", target_position_pct=0.20,
                        asof_execution="2026-05-01T00:00:00+00:00"),   # long opened (buy)
        _live_exec_dict(asset="AAPL", target_position_pct=-0.20,
                        asof_execution="2026-06-10T00:00:00+00:00"),   # flipped short (sell)
    ]
    short_entry = mod.find_entry_record(execs, "AAPL", -100.0)
    assert short_entry is not None
    assert mod._rec_side(short_entry) == "sell"
    assert short_entry.get("asof_execution").startswith("2026-06-10")

    long_entry = mod.find_entry_record(execs, "AAPL", 100.0)
    assert long_entry is not None
    assert mod._rec_side(long_entry) == "buy"
    assert long_entry.get("asof_execution").startswith("2026-05-01")


# ---------------------- cs29: ATR-14_at_entry (ADR-0035 §98) ----------------------
#
# ADR-0035 §98: take-profit fires when `pnl_pct > 3 × ATR-14_at_entry`. The field
# SwingContext.atr14_at_entry_pct encodes "at entry", but fetch_mark_atr historically
# computed CURRENT ATR (a 60d window ENDING TODAY). For a position whose volatility
# shifted since entry, current-ATR yields the WRONG take-profit threshold. These tests
# pin (a) the pure window->atr_pct helper, (b) the entry_date wiring on fetch_mark_atr
# with the legacy no-arg path byte-unchanged, and (c) graceful fallback with NO
# fabrication (a missing/short/unparseable entry window degrades to the recent ATR).

import pandas as pd  # noqa: E402


def _bars(n: int, *, hi_lo_range: float, close: float = 200.0):
    """Build an n-bar OHLC frame with a fixed High-Low range per bar (controls ATR)
    and a flat Close. ATR-14 (Wilder simplification used by the script) = mean of the
    last-14 (High-Low) true ranges, so atr_pct = hi_lo_range / close for n>=14."""
    return pd.DataFrame(
        {
            "High": [close + hi_lo_range / 2.0] * n,
            "Low": [close - hi_lo_range / 2.0] * n,
            "Close": [close] * n,
        }
    )


def test_atr_pct_from_bars_low_vs_high_differ(mod):
    """cs29 core: feeding the WRONG window yields the WRONG 3xATR TP threshold.
    A low-vol window and a high-vol window produce different (and ordered) atr_pct."""
    low = mod._atr_pct_from_bars(_bars(20, hi_lo_range=2.0, close=200.0))   # 0.01
    high = mod._atr_pct_from_bars(_bars(20, hi_lo_range=8.0, close=200.0))  # 0.04
    assert low is not None and high is not None
    assert low != high
    assert low < high
    assert abs(low - 0.01) < 1e-9
    assert abs(high - 0.04) < 1e-9


def test_atr_pct_from_bars_guards(mod):
    """cs29: None for empty / <15-bar / non-positive close frames (no fabrication)."""
    assert mod._atr_pct_from_bars(pd.DataFrame({"High": [], "Low": [], "Close": []})) is None
    assert mod._atr_pct_from_bars(_bars(14, hi_lo_range=2.0)) is None  # <15 bars
    assert mod._atr_pct_from_bars(_bars(20, hi_lo_range=2.0, close=0.0)) is None
    assert mod._atr_pct_from_bars(None) is None


class _FakeTicker:
    """A yfinance.Ticker stand-in. history(period=...) returns the RECENT (today-
    ending) frame; history(start=,end=) returns the ENTRY-window frame — distinguished
    by which kwargs are present, mirroring the two real call shapes."""

    def __init__(self, symbol, *, recent, entry):
        self.symbol = symbol
        self._recent = recent
        self._entry = entry

    def history(self, *, period=None, start=None, end=None, interval="1d", auto_adjust=False):
        if start is not None or end is not None:
            return self._entry
        return self._recent


class _FakeYF:
    def __init__(self, *, recent, entry):
        self._recent = recent
        self._entry = entry

    def Ticker(self, symbol):  # noqa: N802 — mirrors yfinance's real capitalized API
        return _FakeTicker(symbol, recent=self._recent, entry=self._entry)


def _install_fake_yf(monkeypatch, *, recent, entry):
    import sys
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF(recent=recent, entry=entry))


def test_fetch_mark_atr_legacy_no_arg_uses_recent(mod, monkeypatch):
    """cs29: the no-arg call is byte-unchanged — it returns the RECENT (today-ending)
    ATR. recent=HIGH vol, entry-window=LOW vol; with no entry_date we get HIGH."""
    recent = _bars(60, hi_lo_range=8.0, close=200.0)   # 0.04
    entry = _bars(20, hi_lo_range=2.0, close=200.0)     # 0.01
    _install_fake_yf(monkeypatch, recent=recent, entry=entry)
    mark, atr = mod.fetch_mark_atr("AAPL")
    assert mark == 200.0
    assert atr is not None and abs(atr - 0.04) < 1e-9


def test_fetch_mark_atr_entry_date_uses_entry_window(mod, monkeypatch):
    """cs29 the bug: with entry_date set we anchor the ATR to the ENTRY window (LOW),
    NOT today's (HIGH). atr_entry != atr_recent; the mark stays the recent mark."""
    recent = _bars(60, hi_lo_range=8.0, close=200.0)   # 0.04
    entry = _bars(20, hi_lo_range=2.0, close=200.0)     # 0.01
    _install_fake_yf(monkeypatch, recent=recent, entry=entry)
    mark_recent, atr_recent = mod.fetch_mark_atr("AAPL")
    mark_entry, atr_entry = mod.fetch_mark_atr("AAPL", entry_date="2026-01-05T00:00:00+00:00")
    assert atr_entry is not None and abs(atr_entry - 0.01) < 1e-9
    assert atr_entry != atr_recent
    assert mark_entry == mark_recent == 200.0


def test_fetch_mark_atr_falls_back_when_entry_window_empty(mod, monkeypatch):
    """cs29 fallback (NO fabrication): an entry window that is empty/<15 bars falls
    back to the RECENT atr rather than inventing a number or returning None-atr."""
    recent = _bars(60, hi_lo_range=8.0, close=200.0)   # 0.04
    _install_fake_yf(monkeypatch, recent=recent, entry=_bars(5, hi_lo_range=2.0))  # too short
    _, atr = mod.fetch_mark_atr("AAPL", entry_date="2010-01-05T00:00:00+00:00")
    assert atr is not None and abs(atr - 0.04) < 1e-9  # recent fallback

    _install_fake_yf(monkeypatch, recent=recent,
                     entry=pd.DataFrame({"High": [], "Low": [], "Close": []}))
    _, atr2 = mod.fetch_mark_atr("AAPL", entry_date="2010-01-05T00:00:00+00:00")
    assert atr2 is not None and abs(atr2 - 0.04) < 1e-9


def test_fetch_mark_atr_unparseable_entry_date_uses_recent(mod, monkeypatch):
    """cs29: an unparseable entry_date never raises — it degrades to the recent atr."""
    recent = _bars(60, hi_lo_range=8.0, close=200.0)   # 0.04
    entry = _bars(20, hi_lo_range=2.0, close=200.0)     # 0.01
    _install_fake_yf(monkeypatch, recent=recent, entry=entry)
    _, atr = mod.fetch_mark_atr("AAPL", entry_date="not-a-date")
    assert atr is not None and abs(atr - 0.04) < 1e-9


def test_fetch_mark_atr_entry_date_none_byte_identical_to_no_arg(mod, monkeypatch):
    """cs29: entry_date=None is byte-identical to the no-arg call (legacy invariance)."""
    recent = _bars(60, hi_lo_range=8.0, close=200.0)
    entry = _bars(20, hi_lo_range=2.0, close=200.0)
    _install_fake_yf(monkeypatch, recent=recent, entry=entry)
    assert mod.fetch_mark_atr("AAPL", entry_date=None) == mod.fetch_mark_atr("AAPL")


def test_fetch_mark_atr_stable_vol_single_fill_long_unchanged(mod, monkeypatch):
    """cs29 regression: when vol is stable (entry window vol == recent window vol),
    the entry-anchored atr equals the recent atr — a single-fill long whose entry is
    ~now sees a take-profit threshold that is ~unchanged from the legacy behavior."""
    recent = _bars(60, hi_lo_range=6.0, close=200.0)   # 0.03
    entry = _bars(20, hi_lo_range=6.0, close=200.0)     # 0.03 (same vol regime)
    _install_fake_yf(monkeypatch, recent=recent, entry=entry)
    _, atr_recent = mod.fetch_mark_atr("AAPL")
    _, atr_entry = mod.fetch_mark_atr("AAPL", entry_date="2026-06-12T00:00:00+00:00")
    assert atr_entry == atr_recent


def test_parse_entry_date_robust(mod):
    """cs29: _parse_entry_date Z-normalizes, returns a date on success, None (never
    raises) on junk/None/non-str."""
    import datetime as _dt
    assert mod._parse_entry_date("2026-01-05T00:00:00Z") == _dt.date(2026, 1, 5)
    assert mod._parse_entry_date("2026-01-05T00:00:00+00:00") == _dt.date(2026, 1, 5)
    assert mod._parse_entry_date("not-a-date") is None
    assert mod._parse_entry_date(None) is None
    assert mod._parse_entry_date(1234567890) is None


# ---------------------- cs30: days_between_iso iso-guard ----------------------
#
# days_between_iso wraps `asof_iso.replace("Z","+00:00")` then fromisoformat ONLY in
# `except ValueError`. A non-str asof (numeric/None/Timestamp) makes .replace raise
# AttributeError, or fromisoformat raise TypeError — NEITHER is caught today, so the
# exception propagates out of the per-asset loop (run_weekly has no per-asset
# try/except) into main()'s catch-all -> weekly_uncaught_exception, return 1: NO
# decisions for the WHOLE run. The fix widens the guard to also catch TypeError/
# AttributeError so one bad asof degrades to days_held=0 for that single position.

def test_days_between_iso_numeric_asof_does_not_raise(mod):
    """cs30: a numeric asof (today raises AttributeError on .replace) -> 0, no raise."""
    now_dt = mod.datetime(2026, 6, 14, tzinfo=mod.UTC)
    assert mod.days_between_iso(1234567890, now_dt) == 0


def test_days_between_iso_none_asof_does_not_raise(mod):
    """cs30: a None asof -> 0, no raise (today AttributeError on None.replace)."""
    now_dt = mod.datetime(2026, 6, 14, tzinfo=mod.UTC)
    assert mod.days_between_iso(None, now_dt) == 0


def test_days_between_iso_timestamp_asof_does_not_raise(mod):
    """cs30: a pandas/datetime Timestamp asof -> 0, no raise (today TypeError/
    AttributeError path)."""
    now_dt = mod.datetime(2026, 6, 14, tzinfo=mod.UTC)
    ts = pd.Timestamp("2026-06-08T00:00:00+00:00")
    assert mod.days_between_iso(ts, now_dt) == 0
    # A bare datetime instance also has no .replace(str,str) -> guarded to 0.
    assert mod.days_between_iso(mod.datetime(2026, 6, 8, tzinfo=mod.UTC), now_dt) == 0


def test_days_between_iso_str_happy_path_byte_identical(mod):
    """cs30: the str-ISO happy path is byte-identical — '2026-06-08' to '2026-06-14'
    is still 6 calendar days (the guard widening must not touch the success path)."""
    now_dt = mod.datetime(2026, 6, 14, tzinfo=mod.UTC)
    assert mod.days_between_iso("2026-06-08T00:00:00+00:00", now_dt) == 6
    assert mod.days_between_iso("2026-06-08T00:00:00Z", now_dt) == 6
