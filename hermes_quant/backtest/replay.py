"""hermes_quant.backtest.replay — Historical bar replay through the advisor.

Per ADR-0020 §D2 + §D4. Walks bars chronologically, calls
`advisor.recommend()` at each bar with the lookahead-safe as_of cutoff,
applies the resulting Action to a PaperPortfolio, accumulates equity
curve + buy-and-hold baseline + Sharpe + DSR + max drawdown.

The replay uses a thin `_ReplayProvider` that wraps the input bars and
honors the as_of contract (same Protocol as YFinanceProvider /
CcxtProvider).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes_quant.backtest.portfolio import PaperPortfolio
from hermes_quant.protocol import DataProviderError, DataQualityError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestResult:
    """Per ADR-0020 §D4."""

    symbol: str
    timeframe: str
    asset_class: str
    n_bars: int
    n_decisions: int
    n_fires: int
    n_settlements: int = 0  # episodes settled back into the aggregator (V03-5)

    initial_equity: float = 10_000.0
    final_equity: float = 10_000.0
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    sharpe: float = 0.0
    deflated_sharpe: float = float("nan")
    max_drawdown_pct: float = 0.0
    n_trades: int = 0

    # Buy-and-hold baseline (the charter-gating comparison)
    buy_hold_total_return_pct: float = 0.0
    buy_hold_sharpe: float = 0.0
    excess_return_vs_buy_hold_pct: float = 0.0

    # Per-bar series (for plotting / further analysis)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    bh_equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    positions: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    decisions_summary: list[dict] = field(default_factory=list)

    # Reproducibility + posterior diagnostics
    run_at: str = ""
    config_hash: str = ""
    aggregator_posteriors: dict | None = None  # snapshot of BMA per-analyst stats at end of run

    def to_dict(self) -> dict:
        """JSON-serializable view (excludes pd.Series; saves them as lists)."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "asset_class": self.asset_class,
            "n_bars": self.n_bars,
            "n_decisions": self.n_decisions,
            "n_fires": self.n_fires,
            "n_settlements": self.n_settlements,
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "total_return_pct": self.total_return_pct,
            "annualized_return_pct": self.annualized_return_pct,
            "sharpe": self.sharpe,
            "deflated_sharpe": self.deflated_sharpe,
            "max_drawdown_pct": self.max_drawdown_pct,
            "n_trades": self.n_trades,
            "buy_hold_total_return_pct": self.buy_hold_total_return_pct,
            "buy_hold_sharpe": self.buy_hold_sharpe,
            "excess_return_vs_buy_hold_pct": self.excess_return_vs_buy_hold_pct,
            "run_at": self.run_at,
            "config_hash": self.config_hash,
            "aggregator_posteriors": self.aggregator_posteriors,
        }

    def to_markdown_report(self) -> str:
        """Operator-readable summary."""
        lines = [
            f"# Backtest report: {self.symbol} {self.timeframe} ({self.asset_class})",
            "",
            f"**Run at**: {self.run_at}  ",
            f"**Config hash**: `{self.config_hash}`",
            "",
            "## Summary",
            "",
            "| Metric | Strategy | Buy-and-Hold | Delta |",
            "|---|---|---|---|",
            f"| Total return | {self.total_return_pct:+.2%} | {self.buy_hold_total_return_pct:+.2%} | {self.excess_return_vs_buy_hold_pct:+.2%} |",
            f"| Sharpe | {self.sharpe:.3f} | {self.buy_hold_sharpe:.3f} | {self.sharpe - self.buy_hold_sharpe:+.3f} |",
            "",
            "## Strategy details",
            "",
            f"- Bars processed: {self.n_bars}",
            f"- Decisions emitted: {self.n_decisions}",
            f"- Trades executed: {self.n_trades}",
            f"- FIRE actions: {self.n_fires}",
            f"- Episodes settled into aggregator: {self.n_settlements}",
            f"- Initial equity: ${self.initial_equity:,.2f}",
            f"- Final equity: ${self.final_equity:,.2f}",
            f"- Annualized return: {self.annualized_return_pct:+.2%}",
            f"- Max drawdown: {self.max_drawdown_pct:-.2%}",
            f"- Deflated Sharpe (PSR, n_trials=1): {self.deflated_sharpe:.3f}",
            "",
            "## Charter-gating headline",
            "",
            (
                f"**Excess return vs buy-and-hold: {self.excess_return_vs_buy_hold_pct:+.2%}**"
                if self.excess_return_vs_buy_hold_pct > 0
                else f"**Excess return vs buy-and-hold: {self.excess_return_vs_buy_hold_pct:+.2%}** "
                "(NEGATIVE — fix analysts/aggregator before RL aggregator work, per charter)"
            ),
            "",
        ]
        # Aggregator posterior table (Wave G observability surface)
        if self.aggregator_posteriors:
            stats = self.aggregator_posteriors.get("analyst_stats") or {}
            if stats:
                lines.extend(
                    [
                        "## Per-analyst empirical accuracy (BMA posteriors)",
                        "",
                        "| Analyst | n_obs | α | β | posterior_accuracy |",
                        "|---|---|---|---|---|",
                    ]
                )
                for name, s in sorted(stats.items()):
                    lines.append(
                        f"| `{name}` | {s.get('n_observations', 0)} | "
                        f"{s.get('alpha', 0):.2f} | {s.get('beta', 0):.2f} | "
                        f"{s.get('posterior_accuracy', 0.0):.3f} |"
                    )
                lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal: ReplayProvider — exposes input bars via DataProvider Protocol
