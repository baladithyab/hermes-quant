"""iter3 — _normalize_exec_record must drop a NaN/inf qty or fill_price.

RED-proven defect (HEAD 6cbab3f): json.loads accepts bare NaN/Infinity, and an
upstream divide can produce inf. A NaN qty defeated the `qty_f <= 0` guard
(NaN <= 0 is False), so a "zombie" lot entered the FIFO queue — never popped
(NaN <= 1e-12 is False), silently consuming every subsequent real exit for that
(account, asset_class, asset) bucket into matched=NaN, and producing a round-trip
whose realized_return*NaN the kill-switch basis then drops — UNDER-stating the
drawdown (fail-OPEN). Fix: finite-guard qty_f + fill_price before the <= gate.
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.daemon.settlement_loop import _normalize_exec_record, join_exit_fills


def _rec(**over):
    base = {
        "asset": "ASTS",
        "asset_class": "equity",
        "account_id": "paper-default",
        "fill_size_pct": 0.20,
        "fill_price": 100.0,
        "asof_execution": "2026-06-04T15:35:36Z",
        "proposal_id": "p1",
    }
    base.update(over)
    return base


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_inf_qty_dropped(bad):
    # qty supplied directly as a non-finite value -> record dropped (None).
    rec = _rec(side="buy", qty=bad)
    assert _normalize_exec_record(rec) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_inf_fill_price_dropped(bad):
    rec = _rec(side="buy", qty=0.20, fill_price=bad)
    assert _normalize_exec_record(rec) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_inf_derived_qty_from_fill_size_pct_dropped(bad):
    # The real-bus path derives qty from fill_size_pct; a non-finite there must
    # also be dropped (signed_f == 0.0 is False for NaN, so it would otherwise
    # derive qty=abs(NaN)=NaN).
    rec = _rec(fill_size_pct=bad)  # no explicit side/qty -> derived path
    assert _normalize_exec_record(rec) is None


def test_finite_record_still_normalizes():
    # Byte-identical for a valid record: it still produces a normalized lot.
    rec = _rec(side="buy", qty=0.20)
    out = _normalize_exec_record(rec)
    assert out is not None
    assert out["qty"] == pytest.approx(0.20)
    assert out["fill_price"] == pytest.approx(100.0)


def test_nan_lot_does_not_poison_fifo_settlement():
    """End-to-end: a NaN entry record must not zombie-consume a later real exit.

    Without the guard, the NaN buy lot swallows the sell and the round-trip
    realized_return is NaN; with the guard the NaN record is dropped and the
    real entry+exit settle to a clean round-trip.
    """
    entry_nan = _rec(proposal_id="bad", fill_size_pct=float("nan"),
                     asof_execution="2026-06-04T15:00:00Z")
    entry_ok = _rec(proposal_id="e1", fill_size_pct=0.20, fill_price=100.0,
                    asof_execution="2026-06-04T15:35:36Z")
    exit_ok = _rec(proposal_id="x1", fill_size_pct=0.0 - 0.20, fill_price=90.0,
                   asof_execution="2026-06-05T15:35:36Z")
    # fill_size_pct must be signed for the exit; build explicitly.
    exit_ok["fill_size_pct"] = -0.20
    rts, _open = join_exit_fills([entry_nan, entry_ok, exit_ok])
    # exactly one clean round-trip, finite realized_return
    assert len(rts) == 1
    assert math.isfinite(rts[0].realized_return)
    # -10% move on a long, entry 100 -> exit 90
    assert rts[0].realized_return == pytest.approx(-0.10, abs=1e-6)
