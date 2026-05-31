"""Live Alpaca-paper multi-leg roundtrip smoke (network + paper creds required).

Skipped by default. Run with HERMES_QUANT_MULTILEG_REACTOR=1 + paper creds:

    HERMES_QUANT_MULTILEG_REACTOR=1 APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... \
        pytest tests/integration/test_multileg_paper_roundtrip.py --run-integration -q

Observational; never runs in CI. Submits a real covered call + a real debit vertical
to Alpaca paper and asserts terminal status with per-leg filled_avg_price; probes a
calendar spread (different expiries) to record the ADR-0029 OQ1 answer. The unit suite
covers the deterministic model (no network); this exercises the live-paper HTTP body,
which is itself deferred to the go-live wave — so this stub is structural until that
lands.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("alpaca")

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_QUANT_MULTILEG_REACTOR") != "1"
    or not os.environ.get("APCA_API_KEY_ID")
    or not os.environ.get("APCA_API_SECRET_KEY"),
    reason="set HERMES_QUANT_MULTILEG_REACTOR=1 + APCA paper creds to run the live "
    "multi-leg roundtrip smoke (live-paper HTTP body deferred to the go-live wave)",
)


def test_live_paper_mleg_roundtrip_deferred():
    # The live-paper submit/poll body is deferred to the ADR-0029 go-live wave;
    # PaperBroker._submit_live_paper raises NotImplementedError until then. This test
    # documents the live probe and will be fleshed out when that path lands.
    from hermes_quant.react.mleg_fill import PaperBroker

    broker = PaperBroker(paper=True)
    with pytest.raises(NotImplementedError):
        broker._submit_live_paper()