# ---------------------------------------------------------------------------


class _ReplayProvider:
    """DataProvider that returns slices of a fixed input DataFrame
    honoring the as_of contract.

    Used internally by replay() to feed advisor.recommend() the same
    Protocol it expects from YFinanceProvider / CcxtProvider.
    """

    name = "replay"

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars.copy()
        # Normalize timestamp dtype
        self._bars["timestamp"] = pd.to_datetime(
            self._bars["timestamp"],
            utc=True,
        )
        self._bars = self._bars.sort_values("timestamp").reset_index(drop=True)

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        *,
        as_of: pd.Timestamp | None = None,
        # Tolerate the lookback_bars kwarg shape that other Protocol
        # consumers use, in case advisor-side signature changes.
        lookback_bars: int = 200,
    ) -> pd.DataFrame:
        """Honor the advisor's positional fetch_bars(symbol, timeframe,
        start, end, *, as_of) signature. Range bounds (start/end) are
        applied first if provided; then as_of clamps to <=as_of."""
        df = self._bars
        if start is not None:
            start_ts = pd.Timestamp(start)
            if start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("UTC")
            df = df[df["timestamp"] >= start_ts]
        if end is not None:
            end_ts = pd.Timestamp(end)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")
            df = df[df["timestamp"] <= end_ts]
        if as_of is not None:
            as_of_ts = pd.Timestamp(as_of)
            if as_of_ts.tzinfo is None:
                as_of_ts = as_of_ts.tz_localize("UTC")
            df = df[df["timestamp"] <= as_of_ts]
        if len(df) < 2:
            raise DataQualityError(f"replay provider has {len(df)} bars at as_of={as_of}; need >=2")
        return df.reset_index(drop=True)

    def health(self) -> dict:
        return {"name": self.name, "n_bars": len(self._bars)}


# ---------------------------------------------------------------------------
# Replay loop
# ---------------------------------------------------------------------------


