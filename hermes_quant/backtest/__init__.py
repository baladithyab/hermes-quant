"""hermes_quant.backtest — Historical replay of the advisor pipeline.

Per ADR-0020. The backtest is the charter's empirical gate:
  *"if your three-analyst committee on BTC can't beat buy-and-hold
   risk-adjusted, more analysts won't fix it."*

Public API:
- replay(bars, *, symbol, asset_class, timeframe, ...) -> BacktestResult
- PaperPortfolio (the mark-to-market accounting helper)
- BacktestResult (the per-run dataclass)

Cross-cuts ADR-0014 (advisor), ADR-0019 (DSR), AGENTS.md "Reproducibility".
"""
from .portfolio import PaperPortfolio
from .replay import BacktestResult, replay

__all__ = [
    "BacktestResult",
    "PaperPortfolio",
    "replay",
]
