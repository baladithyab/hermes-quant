"""Live AlpacaShortabilityOracle smoke test (network, paper creds required).

Skipped by default. Run with HERMES_QUANT_LIVE_ALPACA=1 and ALPACA_API_KEY/SECRET set:

    HERMES_QUANT_LIVE_ALPACA=1 pytest tests/integration/test_admissibility_alpaca_live.py -q

Verifies the real get_asset() path: a liquid ETB name (AAPL) is ACCEPTED for a whole-share short
with a low CBR; the unit suite already covers the deterministic predicate with an injected fake.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

pytest.importorskip("alpaca")

pytestmark = pytest.mark.skipif(
    "HERMES_QUANT_LIVE_ALPACA" not in os.environ,
    reason="set HERMES_QUANT_LIVE_ALPACA=1 (+ ALPACA creds) to run the live shortability smoke",
)


def test_live_aapl_etb_short_accepted():
    from hermes_quant.admissibility import (
        AdmissibilityContext,
        AdmissibilityState,
        AlpacaShortabilityOracle,
    )

    oracle = AlpacaShortabilityOracle()
    v = oracle.verdict("AAPL", "short", 1, datetime.now(tz=UTC), AdmissibilityContext())
    # AAPL is a perennial ETB name; a 1-share short should be admissible.
    assert v.state is AdmissibilityState.ACCEPTED
    assert 0.0 < v.annual_cbr < 0.02
