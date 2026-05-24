"""hermes_quant.evidence.audit — audit walkback per ADR-0033 D6.

Given a TradeIntent.id or evidence UUID, walks backward:
  TradeIntent
    -> AggregatedSignal (via aggregated_signal_id)
      -> AnalystView[]   (via component_views field)
        -> evidence_ids: tuple[UUID, ...]
          -> EvidenceRecord[] (via store.get())
            -> payload (via payload_ref)

Also traces supersedes chains: a record may have been superseded by a
corrected record; the audit shows BOTH.

Doesn't import the live EvidenceStore type — uses duck-typed `store`
argument with `.get(id)` and `.supersedes_chain(id)` methods. Sibling task
implements the real store.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from hermes_quant.protocol import AggregatedSignal, AnalystView


class _StoreProtocol(Protocol):
    def get(self, evidence_id: UUID | str) -> dict[str, Any] | None: ...
    def supersedes_chain(self, evidence_id: UUID | str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class EvidenceAuditNode:
    """One node in the audit tree."""

    evidence_id: str
    kind: str | None
    source: str | None
    available_at: str | None  # ISO
    supersedes: str | None
    payload_ref: str | None
    found: bool  # False = id not in store (broken link)


@dataclass(frozen=True)
class AnalystAuditNode:
    analyst: str
    direction: int
    confidence: float
    horizon: str
    evidence: tuple[EvidenceAuditNode, ...]


@dataclass(frozen=True)
class AggregatedSignalAuditNode:
    aggregator: str | None
    direction: int
    confidence: float
    component_count: int
    components: tuple[AnalystAuditNode, ...]


@dataclass(frozen=True)
class AuditTree:
    root_id: str  # the TradeIntent or AnalystView id we started from
    root_kind: str  # 'trade_intent' | 'aggregated_signal' | 'analyst_view' | 'evidence'
    aggregated_signal: AggregatedSignalAuditNode | None
    direct_analyst: AnalystAuditNode | None  # set when root is an AnalystView, not TradeIntent
    direct_evidence: EvidenceAuditNode | None
    n_evidence_total: int
    n_evidence_missing: int
    walked_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, indent=2)

    def to_tree_text(self) -> str:
        """Human-readable tree."""
        lines = [f"AuditTree({self.root_kind} {self.root_id}, walked_at={self.walked_at})"]
        if self.aggregated_signal:
            lines.append(
                f"  AggregatedSignal: aggregator={self.aggregated_signal.aggregator}, "
                f"direction={self.aggregated_signal.direction}, "
                f"confidence={self.aggregated_signal.confidence:.3f}, "
                f"components={self.aggregated_signal.component_count}"
            )
            for ana in self.aggregated_signal.components:
                lines.append(
                    f"    AnalystView: {ana.analyst}, dir={ana.direction}, "
                    f"conf={ana.confidence:.3f}, "
                    f"evidence_count={len(ana.evidence)}"
                )
                for ev in ana.evidence:
                    mark = "!" if not ev.found else " "
                    lines.append(
                        f"      {mark} Evidence({ev.kind}, {ev.source}, "
                        f"avail={ev.available_at}, ref={ev.payload_ref})"
                    )
        elif self.direct_analyst:
            lines.append(
                f"  AnalystView: {self.direct_analyst.analyst}, dir={self.direct_analyst.direction}, "
                f"conf={self.direct_analyst.confidence:.3f}"
            )
            for ev in self.direct_analyst.evidence:
                mark = "!" if not ev.found else " "
                lines.append(
                    f"    {mark} Evidence({ev.kind}, {ev.source}, avail={ev.available_at})"
                )
        elif self.direct_evidence:
            ev = self.direct_evidence
            mark = "!" if not ev.found else " "
            lines.append(
                f"  {mark} Evidence({ev.kind}, {ev.source}, avail={ev.available_at}, "
                f"supersedes={ev.supersedes})"
            )
        lines.append(
            f"  Summary: {self.n_evidence_total} evidence, "
            f"{self.n_evidence_missing} missing"
        )
        return "\n".join(lines)


def _resolve_evidence(evidence_id: UUID | str, store: _StoreProtocol) -> EvidenceAuditNode:
    sid = str(evidence_id)
    row = store.get(sid)
    if row is None:
        return EvidenceAuditNode(
            evidence_id=sid,
            kind=None,
            source=None,
            available_at=None,
            supersedes=None,
            payload_ref=None,
            found=False,
        )
    return EvidenceAuditNode(
        evidence_id=sid,
        kind=row.get("kind"),
        source=row.get("source"),
        available_at=row.get("available_at"),
        supersedes=row.get("supersedes"),
        payload_ref=row.get("payload_ref"),
        found=True,
    )


def _walkback_view(view: AnalystView, store: _StoreProtocol) -> AnalystAuditNode:
    evs = tuple(_resolve_evidence(eid, store) for eid in view.evidence_ids)
    return AnalystAuditNode(
        analyst=view.analyst,
        direction=int(view.direction),
        confidence=float(view.confidence),
        horizon=view.horizon,
        evidence=evs,
    )


def walkback_aggregated_signal(
    signal: AggregatedSignal, store: _StoreProtocol
) -> AuditTree:
    """Walk back from an AggregatedSignal."""
    components = tuple(_walkback_view(v, store) for v in (signal.components or ()))
    n_total = sum(len(c.evidence) for c in components)
    n_missing = sum(sum(1 for ev in c.evidence if not ev.found) for c in components)
    return AuditTree(
        root_id=str(getattr(signal, "aggregated_signal_id", "") or signal.asset),
        root_kind="aggregated_signal",
        aggregated_signal=AggregatedSignalAuditNode(
            aggregator=getattr(signal, "aggregator", None),
            direction=int(signal.direction),
            confidence=float(signal.confidence),
            component_count=len(components),
            components=components,
        ),
        direct_analyst=None,
        direct_evidence=None,
        n_evidence_total=n_total,
        n_evidence_missing=n_missing,
        walked_at=datetime.now().isoformat(),
    )


def walkback_analyst_view(view: AnalystView, store: _StoreProtocol) -> AuditTree:
    """Walk back from a single AnalystView."""
    direct = _walkback_view(view, store)
    return AuditTree(
        root_id=str(view.analyst),
        root_kind="analyst_view",
        aggregated_signal=None,
        direct_analyst=direct,
        direct_evidence=None,
        n_evidence_total=len(direct.evidence),
        n_evidence_missing=sum(1 for ev in direct.evidence if not ev.found),
        walked_at=datetime.now().isoformat(),
    )


def walkback_evidence(evidence_id: UUID | str, store: _StoreProtocol) -> AuditTree:
    """Walk back from a single evidence_id (typically used to inspect the
    supersedes chain of a record)."""
    sid = str(evidence_id)
    direct = _resolve_evidence(sid, store)
    return AuditTree(
        root_id=sid,
        root_kind="evidence",
        aggregated_signal=None,
        direct_analyst=None,
        direct_evidence=direct,
        n_evidence_total=1,
        n_evidence_missing=0 if direct.found else 1,
        walked_at=datetime.now().isoformat(),
    )


def supersedes_history(
    evidence_id: UUID | str, store: _StoreProtocol
) -> list[EvidenceAuditNode]:
    """Returns supersedes chain (most recent first)."""
    chain = store.supersedes_chain(evidence_id)
    return [
        EvidenceAuditNode(
            evidence_id=str(row.get("id", "")),
            kind=row.get("kind"),
            source=row.get("source"),
            available_at=row.get("available_at"),
            supersedes=row.get("supersedes"),
            payload_ref=row.get("payload_ref"),
            found=True,
        )
        for row in chain
    ]
