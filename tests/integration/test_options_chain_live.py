"""Integration probe for the live Alpaca options chain (Wave B2).

SKIPPED by default. This is the ONLY place the calendar-spread sandbox probe
(research §5 R7 / ADR-0029 OQ1) is recorded; it is observational and never runs
in CI. Marked requires_network; additionally guarded behind
HERMES_QUANT_OPTIONS_LIVE_CHAIN=1 + paper credentials + the alpaca extra.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.requires_network


@pytest.fixture
def _live_enabled() -> None:
    if os.environ.get("HERMES_QUANT_OPTIONS_LIVE_CHAIN") != "1":
        pytest.skip("HERMES_QUANT_OPTIONS_LIVE_CHAIN != 1 (live chain disabled)")
    if not (os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY")):
        pytest.skip("Alpaca paper credentials not present")
    pytest.importorskip("alpaca", reason="alpaca-py not installed ([alpaca] extra)")


def test_fetch_chain_live_nvda(_live_enabled) -> None:
    from hermes_quant.options.data import ChainSnapshotReader
    from hermes_quant.options.occ import parse_occ

    reader = ChainSnapshotReader()
    chain = reader.fetch_chain_live("NVDA")
    assert len(chain.snapshots) >= 20
    for snap in chain.snapshots:
        # All greeks complete post-completion (provider tier or optlib synth).
        g = snap.greeks
        assert g.delta is not None
        assert g.gamma is not None
        assert g.theta is not None
        assert g.vega is not None
    # Sample symbol round-trips through parse_occ.
    parse_occ(chain.snapshots[0].symbol)
