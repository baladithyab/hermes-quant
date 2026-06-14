"""Spy-counted rail test for the Phase-8 P0-A.3 slippage_only dispatch gate.

SEED cr11 (P2). The rail under test: `dispatch_settlement` MUST perform ZERO
`analyst.update()` calls for RealizedOutcomes whose view metadata carries
`_calibration_quality == CALIBRATION_QUALITY_SLIPPAGE_ONLY`. That value is a
per-fill SLIPPAGE number, not a directional horizon return; feeding it into the
BMA Beta posteriors would corrupt analyst calibration (settlement_loop.py
docstring §"v0.1.1 limitation — calibrator updates gated off").

If a future schema migration drops the `_calibration_quality` tag from the
outcomes that `construct_realized_outcomes` emits — or removes the gate branch
in `dispatch_settlement` — `analyst.update()` would silently resume on corrupt
single-fill data. These tests pin the gate with a SPY that COUNTS every
`update()` invocation so such a regression fails loudly.

Distinct from tests/unit/test_settlement_loop_p0_a3.py, which checks each
polarity in its own single-polarity batch. Here the load-bearing assertion is a
MIXED batch through ONE `dispatch_settlement` call: the spy's exact count must
equal the number of UNTAGGED outcomes — proving the skip is driven per-outcome
by the tag, not by a global "skip-all"/"dispatch-all" behavior. A migration that
drops the tag would flip the mixed-batch count and trip this test.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.daemon.settlement_loop import (
    CALIBRATION_QUALITY_HORIZON_RETURN,
    CALIBRATION_QUALITY_SLIPPAGE_ONLY,
    construct_realized_outcomes,
    dispatch_settlement,
)
from hermes_quant.protocol import AnalystView, RealizedOutcome


class _UpdateCountingAnalyst:
    """Spy StatefulAnalyst — counts every update() call (cr11 rail probe)."""

    def __init__(self, name: str = "classical_ta") -> None:
        self.name = name
        self.n_updates = 0
        self.seen: list[RealizedOutcome] = []

    def emit(self, ctx):  # pragma: no cover — protocol satisfaction only
        raise NotImplementedError

    def update(self, outcome: RealizedOutcome) -> None:
        self.n_updates += 1
        self.seen.append(outcome)


class _InertAggregator:
    """Aggregator that must never be invoked in these analyst-focused tests."""

    def __init__(self) -> None:
        self.n_updates = 0

    def aggregate(self, views, ctx):  # pragma: no cover
        raise NotImplementedError

    def update(self, episode) -> None:
        self.n_updates += 1


def _signal(sig_id: str = "sig-1", direction: int = 1) -> dict:
    return {
        "schema_version": 1,
        "id": sig_id,
        "asof": "2026-05-13T14:00:00.000000Z",
        "asset": "BTC/USDT",
        "exchange": "binance",
        "timeframe": "1h",
        "asset_class": "crypto",
        "direction": direction,
        "magnitude": 0.005,
        "confidence": 0.62,
        "confidence_raw": 0.70,
        "horizon": "4h",
        "decision_price": 50_000.0,
        "target_position_pct": 0.10,
        "components": [
            {
                "analyst": "classical_ta",
                "direction": direction,
                "magnitude": 0.005,
                "confidence": 0.62,
                "confidence_raw": 0.70,
                "horizon": "4h",
            },
        ],
        "aggregator": "bma",
    }


def _exec(sig_id: str = "sig-1", side: str = "buy") -> dict:
    return {
        "schema_version": 1,
        "exec_id": f"exec-{sig_id}",
        "asof": "2026-05-13T14:01:00.000000Z",
        "asset": "BTC/USDT",
        "side": side,
        "qty": 0.01,
        "fill_price": 50_500.0,
        "decision_price": 50_000.0,
        "fees": 0.5,
        "account_id": "freqtrade",
        "asset_class": "crypto",
        "signal_id": sig_id,
    }


def _horizon_outcome(analyst: str = "classical_ta", ret: float = 0.012) -> RealizedOutcome:
    """An UNTAGGED (horizon-quality) outcome the gate MUST dispatch."""
    view = AnalystView(
        analyst=analyst,
        direction=1,
        magnitude=0.005,
        confidence=0.62,
        confidence_raw=0.70,
        horizon="4h",
        metadata={"_calibration_quality": CALIBRATION_QUALITY_HORIZON_RETURN},
    )
    return RealizedOutcome(
        view=view,
        asof_view=pd.Timestamp("2026-05-13T14:00:00Z"),
        asof_settlement=pd.Timestamp("2026-05-13T18:00:00Z"),
        realized_return=ret,
        direction_correct=True,
    )


class TestSlippageOnlySkipSpy:
    def test_constructed_slippage_outcomes_produce_zero_analyst_updates(self):
        """End-to-end: outcomes built by construct_realized_outcomes carry the
        slippage_only tag, so the spy analyst MUST receive ZERO update() calls.

        This is the cr11 rail: if a migration drops the tag from
        construct_realized_outcomes, the spy would count > 0 and this fails.
        """
        sig = _signal()
        outcomes = construct_realized_outcomes([_exec()], {sig["id"]: sig})

        # Guard the premise: construction actually produced a tagged outcome
        # (otherwise the zero-update assertion below would be vacuously true).
        assert len(outcomes) == 1
        assert (
            outcomes[0].view.metadata["_calibration_quality"]
            == CALIBRATION_QUALITY_SLIPPAGE_ONLY
        )

        spy = _UpdateCountingAnalyst()
        agg = _InertAggregator()
        stats = dispatch_settlement(
            outcomes,
            [],  # no episode outcomes; this test isolates the analyst path
            analysts_by_name={"classical_ta": spy},
            aggregator=agg,
        )

        # The rail: ZERO analyst.update() invocations for slippage_only data.
        assert spy.n_updates == 0
        assert spy.seen == []
        assert stats["n_analyst_updates"] == 0
        assert stats["n_skipped_slippage_only"] == 1
        assert agg.n_updates == 0

    def test_untagged_outcome_does_dispatch_positive_control(self):
        """Positive control proving the spy WOULD count if the tag were absent.

        Without this, the zero-update assertion above could pass simply because
        the spy never counts anything. Here a horizon-quality (untagged-as-
        slippage) outcome MUST drive exactly one update() call.
        """
        spy = _UpdateCountingAnalyst()
        agg = _InertAggregator()
        stats = dispatch_settlement(
            [_horizon_outcome()],
            [],
            analysts_by_name={"classical_ta": spy},
            aggregator=agg,
        )

        assert spy.n_updates == 1
        assert stats["n_analyst_updates"] == 1
        assert stats["n_skipped_slippage_only"] == 0

    def test_mixed_batch_gate_discriminates_per_outcome(self):
        """Load-bearing anti-regression: a SINGLE dispatch_settlement call with
        BOTH slippage_only and horizon-quality outcomes must update the spy
        EXACTLY once per untagged outcome and skip every tagged one.

        Proves the skip is per-outcome (tag-driven), not a global behavior. A
        migration that dropped the slippage_only tag would make the tagged
        outcomes count too, flipping n_updates from 2 to 5 and failing here.
        """
        sig = _signal()
        # 3 slippage_only outcomes constructed from real fills...
        tagged = construct_realized_outcomes(
            [_exec(sig_id="sig-1"), _exec(sig_id="sig-1"), _exec(sig_id="sig-1")],
            {sig["id"]: sig},
        )
        assert len(tagged) == 3
        for o in tagged:
            assert (
                o.view.metadata["_calibration_quality"]
                == CALIBRATION_QUALITY_SLIPPAGE_ONLY
            )

        # ...and 2 untagged horizon-quality outcomes that MUST dispatch.
        untagged = [_horizon_outcome(), _horizon_outcome()]

        spy = _UpdateCountingAnalyst()
        agg = _InertAggregator()
        # Interleave so an off-by-one or order-sensitive gate is caught too.
        batch = [tagged[0], untagged[0], tagged[1], untagged[1], tagged[2]]
        stats = dispatch_settlement(
            batch,
            [],
            analysts_by_name={"classical_ta": spy},
            aggregator=agg,
        )

        # Exactly the 2 untagged outcomes flow through; the 3 tagged are skipped.
        assert spy.n_updates == 2
        assert stats["n_analyst_updates"] == 2
        assert stats["n_skipped_slippage_only"] == 3
        # Every outcome the spy saw must be horizon-quality, never slippage_only.
        for outcome in spy.seen:
            assert (
                outcome.view.metadata["_calibration_quality"]
                != CALIBRATION_QUALITY_SLIPPAGE_ONLY
            )
        assert spy.n_updates == sum(
            1
            for o in batch
            if (o.view.metadata or {}).get("_calibration_quality")
            != CALIBRATION_QUALITY_SLIPPAGE_ONLY
        )
