"""i0c — settlement FIFO must agree with the position fold under the delta
normalizer flag (HERMES_QUANT_DELTA_NORMALIZER).

Ported from research/temp/inc-settle/probe_i0c2.py (the RED proof) +
probe_byteid.py / probe_flagon_genuine.py (the byte-identical proofs).

THE MONEY BUG (i0c): the production ExecutionRecord schema writes ``fill_size_pct``
as the ABSOLUTE post-fill TARGET (react/base.py), but ``join_exit_fills`` reads it
as a traded DELTA. Under ADR-0091 Option E (HERMES_QUANT_DELTA_NORMALIZER==1) the
position fold (portfolio_state.py:621-633,662) runs the ONE shared
FillDeltaNormalizer so a re-affirmed unchanged target folds to delta 0 and a
flatten-to-0 record folds to the close delta. The settlement FIFO ran NO such
pre-pass: a re-affirm became a PHANTOM new lot and a ``fill_size_pct=0.0`` flatten
was dropped (zero-size skip) — so the FIFO booked 0 round-trips / 0.0 realized P&L
while the position fold booked a real -10% LOSS. The kill-switch ``_cum_pnl`` then
UNDERCOUNTS the realized loss = fail-OPEN.

The fix (settlement_loop.py, i0c): flag-gated normalizer pre-pass. Under the flag,
stable-sort the raw records by asof_execution (mirror portfolio_state.py:633),
build ONE FillDeltaNormalizer, and override ``fill_size_pct`` on a SHALLOW COPY of
each record with ``normalizer.delta_for(rec)`` BEFORE ``_normalize_exec_record``
runs. Flag OFF (production default) => no pre-pass => byte-identical to legacy.

Offline-deterministic: synthetic in-memory records, no network, no clock.
"""

from __future__ import annotations

import pytest

from hermes_quant.daemon.settlement_loop import join_exit_fills
from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer


def _reaffirm_then_flatten_stream() -> list[dict]:
    """Absolute-target single-lane stream:
    open 10% long @100 (t1), RE-AFFIRM 10% @105 (t2), RE-AFFIRM 10% @120 (t3),
    flatten target 0.0 @90 (t4). The position fold books ONE round-trip:
    long 0.10 entry@100 -> exit@90 = -10% realized LOSS.
    """
    return [
        {"asset": "X", "asset_class": "equity", "account_id": "a", "fill_size_pct": 0.10,
         "target_position_pct": 0.10, "fill_price": 100.0,
         "asof_execution": "2026-06-14T01:00:00Z", "proposal_id": "t1", "signal_id": "s1"},
        {"asset": "X", "asset_class": "equity", "account_id": "a", "fill_size_pct": 0.10,
         "target_position_pct": 0.10, "fill_price": 105.0,
         "asof_execution": "2026-06-14T02:00:00Z", "proposal_id": "t2", "signal_id": "s1"},
        {"asset": "X", "asset_class": "equity", "account_id": "a", "fill_size_pct": 0.10,
         "target_position_pct": 0.10, "fill_price": 120.0,
         "asof_execution": "2026-06-14T03:00:00Z", "proposal_id": "t3", "signal_id": "s1"},
        {"asset": "X", "asset_class": "equity", "account_id": "a", "fill_size_pct": 0.0,
         "target_position_pct": 0.0, "fill_price": 90.0,
         "asof_execution": "2026-06-14T04:00:00Z", "proposal_id": "t4", "signal_id": "s2"},
    ]


def _fifo_realized_pnl_units(round_trips) -> float:
    pnl = 0.0
    for rt in round_trips:
        pnl += rt.realized_return * abs(rt.qty) * abs(rt.entry_price)
    return pnl


