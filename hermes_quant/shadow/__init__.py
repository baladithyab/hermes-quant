"""hermes_quant.shadow — Shadow Account counterfactual backtest engine.

Wave 8b / ADR-0049.

Maintains N isolated shadow portfolios, each following an alternate
decision rule, running in lockstep with the production audit log.
After each session the ShadowAccountRunner emits a ShadowComparisonReport
that shows "if we had used rule X instead of production, P&L would be +Y%".

Shadow accounts are READ-ONLY consumers of the production audit log.
They NEVER write to audit_log.jsonl.  Each rule has its own isolated
SQLite DB at ~/.hermes/quant/shadow/<rule_name>.db.

Public surface
--------------
from hermes_quant.shadow import (
    # rules
    ShadowRule,
    ShadowDecision,
    AlwaysFollowAdvisorRule,
    InverseConsensusRule,
    SemanticOnlyRule,
    SentimentOnlyRule,
    TrendFollowingRule,
    # account
    ShadowAccount,
    # runner
    ShadowAccountRunner,
    ShadowComparisonReport,
)
"""
from __future__ import annotations

from hermes_quant.shadow.account import ShadowAccount
from hermes_quant.shadow.rules import (
    AlwaysFollowAdvisorRule,
    InverseConsensusRule,
    SemanticOnlyRule,
    SentimentOnlyRule,
    ShadowDecision,
    ShadowRule,
    TrendFollowingRule,
)
from hermes_quant.shadow.runner import ShadowAccountRunner, ShadowComparisonReport

__all__ = [
    # rules
    "ShadowRule",
    "ShadowDecision",
    "AlwaysFollowAdvisorRule",
    "InverseConsensusRule",
    "SemanticOnlyRule",
    "SentimentOnlyRule",
    "TrendFollowingRule",
    # account
    "ShadowAccount",
    # runner
    "ShadowAccountRunner",
    "ShadowComparisonReport",
]
