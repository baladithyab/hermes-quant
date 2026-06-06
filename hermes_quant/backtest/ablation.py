"""hermes_quant.backtest.ablation — FlagAblation harness (D1).

Purpose
-------
hermes-quant promotes feature flags to default-ON. Several high-value flags
(the L2 learning-loop cluster in ``aggregators/bma.py`` —
``HERMES_QUANT_STACKING`` / ``L2_POSTERIOR_DECAY`` / ``L2_PER_ANALYST_CALIB`` /
``L2_LESSON_HAIRCUT`` / ``L2_POSTERIOR_PERSIST`` — plus ``ADMISSIBILITY`` /
``EVENT_RISK`` / ``BORROW_COST`` / ``GROUNDING_ENFORCE``) are DEFAULT-OFF
"pending eval", but there was no harness to actually *run* that eval. This
module is it: a reusable A/B backtest that runs the SAME walk-forward window
with a flag OFF vs ON and reports the Sharpe / Sortino / maxDD / DSR delta plus
a conservative PROMOTE / HOLD verdict. It converts "flip and hope" into "measure
then promote."

Discipline (money-software)
---------------------------
* ADDITIVE + READ-ONLY: this is a NEW eval tool. It changes no production
  decision path, no flag default, and no daemon/advisor behavior. It only READS
  by running backtests.
* NO ENV LEAKAGE: the flag is toggled inside an ``_env_override`` context
  manager whose ``finally`` restores the prior value EXACTLY — distinguishing
  "was unset" (restore by ``del``) from "was set to V" (restore by assign). A
  test proves ``os.environ`` is unchanged after the call for both cases.
* DETERMINISM: both legs run the SAME window/universe/ohlcv. The only difference
  is the flag value, so an off-vs-off run (``on_value == off_value``) is
  BIT-IDENTICAL — the engine has no RNG of its own; the strategy is responsible
  for its own determinism (the dry-run stubs are deterministic by contract).
* No-lookahead: the harness relies entirely on ``WalkForwardEngine``'s lookahead
  guard. It never reads holdout data itself.

The verdict policy (see ``verdict``) is deliberately conservative: a marginal or
noisy delta is HOLD, not PROMOTE, because the answer gates real capital.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes_quant.backtest.cost_model import CostModel
from hermes_quant.backtest.engine import (
    WalkForwardConfig,
    WalkForwardEngine,
    WalkForwardResult,
)
from hermes_quant.backtest.strategy import Strategy
from hermes_quant.evaluation.dsr import deflated_sharpe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict thresholds (ADR-style rationale)
# ---------------------------------------------------------------------------
#
# These thresholds are the promote/hold policy. They are intentionally on the
# conservative side of the literature because a PROMOTE flips a default that
# moves real capital, and a false-promote is strictly more expensive than a
# false-hold (you can always promote next quarter; you cannot un-lose money).
#
#   PROMOTE_MIN_D_SHARPE = +0.10
#       The ON leg must beat the OFF leg by at least 0.10 annualised Sharpe.
#       Below this the delta is within the run-to-run noise band of a single
#       walk-forward window (one path, no bootstrap) and is not evidence of a
#       real, repeatable edge. 0.10 is a deliberately modest bar — it is the
#       floor for "worth a closer look", not a claim of large effect.
#
#   MAXDD_TOLERANCE = 0.05  (5 percentage points)
#       ON's max drawdown may not be materially worse than OFF's. "Materially"
#       = more than 5pp deeper. A flag that buys +0.15 Sharpe by doubling the
#       drawdown is NOT a promote: drawdown is the constraint that ends careers
#       and trips circuit breakers (ADR-0004). max_drawdown is a negative
#       fraction, so "worse" means more negative: on.maxdd < off.maxdd - 0.05.
#
#   DSR_MIN = 0.50
#       The ON leg's Deflated Sharpe Ratio (probability the Sharpe is not a
#       false discovery, Bailey & Lopez de Prado 2014) must be > 0.50 — i.e.
#       the ON Sharpe must be more-likely-than-not real, not a coin flip. With
#       n_trials=1 this is the Probabilistic Sharpe Ratio of the observed
#       Sharpe. If DSR is not computable (too few observations, < 31), we cannot
#       confirm significance and therefore HOLD — absence of evidence is not
#       evidence of edge.
#
# All three must hold for PROMOTE. Any single failure -> HOLD with a reason.

PROMOTE_MIN_D_SHARPE: float = 0.10
MAXDD_TOLERANCE: float = 0.05
DSR_MIN: float = 0.50


# ---------------------------------------------------------------------------
# Env override context manager — the no-leakage primitive
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _env_override(key: str, value: str) -> Iterator[None]:
    """Temporarily set ``os.environ[key] = value``; restore EXACTLY on exit.

    The restore distinguishes the two prior states so global env is never
    mutated by a run:

      * key was ABSENT  -> remove it again on exit (``del``), NOT set to "".
      * key was SET to V -> reassign V on exit.

    ``finally`` guarantees restoration even if the body raises.
    """
    sentinel = object()
    prior: str | object = os.environ.get(key, sentinel)
    os.environ[key] = value
    try:
        yield
    finally:
        if prior is sentinel:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# AblationResult
# ---------------------------------------------------------------------------


@dataclass
class AblationResult:
    """The output of one flag ablation: OFF leg, ON leg, deltas, DSR, verdict.

    Attributes
    ----------
    flag:
        The env var that was ablated (e.g. ``HERMES_QUANT_STACKING``).
    off_value / on_value:
        The string values the flag was set to on each leg (default "0"/"1").
    off / on:
        The two ``WalkForwardResult`` objects. The ONLY intended difference
        between them is the flag value.
    d_sharpe / d_sortino / d_maxdd / d_total_return / d_alpha:
        ON minus OFF for each metric. d_maxdd is on.max_drawdown -
        off.max_drawdown (both negative fractions; a POSITIVE d_maxdd means ON
        had a shallower — better — drawdown).
    d_n_trades:
        on.n_trades - off.n_trades.
    dsr_off / dsr_on:
        Deflated Sharpe (probability the Sharpe is not a false discovery) per
        leg, or None when uncomputable (< 31 return observations).
    verdict:
        "PROMOTE" or "HOLD" per the conservative policy in ``verdict()``.
    verdict_reason:
        Human-readable justification for the verdict.
    """

    flag: str
    off_value: str
    on_value: str
    off: WalkForwardResult
    on: WalkForwardResult

    d_sharpe: float = 0.0
    d_sortino: float = 0.0
    d_maxdd: float = 0.0
    d_total_return: float = 0.0
    d_alpha: float = 0.0
    d_n_trades: int = 0

    dsr_off: float | None = None
    dsr_on: float | None = None

    verdict: str = "HOLD"
    verdict_reason: str = ""

    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-serializable summary (drops the heavy nav_series / journal)."""

        def _leg(r: WalkForwardResult) -> dict:
            return {
                "sharpe": r.sharpe,
                "sortino": r.sortino,
                "max_drawdown": r.max_drawdown,
                "total_return": r.total_return,
                "alpha_vs_benchmark": r.alpha_vs_benchmark,
                "n_trades": r.n_trades,
                "win_rate": r.win_rate,
            }

        return {
            "flag": self.flag,
            "off_value": self.off_value,
            "on_value": self.on_value,
            "off": _leg(self.off),
            "on": _leg(self.on),
            "deltas": {
                "d_sharpe": self.d_sharpe,
                "d_sortino": self.d_sortino,
                "d_maxdd": self.d_maxdd,
                "d_total_return": self.d_total_return,
                "d_alpha": self.d_alpha,
                "d_n_trades": self.d_n_trades,
            },
            "dsr_off": self.dsr_off,
            "dsr_on": self.dsr_on,
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
        }