def replay(
    bars: pd.DataFrame,
    *,
    symbol: str,
    asset_class: str,
    timeframe: str,
    initial_equity: float = 10_000.0,
    warmup_bars: int = 60,
    commission: float = 0.001,
    slippage: float = 0.0005,
    settlement_horizon_bars: int = 1,
    learn_from_fills: bool = True,
    recipe_id: str | None = None,
    semantic_packets: list[dict] | None = None,
    committee_turns: list[dict] | None = None,
    advisor_recommend=None,  # inject for testing
    aggregator=None,  # inject for testing or to seed posteriors
) -> BacktestResult:
    """Walk bars[warmup_bars:] forward chronologically, replaying through
    the advisor pipeline.

    Args:
        bars: OHLCV DataFrame (timestamp, open, high, low, close, volume)
        symbol, asset_class, timeframe: identifiers passed through to advisor
        initial_equity: starting NAV (default $10k)
        warmup_bars: bars consumed before first decision
        commission, slippage: applied to every PaperPortfolio fill
        settlement_horizon_bars: how many bars forward to look when constructing
            the EpisodeOutcome that feeds back into the aggregator's posteriors.
            1 = next-bar return (default, suitable for momentum/mean-reversion);
            for daily-bar swing strategies, 5 (one trading week) is more honest.
        learn_from_fills: if True, settle each decision back into the aggregator
            after `settlement_horizon_bars` bars elapse. Closes the calibrator
            feedback loop within the backtest. Default True per charter
            "continual learning loop".
        advisor_recommend: inject for testing
        aggregator: inject a pre-seeded BMAAggregator (e.g., from a prior
            backtest's posteriors) to test out-of-sample generalization.

    Returns BacktestResult with strategy + buy-and-hold metrics + per-bar
    equity curve.

    Raises:
        ValueError: bars insufficient for warmup_bars + 10
    """
    if len(bars) < warmup_bars + 10:
        raise ValueError(
            f"need at least {warmup_bars + 10} bars for backtest with "
            f"warmup_bars={warmup_bars}; got {len(bars)}"
        )

    # Lazy advisor import (avoid heavy deps for backtest-config tests)
    if advisor_recommend is None:
        from hermes_quant.advisor import recommend as advisor_recommend

    # Long-lived aggregator across the entire backtest: this is what makes
    # `learn_from_fills` actually work — fresh per-call aggregators from
    # the production advisor would lose their posterior updates between
    # bars. Per ADR-0020 §D8 + ADR-0009 §P1-10.
    if aggregator is None and learn_from_fills:
        if recipe_id:
            from hermes_quant.recipes import get_recipe, instantiate_recipe_aggregator

            aggregator = instantiate_recipe_aggregator(get_recipe(recipe_id))
        else:
            from hermes_quant.aggregators.bma import BMAAggregator

            aggregator = BMAAggregator()

    # Normalize bars
    bars = bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars.sort_values("timestamp").reset_index(drop=True)

    provider = _ReplayProvider(bars)
    portfolio = PaperPortfolio.fresh(initial_equity)

    # Equity-curve accumulators
    equity_records: list[tuple[pd.Timestamp, float]] = []
    bh_equity_records: list[tuple[pd.Timestamp, float]] = []
    position_records: list[tuple[pd.Timestamp, float]] = []
    decisions_summary: list[dict] = []

    # Buy-and-hold reference (compute over the same window the strategy sees)
    bh_anchor_close = float(bars["close"].iloc[warmup_bars])
    # cs80: fail-CLOSED on a non-finite/zero anchor close. The buy-and-hold
    # leg is divided into `initial_equity`; a 0.0 anchor -> bh_qty=inf and a NaN
    # anchor -> NaN poison, both of which silently corrupt the honesty metrics
    # (buy_hold_total_return_pct / buy_hold_sharpe / excess_return_vs_buy_hold_pct)
    # an operator reads to judge a strategy. A 0/NaN at the very first priced
    # bar means the price series itself is corrupt at the decision boundary, so
    # the WHOLE backtest is untrustworthy — raise, consistent with the
    # insufficient-bars guard above. The operator must never receive a silent
    # -inf/NaN excess-return.
    if not math.isfinite(bh_anchor_close) or bh_anchor_close <= 0.0:
        raise ValueError(
            "buy-and-hold anchor close at warmup boundary "
            f"(bar index {warmup_bars}) is non-finite or non-positive: "
            f"{bh_anchor_close!r}; the price series is corrupt at the decision "
            "boundary and the backtest cannot be computed honestly"
        )
    bh_qty = initial_equity / bh_anchor_close

    n_decisions = 0
    n_fires = 0
    n_settlements = 0

    # Pending settlements: list of (settle_at_idx, decision_idx, agg_signal_dict, decision_close)
    # When bar `i` advances past `settle_at_idx`, we construct an EpisodeOutcome
    # and feed it back to the long-lived aggregator. This is how the
    # calibrator-from-fills loop (V03-5) actually closes during a backtest —
    # in production the daemon's settlement_loop does it from the journal,
    # but inside replay we have full fwd-lookahead so we can settle inline.
    pending_settlements: list[dict] = []

    for i in range(warmup_bars, len(bars)):
        bar = bars.iloc[i]
        as_of = bar["timestamp"]
        bar_close = float(bar["close"])

        # First, settle any pending outcomes whose horizon has elapsed
        if learn_from_fills and aggregator is not None:
            still_pending = []
            for entry in pending_settlements:
                if i >= entry["settle_at_idx"]:
                    n_settlements += _settle_episode(
                        aggregator=aggregator,
                        entry=entry,
                        future_close=bar_close,
                    )
                else:
                    still_pending.append(entry)
            pending_settlements = still_pending

        # Advisor call
        try:
            kwargs = dict(
                symbol=symbol,
                asset_class=asset_class,
                timeframe=timeframe,
                as_of=as_of,
                provider=provider,
                include_lessons=False,
                recipe_id=recipe_id,
                market_extras={
                    "semantic_packets": semantic_packets or [],
                    "committee_turns": committee_turns or [],
                },
            )
            # Long-lived aggregator — pass through if we have one
            if aggregator is not None:
                kwargs["aggregator"] = aggregator
            result = advisor_recommend(**kwargs)
        except (DataProviderError, DataQualityError) as exc:
            logger.warning("backtest: advisor failed at %s: %s; skipping bar", as_of, exc)
            # Mark-to-market with no action
            equity_records.append((as_of, portfolio.equity(bar_close)))
            bh_equity_records.append((as_of, bh_qty * bar_close))
            position_records.append((as_of, portfolio.position_qty))
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backtest: advisor raised at %s: %s; treating as flat",
                as_of,
                exc,
                exc_info=True,
            )
            equity_records.append((as_of, portfolio.equity(bar_close)))
            bh_equity_records.append((as_of, bh_qty * bar_close))
            position_records.append((as_of, portfolio.position_qty))
            continue

        # Extract Action
        rg = result.get("risk_gate") or {}
        rg_pass = bool(rg.get("pass", False))
        target_pct = float(rg.get("kelly_fraction", 0.0)) if rg_pass else 0.0
        sig = result.get("aggregated_signal") or {}
        direction = int(sig.get("direction", 0))
        # `kelly_fraction` is ALREADY SIGNED — it is a verbatim copy of
        # Action.target_position_pct (protocol: "signed; -0.05 = 5% NAV short"),
        # produced by quarter_kelly_size() which returns a negative target for a
        # short signal. Multiplying by `direction` again double-applied the sign
        # and INVERTED every short trade into a long (and back), systematically
        # mis-scoring short-taking strategies against the ADR-0020 empirical
        # gate. Use it directly, as every other consumer does (autonomous.py,
        # tools.py, journal/writer.py).
        signed_target = target_pct

        if rg_pass and abs(signed_target) > 1e-9:
            n_decisions += 1
            # Schedule settlement for `settlement_horizon_bars` bars from now
            if learn_from_fills and aggregator is not None:
                settle_at = i + settlement_horizon_bars
                if settle_at < len(bars):
                    pending_settlements.append(
                        {
                            "settle_at_idx": settle_at,
                            "decision_idx": i,
                            "decision_close": bar_close,
                            "as_of": as_of,
                            "agg_signal_dict": sig,
                            "components": result.get("analyst_views", []),
                        }
                    )

        # Apply to portfolio
        trade = portfolio.apply_target(
            signed_target,
            bar_close,
            commission=commission,
            slippage=slippage,
        )
        if not trade.get("skipped"):
            n_fires += 1

        # Record equity (post-trade mark-to-market)
        equity_records.append((as_of, portfolio.equity(bar_close)))
        bh_equity_records.append((as_of, bh_qty * bar_close))
        position_records.append((as_of, portfolio.position_qty))
        signal_metadata = sig.get("metadata") or {}
        committee = signal_metadata.get("committee") or {}
        semantic_hashes = [
            ((view.get("metadata") or {}).get("packet_hash"))
            for view in result.get("analyst_views", [])
            if ((view.get("metadata") or {}).get("packet_hash"))
        ]
        decisions_summary.append(
            {
                "asof": as_of.isoformat(),
                "bar_close": bar_close,
                "signal_direction": direction,
                "kelly_fraction": float(rg.get("kelly_fraction", 0.0)),
                "rg_pass": rg_pass,
                "trade": trade if not trade.get("skipped") else None,
                "recipe": result.get("recipe"),
                "semantic_packet_hashes": semantic_hashes,
                "committee_turns_hashes": [
                    turn.get("input_hash")
                    for turn in committee.get("model_backed_turns", [])
                    if turn.get("input_hash")
                ],
                "committee_decision": committee.get("decision"),
                "aggregator": sig.get("aggregator"),
            }
        )

    # ---- Compute metrics ----
    equity_curve = pd.Series(
        [e for _, e in equity_records],
        index=pd.DatetimeIndex([t for t, _ in equity_records]),
        name="equity",
    )
    bh_equity_curve = pd.Series(
        [e for _, e in bh_equity_records],
        index=pd.DatetimeIndex([t for t, _ in bh_equity_records]),
        name="bh_equity",
    )
    positions = pd.Series(
        [q for _, q in position_records],
        index=pd.DatetimeIndex([t for t, _ in position_records]),
        name="position",
    )

    final_equity = float(equity_curve.iloc[-1])
    total_return_pct = (final_equity / initial_equity) - 1.0

    bh_final = float(bh_equity_curve.iloc[-1])
    bh_total_return_pct = (bh_final / initial_equity) - 1.0

    # Per-bar returns (for Sharpe)
    strat_returns = equity_curve.pct_change().dropna()
    bh_returns = bh_equity_curve.pct_change().dropna()

    # Bars-per-year for annualization (1d -> 252, 1h -> 24*365=8760, etc.)
    bars_per_year = _bars_per_year(timeframe)
    sharpe = _sharpe(strat_returns, bars_per_year=bars_per_year)
    bh_sharpe = _sharpe(bh_returns, bars_per_year=bars_per_year)

    n_observations = len(strat_returns)
    annualized_return_pct = (
        ((1 + total_return_pct) ** (bars_per_year / max(n_observations, 1))) - 1.0
        if n_observations > 0
        else 0.0
    )

    # Max drawdown
    running_max = equity_curve.cummax()
    drawdowns = (equity_curve - running_max) / running_max
    max_dd_pct = float(drawdowns.min()) if len(drawdowns) else 0.0

    # Deflated Sharpe (PSR with n_trials=1) — only when n_observations >= 30
    if n_observations >= 30:
        from hermes_quant.evaluation.dsr import deflated_sharpe

        skew = float(strat_returns.skew()) if n_observations >= 3 else 0.0
        kurtosis = float(strat_returns.kurtosis() + 3.0) if n_observations >= 4 else 3.0

        # cs56 (sibling of cs48 on the replay path): a zero-variance OOS
        # strategy series (e.g. bit-identical per-bar returns from a flat-but-
        # marked position, or a synthetic geometric-doubling instrument) makes
        # `_sharpe` return ±inf (see _sharpe below: std==0, mean!=0 branch).
        # dsr.deflated_sharpe then forms
        # ``variance_term = 1 - skew*SR + (kurt-1)/4*SR**2``; for a constant
        # series skew==0, so ``skew*inf == NaN`` -> variance_term is NaN, the
        # ``variance_term <= 0`` guard (NaN<=0 == False) is bypassed, and
        # ``Φ(sr_diff·sqrt(n-1)/sqrt(NaN))`` collapses to NaN WITHOUT raising —
        # the try/except below only catches ValueError/ZeroDivisionError, so the
        # NaN escapes into BacktestResult.deflated_sharpe and renders as ``null``
        # in result.json, INDISTINGUISHABLE from the legitimate
        # n_observations<30 low-power omission and silently erasing the
        # false-discovery hedge from the operator's view of a degenerate
        # backtest. Mirror cs48's validation.py guard EXACTLY: when any DSR input
        # is non-finite the deflated Sharpe is not estimable; report a
        # CONSERVATIVE finite 0.0 (zero probability the Sharpe is real — fails any
        # ``dsr >= floor`` gate) plus a warning. A finite-variance series leaves
        # every input finite, this guard never fires, and the result is
        # byte-identical to the bare dsr call.
        if not (
            np.isfinite(sharpe) and np.isfinite(skew) and np.isfinite(kurtosis)
        ):
            dsr = 0.0
            logger.warning(
                "backtest: non-finite Sharpe/skew/kurtosis "
                "(sharpe=%s, skew=%s, kurtosis=%s); degenerate (likely "
                "zero-variance) OOS series. Reporting a conservative deflated "
                "Sharpe of 0.0 (fails the DSR floor) rather than a NaN that "
                "would render as null and masquerade as a low-power omission.",
                sharpe,
                skew,
                kurtosis,
            )
        else:
            try:
                dsr = deflated_sharpe(
                    observed_sharpe=sharpe,
                    n_trials=1,
                    n_observations=n_observations,
                    skew=skew,
                    kurtosis=kurtosis,
                )
            except (ValueError, ZeroDivisionError):
                dsr = float("nan")
            else:
                # Defensive belt: dsr.deflated_sharpe can in principle return a
                # non-finite probability if a future input combination escapes
                # its internal guards. Never let a NaN/inf DSR reach the
                # artifact; collapse to the conservative 0.0.
                if not np.isfinite(dsr):
                    logger.warning(
                        "backtest: non-finite deflated Sharpe result; reporting "
                        "a conservative 0.0 (fails the DSR floor)."
                    )
                    dsr = 0.0
    else:
        dsr = float("nan")

    # Config hash for reproducibility
    config_hash = _compute_config_hash(
        symbol=symbol,
        timeframe=timeframe,
        asset_class=asset_class,
        warmup_bars=warmup_bars,
        commission=commission,
        slippage=slippage,
        settlement_horizon_bars=settlement_horizon_bars,
        learn_from_fills=learn_from_fills,
        recipe_id=recipe_id,
        semantic_packet_hashes=[
            p.get("packet_hash") for p in (semantic_packets or []) if p.get("packet_hash")
        ],
        committee_turn_hashes=[
            t.get("input_hash") for t in (committee_turns or []) if t.get("input_hash")
        ],
        n_bars=len(bars),
    )

    # Snapshot final aggregator posteriors (closes Wave G observability loop —
    # operators can see how each analyst's empirical accuracy evolved during
    # the backtest window).
    aggregator_posteriors = None
    if aggregator is not None and hasattr(aggregator, "status"):
        try:
            aggregator_posteriors = aggregator.status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("aggregator.status() failed: %s", exc)

    return BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        asset_class=asset_class,
        n_bars=len(bars) - warmup_bars,
        n_decisions=n_decisions,
        n_fires=n_fires,
        n_settlements=n_settlements,
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct,
        sharpe=sharpe,
        deflated_sharpe=dsr,
        max_drawdown_pct=max_dd_pct,
        n_trades=portfolio.n_trades,
        buy_hold_total_return_pct=bh_total_return_pct,
        buy_hold_sharpe=bh_sharpe,
        excess_return_vs_buy_hold_pct=total_return_pct - bh_total_return_pct,
        equity_curve=equity_curve,
        bh_equity_curve=bh_equity_curve,
        positions=positions,
        decisions_summary=decisions_summary,
        run_at=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
        config_hash=config_hash,
        aggregator_posteriors=aggregator_posteriors,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sharpe(returns: pd.Series, *, bars_per_year: float, rf_rate: float = 0.0) -> float:
    """Annualized Sharpe ratio."""
    if len(returns) < 2:
        return float("nan")
    mean = returns.mean() - rf_rate / bars_per_year
    std = returns.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0 if mean == 0 else float("inf") * np.sign(mean)
    return float(mean / std * np.sqrt(bars_per_year))


