"""hermes_quant.backtest — Historical replay + walk-forward backtesting.

Per ADR-0020 (original backtest) and ADR-0045 (Wave 6a: CostModel + WalkForwardEngine).

The backtest is the charter's empirical gate:
  *"if your three-analyst committee on BTC can't beat buy-and-hold
   risk-adjusted, more analysts won't fix it."*

Wave 6a public API (ADR-0045):
- CostModel / LIQUID_EQUITY / MIDCAP_EQUITY / ILLIQUID
- WalkForwardConfig / WalkForwardEngine / WalkForwardResult
- LookaheadViolation (raised on leakage attempts)
- StubLLMCommittee (dry-run mode — deterministic, no API calls)
- Strategy (protocol) / Decision
- HermesQuantStrategy / BuyAndHoldStrategy

Original public API (ADR-0020):
- replay(bars, *, symbol, ...) -> BacktestResult
- PaperPortfolio
- BacktestResult
- WalkForwardBacktestResult / WalkForwardFoldResult / walk_forward_replay
"""

from .portfolio import PaperPortfolio
from .replay import BacktestResult, replay
from .walk_forward import (
    WalkForwardBacktestResult,
    WalkForwardFoldResult,
    walk_forward_replay,
)

# Wave 6a additions
from .cost_model import CostModel, LIQUID_EQUITY, MIDCAP_EQUITY, ILLIQUID
from .engine import (
    LookaheadViolation,
    WalkForwardConfig,
    WalkForwardEngine,
    WalkForwardResult,
)
from .stub_llm import StubLLMCommittee
from .strategy import (
    BuyAndHoldStrategy,
    Decision,
    HermesQuantStrategy,
    Strategy,
)

__all__ = [
    # Original API
    "BacktestResult",
    "PaperPortfolio",
    "WalkForwardBacktestResult",
    "WalkForwardFoldResult",
    "replay",
    "walk_forward_replay",
    # Wave 6a — cost model
    "CostModel",
    "LIQUID_EQUITY",
    "MIDCAP_EQUITY",
    "ILLIQUID",
    # Wave 6a — engine
    "LookaheadViolation",
    "WalkForwardConfig",
    "WalkForwardEngine",
    "WalkForwardResult",
    # Wave 6a — stub + strategies
    "StubLLMCommittee",
    "BuyAndHoldStrategy",
    "Decision",
    "HermesQuantStrategy",
    "Strategy",
]