# ---------------------------------------------------------------------------
# DSR helper
# ---------------------------------------------------------------------------


def _leg_dsr(result: WalkForwardResult) -> float | None:
    """Deflated Sharpe for one leg, or None when uncomputable.

    DSR (n_trials=1 == Probabilistic Sharpe Ratio) needs >= 30 return
    observations. The NAV series has len = n_holdout_days + 1, so there are
    len(nav_series) - 1 daily returns. Skew/kurtosis are estimated from the
    realized daily returns so the PSR variance term reflects fat tails honestly.
    """
    nav = list(result.nav_series or [])
    if len(nav) < 2:
        return None
    arr = np.asarray(nav, dtype=float)
    rets = np.diff(arr) / np.where(arr[:-1] != 0.0, arr[:-1], 1.0)
    n_obs = int(len(rets))
    if n_obs < 30:
        return None
    skew = float(_sample_skew(rets))
    kurt = float(_sample_kurtosis(rets))
    try:
        return float(
            deflated_sharpe(
                result.sharpe,
                n_trials=1,
                n_observations=n_obs,
                skew=skew,
                kurtosis=kurt,
            )
        )
    except ValueError:
        return None


def _sample_skew(x: np.ndarray) -> float:
    if len(x) < 3:
        return 0.0
    m = x.mean()
    s = x.std(ddof=0)
    if s == 0:
        return 0.0
    return float(np.mean(((x - m) / s) ** 3))


