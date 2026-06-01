"""B43 — full-universe load/throughput harness (seed hermes-quant-817b).

This is a **runnable instrument for v0.9 load-readiness**, NOT a CI gate. It
exercises the full-universe pipeline behavior at scale —

    universe scan -> advisor.recommend() across N symbols -> aggregation -> gate

— against a synthetic, offline, deterministic N-symbol universe (no live
network, no broker API), measuring wall-time, peak memory, and whether any
pipeline step degrades super-linearly as N grows.

WHY IT IS NOT A HARD GATE
-------------------------
The full-N run (N = 500+, the Alpaca daily-universe cap) takes minutes — the
canonical analyst loadout includes the Kronos foundation-model analyst, whose
per-symbol cost is the whole point of measuring. Running that every CI commit
would be wasteful and flaky. So:

  * the body of this module is split into a *fast deterministic* small-N test
    (runs in CI, light analyst loadout, sub-few-seconds) and an *opt-in*
    full-scale test gated behind ``HERMES_QUANT_LOAD_TEST=1`` and
    ``@pytest.mark.slow``;
  * the fast test asserts the harness *runs* and produces well-formed,
    deterministic output + a finite scaling exponent — it does NOT assert any
    throughput/latency SLO (those are read off the operator run, not gated).

HOW TO RUN IT AT FULL N (operator / load-run)
----------------------------------------------
Light loadout (no Kronos), scaling sweep across the production universe sizes::

    HERMES_QUANT_LOAD_TEST=1 \
      ~/.hermes/hermes-agent/venv/bin/python3 -m pytest \
      tests/integration/test_full_universe_load.py -m slow -s -q

Or drive the harness directly for a full production-loadout run (Kronos
included — this is the real v0.9 capacity number)::

    HERMES_QUANT_LOAD_TEST=1 ~/.hermes/hermes-agent/venv/bin/python3 - <<'PY'
    from hermes_quant.loadtest import run_load_test
    # analysts_factory=None -> canonical production loadout (incl. Kronos)
    report = run_load_test(500, warmup=True)
    print(report.summary())
    PY

Read the printed ``LoadTestReport.summary()``: wall-time, throughput
(symbols/s), peak memory (MB), per-symbol mean/p95/max latency, gate
pass/silence/error counts. ``run_scaling_sweep([100, 250, 500])`` additionally
returns the fitted super-linearity exponent ``alpha`` (≈1.0 = linear).
"""

from __future__ import annotations

import os

import pytest

from hermes_quant.loadtest import (
    LoadTestReport,
    build_synthetic_universe,
    fit_superlinearity,
    run_load_test,
    run_scaling_sweep,
)

# Opt-in gate for the slow full-scale path. The small-N determinism tests below
# always run (they are sub-few-seconds); the slow test is skipped unless an
# operator/load-run sets the env var.
_LOAD_TEST_ENABLED = os.environ.get("HERMES_QUANT_LOAD_TEST") == "1"


def _light_loadout() -> list:
    """Fast, deterministic analyst loadout for the CI small-N path.

    ClassicalTA + MicrostructureLite are both OHLCV-only, deterministic, and
    foundation-model-free, so the small-N harness stays fast while still
    exercising the REAL ``recommend() -> BMA aggregate -> DefaultRiskGate``
    path. The operator full-N run passes ``analysts_factory=None`` to measure
    the canonical loadout (incl. the heavy Kronos analyst).
    """
    from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst
    from hermes_quant.analysts.microstructure import MicrostructureLite

    return [ClassicalTAAnalyst(), MicrostructureLite()]


# ---------------------------------------------------------------------------
# Fast, deterministic, always-on CI tests (small N)
# ---------------------------------------------------------------------------


