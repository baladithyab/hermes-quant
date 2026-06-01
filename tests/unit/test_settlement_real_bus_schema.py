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
