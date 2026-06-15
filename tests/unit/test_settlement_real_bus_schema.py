"""Tests for settlement v0.1.2 against the REAL executions.jsonl bus schema.

Review-team-3 e8b9 hardening: ``join_exit_fills`` originally read
``rec['side']`` / ``['qty']`` / ``['asof']`` / ``['exec_id']`` / ``['fees']``,
none of which exist on the real bus. The real records are the ExecutionRecord
schema serialized by ``hermes_quant.react.paper._record_to_dict`` — fields are
``asset``, ``asset_class``, ``asof_execution``, ``target_position_pct``,
``fill_price``, ``fill_size_pct`` (signed NAV fraction), ``signal_id``,
``proposal_id`` … The bus->lot adapter (``_normalize_exec_record``) must derive
the lot side/qty/asof from those.

Covers the five defects:
  (1) real-bus shape produces a correct, non-empty round trip;
  (2) same-asof open-before-close ordering is by bus index, not lexical id;
  (3) a carried-in lot that opened LATER than an arriving exit DEFERS the exit
      (never opens an opposing residual that mixes directions in one bucket);
  (4) tz-mixed asof (aware "Z" + naive carry-in) compares without TypeError;
  (5) compute_horizon_return's qty param agrees with join_exit_fills' fee drag.

Offline-deterministic: synthetic in-memory records, no network, no clock.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.daemon.settlement_loop import (
    compute_horizon_return,
    join_exit_fills,
    realized_returns_by_signal,
)
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import _record_to_dict


def _bus_rec(
    *,
    proposal_id: str,
    fill_size_pct: float,
    fill_price: float,
    asof_execution: str,
    asset: str = "BTC/USDT",
    asset_class: str = "crypto",
    signal_id: str | None = None,
    account_id: str | None = None,
    fees: float | None = None,
) -> dict:
    """A record shaped EXACTLY like react.paper._record_to_dict emits.

    Optionally injects account_id / fees into reactor_metadata the way a live
    reactor (or the PaperReactor's PortfolioState shim) would.
    """
    rmeta: dict = {"paper": True}
    if account_id is not None:
        rmeta["account_id"] = account_id
    if fees is not None:
        rmeta["fees"] = fees
    rec = ExecutionRecord(
        proposal_id=proposal_id,
        signal_id=signal_id,
        asset=asset,
        asset_class=asset_class,
        timeframe="1h",
        asof_decision=asof_execution,
        asof_execution=asof_execution,
        target_position_pct=fill_size_pct,
        decision_price=fill_price,
        fill_price=fill_price,
        fill_size_pct=fill_size_pct,
        reactor_name="paper",
        human_in_the_loop=True,
        reactor_metadata=rmeta,
    )
    return _record_to_dict(rec)


# ---------------------------------------------------------------------------
# Defect (1): real-bus schema yields a correct, non-empty round trip
# ---------------------------------------------------------------------------


class TestRealBusSchema:
    def test_long_round_trip_from_record_to_dict_shape(self):
        # +5% NAV long opened, then closed by a -5% NAV fill (sign flip = exit).
        recs = [
            _bus_rec(proposal_id="p-entry", fill_size_pct=+0.05, fill_price=100.0,
                     asof_execution="2026-05-13T14:00:00Z", signal_id="sig-A"),
            _bus_rec(proposal_id="p-exit", fill_size_pct=-0.05, fill_price=110.0,
                     asof_execution="2026-05-13T18:00:00Z", signal_id="sig-B"),
        ]
        # Sanity: the keys the OLD code looked for genuinely do not exist.
        assert "side" not in recs[0]
        assert "qty" not in recs[0]
        assert "exec_id" not in recs[0]
        assert "asof" not in recs[0]

        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 1
        rt = trips[0]
        assert rt.side == "buy"  # +fill_size_pct -> long lot
        assert rt.qty == pytest.approx(0.05)  # magnitude of the NAV fraction
        assert rt.entry_price == pytest.approx(100.0)
        assert rt.exit_price == pytest.approx(110.0)
        assert rt.realized_return == pytest.approx(0.10)  # 100 -> 110
        assert rt.entry_signal_id == "sig-A"
        assert rt.exit_signal_id == "sig-B"
        # asof comes from asof_execution (the fill time).
        assert rt.asof_entry == pd.Timestamp("2026-05-13T14:00:00Z")
        assert rt.asof_exit == pd.Timestamp("2026-05-13T18:00:00Z")
        assert rt.asof_exit >= rt.asof_entry
        assert open_lots == {}

    def test_short_round_trip_from_real_shape(self):
        recs = [
            _bus_rec(proposal_id="p1", fill_size_pct=-0.10, fill_price=100.0,
                     asof_execution="2026-05-13T14:00:00Z", signal_id="sig-S"),
            _bus_rec(proposal_id="p2", fill_size_pct=+0.10, fill_price=95.0,
                     asof_execution="2026-05-13T16:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 1
        assert trips[0].side == "sell"  # short entry
        assert trips[0].realized_return == pytest.approx(0.05)  # short 100 -> cover 95
        assert open_lots == {}

    def test_account_id_and_fees_from_reactor_metadata(self):
        recs = [
            _bus_rec(proposal_id="p1", fill_size_pct=+1.0, fill_price=100.0,
                     asof_execution="2026-05-13T14:00:00Z",
                     account_id="freqtrade", fees=1.0, signal_id="sig-A"),
            _bus_rec(proposal_id="p2", fill_size_pct=-1.0, fill_price=110.0,
                     asof_execution="2026-05-13T18:00:00Z",
                     account_id="freqtrade", fees=1.0),
        ]
        trips, _ = join_exit_fills(recs)
        rt = trips[0]
        assert rt.account_id == "freqtrade"
        # gross 0.10; fee drag = (1+1)/(100*1) = 0.02 -> 0.08
        assert rt.realized_return == pytest.approx(0.08)
        assert rt.fees == pytest.approx(2.0)

    def test_default_account_id_when_absent(self):
        recs = [
            _bus_rec(proposal_id="p1", fill_size_pct=+0.05, fill_price=100.0,
                     asof_execution="2026-05-13T14:00:00Z"),
        ]
        _, open_lots = join_exit_fills(recs)
        # No account_id anywhere -> bus default "paper-default".
        assert ("paper-default", "crypto", "BTC/USDT") in open_lots

    def test_zero_size_reject_record_skipped(self):
        # An admissibility REJECT stamps fill_size_pct=0.0; not a lot.
        recs = [
            _bus_rec(proposal_id="p-reject", fill_size_pct=0.0, fill_price=100.0,
                     asof_execution="2026-05-13T14:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)
        assert trips == []
        assert open_lots == {}


# ---------------------------------------------------------------------------
# Defect (2): same-asof open-before-close ordering is by bus index, not id
# ---------------------------------------------------------------------------


class TestSameAsofOrdering:
    def test_same_asof_buy_then_sell_records_buy_side(self):
        # buy@100 then sell@110 at the SAME asof. The exit's proposal_id sorts
        # BEFORE the entry's lexically — the old exec_id-first tie-break would
        # treat the sell as the opening lot (recording side='sell', a -9.09%
        # return). Bus index must keep the buy as the opening lot.
        same = "2026-05-13T14:00:00Z"
        recs = [
            # entry first in bus order, but id "zzz" sorts AFTER exit id "aaa".
            _bus_rec(proposal_id="zzz-entry", fill_size_pct=+1.0, fill_price=100.0,
                     asof_execution=same, signal_id="sig-A"),
            _bus_rec(proposal_id="aaa-exit", fill_size_pct=-1.0, fill_price=110.0,
                     asof_execution=same, signal_id="sig-B"),
        ]
        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 1
        rt = trips[0]
        assert rt.side == "buy"           # NOT inverted to sell
        assert rt.realized_return == pytest.approx(0.10)  # NOT -9.09%
        assert rt.entry_signal_id == "sig-A"
        assert rt.exit_signal_id == "sig-B"
        assert open_lots == {}


# ---------------------------------------------------------------------------
# Defect (3): carried-in lot opened LATER -> defer the exit, never mix sides
# ---------------------------------------------------------------------------


class TestDeferredExitNoPhantomLot:
    def test_later_carry_in_lot_defers_exit(self):
        # Carry-in: an open LONG lot whose asof is 18:00 (in the "future").
        # An arriving SELL at 16:00 cannot honestly close it (lookahead). The
        # old code fell through and opened a SHORT residual in the SAME bucket
        # as the future long -> a mixed-direction queue. We must DEFER instead.
        recs1 = [
            _bus_rec(proposal_id="p-late-long", fill_size_pct=+1.0, fill_price=100.0,
                     asof_execution="2026-05-13T18:00:00Z", signal_id="sig-late"),
        ]
        _, open1 = join_exit_fills(recs1)
        bucket = ("paper-default", "crypto", "BTC/USDT")
        assert open1[bucket][0]["side"] == "buy"

        recs2 = [
            _bus_rec(proposal_id="p-early-sell", fill_size_pct=-1.0, fill_price=90.0,
                     asof_execution="2026-05-13T16:00:00Z", signal_id="sig-early"),
        ]
        trips2, open2 = join_exit_fills(recs2, open_lots=open1)

        # No lookahead pairing was fabricated.
        assert trips2 == []
        # The original long lot is untouched and still one-direction.
        assert open2[bucket][0]["side"] == "buy"
        assert open2[bucket][0]["qty"] == pytest.approx(1.0)
        # The exit was DEFERRED into a namespaced key, not enqueued as an
        # opposing residual in the real bucket (which would mix directions).
        deferred_key = ("_deferred", *bucket)
        assert deferred_key in open2
        assert open2[deferred_key][0]["side"] == "sell"
        assert open2[deferred_key][0]["qty"] == pytest.approx(1.0)
        # The real bucket holds exactly one direction (invariant).
        assert {lot["side"] for lot in open2[bucket]} == {"buy"}


# ---------------------------------------------------------------------------
# Defect (4): tz-mixed asof (aware + naive carry-in) compares without error
# ---------------------------------------------------------------------------


class TestTzNormalization:
    def test_naive_carry_in_compares_with_aware_exit(self):
        # Build a carry-in lot with a NAIVE asof (no tz) to mimic an older
        # record. The arriving exit carries a tz-aware "Z" asof. Comparing the
        # two at the asof-honesty check must NOT raise TypeError.
        bucket = ("paper-default", "crypto", "BTC/USDT")
        carry_in = {
            bucket: [
                {
                    "asset": "BTC/USDT",
                    "account_id": "paper-default",
                    "asset_class": "crypto",
                    "side": "buy",
                    "qty": 1.0,
                    "price": 100.0,
                    # NAIVE timestamp (no tzinfo) — the defect trigger.
                    "asof": pd.Timestamp("2026-05-13T14:00:00"),
                    "exec_id": "p-old",
                    "signal_id": "sig-old",
                    "fee_per_unit": 0.0,
                }
            ]
        }
        recs = [
            _bus_rec(proposal_id="p-exit", fill_size_pct=-1.0, fill_price=110.0,
                     asof_execution="2026-05-13T18:00:00Z"),
        ]
        # Must not raise TypeError: can't compare naive vs aware.
        trips, open_lots = join_exit_fills(recs, open_lots=carry_in)
        assert len(trips) == 1
        assert trips[0].realized_return == pytest.approx(0.10)
        assert open_lots == {}

    def test_offset_and_z_normalize_to_same_instant(self):
        # 18:00Z entry vs 19:00+01:00 exit are the SAME instant; the exit must
        # still be allowed to close the entry (asof_exit >= asof_entry holds
        # after UTC normalization).
        recs = [
            _bus_rec(proposal_id="p-entry", fill_size_pct=+1.0, fill_price=100.0,
                     asof_execution="2026-05-13T18:00:00Z", signal_id="sig-A"),
            _bus_rec(proposal_id="p-exit", fill_size_pct=-1.0, fill_price=110.0,
                     asof_execution="2026-05-13T19:00:00+01:00"),
        ]
        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 1
        assert trips[0].realized_return == pytest.approx(0.10)
        assert open_lots == {}


# ---------------------------------------------------------------------------
# Defect (5): compute_horizon_return qty param agrees with join_exit_fills
# ---------------------------------------------------------------------------


class TestFeeConventionAgreement:
    def test_qty_param_matches_join_fee_drag(self):
        # Same lot computed two ways: through join_exit_fills (which divides
        # fees by entry_price*qty) and through compute_horizon_return with the
        # matching qty. They must agree.
        qty = 0.05
        recs = [
            _bus_rec(proposal_id="p1", fill_size_pct=+qty, fill_price=100.0,
                     asof_execution="2026-05-13T14:00:00Z",
                     fees=0.5, signal_id="sig-A"),
            _bus_rec(proposal_id="p2", fill_size_pct=-qty, fill_price=110.0,
                     asof_execution="2026-05-13T18:00:00Z", fees=0.5),
        ]
        trips, _ = join_exit_fills(recs)
        joined = trips[0].realized_return
        total_fees = trips[0].fees  # prorated entry+exit fees = 1.0
        helper = compute_horizon_return(100.0, 110.0, "buy", fees=total_fees, qty=qty)
        assert helper == pytest.approx(joined)
        # Explicit value: gross 0.10, fee drag = 1.0/(100*0.05) = 0.20 -> -0.10
        assert joined == pytest.approx(0.10 - 0.20)

    def test_default_qty_is_one_unit(self):
        # Backward-compat: default qty=1.0 keeps the old single-unit math.
        assert compute_horizon_return(100.0, 110.0, "buy", fees=1.0) == pytest.approx(0.09)

    def test_nonpositive_qty_returns_none(self):
        assert compute_horizon_return(100.0, 110.0, "buy", qty=0.0) is None
        assert compute_horizon_return(100.0, 110.0, "buy", qty=-1.0) is None


# ---------------------------------------------------------------------------
# realized_returns_by_signal still keys on entry_signal_id under real shape
# ---------------------------------------------------------------------------


def test_realized_returns_by_signal_real_shape():
    recs = [
        _bus_rec(proposal_id="p1", fill_size_pct=+1.0, fill_price=100.0,
                 asof_execution="2026-05-13T14:00:00Z", signal_id="sig-1"),
        _bus_rec(proposal_id="p2", fill_size_pct=-1.0, fill_price=110.0,
                 asof_execution="2026-05-13T18:00:00Z", signal_id="sig-x"),
    ]
    trips, _ = join_exit_fills(recs)
    realized = realized_returns_by_signal(trips)
    assert realized == {"sig-1": pytest.approx(0.10)}


# ---------------------------------------------------------------------------
# i0c: the settlement FIFO must agree with the position fold on realized P&L.
#
# join_exit_fills' realized P&L is a LIVE money-safety input: it feeds the
# kill-switch (autonomous.py:296 -> compute_cumulative_realized_pnl_pct ->
# :445 `_cum_pnl = ...` -> :446 `if _cum_pnl <= -kill_switch_pct: trip`). The
# position fold (reconstruct_from, under HERMES_QUANT_DELTA_NORMALIZER) feeds
# the gate NAV. Under the production absolute-target ExecutionRecord schema,
# fill_size_pct is the ABSOLUTE post-fill target, not a traded delta — a
# re-affirmation re-stamps the SAME target. The raw FIFO read that field as a
# delta and double-counted re-affirmations. Under the flag the FIFO runs the
# SAME FillDeltaNormalizer pre-pass the position fold uses (cs85 single-net),
# so realized P&L matches the position fold on the same stream.
# ---------------------------------------------------------------------------


class TestI0cFifoMatchesPositionFoldUnderNormalizerFlag:
    def test_fifo_matches_position_fold_under_normalizer_flag(self, monkeypatch):
        # Single-lane absolute-target stream: open +0.10 @100, re-affirm +0.10
        # @105 (NO trade), re-affirm +0.10 @120 (NO trade), flatten to 0.0 @90.
        # The position fold normalizer yields deltas [+0.10, 0, 0, -0.10] -> ONE
        # 10%-long round-trip entry@100 -> exit@90 = a -10% realized LOSS. Under
        # the flag the FIFO must book the SAME single -10% round-trip (the loss
        # the kill-switch must see), not three phantom buy lots.
        monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
        recs = [
            _bus_rec(proposal_id="t1", fill_size_pct=+0.10, fill_price=100.0,
                     asof_execution="2026-06-14T01:00:00Z", signal_id="s1"),
            _bus_rec(proposal_id="t2", fill_size_pct=+0.10, fill_price=105.0,
                     asof_execution="2026-06-14T02:00:00Z", signal_id="s1"),
            _bus_rec(proposal_id="t3", fill_size_pct=+0.10, fill_price=120.0,
                     asof_execution="2026-06-14T03:00:00Z", signal_id="s1"),
            _bus_rec(proposal_id="t4", fill_size_pct=0.0, fill_price=90.0,
                     asof_execution="2026-06-14T04:00:00Z", signal_id="s2"),
        ]
        trips, open_lots = join_exit_fills(recs)
        # Exactly ONE round-trip: the re-affirmations folded to delta 0 and were
        # dropped; the flatten-to-0 became the closing delta.
        assert len(trips) == 1
        rt = trips[0]
        assert rt.side == "buy"
        assert rt.qty == pytest.approx(0.10)
        assert rt.entry_price == pytest.approx(100.0)
        assert rt.exit_price == pytest.approx(90.0)
        # The -10% loss the position fold books -> the kill-switch input matches.
        assert rt.realized_return == pytest.approx(-0.10)
        assert open_lots == {}

    def test_normalizer_off_is_byte_identical(self):
        # Flag UNSET (production default): the SAME absolute-target re-affirmation
        # stream is read RAW (fill_size_pct as a delta), so the three +0.10
        # re-affirmations stack as three open buy lots and the 0.0 flatten is a
        # zero-size record that is dropped -> 0 round-trips, three stacked lots.
        # Pins the OFF default path so a future edit cannot silently change it.
        recs = [
            _bus_rec(proposal_id="t1", fill_size_pct=+0.10, fill_price=100.0,
                     asof_execution="2026-06-14T01:00:00Z", signal_id="s1"),
            _bus_rec(proposal_id="t2", fill_size_pct=+0.10, fill_price=105.0,
                     asof_execution="2026-06-14T02:00:00Z", signal_id="s1"),
            _bus_rec(proposal_id="t3", fill_size_pct=+0.10, fill_price=120.0,
                     asof_execution="2026-06-14T03:00:00Z", signal_id="s1"),
            _bus_rec(proposal_id="t4", fill_size_pct=0.0, fill_price=90.0,
                     asof_execution="2026-06-14T04:00:00Z", signal_id="s2"),
        ]
        trips, open_lots = join_exit_fills(recs)
        assert trips == []
        bucket = ("paper-default", "crypto", "BTC/USDT")
        assert len(open_lots[bucket]) == 3
        assert {lot["side"] for lot in open_lots[bucket]} == {"buy"}
        assert [lot["qty"] for lot in open_lots[bucket]] == pytest.approx([0.10, 0.10, 0.10])


# ---------------------------------------------------------------------------
# 335e: a DEFERRED exit must be able to settle later against a valid earlier
# opening lot. The e8b9 fix (wave-9) deferred an unmatched exit under a
# namespaced ("_deferred", *bucket) key instead of fabricating an opposing
# residual lot — honest, but the deferred exit was only COPIED FORWARD verbatim
# on subsequent calls and never re-fed into the matching loop, so it could
# NEVER settle even when a valid opening lot later arrived. The realized P&L
# permanently OMITTED a legitimately-matchable round-trip, so the kill-switch
# _cum_pnl undercounted a real loss. The fix DRAINS the deferred queue back
# into the matching stream so a later valid opener settles it.
# ---------------------------------------------------------------------------


class TestDeferredExitDrainAndSettle:
    _bucket = ("paper-default", "equity", "AAPL")

    def _defer_call(self):
        # CALL 1: carry-in a LATE buy@20:00 (lookahead); feed a sell exit@10:00.
        # The exit cannot honestly close the later buy, so it DEFERS (the
        # unchanged e8b9 behavior — pinned here).
        carry = {
            self._bucket: [
                {"asset": "AAPL", "account_id": "paper-default",
                 "asset_class": "equity", "side": "buy", "qty": 10.0,
                 "price": 100.0,
                 "asof": pd.Timestamp("2026-06-14T20:00:00Z"),
                 "exec_id": "late-buy", "signal_id": "sig-buy",
                 "fee_per_unit": 0.0},
            ]
        }
        exit_rec = _bus_rec(
            proposal_id="exit-1", fill_size_pct=-10.0, fill_price=110.0,
            asof_execution="2026-06-14T10:00:00Z",
            asset="AAPL", asset_class="equity", account_id="paper-default",
            signal_id="sig-exit",
        )
        trips1, open1 = join_exit_fills([exit_rec], open_lots=carry)
        assert trips1 == []  # deferred, not settled
        deferred_key = ("_deferred", *self._bucket)
        assert deferred_key in open1
        assert open1[deferred_key][0]["side"] == "sell"
        assert open1[deferred_key][0]["qty"] == pytest.approx(10.0)
        return open1

    def test_deferred_exit_settles_against_later_valid_opening_lot(self):
        open1 = self._defer_call()
        # CALL 2: a VALID buy@100 at t=05:00 arrives — EARLIER than the deferred
        # sell@t=10:00, so it can honestly open and the deferred sell settles
        # against it. (Carry-in still also holds the late buy@t=20:00.)
        valid_buy = _bus_rec(
            proposal_id="early-buy", fill_size_pct=10.0, fill_price=100.0,
            asof_execution="2026-06-14T05:00:00Z",
            asset="AAPL", asset_class="equity", account_id="paper-default",
            signal_id="sig-early-buy",
        )
        trips2, open2 = join_exit_fills([valid_buy], open_lots=open1)
        # The deferred round-trip is now REALIZED — the kill-switch sees it.
        assert len(trips2) == 1
        rt = trips2[0]
        assert rt.side == "buy"
        assert rt.entry_price == pytest.approx(100.0)
        assert rt.exit_price == pytest.approx(110.0)
        assert rt.realized_return == pytest.approx(0.10)
        assert rt.entry_signal_id == "sig-early-buy"
        assert rt.exit_signal_id == "sig-exit"
        # The deferred bucket is DRAINED (no double-count, no lingering exit).
        deferred_key = ("_deferred", *self._bucket)
        assert deferred_key not in open2
        # The late buy@t=20:00 still cannot honestly match a t=05/t=10 stream;
        # it stays OPEN, exactly one direction in the real bucket.
        assert open2[self._bucket][0]["side"] == "buy"
        assert open2[self._bucket][0]["qty"] == pytest.approx(10.0)
        assert {lot["side"] for lot in open2[self._bucket]} == {"buy"}

    def test_deferred_exit_redefers_when_no_honest_opener(self):
        open1 = self._defer_call()
        # CALL 2: a buy@t=15:00 arrives — LATER than the deferred sell@t=10:00,
        # so it STILL cannot honestly close the sell. The drain must RE-DEFER
        # the sell (not lose it, not fabricate a return). Idempotent + honest.
        late_buy = _bus_rec(
            proposal_id="still-late-buy", fill_size_pct=10.0, fill_price=100.0,
            asof_execution="2026-06-14T15:00:00Z",
            asset="AAPL", asset_class="equity", account_id="paper-default",
            signal_id="sig-still-late",
        )
        trips2, open2 = join_exit_fills([late_buy], open_lots=open1)
        assert trips2 == []  # nothing honestly settles
        deferred_key = ("_deferred", *self._bucket)
        # The sell is RE-DEFERRED — still present, still pending, exactly once.
        assert deferred_key in open2
        assert open2[deferred_key][0]["side"] == "sell"
        assert open2[deferred_key][0]["qty"] == pytest.approx(10.0)

    def test_drain_and_normalizer_flag_coexist(self, monkeypatch):
        # Interaction: with the i0c normalizer flag ON, the 335e drain still
        # works. The re-fed deferred records carry EXPLICIT side/qty, so the
        # normalizer pre-pass (which overrides fill_size_pct on this call's
        # records) does not perturb them. The deferred sell still settles
        # against the valid earlier buy.
        monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
        open1 = self._defer_call()
        valid_buy = _bus_rec(
            proposal_id="early-buy", fill_size_pct=10.0, fill_price=100.0,
            asof_execution="2026-06-14T05:00:00Z",
            asset="AAPL", asset_class="equity", account_id="paper-default",
            signal_id="sig-early-buy",
        )
        trips2, open2 = join_exit_fills([valid_buy], open_lots=open1)
        assert len(trips2) == 1
        assert trips2[0].realized_return == pytest.approx(0.10)
        deferred_key = ("_deferred", *self._bucket)
        assert deferred_key not in open2
