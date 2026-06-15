"""335e — a deferred exit must settle once a valid earlier opener arrives.

Ported from research/temp/inc-settle/probe.py (the RED proof).

THE MONEY BUG (335e): ``join_exit_fills`` GATES the live kill-switch
(autonomous.py:280/296/445/446 -> trip_kill_switch). A round-trip it OMITS lets
``compute_cumulative_realized_pnl_pct`` UNDERCOUNT a realized loss, so the rail
can FAIL TO TRIP on a real loss = a fail-OPEN in a capital-preservation rail.

The defect: an exit whose only opposing lots opened LATER (carry-in lookahead)
is correctly DEFERRED into a namespaced ``("_deferred", *bucket)`` key. But the
old carry-in loop copied that deferred lot forward VERBATIM and never re-fed it
into the matching loop. So a deferred exit could NEVER settle even when a valid
EARLIER opening lot arrived in a later incremental call — its realized P&L was
permanently omitted from the kill-switch basis.

The fix (settlement_loop.py, 335e): re-express the deferred exit AND the real
position lots that share its bucket as records, merge them with this call's new
records, RE-SORT by the asof-honest key, and run the existing matching loop. An
earlier valid opener now settles the deferred exit; one still without an honest
opener re-defers (idempotent). The one-direction invariant + namespaced re-defer
are unchanged.

Offline-deterministic: synthetic in-memory records, no network, no clock.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.daemon.settlement_loop import join_exit_fills

_BUCKET = ("paper-default", "equity", "AAPL")
_DEFERRED = ("_deferred", *_BUCKET)


def _carry_with_future_buy() -> dict:
    """Carry-in holding a buy lot opened LATER (t=20:00) than the exit to come."""
    return {
        _BUCKET: [
            {
                "asset": "AAPL",
                "account_id": "paper-default",
                "asset_class": "equity",
                "side": "buy",
                "qty": 10.0,
                "price": 100.0,
                "asof": pd.Timestamp("2026-06-14T20:00:00Z"),
                "exec_id": "late-buy",
                "signal_id": "sig-buy",
                "fee_per_unit": 0.0,
            }
        ]
    }


def _exit_at_t10() -> dict:
    """A sell exit at t=10:00 — EARLIER than the carry-in buy -> must defer."""
    return {
        "asset": "AAPL",
        "asset_class": "equity",
        "account_id": "paper-default",
        "fill_size_pct": -10.0,
        "fill_price": 110.0,
        "asof_execution": "2026-06-14T10:00:00Z",
        "proposal_id": "exit-1",
        "signal_id": "sig-exit",
    }


def _early_buy_at_t05() -> dict:
    """A VALID opening buy at t=05:00 — earlier than the deferred sell at t=10."""
    return {
        "asset": "AAPL",
        "asset_class": "equity",
        "account_id": "paper-default",
        "fill_size_pct": 10.0,
        "fill_price": 100.0,
        "asof_execution": "2026-06-14T05:00:00Z",
        "proposal_id": "early-buy",
        "signal_id": "sig-early-buy",
    }


class TestDeferredExitDrainsAndSettles:
    def test_call1_defers_the_lookahead_exit(self):
        # CALL 1: the exit at t=10 cannot honestly close the carry-in buy at
        # t=20 (lookahead) -> it is DEFERRED. (Unchanged pre-existing behavior.)
        rts1, open1 = join_exit_fills([_exit_at_t10()], open_lots=_carry_with_future_buy())
        assert rts1 == []
        assert _DEFERRED in open1
        assert open1[_DEFERRED][0]["side"] == "sell"
        assert open1[_DEFERRED][0]["qty"] == pytest.approx(10.0)
        # The future buy lot is untouched and one-direction.
        assert open1[_BUCKET][0]["side"] == "buy"

    def test_call2_earlier_opener_settles_the_deferred_exit(self):
        # 335e GREEN: a valid EARLIER opening buy (t=05) arrives. The deferred
        # sell (t=10) must finally settle: (110-100)/100 = +0.10.
        #
        # BEFORE THE FIX this returned 0 round-trips forever — the deferred sell
        # was copied forward verbatim and never re-matched (the fail-OPEN that
        # let the kill-switch undercount the round-trip's realized P&L).
        _, open1 = join_exit_fills([_exit_at_t10()], open_lots=_carry_with_future_buy())
        rts2, open2 = join_exit_fills([_early_buy_at_t05()], open_lots=open1)

        assert len(rts2) == 1, "the deferred exit must now settle against the earlier buy"
        rt = rts2[0]
        assert rt.side == "buy"  # the opener (the t=05 buy) is the entry lot
        assert rt.entry_signal_id == "sig-early-buy"
        assert rt.exit_signal_id == "sig-exit"
        assert rt.entry_price == pytest.approx(100.0)
        assert rt.exit_price == pytest.approx(110.0)
        assert rt.realized_return == pytest.approx(0.10)
        assert rt.asof_entry == pd.Timestamp("2026-06-14T05:00:00Z")
        assert rt.asof_exit == pd.Timestamp("2026-06-14T10:00:00Z")
        assert rt.asof_exit >= rt.asof_entry  # asof-honest

        # The deferred bucket is drained (the exit settled).
        assert not [k for k in open2 if k and k[0] == "_deferred"]
        # The still-future buy (t=20) survives as a single one-direction open lot.
        assert open2[_BUCKET][0]["side"] == "buy"
        assert open2[_BUCKET][0]["qty"] == pytest.approx(10.0)
        assert open2[_BUCKET][0]["price"] == pytest.approx(100.0)
        assert {lot["side"] for lot in open2[_BUCKET]} == {"buy"}

    def test_call2_no_honest_opener_re_defers_idempotently(self):
        # Idempotency: if CALL 2 brings only ANOTHER later (lookahead) buy and no
        # earlier opener, the deferred sell must RE-DEFER (not settle, not vanish,
        # not fabricate a phantom). Run twice -> the deferred state is stable.
        _, open1 = join_exit_fills([_exit_at_t10()], open_lots=_carry_with_future_buy())
        # CALL 2: a buy at t=30 (still LATER than the deferred sell at t=10).
        later_buy = {
            "asset": "AAPL",
            "asset_class": "equity",
            "account_id": "paper-default",
            "fill_size_pct": 10.0,
            "fill_price": 105.0,
            "asof_execution": "2026-06-14T23:00:00Z",
            "proposal_id": "later-buy",
            "signal_id": "sig-later-buy",
        }
        rts2, open2 = join_exit_fills([later_buy], open_lots=open1)
        assert rts2 == [], "no honest earlier opener -> nothing settles"
        assert _DEFERRED in open2
        assert open2[_DEFERRED][0]["side"] == "sell"
        assert open2[_DEFERRED][0]["qty"] == pytest.approx(10.0)
        # Real bucket still one-direction (buy lots only).
        assert {lot["side"] for lot in open2[_BUCKET]} == {"buy"}

        # CALL 3: feed an empty stream -> still idempotent (re-defers, no churn).
        rts3, open3 = join_exit_fills([], open_lots=open2)
        assert rts3 == []
        assert _DEFERRED in open3
        assert open3[_DEFERRED][0]["qty"] == pytest.approx(10.0)