def _bars_per_year(timeframe: str) -> float:
    """Approximate annualization factor by bar timeframe."""
    return {
        "1m": 252 * 6.5 * 60,
        "5m": 252 * 6.5 * 12,
        "15m": 252 * 6.5 * 4,
        "30m": 252 * 6.5 * 2,
        "1h": 252 * 6.5,  # equity hours/year (close enough for crypto too)
        "4h": 252 * 1.6,
        "1d": 252,
    }.get(timeframe, 252)


def _compute_config_hash(**kwargs) -> str:
    """sha256 of sorted kwargs. The hash pins the reproducibility bound:
    same hash + same bars => same result (charter Reproducibility invariant).
    """
    raw = json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _settle_episode(
    *,
    aggregator,
    entry: dict,
    future_close: float,
) -> int:
    """Construct an EpisodeOutcome from a pending settlement and feed it
    back to the aggregator. Returns 1 on successful update, 0 if skipped.

    The realized return is (future_close - decision_close) / decision_close.
    For each component analyst, direction_correct = (sign(realized_return)
    == component.direction).
    """
    try:
        from hermes_quant.protocol import (
            AggregatedSignal,
            AnalystView,
            EpisodeOutcome,
        )
    except ImportError:
        return 0

    decision_close = entry["decision_close"]
    if decision_close <= 0:
        return 0
    realized_return = (future_close - decision_close) / decision_close

    # The components passed in are dicts (from advisor's analyst_views list);
    # we reconstruct AnalystView objects for the EpisodeOutcome.
    components_dicts = entry.get("components") or []
    components: list = []
    direction_correct: dict[str, bool] = {}
    for c in components_dicts:
        try:
            view = AnalystView(
                analyst=c.get("analyst", "unknown"),
                direction=int(c.get("direction", 0)),
                magnitude=float(c.get("magnitude", 0.0)),
                confidence=float(c.get("confidence", 0.0)),
                confidence_raw=float(c.get("confidence_raw", c.get("confidence", 0.0))),
                horizon=str(c.get("horizon", "1h")),
            )
        except (TypeError, ValueError):
            continue
        components.append(view)
        # direction_correct: did the analyst's direction match the realized?
        if view.direction == 0:
            # Flat call — only "correct" if realized was also (approximately) flat
            direction_correct[view.analyst] = abs(realized_return) < 1e-6
        else:
            direction_correct[view.analyst] = (view.direction > 0 and realized_return > 0) or (
                view.direction < 0 and realized_return < 0
            )

    if not components:
        return 0

    sig = entry["agg_signal_dict"]
    try:
        agg_sig = AggregatedSignal(
            asset=sig.get("asset", "unknown"),
            timeframe=sig.get("timeframe", "1h"),
            asset_class=sig.get("asset_class", "unknown"),
            asof=pd.Timestamp(entry["as_of"]),
            direction=int(sig.get("direction", 0)),
            magnitude=float(sig.get("magnitude", 0.0)),
            confidence=float(sig.get("confidence", 0.0)),
            confidence_raw=float(sig.get("confidence_raw", sig.get("confidence", 0.0))),
            horizon=str(sig.get("horizon", "1h")),
            components=tuple(components),
            aggregator=sig.get("aggregator", "bma"),
        )
        outcome = EpisodeOutcome(
            asset=agg_sig.asset,
            timeframe=agg_sig.timeframe,
            asof=agg_sig.asof,
            aggregated_signal=agg_sig,
            realized_returns={agg_sig.horizon: realized_return},
            direction_correct=direction_correct,
            realized_net_pnl=None,  # paper backtest; per-trade P&L handled by PaperPortfolio
        )
    except (TypeError, ValueError) as exc:
        logger.debug("settle_episode: failed to build EpisodeOutcome: %s", exc)
        return 0

    try:
        aggregator.update(outcome)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "settle_episode: aggregator.update raised: %s",
            exc,
            exc_info=True,
        )
        return 0