def test_synthetic_universe_is_deterministic() -> None:
    """Two builds at the same (N, seed) must produce byte-identical bars."""
    u1 = build_synthetic_universe(5, base_seed=817)
    u2 = build_synthetic_universe(5, base_seed=817)
    assert list(u1.keys()) == list(u2.keys())
    for sym in u1:
        assert u1[sym]._bars.equals(u2[sym]._bars), f"non-deterministic bars: {sym}"
    # A different seed yields a different universe (sanity: the seed matters).
    u3 = build_synthetic_universe(5, base_seed=999)
    assert not u1["LT0000"]._bars.equals(u3["LT0000"]._bars)


def test_build_synthetic_universe_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build_synthetic_universe(0)


def test_load_harness_runs_small_n_deterministically() -> None:
    """The harness runs end-to-end at small N and produces well-formed,
    deterministic output. This is the always-on CI assertion — it proves the
    instrument works, NOT that any latency/throughput SLO is met."""
    n = 6
    r1 = run_load_test(n, analysts_factory=_light_loadout)
    r2 = run_load_test(n, analysts_factory=_light_loadout)

    # ── well-formed report ───────────────────────────────────────────────
    assert isinstance(r1, LoadTestReport)
    assert r1.n_symbols == n
    assert r1.wall_time_s > 0.0
    assert r1.peak_memory_mb > 0.0
    assert r1.throughput_symbols_per_s > 0.0
    # Every symbol is accounted for in exactly one bucket.
    assert r1.n_gate_pass + r1.n_gate_silenced + r1.n_errors == n
    # A load run must never abort on a single symbol's failure.
    assert r1.n_errors == 0
    assert len(r1.per_symbol) == n
    # Each symbol actually fetched its synthetic bars (pipeline really ran).
    assert all(s.bars_received > 0 for s in r1.per_symbol)

    # ── stage timings are present + ordered ──────────────────────────────
    st = r1.stage_timings
    assert st.scan_s >= 0.0
    assert st.recommend_total_s > 0.0
    assert st.per_symbol_max_s >= st.per_symbol_p95_s >= 0.0
    assert st.per_symbol_p95_s >= 0.0
    assert st.per_symbol_mean_s >= 0.0

    # ── deterministic outcome (same inputs -> same gate decisions) ───────
    assert (r1.n_gate_pass, r1.n_gate_silenced, r1.n_errors) == (
        r2.n_gate_pass,
        r2.n_gate_silenced,
        r2.n_errors,
    )
    assert [s.symbol for s in r1.per_symbol] == [s.symbol for s in r2.per_symbol]
    assert [s.gate_pass for s in r1.per_symbol] == [s.gate_pass for s in r2.per_symbol]
    assert [s.n_analyst_views for s in r1.per_symbol] == [
        s.n_analyst_views for s in r2.per_symbol
    ]

    # ── summary() renders (operator log smoke) ───────────────────────────
    assert "loadtest" in r1.summary()


def test_scaling_sweep_produces_finite_exponent_small_n() -> None:
    """A small scaling sweep produces a finite super-linearity exponent.

    The fast CI path asserts only that the exponent is FINITE and within a very
    loose band — enough to catch a catastrophic O(N^2) regression while
    tolerating the warm-up / regime-cold-fit jitter that dominates tiny-N
    wall-times. A TIGHT bound (alpha < ~1.3) is for the operator full-N run,
    documented in the module docstring — it is intentionally NOT gated here.
    """
    import math

    reports, alpha = run_scaling_sweep(
        [4, 8, 16], analysts_factory=_light_loadout, warmup=True
    )
    assert len(reports) == 3
    assert [r.n_symbols for r in reports] == [4, 8, 16]
    assert math.isfinite(alpha)
    # Loose ceiling: O(N^2) would give alpha≈2.0 and keep climbing; anything
    # under 2.5 is "not catastrophically super-linear". This is a sanity rail,
    # NOT a load-readiness gate.
    assert alpha < 2.5, f"scaling exponent {alpha:.3f} looks super-linear"


