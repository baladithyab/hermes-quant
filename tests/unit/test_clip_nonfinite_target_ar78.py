"""ar78 — clip_one_to_remaining_headroom must fail CLOSED on a non-finite INCOMING target.

Found by the parallel find->fix workflow (wf_d7d2cc27). ar07 partitions non-finite targets in the
BATCH normalize_targets, and ar03 guards the existing book inside clip_one_to_remaining_headroom — but
the single-pick clip entry point (the autonomous cap path calls it directly) never guarded its OWN
per_symbol_target_pct argument. A NaN/inf target slips past the g_room/c_room<=0 breach tests (every
NaN/inf comparison is False; inf*scale stays inf), so an inf target FIRES unclipped. Fix: silence a
non-finite incoming target (fail-closed, nonfinite_target reason), mirroring ar07.
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.risk.portfolio_normalize import (
    PortfolioCaps,
    PortfolioState,
    clip_one_to_remaining_headroom,
)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_ar78_clip_silences_nonfinite_incoming_target(bad):
    state = PortfolioState(positions={"X": 0.5})  # finite book with headroom
    caps = PortfolioCaps.standard()
    r = clip_one_to_remaining_headroom(asset="Y", per_symbol_target_pct=bad, state=state, caps=caps)
    assert r.fired is False, f"ar78: a {bad!r} incoming target FIRED unclipped"
    assert r.silence_reason == "nonfinite_target"
    assert r.portfolio_target_pct == 0.0


def test_ar78_finite_target_byte_identical():
    """A finite target with ample headroom still fires (the guard is a no-op on good input)."""
    state = PortfolioState(positions={"X": 0.5})
    caps = PortfolioCaps.standard()
    r = clip_one_to_remaining_headroom(asset="Y", per_symbol_target_pct=0.10, state=state, caps=caps)
    assert r.fired is True
    assert math.isfinite(r.portfolio_target_pct) and r.portfolio_target_pct != 0.0
