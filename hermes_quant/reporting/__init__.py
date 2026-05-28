"""hermes_quant.reporting — operator-facing report generators.

Read-only consumers of the canonical event stores (audit_log.jsonl,
reflections.jsonl, hypotheses.jsonl, factor_verdicts.jsonl, state.db,
proposals.db). Reports never mutate state.

ADR-0061 — daily Markdown brief.
"""

from __future__ import annotations

from hermes_quant.reporting.daily_report import (
    DailyReport,
    format_markdown,
    format_telegram,
    generate_daily_report,
)

__all__ = [
    "DailyReport",
    "format_markdown",
    "format_telegram",
    "generate_daily_report",
]