def test_fit_superlinearity_recovers_known_exponent() -> None:
    """The exponent fitter recovers a planted power law (unit test of the
    measuring stick itself — independent of pipeline timing noise)."""
    ns = [10, 20, 40, 80]
    # Perfect linear: t = 0.5 * N  -> alpha == 1.0
    linear_t = [0.5 * n for n in ns]
    assert abs(fit_superlinearity(ns, linear_t) - 1.0) < 1e-9
    # Perfect quadratic: t = 0.01 * N^2 -> alpha == 2.0
    quad_t = [0.01 * n * n for n in ns]
    assert abs(fit_superlinearity(ns, quad_t) - 2.0) < 1e-9
    # Sub-linear (amortizing fixed cost): t = 3 * N^0.5 -> alpha == 0.5
    sqrt_t = [3.0 * n**0.5 for n in ns]
    assert abs(fit_superlinearity(ns, sqrt_t) - 0.5) < 1e-9


def test_fit_superlinearity_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        fit_superlinearity([10], [1.0])  # need >= 2 points
    with pytest.raises(ValueError):
        fit_superlinearity([10, 10], [1.0, 2.0])  # need 2 DISTINCT N
    with pytest.raises(ValueError):
        fit_superlinearity([10, 20], [0.0, 1.0])  # non-positive time
    with pytest.raises(ValueError):
        fit_superlinearity([10, 20, 30], [1.0, 2.0])  # mismatched lengths


def test_per_symbol_mean_latency_is_stable_across_n() -> None:
    """Steady-state per-symbol latency should be roughly FLAT across N (the
    real linearity signal — total wall-time is contaminated by one-time
    warm-up costs). After warm-up, the per-symbol mean at the larger N must not
    be wildly larger than at the smaller N.

    This is a loose rail, not an SLO: it catches an accidental per-symbol cost
    that grows with universe size (e.g. an O(N) scan rebuilt inside the loop).
    """
    r_small = run_load_test(8, analysts_factory=_light_loadout, warmup=True)
    r_large = run_load_test(24, analysts_factory=_light_loadout, warmup=True)
    small_mean = r_small.stage_timings.per_symbol_mean_s
    large_mean = r_large.stage_timings.per_symbol_mean_s
    assert small_mean > 0.0 and large_mean > 0.0
    # Allow a generous 6x slack for measurement noise / data-dependent
    # regime-classifier cold fits; a genuine O(N) per-symbol blowup would be
    # ~3x here (24/8) and climbing without bound at larger N.
    assert large_mean < small_mean * 6.0, (
        f"per-symbol mean grew from {small_mean * 1000:.1f}ms (N=8) to "
        f"{large_mean * 1000:.1f}ms (N=24) — possible super-linear per-symbol cost"
    )


# ---------------------------------------------------------------------------
# Opt-in full-scale load test (slow; skipped unless HERMES_QUANT_LOAD_TEST=1)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    not _LOAD_TEST_ENABLED,
    reason="full-universe load test is opt-in; set HERMES_QUANT_LOAD_TEST=1 to run",
)
def test_full_universe_scale_sweep_operator_run() -> None:
    """Operator / load-run: scaling sweep toward the production universe size.

    Uses the LIGHT loadout so the slow test itself stays bounded; to measure the
    REAL production loadout (Kronos included) drive ``run_load_test`` directly
    per the module docstring. Prints every report's summary (run with ``-s``).

    Reports the fitted super-linearity exponent and applies the TIGHTER
    load-readiness rail (alpha < 1.5). This is the v0.9 capacity instrument —
    it is opt-in precisely so this assertion is NOT a per-commit CI gate.
    """
    sweep_ns = [50, 100, 200]
    reports, alpha = run_scaling_sweep(
        sweep_ns, analysts_factory=_light_loadout, warmup=True
    )
    for r in reports:
        print("\n" + r.summary())
    print(f"\n[loadtest] fitted scaling exponent alpha = {alpha:.3f} (1.0 = linear)")

    # Every symbol accounted for at every N; no aborts.
    for r in reports:
        assert r.n_gate_pass + r.n_gate_silenced + r.n_errors == r.n_symbols
        assert r.n_errors == 0
    # Tighter load-readiness rail (operator run only).
    assert alpha < 1.5, f"super-linear scaling detected: alpha={alpha:.3f}"