def _sample_kurtosis(x: np.ndarray) -> float:
    """NON-excess kurtosis (normal == 3.0), matching deflated_sharpe()'s contract."""
    if len(x) < 4:
        return 3.0
    m = x.mean()
    s = x.std(ddof=0)
    if s == 0:
        return 3.0
    return float(np.mean(((x - m) / s) ** 4))


# ---------------------------------------------------------------------------
# Verdict policy
# ---------------------------------------------------------------------------


def verdict(result: AblationResult) -> str:
    """Return "PROMOTE" or "HOLD" and stamp ``result.verdict[_reason]``.

    Conservative by design — this gates real capital (see module + threshold
    docstrings). PROMOTE requires ALL of:

      1. d_sharpe >= PROMOTE_MIN_D_SHARPE (a meaningful Sharpe improvement),
      2. ON's max drawdown not materially worse than OFF's
         (on.max_drawdown >= off.max_drawdown - MAXDD_TOLERANCE), and
      3. ON's DSR computable AND > DSR_MIN (the ON Sharpe is more-likely-than-
         not a real edge, not a false discovery).

    Any single failure -> HOLD, with the first failing reason recorded. A
    marginal/noisy delta is HOLD, never PROMOTE. NON-FINITE inputs (NaN/inf
    Sharpe or drawdown — degenerate legs) force HOLD: a comparison against NaN is
    always False, which would otherwise let a NaN delta silently bypass the
    Sharpe gate and emit a spurious PROMOTE.
    """
    reasons: list[str] = []

    # Non-finite guard FIRST — a degenerate leg (NaN/inf Sharpe or drawdown) can
    # never be evidence of a real, repeatable edge. Fail closed to HOLD before
    # any `<` comparison (which is False against NaN and would let it slip).
    if not (
        math.isfinite(result.d_sharpe)
        and math.isfinite(result.on.max_drawdown)
        and math.isfinite(result.off.max_drawdown)
    ):
        result.verdict = "HOLD"
        result.verdict_reason = (
            f"non-finite metric (d_sharpe={result.d_sharpe}, "
            f"on.maxdd={result.on.max_drawdown}, off.maxdd={result.off.max_drawdown}) "
            "— cannot confirm a real edge"
        )
        return result.verdict

    # Use `not (a >= b)` rather than `a < b` so a NaN that somehow reaches here
    # still fails closed (NaN >= x is False -> the gate is treated as failed).
    if not (result.d_sharpe >= PROMOTE_MIN_D_SHARPE):
        reasons.append(
            f"d_sharpe {result.d_sharpe:+.3f} < required +{PROMOTE_MIN_D_SHARPE:.2f} "
            "(improvement within noise band)"
        )

    # max_drawdown is negative; ON worse means more-negative than OFF by > tol.
    if result.on.max_drawdown < result.off.max_drawdown - MAXDD_TOLERANCE:
        worsening = result.off.max_drawdown - result.on.max_drawdown
        reasons.append(
            f"max drawdown materially worse by {worsening:.3f} "
            f"(> {MAXDD_TOLERANCE:.2f} tolerance)"
        )

    if result.dsr_on is None:
        reasons.append("ON deflated-Sharpe uncomputable (< 30 observations) — cannot confirm edge")
    elif not math.isfinite(result.dsr_on):
        # NaN/inf DSR is NOT < 0.50 (any comparison against NaN is False), so it
        # would slip past the `<= DSR_MIN` gate below and reach PROMOTE. A
        # degenerate significance estimate is never evidence of a real edge —
        # fail closed to HOLD (codex review, claim 1).
        reasons.append(
            f"ON deflated-Sharpe {result.dsr_on} is not finite — "
            "degenerate significance estimate cannot confirm edge"
        )
    elif result.dsr_on <= DSR_MIN:
        reasons.append(
            f"ON deflated-Sharpe {result.dsr_on:.3f} <= {DSR_MIN:.2f} "
            "(ON Sharpe not more-likely-than-not real)"
        )

    if reasons:
        result.verdict = "HOLD"
        result.verdict_reason = "; ".join(reasons)
    else:
        result.verdict = "PROMOTE"
        result.verdict_reason = (
            f"d_sharpe {result.d_sharpe:+.3f} >= +{PROMOTE_MIN_D_SHARPE:.2f}, "
            f"drawdown not materially worse, ON DSR {result.dsr_on:.3f} > {DSR_MIN:.2f}"
        )
    return result.verdict


