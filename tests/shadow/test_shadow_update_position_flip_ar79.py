"""ar79 — shadow _update_position must add the direction-flip branch (re-base to fill_price).

Found by the parallel find->fix workflow (wf_d7d2cc27). The shadow account's _update_position lacked
the direction-flip branch the canonical state.portfolio_state._update_position has: a fill larger than
the open lot flips the position's sign, and the surviving lot is a NEW position opened at fill_price —
its avg_entry_price MUST become fill_price, not keep the OLD side's basis. A wrong basis corrupts the
shadow ledger's mark-to-market (the basis is the missing-price fallback), biasing the ADR-0049
promotion-gate equity. The shadow copy must AGREE with the canonical cost-basis math.
"""
from __future__ import annotations

from hermes_quant.shadow.account import _update_position as shadow_update
from hermes_quant.state.portfolio_state import _update_position as canonical_update


def test_ar79_long_to_short_flip_uses_fill_price():
    # 10 long @ 100, sell 25 @ 120 -> net 15 short, opened at 120 (NOT the stale 100 basis).
    assert shadow_update(10.0, 100.0, -25.0, 120.0) == (-15.0, 120.0)


def test_ar79_short_to_long_flip_uses_fill_price():
    assert shadow_update(-10.0, 100.0, 25.0, 120.0) == (15.0, 120.0)


def test_ar79_flip_and_neighbors_match_canonical():
    """The shadow copy must not diverge from the canonical cost-basis math on the flip
    case OR its neighbors (partial close, add, full close, open-from-flat)."""
    cases = [
        (10.0, 100.0, -25.0, 120.0),   # long -> short flip
        (-10.0, 100.0, 25.0, 120.0),   # short -> long flip
        (10.0, 100.0, -3.0, 120.0),    # partial close (same sign survives)
        (10.0, 100.0, 5.0, 120.0),     # add same direction
        (10.0, 100.0, -10.0, 120.0),   # full close
        (0.0, 0.0, 7.0, 50.0),         # open from flat
    ]
    for old_qty, old_avg, delta, fill in cases:
        assert shadow_update(old_qty, old_avg, delta, fill) == canonical_update(
            old_qty, old_avg, delta, fill
        ), f"shadow diverges from canonical at {(old_qty, old_avg, delta, fill)}"
