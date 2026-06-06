"""hermes_quant.cli.ablate — `hermes quant ablate <flag>` (D2).

Operator verb for the flag-ablation harness (hermes_quant.backtest.ablation):
run the SAME walk-forward window with a feature flag OFF vs ON and print a
compact card — OFF metrics, ON metrics, deltas, deflated-Sharpe per side, and a
conservative PROMOTE/HOLD verdict. This turns "flip and hope" on a default-OFF
flag into "measure then promote".

Release-gate convention (mirrors tests/backtest/test_fundamentals_ablation.py +
the v0.6.1 charter): the heavy REAL-data path needs the universe bar cache +
yfinance history, so it is gated behind HERMES_QUANT_RUN_BACKTEST=1. Without that
flag the verb prints a clear "set HERMES_QUANT_RUN_BACKTEST=1; needs bar cache +
history" message and exits 0 — backtest ablations are a release-gate check, not a
per-PR check, so CI never stalls here.

A `--synthetic` self-test path runs fully offline on deterministic GBM bars (no
network) so the card-rendering + verdict plumbing is exercised per-PR.

READ-ONLY (ADR-0007): this verb only READS by running backtests. It never flips a
flag default, writes state, or moves capital.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from hermes_quant.backtest.ablation import AblationResult, run_flag_ablation
from hermes_quant.backtest.engine import WalkForwardConfig

_RUN_FLAG = "HERMES_QUANT_RUN_BACKTEST"


# ---------------------------------------------------------------------------
# Synthetic offline bars (self-test path)
# ---------------------------------------------------------------------------


def _synthetic_ohlcv(start: str, end: str, *, seed: int = 11) -> pd.DataFrame:
    """Deterministic GBM OHLCV for the --synthetic self-test (offline).

    Used only by --synthetic so the CLI card/verdict path runs offline. The
    drift is mildly positive so a long ensemble actually trades. The window is
    capped at ~70 business days regardless of the requested span: the self-test
    only needs to exercise the card/verdict plumbing, and the real advisor
    committee is heavy per-bar — a long synthetic window would make the per-PR
    unit test slow without adding coverage. (The release-gated real-data path
    honors the full requested window.)
    """
    dates = pd.bdate_range(start=start, periods=70)
    rng = np.random.default_rng(seed)
    n = len(dates)
    rets = rng.normal(0.0007, 0.013, n)
    closes = 100.0 * np.cumprod(1 + rets)
    opens = np.roll(closes, 1)
    opens[0] = 100.0
    highs = np.maximum(opens, closes) * 1.004
    lows = np.minimum(opens, closes) * 0.996
    volumes = rng.integers(500_000, 1_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


class _SyntheticAnalyst:
    """Lightweight deterministic analyst for the --synthetic self-test only.

    Emits a fixed-direction view once enough history exists. Two of these with
    OPPOSITE directions form a dissenting committee so flags like STACKING have
    something to bite (a unanimous committee makes STACKING a no-op — BMA
    vote-share math, see NOTES_ABLATION.md). No network, no foundation model.
    """

    def __init__(self, name: str, direction: int, conf: float) -> None:
        self.name = name
        self.timeframes = ["1d"]
        self.asset_classes = ["equity", "etf", "crypto", "fx"]
        self.enabled = True
        self._direction = direction
        self._conf = conf

    def analyze(self, ctx):
        if len(ctx.bars) < 5:
            return None
        from hermes_quant.protocol import AnalystView

        return AnalystView(
            analyst=self.name,
            direction=self._direction,
            magnitude=0.02,
            confidence=self._conf,
            confidence_raw=self._conf,
            horizon="1d",
        )

    def health(self) -> dict:
        return {"n_views_emitted": 0, "last_view_at": None, "error_count": 0}


def _synthetic_committee() -> list:
    """Two correlated long supporters + one short dissenter (offline, fast)."""
    return [
        _SyntheticAnalyst("syn-ta", 1, 0.7),
        _SyntheticAnalyst("syn-micro", 1, 0.7),
        _SyntheticAnalyst("syn-dissent", -1, 0.4),
    ]


def _config_for(ohlcv: pd.DataFrame) -> WalkForwardConfig:
    """Build a WalkForwardConfig: first ~30% train, the rest holdout."""
    dates = ohlcv.index
    split = max(20, len(dates) // 3)
    split = min(split, len(dates) - 2)
    return WalkForwardConfig(
        train_start=dates[0],
        train_end=dates[split - 1],
        holdout_start=dates[split],
        holdout_end=dates[-1],
        step_days=1,
        lookback_days=400,
        initial_nav=100_000.0,
    )


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------


def _print_card(result: AblationResult, *, window: str, universe: list[str]) -> None:
    off, on = result.off, result.on

    def _dsr(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.3f}"

    print(f"hermes-quant ablate — {result.flag}")
    print("=" * 64)
    print(f"  window:    {window}")
    print(f"  universe:  {', '.join(universe)}")
    print(f"  legs:      OFF={result.off_value!r}  ON={result.on_value!r}")
    print()
    print(f"  {'metric':<16}{'OFF':>12}{'ON':>12}{'Δ (ON-OFF)':>14}")
    print(f"  {'-' * 54}")
    _row("sharpe", off.sharpe, on.sharpe, result.d_sharpe)
    _row("sortino", off.sortino, on.sortino, result.d_sortino)
    _row("max_drawdown", off.max_drawdown, on.max_drawdown, result.d_maxdd)
    _row("total_return", off.total_return, on.total_return, result.d_total_return)
    _row("alpha_vs_bh", off.alpha_vs_benchmark, on.alpha_vs_benchmark, result.d_alpha)
    print(
        f"  {'n_trades':<16}{off.n_trades:>12d}{on.n_trades:>12d}"
        f"{result.d_n_trades:>+14d}"
    )
    print()
    print(f"  deflated-Sharpe:  OFF={_dsr(result.dsr_off)}   ON={_dsr(result.dsr_on)}")
    print()
    print(f"  VERDICT: {result.verdict}")
    print(f"    {result.verdict_reason}")


def _row(name: str, off_v: float, on_v: float, delta: float) -> None:
    print(f"  {name:<16}{_fmt(off_v):>12}{_fmt(on_v):>12}{_fmt(delta, signed=True):>14}")


def _fmt(v: float, *, signed: bool = False) -> str:
    """Format a metric for the card, taming huge/degenerate values.

    Sortino can blow up to ~1e14 when the downside-deviation denominator is
    near-zero on a smooth synthetic series; printing that raw wrecks the column
    alignment. Clamp the display to scientific notation past +/-1e6 and guard
    non-finite values so the operator card stays readable.
    """
    if not np.isfinite(v):
        return "nan" if np.isnan(v) else ("+inf" if v > 0 else "-inf")
    if abs(v) >= 1e6:
        return f"{v:+.2e}" if signed else f"{v:.2e}"
    return f"{v:+.4f}" if signed else f"{v:.4f}"


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def cmd_ablate(args: argparse.Namespace) -> int:
    """Run `hermes quant ablate`. Returns a shell exit code (0).

    Exit code is 0 in all non-error cases (gate-skip and ran-card alike): the
    PROMOTE/HOLD verdict is advisory evidence for a human, NOT a pass/fail gate
    that should fail CI. (Contrast `admission-precision`, which IS a gate.)
    """
    universe = [s.strip() for s in (args.universe or "SYN").split(",") if s.strip()]
    window = f"{args.from_date} → {args.to_date}"
    synthetic = bool(getattr(args, "synthetic", False))
    want_json = bool(getattr(args, "json", False))

    # NOT-MEASURABLE guard (codex review, "other defects"): the CLI always uses
    # AdvisorStrategy, which exercises the analyst-pool→BMA→gate chain but NOT the
    # reactor/admissibility seam and does not populate ctx.extras carriers. So
    # some advertised flags would run, produce a confident-looking HOLD card, and
    # silently report a FALSE NULL — the flag never actually toggled any decision.
    # Refuse to emit a verdict for those flags rather than mislead the operator;
    # point at NOTES_ABLATION.md (§ "ADMISSIBILITY / EVENT_RISK / ...") for why.
    # This is a documented scope boundary, not a measurement.
    flag = args.flag
    not_measurable = {
        # Reactor/admissibility-precondition seam — downstream of the advisor
        # signal; needs a reactor-level ablation harness (follow-up lane).
        "HERMES_QUANT_ADMISSIBILITY": "reactor/admissibility precondition (downstream of the advisor signal)",
        "HERMES_QUANT_BORROW_COST": "reactor borrow-carry precondition (downstream of the advisor signal)",
        # Gate/views seams that only bite when a ctx.extras carrier is supplied,
        # which the offline AdvisorStrategy path leaves empty.
        "HERMES_QUANT_EVENT_RISK": "risk-gate pre-event reject — needs an event_risk payload on ctx.extras / signal metadata",
        "HERMES_QUANT_GROUNDING_ENFORCE": "views→aggregator grounding seam — needs a ground_truth_block on ctx.extras",
        # BMA lesson-haircut: real but only bites with an injected loss_lesson
        # provider, which the default hermetic aggregator does not wire.
        "HERMES_QUANT_L2_LESSON_HAIRCUT": "BMA lesson-haircut — needs an injected loss_lesson_provider (default aggregator has none)",
    }
    if flag in not_measurable:
        reason = not_measurable[flag]
        msg = (
            f"hermes quant ablate: {flag} is NOT measurable through this CLI path. "
            f"It acts on the {reason}, which AdvisorStrategy does not exercise "
            f"offline — running it would print a misleading null verdict. See "
            f"NOTES_ABLATION.md for the measurability matrix and how to measure it "
            f"(inject the carrier/provider, or use a reactor-level harness)."
        )
        if want_json:
            print(
                json.dumps(
                    {
                        "ran": False,
                        "flag": flag,
                        "verdict": "NOT_MEASURABLE",
                        "reason": reason,
                        "message": msg,
                    },
                    indent=2,
                )
            )
        else:
            print(msg)
        return 0

    # Multi-symbol real-data ablations are silently wrong (codex review): the
    # real-data fetch loads only universe[0] and the engine then reuses that one
    # frame for every symbol. Hard-error rather than mismeasure. (--synthetic is
    # single-symbol "SYN" by construction, so it is unaffected.)
    if not synthetic and len(universe) > 1:
        msg = (
            f"hermes quant ablate: multi-symbol real-data ablation is not supported "
            f"(got {universe}). The real-data path is single-symbol in v0.1.2 — pass "
            f"exactly one symbol via --universe, or use --synthetic for the offline "
            f"smoke test."
        )
        if want_json:
            print(json.dumps({"ran": False, "error": "multi_symbol_unsupported", "message": msg}))
        else:
            print(msg)
        return 0

    # Release-gate: the real-data path needs the bar cache + yfinance history.
    # --synthetic bypasses the gate (it fabricates offline bars).
    if not synthetic and os.environ.get(_RUN_FLAG, "0") != "1":
        gate_msg = (
            f"hermes quant ablate: real-data ablation is release-gated. "
            f"Set {_RUN_FLAG}=1 to run it (needs the universe bar cache + yfinance "
            f"history). For an offline smoke test, pass --synthetic."
        )
        if want_json:
            print(
                json.dumps(
                    {
                        "ran": False,
                        "gate": _RUN_FLAG,
                        "flag": args.flag,
                        "message": gate_msg,
                    },
                    indent=2,
                )
            )
        else:
            print(gate_msg)
        return 0

    # Build OHLCV + config.
    if synthetic:
        ohlcv = _synthetic_ohlcv(args.from_date, args.to_date)
    else:
        ohlcv = _fetch_real_ohlcv(universe, args.from_date, args.to_date)
        if ohlcv is None or len(ohlcv) < 40:
            msg = (
                "hermes quant ablate: could not load enough real bars for "
                f"{universe} over {window}. Check the bar cache / provider, or "
                "use --synthetic for an offline smoke test."
            )
            if want_json:
                print(json.dumps({"ran": False, "error": "insufficient_bars", "message": msg}))
            else:
                print(msg)
            return 0

    config = _config_for(ohlcv)

    # The strategy: AdvisorStrategy runs the REAL analyst-pool -> BMA -> gate
    # chain so analyst-pool/BMA flags (the L2 cluster) are genuinely measurable.
    #
    # On the --synthetic self-test path we inject a LIGHTWEIGHT deterministic
    # committee (TA + a dissenter) instead of the full production loadout: the
    # self-test's job is to exercise the card/verdict/wiring offline and fast,
    # and the production Kronos foundation-model analyst is heavy per-bar — a
    # full-committee synthetic run would make the per-PR unit test slow without
    # adding coverage (D3's tests already cover the real committee + L2 flags).
    # A dissenter is included so flags like STACKING actually have something to
    # bite (see NOTES_ABLATION.md). The release-gated real-data path uses the
    # full production committee (analysts=None -> _build_default_analysts()).
    from hermes_quant.backtest.strategy import AdvisorStrategy

    def _factory():
        # synthetic -> lightweight committee; real -> None (full production loadout).
        analysts = _synthetic_committee() if synthetic else None
        return AdvisorStrategy(universe, analysts=analysts, learn_from_fills=True)

    result = run_flag_ablation(
        args.flag,
        on_value=args.on,
        off_value=args.off,
        strategy_factory=_factory,
        universe=universe,
        ohlcv=ohlcv,
        config=config,
    )

    if want_json:
        payload = {"ran": True, "window": window, "universe": universe, **result.to_dict()}
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_card(result, window=window, universe=universe)

    return 0


def _fetch_real_ohlcv(
    universe: list[str], from_date: str, to_date: str
) -> pd.DataFrame | None:
    """Fetch DatetimeIndex'd OHLCV for the FIRST universe symbol via yfinance.

    Single-symbol path (v0.1.2 advisor is single-symbol). Returns a frame the
    WalkForwardEngine accepts (DatetimeIndex, [open,high,low,close,volume]), or
    None on any failure — the caller degrades to a clear operator message rather
    than raising. Only reached when HERMES_QUANT_RUN_BACKTEST=1.
    """
    symbol = universe[0]
    try:
        from hermes_quant.data.yfinance_provider import YFinanceProvider
    except Exception:  # noqa: BLE001
        return None
    try:
        provider = YFinanceProvider()
        start = pd.Timestamp(from_date)
        end = pd.Timestamp(to_date)
        bars = provider.fetch_bars(symbol, "1d", start, end)
    except Exception:  # noqa: BLE001 — operator-facing degrade, never crash the CLI
        return None
    if bars is None or len(bars) == 0:
        return None
    # Provider returns a `timestamp` COLUMN; the engine wants a DatetimeIndex.
    if "timestamp" in bars.columns:
        bars = bars.set_index(pd.DatetimeIndex(pd.to_datetime(bars["timestamp"])))
        bars = bars.drop(columns=["timestamp"])
    return bars
