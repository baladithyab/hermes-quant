"""Full-universe load/throughput harness (B43, seed hermes-quant-817b).

See :mod:`hermes_quant.loadtest` for the posture summary. This module contains:

- :func:`build_synthetic_universe` — deterministic N-symbol synthetic universe
  (symbol list + per-symbol seeded OHLCV provider). No network.
- :class:`_SyntheticProvider` — a per-symbol DataProvider test-double that
  returns canned, seeded bars. It mirrors the contract the advisor's real
  yfinance provider exposes (``fetch_bars(asset, timeframe, start, end, *, as_of)``).
- :func:`run_load_test` — runs the full pipeline (scan -> recommend across N ->
  aggregate -> gate, all inside ``advisor.recommend()``) and returns a
  :class:`LoadTestReport` with wall-time, peak memory, throughput, and per-stage
  timing.
- :func:`fit_superlinearity` — given measurements at several N, fits an exponent
  ``alpha`` to ``wall_time ≈ c · N**alpha`` so a caller can assert "no step
  degrades super-linearly" (alpha ≈ 1.0 is linear; alpha >> 1.0 is bad).

The harness deliberately calls the *real* ``advisor.recommend()`` so it exercises
the genuine analyst fan-out + BMA aggregation + DefaultRiskGate path. Only the
data provider is synthetic — exactly the seam ADR-0014 §D documents for tests.
"""

from __future__ import annotations

import gc
import time
import tracemalloc
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Synthetic, deterministic, offline universe
# ---------------------------------------------------------------------------

# Default bars per symbol. 260 ≈ one trading year of daily bars — enough for
# ClassicalTA (min_history_bars=60) + ATR warm-up + a stable BMA posterior, so
# the harness exercises the *signal-producing* path (not the cold-start gate).
_DEFAULT_N_BARS = 260
_DEFAULT_TIMEFRAME = "1d"


def _symbol_seed(base_seed: int, symbol: str) -> int:
    """Deterministic per-symbol RNG seed.

    Keyed on ``(base_seed, symbol)`` so the same (N, base_seed) reproduces the
    same universe byte-for-byte regardless of iteration order or machine.
    """
    # Stable hash: sum of ordinals is order-insensitive within a symbol but
    # distinct across symbols; combined with base_seed it spreads the RNGs.
    h = base_seed & 0xFFFF
    for i, ch in enumerate(symbol):
        h = (h * 131 + ord(ch) + i) & 0x7FFFFFFF
    return h