class TestNormalizerPrepassFlagOn:
    def test_reaffirm_stream_flag_off_diverges_from_position_fold(self, monkeypatch):
        # FLAG OFF (production default today): the FIFO reads fill_size_pct as a
        # raw delta. The three +0.10 re-affirms stack as phantom buy lots and the
        # t4 flatten (fill_size_pct=0.0) is dropped (zero-size skip) -> 0 trips,
        # 0.0 P&L. This DIVERGES from the position fold's -10% loss. (This is the
        # legacy behavior the FIX must keep byte-identical when the flag is OFF.)
        monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
        rts, open_lots = join_exit_fills(_reaffirm_then_flatten_stream())
        assert rts == []
        assert _fifo_realized_pnl_units(rts) == pytest.approx(0.0)
        # Three phantom buy lots remain open (the re-affirm inflation).
        assert len(open_lots[("a", "equity", "X")]) == 3

    def test_reaffirm_stream_flag_on_matches_position_fold_loss(self, monkeypatch):
        # FLAG ON: the normalizer pre-pass collapses the two re-affirms to delta 0
        # and turns the flatten into a -0.10 close delta, so the FIFO books ONE
        # round-trip: long 0.10 entry@100 -> exit@90 = -10% realized LOSS, exactly
        # the value the position fold feeds the kill-switch.
        monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")

        # Reference: the position fold's deltas + realized P&L on this stream.
        recs = _reaffirm_then_flatten_stream()
        norm = FillDeltaNormalizer()
        deltas = [norm.delta_for(r) for r in recs]
        assert deltas == [pytest.approx(0.10), pytest.approx(0.0),
                          pytest.approx(0.0), pytest.approx(-0.10)]
        position_fold_pnl = ((90 - 100) / 100) * (100.0 * 0.10)  # -1.0 units

        rts, open_lots = join_exit_fills(_reaffirm_then_flatten_stream())
        assert len(rts) == 1, "the normalizer pre-pass must produce ONE settled round-trip"
        rt = rts[0]
        assert rt.side == "buy"
        assert rt.qty == pytest.approx(0.10)
        assert rt.entry_price == pytest.approx(100.0)
        assert rt.exit_price == pytest.approx(90.0)
        assert rt.realized_return == pytest.approx(-0.10)  # the real LOSS
        # The FIFO realized P&L now AGREES with the position fold (no more
        # undercounting; the kill-switch sees the -10% loss).
        assert _fifo_realized_pnl_units(rts) == pytest.approx(position_fold_pnl)
        # Position fully closed (no phantom lots left open).
        assert open_lots == {}


class TestNormalizerPrepassByteIdenticalFlagOff:
    def test_legacy_delta_stream_flag_off_unchanged(self, monkeypatch):
        # probe_byteid.py: a legacy delta stream (+0.05 open, -0.05 close) under
        # flag OFF must stay byte-identical to the existing test_long_round_trip.
        monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
        recs = [
            {"asset": "BTC/USDT", "asset_class": "crypto", "fill_size_pct": 0.05,
             "target_position_pct": 0.05, "fill_price": 100.0,
             "asof_execution": "2026-05-13T14:00:00Z", "proposal_id": "p-entry", "signal_id": "sig-A"},
            {"asset": "BTC/USDT", "asset_class": "crypto", "fill_size_pct": -0.05,
             "target_position_pct": -0.05, "fill_price": 110.0,
             "asof_execution": "2026-05-13T18:00:00Z", "proposal_id": "p-exit", "signal_id": "sig-B"},
        ]
        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 1
        assert trips[0].realized_return == pytest.approx(0.10)
        assert open_lots == {}

    def test_genuine_open_close_flag_on_equals_legacy_round_trip(self, monkeypatch):
        # probe_flagon_genuine.py: a GENUINE single-lane open(+0.05 target) ->
        # close(0.0 target) under flag ON normalizes to deltas [+0.05, -0.05] —
        # the SAME open/close the legacy delta stream encodes. The realized return
        # is identical to the flag-OFF byte-identical case (cs85 single-running-net
        # guarantee: the normalizer only collapses re-affirms / drops phantoms; it
        # never alters a genuine round-trip's entry/exit/qty).
        monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
        recs = [
            {"asset": "BTC/USDT", "asset_class": "crypto", "fill_size_pct": 0.05,
             "target_position_pct": 0.05, "fill_price": 100.0,
             "asof_execution": "2026-05-13T14:00:00Z", "proposal_id": "p-entry", "signal_id": "sig-A"},
            {"asset": "BTC/USDT", "asset_class": "crypto", "fill_size_pct": 0.0,
             "target_position_pct": 0.0, "fill_price": 110.0,
             "asof_execution": "2026-05-13T18:00:00Z", "proposal_id": "p-exit", "signal_id": "sig-B"},
        ]
        trips, open_lots = join_exit_fills(recs)
        assert len(trips) == 1
        assert trips[0].side == "buy"
        assert trips[0].qty == pytest.approx(0.05)
        assert trips[0].realized_return == pytest.approx(0.10)
        assert open_lots == {}

    def test_flag_on_does_not_mutate_caller_records(self, monkeypatch):
        # The pre-pass overrides fill_size_pct on a SHALLOW COPY (records are
        # read-only). The caller's dicts must be untouched after the call.
        monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
        recs = _reaffirm_then_flatten_stream()
        before = [dict(r) for r in recs]
        join_exit_fills(recs)
        assert recs == before
