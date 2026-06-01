"""Tests for settlement v0.1.2 — exit-fill joining + horizon-return math.

Per ADR-0083 Phase 0b: the settlement loop must join an EXIT fill to its
ENTRY fill (FIFO lot matching) and compute the realized holding-period return
over the hold (entry_price -> exit_price, net of cost), so the reflector +
calibrator can read realized alpha. This is the measurement instrument that
unblocks BMA Beta auto-learning (O6) and any horizon-mode measurement.

Rails pinned here:
  - a paired entry+exit yields the correct realized return;
  - an unpaired / still-open position yields None (NOT a fabricated 0);
  - FIFO lot-matching across multiple entries;
  - asof-honest: an exit only matches entries with asof <= the exit's asof.

Offline-deterministic: synthetic in-memory execution records, no network.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from hermes_quant.daemon.settlement_loop import (
    SettledRoundTrip,
    compute_horizon_return,
    join_exit_fills,
    realized_returns_by_signal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _exec(
    *,
    exec_id: str,
    side: str,
    qty: float,
    fill_price: float,
    asof: str,
    asset: str = "BTC/USDT",
    account_id: str = "freqtrade",
    asset_class: str = "crypto",
    fees: float = 0.0,
    signal_id: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "exec_id": exec_id,
        "asof": asof,
        "asset": asset,
        "side": side,
        "qty": qty,
        "fill_price": fill_price,
        "decision_price": fill_price,
        "fees": fees,
        "account_id": account_id,
        "asset_class": asset_class,
        "signal_id": signal_id,
        "realized_pnl": None,
    }


# ---------------------------------------------------------------------------
# compute_horizon_return — the pure helper
# ---------------------------------------------------------------------------


class TestComputeHorizonReturn:
    def test_long_winner(self):
        # buy 100 -> sell 110 = +10%
        assert compute_horizon_return(100.0, 110.0, "buy") == pytest.approx(0.10)

    def test_long_loser(self):
        # buy 100 -> sell 90 = -10%
        assert compute_horizon_return(100.0, 90.0, "buy") == pytest.approx(-0.10)

    def test_short_winner(self):
        # short (entry side sell) 100 -> cover 90 = +10%
        assert compute_horizon_return(100.0, 90.0, "sell") == pytest.approx(0.10)

    def test_short_loser(self):
        # short 100 -> cover 110 = -10%
        assert compute_horizon_return(100.0, 110.0, "sell") == pytest.approx(-0.10)

    def test_fees_drag(self):
        # buy 100 -> sell 110, fees=1 (per unit notional) -> 0.10 - 0.01
        assert compute_horizon_return(100.0, 110.0, "buy", fees=1.0) == pytest.approx(0.09)

    def test_bad_side_returns_none(self):
        assert compute_horizon_return(100.0, 110.0, "hold") is None

    def test_nonpositive_price_returns_none(self):
        assert compute_horizon_return(0.0, 110.0, "buy") is None
        assert compute_horizon_return(100.0, -1.0, "buy") is None


# ---------------------------------------------------------------------------
# join_exit_fills — paired entry + exit yields the correct realized return
# ---------------------------------------------------------------------------


class TestPairedRoundTrip:
    def test_long_round_trip_realized_return(self):
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-A"),
            _exec(exec_id="x1", side="sell", qty=1.0, fill_price=110.0,
                  asof="2026-05-13T18:00:00Z", signal_id="sig-B"),
        ]
        trips, open_lots = join_exit_fills(recs)

        assert len(trips) == 1
        rt = trips[0]
        assert rt.realized_return == pytest.approx(0.10)
        assert rt.side == "buy"
        assert rt.qty == pytest.approx(1.0)
        assert rt.entry_price == pytest.approx(100.0)
        assert rt.exit_price == pytest.approx(110.0)
        assert rt.entry_signal_id == "sig-A"
        assert rt.exit_signal_id == "sig-B"
        # asof-honest: exit asof >= entry asof.
        assert rt.asof_exit >= rt.asof_entry
        # fully closed -> nothing open.
        assert open_lots == {}

    def test_short_round_trip_realized_return(self):
        recs = [
            _exec(exec_id="e1", side="sell", qty=2.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z"),
            _exec(exec_id="x1", side="buy", qty=2.0, fill_price=95.0,
                  asof="2026-05-13T16:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)

        assert len(trips) == 1
        rt = trips[0]
        # short 100 -> cover 95 = +5%
        assert rt.realized_return == pytest.approx(0.05)
        assert rt.side == "sell"
        assert open_lots == {}

    def test_fees_netted_out(self):
        # entry fee 1.0 + exit fee 1.0 on a 100-notional 1-unit lot.
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", fees=1.0),
            _exec(exec_id="x1", side="sell", qty=1.0, fill_price=110.0,
                  asof="2026-05-13T18:00:00Z", fees=1.0),
        ]
        trips, _ = join_exit_fills(recs)
        rt = trips[0]
        # gross 0.10, fee drag = (1+1)/100 = 0.02 -> 0.08
        assert rt.realized_return == pytest.approx(0.08)
        assert rt.fees == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# unpaired / open position yields None (NOT a fabricated 0)
# ---------------------------------------------------------------------------


class TestUnpairedYieldsNone:
    def test_open_entry_produces_no_round_trip(self):
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-open"),
        ]
        trips, open_lots = join_exit_fills(recs)
        # No exit -> no settled round trip. NOT a fabricated 0.0.
        assert trips == []
        # The open lot is carried out for incremental settlement.
        bucket = ("freqtrade", "crypto", "BTC/USDT")
        assert bucket in open_lots
        assert open_lots[bucket][0]["qty"] == pytest.approx(1.0)

    def test_open_signal_absent_from_realized_map(self):
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-open"),
        ]
        trips, _ = join_exit_fills(recs)
        realized = realized_returns_by_signal(trips)
        # The still-open signal must NOT appear with a fabricated 0.
        assert "sig-open" not in realized
        assert realized == {}

    def test_partial_exit_leaves_residual_open(self):
        recs = [
            _exec(exec_id="e1", side="buy", qty=2.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-A"),
            _exec(exec_id="x1", side="sell", qty=1.0, fill_price=110.0,
                  asof="2026-05-13T18:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)
        # One lot of 1.0 settled; 1.0 still open.
        assert len(trips) == 1
        assert trips[0].qty == pytest.approx(1.0)
        assert trips[0].realized_return == pytest.approx(0.10)
        bucket = ("freqtrade", "crypto", "BTC/USDT")
        assert open_lots[bucket][0]["qty"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# FIFO lot-matching across multiple entries
# ---------------------------------------------------------------------------


class TestFifoMatching:
    def test_fifo_two_entries_one_exit(self):
        # Two entries at different prices, one exit consuming both FIFO.
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-1"),
            _exec(exec_id="e2", side="buy", qty=1.0, fill_price=120.0,
                  asof="2026-05-13T15:00:00Z", signal_id="sig-2"),
            _exec(exec_id="x1", side="sell", qty=2.0, fill_price=130.0,
                  asof="2026-05-13T18:00:00Z", signal_id="sig-x"),
        ]
        trips, open_lots = join_exit_fills(recs)

        assert len(trips) == 2
        # FIFO: first matched lot is the OLDEST entry (e1 @ 100).
        first, second = trips
        assert first.entry_exec_id == "e1"
        assert first.entry_price == pytest.approx(100.0)
        # 100 -> 130 = +30%
        assert first.realized_return == pytest.approx(0.30)
        assert second.entry_exec_id == "e2"
        assert second.entry_price == pytest.approx(120.0)
        # 120 -> 130 = +8.333%
        assert second.realized_return == pytest.approx((130.0 - 120.0) / 120.0)
        assert open_lots == {}

    def test_one_entry_two_partial_exits_fifo(self):
        recs = [
            _exec(exec_id="e1", side="buy", qty=2.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-1"),
            _exec(exec_id="x1", side="sell", qty=1.0, fill_price=110.0,
                  asof="2026-05-13T16:00:00Z"),
            _exec(exec_id="x2", side="sell", qty=1.0, fill_price=90.0,
                  asof="2026-05-13T18:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 2
        # Both halves trace back to the single entry sig-1.
        assert trips[0].realized_return == pytest.approx(0.10)
        assert trips[1].realized_return == pytest.approx(-0.10)
        # Notional-weighted realized return for sig-1: (0.10 + -0.10)/2 = 0.0
        # NOTE this is a real 0.0 (both halves closed), distinct from "open".
        realized = realized_returns_by_signal(trips)
        assert realized["sig-1"] == pytest.approx(0.0)
        assert open_lots == {}

    def test_fifo_ordering_independent_of_input_order(self):
        # Same fills, shuffled input order; asof-sort must restore FIFO.
        recs = [
            _exec(exec_id="x1", side="sell", qty=2.0, fill_price=130.0,
                  asof="2026-05-13T18:00:00Z"),
            _exec(exec_id="e2", side="buy", qty=1.0, fill_price=120.0,
                  asof="2026-05-13T15:00:00Z"),
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z"),
        ]
        trips, _ = join_exit_fills(recs)
        assert [t.entry_exec_id for t in trips] == ["e1", "e2"]


# ---------------------------------------------------------------------------
# asof-honesty: an exit cannot match an entry that opened "after" it
# ---------------------------------------------------------------------------


class TestAsofHonesty:
    def test_exit_does_not_match_later_entry(self):
        # A sell at 14:00 cannot be matched to a buy at 15:00 (lookahead).
        # After asof-sort the buy at 15:00 comes after the sell at 14:00, so
        # there is no open long for the sell to close. It instead opens a
        # short lot. The later buy then closes that short (cover). The point:
        # NO round trip ever pairs an entry asof > exit asof.
        recs = [
            _exec(exec_id="s1", side="sell", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z"),
            _exec(exec_id="b1", side="buy", qty=1.0, fill_price=90.0,
                  asof="2026-05-13T15:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)
        for rt in trips:
            assert rt.asof_exit >= rt.asof_entry
        # The sell @100 opened a short; the later buy @90 covered it: +10%.
        assert len(trips) == 1
        assert trips[0].side == "sell"
        assert trips[0].realized_return == pytest.approx(0.10)
        assert open_lots == {}


# ---------------------------------------------------------------------------
# Direction flip + incremental carry-in
# ---------------------------------------------------------------------------


class TestFlipAndCarryIn:
    def test_direction_flip_opens_residual(self):
        # Long 1, then sell 3: closes the 1 long, opens a 2 short.
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z"),
            _exec(exec_id="x1", side="sell", qty=3.0, fill_price=110.0,
                  asof="2026-05-13T16:00:00Z"),
        ]
        trips, open_lots = join_exit_fills(recs)
        # One settled lot (the long), residual 2-short still open.
        assert len(trips) == 1
        assert trips[0].side == "buy"
        assert trips[0].realized_return == pytest.approx(0.10)
        bucket = ("freqtrade", "crypto", "BTC/USDT")
        assert open_lots[bucket][0]["side"] == "sell"
        assert open_lots[bucket][0]["qty"] == pytest.approx(2.0)

    def test_carry_in_open_lots_across_calls(self):
        # First call opens a long with no exit.
        recs1 = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", signal_id="sig-1"),
        ]
        trips1, open1 = join_exit_fills(recs1)
        assert trips1 == []
        # Second call provides the exit; carry-in the open lot.
        recs2 = [
            _exec(exec_id="x1", side="sell", qty=1.0, fill_price=110.0,
                  asof="2026-05-13T18:00:00Z"),
        ]
        trips2, open2 = join_exit_fills(recs2, open_lots=open1)
        assert len(trips2) == 1
        assert trips2[0].realized_return == pytest.approx(0.10)
        assert trips2[0].entry_signal_id == "sig-1"
        assert open2 == {}
        # Carry-in must not have mutated the first call's returned state.
        bucket = ("freqtrade", "crypto", "BTC/USDT")
        assert open1[bucket][0]["qty"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bucketing: distinct assets/accounts don't cross-settle
# ---------------------------------------------------------------------------


class TestBucketing:
    def test_distinct_assets_do_not_match(self):
        recs = [
            _exec(exec_id="e1", side="buy", qty=1.0, fill_price=100.0,
                  asof="2026-05-13T14:00:00Z", asset="BTC/USDT"),
            _exec(exec_id="x1", side="sell", qty=1.0, fill_price=110.0,
                  asof="2026-05-13T18:00:00Z", asset="ETH/USDT"),
        ]
        trips, open_lots = join_exit_fills(recs)
        # No cross-asset settlement; both remain open in their own buckets.
        assert trips == []
        assert ("freqtrade", "crypto", "BTC/USDT") in open_lots
        assert ("freqtrade", "crypto", "ETH/USDT") in open_lots


# ---------------------------------------------------------------------------
# Type / dataclass sanity
# ---------------------------------------------------------------------------


def test_settled_round_trip_is_frozen():
    rt = SettledRoundTrip(
        asset="BTC/USDT",
        account_id="freqtrade",
        asset_class="crypto",
        side="buy",
        qty=1.0,
        entry_price=100.0,
        exit_price=110.0,
        asof_entry=pd.Timestamp("2026-05-13T14:00:00Z"),
        asof_exit=pd.Timestamp("2026-05-13T18:00:00Z"),
        entry_exec_id="e1",
        exit_exec_id="x1",
        entry_signal_id="sig-A",
        exit_signal_id="sig-B",
        fees=0.0,
        realized_return=0.10,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rt.realized_return = 0.0  # type: ignore[misc]