def _synthetic_bars(symbol: str, n_bars: int, base_seed: int) -> pd.DataFrame:
    """Deterministic GBM-style OHLCV bars for one symbol.

    Each symbol gets its own seeded RNG so directions/magnitudes differ across
    the universe (the gate then admits some and silences others — realistic
    mix). Same (symbol, n_bars, base_seed) -> identical frame.
    """
    rng = np.random.default_rng(_symbol_seed(base_seed, symbol))
    # Per-symbol drift in [-0.001, +0.001] so the universe is a mix of up/down
    # trends — drives a realistic blend of admit/silence at the gate.
    drift = rng.uniform(-0.001, 0.001)
    rets = rng.normal(drift, 0.015, size=n_bars)
    start_price = float(rng.uniform(20.0, 400.0))
    closes = start_price * np.cumprod(1.0 + rets)
    opens = np.concatenate([[start_price], closes[:-1]])
    highs = np.maximum(opens, closes) * (1.0 + np.abs(rng.normal(0, 0.004, n_bars)))
    lows = np.minimum(opens, closes) * (1.0 - np.abs(rng.normal(0, 0.004, n_bars)))
    volumes = rng.integers(1_000_000, 12_000_000, n_bars).astype(float)
    # End the window "yesterday-ish" relative to a FIXED anchor so the frame is
    # wall-clock-independent (deterministic across runs).
    timestamps = pd.date_range(end="2026-05-29", periods=n_bars, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


class _SyntheticProvider:
    """Per-symbol offline DataProvider test-double.

    Holds one symbol's pre-generated bars and returns them from ``fetch_bars``
    irrespective of the requested window — the advisor applies its own ``as_of``
    / lookback trimming downstream, so handing back the full canned frame is
    correct and keeps the harness allocation-light. Mirrors the real provider's
    signature so ``advisor.recommend()`` takes the same code path it would in
    production (no special-casing).
    """

    name = "synthetic-loadtest"
    asset_classes = ("equity",)
    timeframes = ("1d",)
    requires_credentials = False

    __slots__ = ("_bars", "fetch_count")

    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars
        self.fetch_count = 0

    def fetch_bars(
        self,
        asset: str,
        timeframe: str,
        start: Any = None,
        end: Any = None,
        *,
        as_of: Any = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        self.fetch_count += 1
        # Return a copy so the advisor's in-place ``.copy()`` filtering never
        # mutates our canned frame across repeated runs at multiple N.
        return self._bars.copy()


def build_synthetic_universe(
    n_symbols: int,
    *,
    n_bars: int = _DEFAULT_N_BARS,
    base_seed: int = 817,
) -> dict[str, _SyntheticProvider]:
    """Build a deterministic, offline N-symbol universe.

    This is the harness's stand-in for the production universe scan
    (``hermes_quant.universe.scan_universe``). The scan's *output contract* is a
    list of symbols; here we additionally attach a seeded synthetic provider to
    each so the downstream ``recommend()`` fan-out has bars to chew on without a
    network call.

    Args:
        n_symbols: how many symbols to synthesize (e.g. 5 in CI, 500+ at load).
        n_bars: daily bars per symbol (default ~1 trading year).
        base_seed: RNG base; ``(base_seed, symbol)`` keys each per-symbol RNG.

    Returns:
        Ordered mapping ``{symbol: provider}``. Symbols are ``LT0000`` … so the
        universe is reproducible and human-scannable in a report.
    """
    if n_symbols < 1:
        raise ValueError(f"n_symbols must be >= 1, got {n_symbols}")
    universe: dict[str, _SyntheticProvider] = {}
    width = max(4, len(str(n_symbols - 1)))
    for i in range(n_symbols):
        symbol = f"LT{i:0{width}d}"
        universe[symbol] = _SyntheticProvider(
            _synthetic_bars(symbol, n_bars, base_seed)
        )
    return universe


# ---------------------------------------------------------------------------
# Result / report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageTimings:
    """Wall-time (seconds) attributed to each coarse pipeline stage."""

    scan_s: float
    recommend_total_s: float
    # recommend() internally folds aggregate + gate; we surface the slowest and
    # mean per-symbol recommend latency so an operator can spot a degrading tail.
    per_symbol_mean_s: float
    per_symbol_p95_s: float
    per_symbol_max_s: float


@dataclass(frozen=True)
class SymbolResult:
    """Per-symbol outcome — enough to assert the pipeline actually ran."""

    symbol: str
    elapsed_s: float
    bars_received: int
    gate_pass: bool
    gated_reason: str | None
    n_analyst_views: int


@dataclass
class LoadTestReport:
    """One full-universe run's measurements.

    All fields are plain Python scalars / lists so the report is trivially
    JSON-serialisable for an operator log.
    """

    n_symbols: int
    n_bars: int
    base_seed: int
    wall_time_s: float
    peak_memory_mb: float
    throughput_symbols_per_s: float
    stage_timings: StageTimings
    n_gate_pass: int
    n_gate_silenced: int
    n_errors: int
    per_symbol: list[SymbolResult] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable one-block summary for an operator log."""
        st = self.stage_timings
        return (
            f"[loadtest] N={self.n_symbols} bars={self.n_bars} seed={self.base_seed}\n"
            f"  wall_time      : {self.wall_time_s:.3f} s\n"
            f"  throughput     : {self.throughput_symbols_per_s:.1f} symbols/s\n"
            f"  peak_memory    : {self.peak_memory_mb:.1f} MB\n"
            f"  scan           : {st.scan_s:.3f} s\n"
            f"  recommend tot  : {st.recommend_total_s:.3f} s\n"
            f"  per-symbol mean: {st.per_symbol_mean_s * 1000:.1f} ms\n"
            f"  per-symbol p95 : {st.per_symbol_p95_s * 1000:.1f} ms\n"
            f"  per-symbol max : {st.per_symbol_max_s * 1000:.1f} ms\n"
            f"  gate pass      : {self.n_gate_pass}\n"
            f"  gate silenced  : {self.n_gate_silenced}\n"
            f"  errors         : {self.n_errors}"
        )


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


def run_load_test(
    n_symbols: int,
    *,
    n_bars: int = _DEFAULT_N_BARS,
    base_seed: int = 817,
    as_of: str = "2026-05-29T00:00:00Z",
    timeframe: str = _DEFAULT_TIMEFRAME,
    keep_per_symbol: bool = True,
    universe: dict[str, _SyntheticProvider] | None = None,
    analysts_factory: Any = None,
    warmup: bool = False,
) -> LoadTestReport:
    """Run the full-universe pipeline once at ``n_symbols`` and measure it.

    Pipeline exercised (the genuine code paths, only the provider is synthetic):

        1. universe scan          -> :func:`build_synthetic_universe`
        2. advisor.recommend()    -> per symbol, which itself runs
           analysts -> BMA aggregate -> DefaultRiskGate
        3. aggregation across N   -> we tally pass / silence / error counts

    Memory is measured with :mod:`tracemalloc` (peak allocation during the run),
    which is allocator-accurate and machine-portable (unlike RSS). Wall-time is
    :func:`time.perf_counter`.

    Args:
        n_symbols: universe size to exercise.
        n_bars: bars per symbol.
        base_seed: RNG base seed (also tagged into the report).
        as_of: replay anchor passed to ``recommend()`` — keeps the run
            wall-clock-independent / deterministic.
        timeframe: bar timeframe handed to ``recommend()``.
        keep_per_symbol: retain per-symbol :class:`SymbolResult` rows. Set False
            at very large N to keep the report compact.
        universe: pre-built universe (skips the scan timing of building it). If
            None, the scan stage builds one and is timed.
        analysts_factory: optional zero-arg callable returning the analyst list
            to inject per symbol. When None (the operator full-N default),
            ``recommend()`` builds its canonical loadout — including the heavy
            Kronos foundation-model analyst, so the run measures *production*
            per-symbol latency. The deterministic small-N CI path passes a
            light, fast loadout here so the smoke test stays sub-second while
            still exercising the real ``recommend -> BMA aggregate ->
            DefaultRiskGate`` path. The factory is called fresh per symbol so a
            stateful analyst can't bleed state across the universe.
        warmup: when True, run two throwaway ``recommend()`` calls on a separate
            warm-up symbol BEFORE the measured region, so one-time process costs
            (module imports, sklearn/HMM JIT, regime-classifier cold fit) are
            paid outside the measurement. This matters for the scaling-exponent
            fit: those fixed costs otherwise dominate small-N wall-time and
            inflate the apparent exponent. Leave False for the most pessimistic
            "cold operator run" number; set True to measure steady-state cost.

    Returns:
        A :class:`LoadTestReport`.
    """
    # Import lazily so importing this module never drags the advisor (and its
    # optional analyst deps) into a plain ``import hermes_quant.loadtest``.
    from hermes_quant.advisor import recommend

    gc.collect()

    # ---- Stage 0: optional warm-up (NOT measured) ----
    # Pay one-time import / JIT / classifier-cold-fit costs before we start the
    # clock + tracemalloc so the steady-state per-symbol cost is what we report.
    if warmup:
        warm_provider = _SyntheticProvider(
            _synthetic_bars("LTWARMUP", n_bars, base_seed)
        )
        for _ in range(2):
            try:
                recommend(
                    symbol="LTWARMUP",
                    asset_class="equity",
                    timeframe=timeframe,
                    provider=warm_provider,
                    analysts=(analysts_factory() if analysts_factory else None),
                    include_lessons=False,
                    as_of=as_of,
                )
            except Exception:  # noqa: BLE001 — warm-up failure must not abort
                break

    tracemalloc.start()
    wall_start = time.perf_counter()

    # ---- Stage 1: universe scan (synthetic) ----
    scan_start = time.perf_counter()
    if universe is None:
        universe = build_synthetic_universe(
            n_symbols, n_bars=n_bars, base_seed=base_seed
        )
    scan_s = time.perf_counter() - scan_start

    # ---- Stage 2+3: recommend across N -> aggregate counts ----
    per_symbol: list[SymbolResult] = []
    per_symbol_elapsed: list[float] = []
    n_pass = n_silenced = n_errors = 0

    recommend_start = time.perf_counter()
    for symbol, provider in universe.items():
        sym_start = time.perf_counter()
        try:
            analysts = analysts_factory() if analysts_factory is not None else None
            result = recommend(
                symbol=symbol,
                asset_class="equity",
                timeframe=timeframe,
                provider=provider,
                analysts=analysts,
                include_lessons=False,  # journal IO is out of scope for load
                as_of=as_of,
            )
        except Exception:  # noqa: BLE001 — a load run must not abort on one symbol
            n_errors += 1
            elapsed = time.perf_counter() - sym_start
            per_symbol_elapsed.append(elapsed)
            if keep_per_symbol:
                per_symbol.append(
                    SymbolResult(
                        symbol=symbol,
                        elapsed_s=elapsed,
                        bars_received=0,
                        gate_pass=False,
                        gated_reason="harness_exception",
                        n_analyst_views=0,
                    )
                )
            continue

        elapsed = time.perf_counter() - sym_start
        per_symbol_elapsed.append(elapsed)

        gate = result.get("risk_gate") or {}
        gate_pass = bool(gate.get("pass"))
        if gate_pass:
            n_pass += 1
        else:
            n_silenced += 1

        if keep_per_symbol:
            per_symbol.append(
                SymbolResult(
                    symbol=symbol,
                    elapsed_s=elapsed,
                    bars_received=int(
                        (result.get("data_quality") or {}).get("bars_received", 0)
                    ),
                    gate_pass=gate_pass,
                    gated_reason=gate.get("gated_reason"),
                    n_analyst_views=len(result.get("analyst_views") or []),
                )
            )
    recommend_total_s = time.perf_counter() - recommend_start

    wall_time_s = time.perf_counter() - wall_start
    _current, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    elapsed_arr = np.asarray(per_symbol_elapsed, dtype=float)
    per_symbol_mean = float(elapsed_arr.mean()) if elapsed_arr.size else 0.0
    per_symbol_p95 = (
        float(np.percentile(elapsed_arr, 95)) if elapsed_arr.size else 0.0
    )
    per_symbol_max = float(elapsed_arr.max()) if elapsed_arr.size else 0.0

    throughput = (n_symbols / wall_time_s) if wall_time_s > 0 else 0.0

    return LoadTestReport(
        n_symbols=n_symbols,
        n_bars=n_bars,
        base_seed=base_seed,
        wall_time_s=wall_time_s,
        peak_memory_mb=peak_bytes / (1024 * 1024),
        throughput_symbols_per_s=throughput,
        stage_timings=StageTimings(
            scan_s=scan_s,
            recommend_total_s=recommend_total_s,
            per_symbol_mean_s=per_symbol_mean,
            per_symbol_p95_s=per_symbol_p95,
            per_symbol_max_s=per_symbol_max,
        ),
        n_gate_pass=n_pass,
        n_gate_silenced=n_silenced,
        n_errors=n_errors,
        per_symbol=per_symbol,
    )


# ---------------------------------------------------------------------------
# Super-linearity check
# ---------------------------------------------------------------------------


def fit_superlinearity(
    ns: Sequence[int], wall_times_s: Sequence[float]
) -> float:
    """Fit the scaling exponent ``alpha`` in ``wall_time ≈ c · N**alpha``.

    Estimated via an ordinary least-squares fit in log-log space:

        log(t) = log(c) + alpha · log(N)

    Interpretation:
        - ``alpha ≈ 1.0``  -> linear scaling (ideal; the per-symbol cost is flat).
        - ``alpha < 1.0``  -> sub-linear (fixed costs amortizing — also fine).
        - ``alpha >> 1.0`` -> **super-linear** — a step is degrading as the
          universe grows (e.g. an accidental O(N^2) aggregation). This is the
          number a load-readiness gate would assert an upper bound on.

    Needs at least two distinct (N, t) points with positive values.

    Args:
        ns: universe sizes (must be strictly positive, >= 2 distinct values).
        wall_times_s: corresponding wall-times (strictly positive).

    Returns:
        The fitted exponent ``alpha`` as a float.
    """
    ns_arr = np.asarray(ns, dtype=float)
    t_arr = np.asarray(wall_times_s, dtype=float)
    if ns_arr.shape != t_arr.shape:
        raise ValueError("ns and wall_times_s must have the same length")
    if ns_arr.size < 2:
        raise ValueError("need at least two (N, time) points to fit a slope")
    if np.any(ns_arr <= 0) or np.any(t_arr <= 0):
        raise ValueError("all N and wall_times must be strictly positive")
    if np.unique(ns_arr).size < 2:
        raise ValueError("need at least two DISTINCT N values to fit a slope")

    log_n = np.log(ns_arr)
    log_t = np.log(t_arr)
    # slope of the log-log line == the scaling exponent alpha
    alpha, _intercept = np.polyfit(log_n, log_t, 1)
    return float(alpha)


def run_scaling_sweep(
    ns: Iterable[int],
    *,
    n_bars: int = _DEFAULT_N_BARS,
    base_seed: int = 817,
    keep_per_symbol: bool = False,
    analysts_factory: Any = None,
    warmup: bool = True,
) -> tuple[list[LoadTestReport], float]:
    """Run the load test at each N in ``ns`` and return (reports, alpha).

    Convenience wrapper for the operator full-N flow and the deterministic
    small-N test: runs :func:`run_load_test` at each N, then fits the
    super-linearity exponent across the wall-times.

    ``analysts_factory`` is forwarded to :func:`run_load_test` (see its docstring)
    so the same sweep can exercise either the heavy production loadout (None) or
    a light deterministic one. ``warmup`` defaults to True here (vs False on the
    single-shot ``run_load_test``) because the exponent fit is only meaningful
    once fixed one-time costs are amortized out of every point in the sweep.

    Returns:
        ``(reports, alpha)`` where ``reports`` is one :class:`LoadTestReport`
        per N (in input order) and ``alpha`` is the fitted scaling exponent
        from :func:`fit_superlinearity`.
    """
    ns_list = list(ns)
    reports = [
        run_load_test(
            n,
            n_bars=n_bars,
            base_seed=base_seed,
            keep_per_symbol=keep_per_symbol,
            analysts_factory=analysts_factory,
            warmup=warmup,
        )
        for n in ns_list
    ]
    alpha = fit_superlinearity(
        [r.n_symbols for r in reports], [r.wall_time_s for r in reports]
    )
    return reports, alpha
