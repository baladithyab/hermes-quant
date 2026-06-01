"""hermes_quant.loadtest — full-universe load/throughput harness (B43, seed hermes-quant-817b).

This package is a **runnable instrument**, not a CI gate. It exercises the
full-universe pipeline behavior at scale —

    universe scan -> advisor.recommend() across N symbols -> aggregation -> gate

— against a *synthetic, offline, deterministic* N-symbol universe (no live
network, no broker API). It measures wall-time, peak memory, and whether any
pipeline step degrades super-linearly as N grows.

POSTURE (money-software rails preserved):
- READ-ONLY. The harness never writes state.db / signals.jsonl, never mutates a
  calibrator, never touches the gate / sizing-ladder / kill-switch. It only
  *calls* ``advisor.recommend()`` (already read-only per ADR-0014) with an
  injected synthetic provider per symbol.
- OFFLINE + DETERMINISTIC. Every bar is generated from a seeded RNG keyed on the
  symbol, so a run at a given (N, seed) is byte-stable across machines. No
  ``yfinance`` / ``alpaca`` import is required to run it.
- ADDITIVE / DEFAULT-OFF. Nothing in the existing pipeline imports this package;
  it is opt-in via the harness functions here (and the ``@pytest.mark.slow``
  test gated behind ``HERMES_QUANT_LOAD_TEST=1``).

The point is a v0.9 load-readiness measuring stick: an operator can run it at
full N (500+ symbols, the Alpaca daily universe cap) and read off throughput +
memory + a super-linearity verdict, while CI runs only a tiny deterministic N.
"""

from hermes_quant.loadtest.harness import (
    LoadTestReport,
    StageTimings,
    SymbolResult,
    build_synthetic_universe,
    fit_superlinearity,
    run_load_test,
    run_scaling_sweep,
)

__all__ = [
    "LoadTestReport",
    "StageTimings",
    "SymbolResult",
    "build_synthetic_universe",
    "fit_superlinearity",
    "run_load_test",
    "run_scaling_sweep",
]
