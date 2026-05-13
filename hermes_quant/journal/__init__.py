"""hermes_quant.journal — Settlement journal (ADR-0010).

Two-phase markdown ledger of every trading decision and its realized outcome.
- ~/.hermes/quant/journal.md is the on-disk artifact (operator UX, not
  daemon input — per ADR-0010 §9 reproducibility).
- Pydantic SettlementEntry is the source of truth; markdown is a render
  derivative.

Public API:
- append_pending(entry) — write a Phase-A pending entry (decision time)
- resolve(entry_id, ...) — patch a pending entry with Phase-B realized
  outcome (settlement time)
- get_recent_lessons(symbol, n_same, n_cross) — retrieval helper for
  consumers (LLMAnalyst future, advisor present)
- append_human_override(proposal, kind, reason) — HITL Wave A integration:
  HITL approve/reject events render as journal entries even when the daemon
  isn't running. Per ADR-0015 §D8.

This module is lazily-importable: tools that depend on the journal degrade
to no-ops when the writer isn't available, so older deploys keep working.
"""
from __future__ import annotations

from .models import (
    AnalystComponent,
    Reflection,
    SettlementEntry,
)
from .reader import (
    get_recent_lessons,
    parse_journal,
)
from .writer import (
    DEFAULT_JOURNAL_PATH,
    JournalEntryNotFound,
    JournalEntryAlreadyResolved,
    append_human_override,
    append_pending,
    resolve,
)

__all__ = [
    "AnalystComponent",
    "DEFAULT_JOURNAL_PATH",
    "JournalEntryAlreadyResolved",
    "JournalEntryNotFound",
    "Reflection",
    "SettlementEntry",
    "append_human_override",
    "append_pending",
    "get_recent_lessons",
    "parse_journal",
    "resolve",
]
