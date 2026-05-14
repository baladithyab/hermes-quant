"""Walk-forward backtest composition.

Composes ADR-0019's PurgedWalkForward splitter with ADR-0020's replay
backtest. This is intentionally evaluation-only: no optimizer is allowed to
peek at the test fold. Train/validation windows are recorded as metadata for
future calibrator pretraining, but v0.4's first use is simply: run the same
advisor pipeline on each out-of-sample test slice and aggregate results.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from hermes_quant.backtest.replay import BacktestResult, replay
from hermes_quant.evaluation.cv import PurgedWalkForward, WalkForwardSplit


@dataclass(frozen=True)
class WalkForwardFoldResult:
    """One out-of-sample fold result."""

    fold: int
    split: WalkForwardSplit
    result: BacktestResult
    n_train_bars: int
    n_val_bars: int
    n_test_bars: int

    def to_dict(self) -> dict:
        return {
            "fold": self.fold,
            "split": {
                "train_start": self.split.train_start.isoformat(),
                "train_end": self.split.train_end.isoformat(),
                "val_start": self.split.val_start.isoformat(),
                "val_end": self.split.val_end.isoformat(),
                "test_start": self.split.test_start.isoformat(),
                "test_end": self.split.test_end.isoformat(),
            },
            "n_train_bars": self.n_train_bars,
            "n_val_bars": self.n_val_bars,
            "n_test_bars": self.n_test_bars,
            "result": self.result.to_dict(),
        }


@dataclass(frozen=True)
class WalkForwardBacktestResult:
    """Aggregate of multiple out-of-sample replay folds."""

    symbol: str
    timeframe: str
    asset_class: str
    n_splits: int
    folds: list[WalkForwardFoldResult] = field(default_factory=list)

    @property
    def mean_excess_return_vs_buy_hold_pct(self) -> float:
        if not self.folds:
            return float("nan")
        return sum(f.result.excess_return_vs_buy_hold_pct for f in self.folds) / len(self.folds)

    @property
    def mean_sharpe_delta(self) -> float:
        if not self.folds:
            return float("nan")
        return sum((f.result.sharpe - f.result.buy_hold_sharpe) for f in self.folds) / len(self.folds)

    @property
    def positive_excess_fold_rate(self) -> float:
        if not self.folds:
            return float("nan")
        return sum(1 for f in self.folds if f.result.excess_return_vs_buy_hold_pct > 0) / len(self.folds)

    @property
    def total_decisions(self) -> int:
        return sum(f.result.n_decisions for f in self.folds)

    @property
    def total_settlements(self) -> int:
        return sum(f.result.n_settlements for f in self.folds)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "asset_class": self.asset_class,
            "n_splits": self.n_splits,
            "mean_excess_return_vs_buy_hold_pct": self.mean_excess_return_vs_buy_hold_pct,
            "mean_sharpe_delta": self.mean_sharpe_delta,
            "positive_excess_fold_rate": self.positive_excess_fold_rate,
            "total_decisions": self.total_decisions,
            "total_settlements": self.total_settlements,
            "folds": [f.to_dict() for f in self.folds],
        }

    def to_markdown_report(self) -> str:
        lines = [
            f"# Walk-forward backtest: {self.symbol} {self.timeframe} ({self.asset_class})",
            "",
            "## Aggregate out-of-sample summary",
            "",
            f"- Folds: {self.n_splits}",
            f"- Mean excess return vs buy-and-hold: {self.mean_excess_return_vs_buy_hold_pct:+.2%}",
            f"- Mean Sharpe delta vs buy-and-hold: {self.mean_sharpe_delta:+.3f}",
            f"- Positive-excess fold rate: {self.positive_excess_fold_rate:.1%}",
            f"- Total decisions: {self.total_decisions}",
            f"- Total settlements: {self.total_settlements}",
            "",
            "## Fold table",
            "",
            "| Fold | Test window | Bars | Return | Buy/Hold | Excess | Sharpe Δ | Decisions | Settlements |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for fold in self.folds:
            r = fold.result
            lines.append(
                f"| {fold.fold} | {fold.split.test_start.date()} → {fold.split.test_end.date()} | "
                f"{fold.n_test_bars} | {r.total_return_pct:+.2%} | "
                f"{r.buy_hold_total_return_pct:+.2%} | {r.excess_return_vs_buy_hold_pct:+.2%} | "
                f"{r.sharpe - r.buy_hold_sharpe:+.3f} | {r.n_decisions} | {r.n_settlements} |"
            )
        lines.append("")
        if self.mean_excess_return_vs_buy_hold_pct <= 0:
            lines.extend([
                "## Charter decision",
                "",
                "**NEGATIVE aggregate excess return** — per charter, fix analysts/aggregator before RL aggregator work.",
                "",
            ])
        else:
            lines.extend([
                "## Charter decision",
                "",
                "**POSITIVE aggregate excess return** — paper-trade/live-reactor work may proceed only if risk gates and drawdown locks also pass.",
                "",
            ])
        return "\n".join(lines)


def walk_forward_replay(
    bars: pd.DataFrame,
    *,
    symbol: str,
    asset_class: str,
    timeframe: str,
    n_splits: int = 5,
    embargo_pct: float = 0.01,
    train_pct: float = 0.6,
    val_pct: float = 0.2,
    initial_equity: float = 10_000.0,
    warmup_bars: int = 60,
    commission: float = 0.001,
    slippage: float = 0.0005,
    settlement_horizon_bars: int = 1,
    learn_from_fills: bool = True,
    recipe_id: str | None = None,
    semantic_packets: list[dict] | None = None,
    committee_turns: list[dict] | None = None,
    advisor_recommend=None,
) -> WalkForwardBacktestResult:
    """Run replay() independently on each out-of-sample test fold.

    Important: each fold gets its own replay call and therefore its own default
    BMAAggregator. That prevents test-fold posterior state from leaking into
    later folds. Future calibrator-pretraining can explicitly train on the
    train/validation windows and inject a seeded aggregator, but that must be a
    separate opt-in path.
    """
    bars = bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars.sort_values("timestamp").reset_index(drop=True)

    splitter = PurgedWalkForward(
        n_splits=n_splits,
        embargo_pct=embargo_pct,
        train_pct=train_pct,
        val_pct=val_pct,
    )

    fold_results: list[WalkForwardFoldResult] = []
    for split in splitter.split(bars):
        train_mask = (bars["timestamp"] >= split.train_start) & (bars["timestamp"] < split.train_end)
        val_mask = (bars["timestamp"] >= split.val_start) & (bars["timestamp"] < split.val_end)
        test_mask = (bars["timestamp"] >= split.test_start) & (bars["timestamp"] <= split.test_end)
        test_bars = bars.loc[test_mask].copy().reset_index(drop=True)

        # replay() needs warmup inside the fold. If a test fold is too short,
        # use at most 40% of the fold as warmup so the fold still emits out-of-
        # sample observations. Small synthetic tests can pass warmup_bars lower.
        fold_warmup = min(warmup_bars, max(1, int(len(test_bars) * 0.4)))
        result = replay(
            test_bars,
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            initial_equity=initial_equity,
            warmup_bars=fold_warmup,
            commission=commission,
            slippage=slippage,
            settlement_horizon_bars=settlement_horizon_bars,
            learn_from_fills=learn_from_fills,
            recipe_id=recipe_id,
            semantic_packets=semantic_packets,
            committee_turns=committee_turns,
            advisor_recommend=advisor_recommend,
        )
        fold_results.append(WalkForwardFoldResult(
            fold=split.fold,
            split=split,
            result=result,
            n_train_bars=int(train_mask.sum()),
            n_val_bars=int(val_mask.sum()),
            n_test_bars=len(test_bars),
        ))

    return WalkForwardBacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        asset_class=asset_class,
        n_splits=n_splits,
        folds=fold_results,
    )