# ---------------------------------------------------------------------------
# Result assembly (shared by run_flag_ablation + verdict unit tests)
# ---------------------------------------------------------------------------


def _assemble_result(
    *,
    flag: str,
    on_value: str,
    off_value: str,
    off: WalkForwardResult,
    on: WalkForwardResult,
) -> AblationResult:
    """Compute deltas + DSR + verdict from two finished legs."""
    res = AblationResult(
        flag=flag,
        off_value=off_value,
        on_value=on_value,
        off=off,
        on=on,
        d_sharpe=on.sharpe - off.sharpe,
        d_sortino=on.sortino - off.sortino,
        d_maxdd=on.max_drawdown - off.max_drawdown,
        d_total_return=on.total_return - off.total_return,
        d_alpha=on.alpha_vs_benchmark - off.alpha_vs_benchmark,
        d_n_trades=on.n_trades - off.n_trades,
        dsr_off=_leg_dsr(off),
        dsr_on=_leg_dsr(on),
    )
    verdict(res)
    return res


# ---------------------------------------------------------------------------
# The core primitive
# ---------------------------------------------------------------------------


def run_flag_ablation(
    flag: str,
    *,
    on_value: str = "1",
    off_value: str = "0",
    strategy_factory: Callable[[], Strategy],
    universe: list[str],
    ohlcv: pd.DataFrame,
    config: WalkForwardConfig,
    cost_model: CostModel | None = None,
) -> AblationResult:
    """Run the SAME walk-forward window with ``flag`` OFF vs ON; report the delta.

    The harness:
      (a) runs the engine ONCE with ``os.environ[flag] = off_value``,
      (b) runs it AGAIN with ``os.environ[flag] = on_value``, on the SAME
          window/universe/ohlcv,
      (c) restores the prior env value in a ``finally`` (no global leakage —
          via ``_env_override``), and
      (d) returns an :class:`AblationResult` with both legs, the metric deltas,
          a deflated-Sharpe per side, and a PROMOTE/HOLD verdict.

    The strategy is built by ``strategy_factory`` INSIDE each flag context, so
    flags that are read at strategy/aggregator construction time (e.g. BMA's
    posterior-persist load) take effect. A fresh strategy per leg also prevents
    state from the OFF leg bleeding into the ON leg.

    Determinism: ``on_value == off_value`` makes the two legs bit-identical (the
    engine has no internal RNG; deterministic strategies produce identical
    journals). The unit suite asserts this.

    Parameters
    ----------
    flag:
        Env var to ablate (e.g. ``"HERMES_QUANT_STACKING"``).
    on_value / off_value:
        String values for the ON / OFF legs. Default "1" / "0".
    strategy_factory:
        Zero-arg callable returning a fresh ``Strategy``. Called once per leg,
        inside that leg's flag context.
    universe:
        Tickers passed to the engine (benchmark + fill bookkeeping).
    ohlcv:
        Full OHLCV spanning the config window. Passed unchanged to both legs.
    config:
        The ``WalkForwardConfig`` (identical for both legs).
    cost_model:
        Optional cost model; defaults to the engine's LIQUID_EQUITY.

    Returns
    -------
    AblationResult
    """

    def _run_leg(value: str) -> WalkForwardResult:
        with _env_override(flag, value):
            strategy = strategy_factory()
            engine = WalkForwardEngine(config)
            return engine.run(
                strategy,
                universe,
                ohlcv,
                cost_model=cost_model,
                dry_run_llm=True,
            )

    off_result = _run_leg(off_value)
    on_result = _run_leg(on_value)

    logger.info(
        "flag-ablation %s: OFF(sharpe=%.3f, maxdd=%.3f, n=%d) vs "
        "ON(sharpe=%.3f, maxdd=%.3f, n=%d)",
        flag,
        off_result.sharpe,
        off_result.max_drawdown,
        off_result.n_trades,
        on_result.sharpe,
        on_result.max_drawdown,
        on_result.n_trades,
    )

    return _assemble_result(
        flag=flag,
        on_value=on_value,
        off_value=off_value,
        off=off_result,
        on=on_result,
    )
