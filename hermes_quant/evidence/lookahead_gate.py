"""hermes_quant.evidence.lookahead_gate — universal lookahead gate per ADR-0033 D5.

Replaces the kind-by-kind lookahead checks (ADR-0028 D5/D7 for option_chain,
AGENTS.md OHLCV-only check) with a single CI test that runs against any
analyst that emits AnalystView with non-empty evidence_ids.

Used in two places:
  1. tests/test_no_lookahead.py extends the existing shuffle_timestamps_test
     with this gate.
  2. The risk gate (hermes_quant/risk/gate.py) MAY call
     `assert_no_lookahead(view, asof)` at runtime to drop look-ahead-tainted
     signals before they reach the aggregator.

The gate is decoupled from any specific EvidenceStore implementation —
it requires only a `store.get(evidence_id) -> dict | None` shape where
the returned row has an `available_at` field (datetime or ISO str).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from hermes_quant.protocol import AnalystView


class EvidenceLookaheadError(Exception):
    """Raised when an analyst emits a view that cites evidence whose
    available_at is in the FUTURE relative to the analyst's asof.

    Carries the offending evidence_id and the (asof, available_at)
    pair for diagnostics. Caught by the CI gate; in runtime mode the
    risk gate just drops the view and increments a counter.
    """

    def __init__(
        self,
        evidence_id: str | UUID,
        asof: datetime,
        available_at: datetime,
        view_analyst: str | None = None,
    ):
        self.evidence_id = str(evidence_id)
        self.asof = asof
        self.available_at = available_at
        self.view_analyst = view_analyst
        msg = (
            f"Lookahead violation: evidence {evidence_id} has "
            f"available_at={available_at.isoformat()} > "
            f"asof={asof.isoformat()}"
        )
        if view_analyst:
            msg += f" (analyst={view_analyst})"
        super().__init__(msg)


@dataclass(frozen=True)
class LookaheadCheckResult:
    """Result of a (non-raising) lookahead check.

    Attributes:
        ok: True iff no violations were found.
        n_evidence_checked: total evidence_ids inspected across all views.
        violations: tuple of EvidenceLookaheadError objects (one per offending row).
        n_evidence_with_unknown_id: count of evidence_ids that didn't resolve in
            the store. Treated as a WARNING, not an error — gate stays ok=True
            unless a real lookahead violation is also present.
    """

    ok: bool
    n_evidence_checked: int
    violations: tuple[EvidenceLookaheadError, ...]
    n_evidence_with_unknown_id: int


class _StoreLike(Protocol):
    """Minimal store shape required by the gate."""

    def get(self, evidence_id: str | UUID) -> dict | None: ...


def _coerce_available_at(value: object) -> datetime:
    """Accept either a datetime or an ISO-8601 string for `available_at`."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(
        f"available_at must be datetime or ISO str, got {type(value).__name__}"
    )


def check_view_lookahead(
    view: AnalystView, asof: datetime, store: _StoreLike
) -> LookaheadCheckResult:
    """Check a single AnalystView. Resolves evidence_ids against `store`
    and returns a LookaheadCheckResult. Does NOT raise.

    `store` must have a `.get(evidence_id)` method returning a row dict
    with at least an `available_at` field (ISO datetime str or datetime),
    or None if the evidence_id is unknown.
    """
    violations: list[EvidenceLookaheadError] = []
    unknown = 0
    n_checked = 0
    for ev_id in view.evidence_ids:
        n_checked += 1
        row = store.get(ev_id)
        if row is None:
            unknown += 1
            continue
        avail = _coerce_available_at(row.get("available_at"))
        if avail > asof:
            violations.append(
                EvidenceLookaheadError(
                    evidence_id=ev_id,
                    asof=asof,
                    available_at=avail,
                    view_analyst=view.analyst,
                )
            )
    return LookaheadCheckResult(
        ok=(len(violations) == 0),
        n_evidence_checked=n_checked,
        violations=tuple(violations),
        n_evidence_with_unknown_id=unknown,
    )


def assert_no_lookahead(
    view: AnalystView, asof: datetime, store: _StoreLike
) -> None:
    """Strict-mode assert. Raises EvidenceLookaheadError on the first
    violation. Used by the CI gate."""
    result = check_view_lookahead(view, asof, store)
    if not result.ok:
        raise result.violations[0]


def check_views_lookahead(
    views: Iterable[AnalystView], asof: datetime, store: _StoreLike
) -> LookaheadCheckResult:
    """Aggregate check across multiple views. Returns one
    LookaheadCheckResult summarizing all of them."""
    total_checked = 0
    total_unknown = 0
    all_violations: list[EvidenceLookaheadError] = []
    for view in views:
        sub = check_view_lookahead(view, asof, store)
        total_checked += sub.n_evidence_checked
        total_unknown += sub.n_evidence_with_unknown_id
        all_violations.extend(sub.violations)
    return LookaheadCheckResult(
        ok=(len(all_violations) == 0),
        n_evidence_checked=total_checked,
        violations=tuple(all_violations),
        n_evidence_with_unknown_id=total_unknown,
    )
