"""hermes_quant.evidence — EvidenceRecord schema + append-only store (ADR-0033).

Public surface:
    EvidenceRecord, BarEvidence, NewsEvidence, FilingEvidence, SocialEvidence,
    OptionChainEvidence, EarningsCallEvidence, MacroPrintEvidence,
    EvidenceCausalityError, EvidenceStore, EvidenceStoreImmutable,
    EvidenceStoreFull.
"""
from __future__ import annotations

from hermes_quant.evidence.schema import (
    INGEST_LAG_FLOOR_SECONDS,
    BarEvidence,
    EarningsCallEvidence,
    EvidenceCausalityError,
    EvidenceRecord,
    FilingEvidence,
    MacroPrintEvidence,
    NewsEvidence,
    OptionChainEvidence,
    SocialEvidence,
    compute_available_at,
    derive_evidence_id,
    sha256_of_bytes,
    sha256_of_json,
)
from hermes_quant.evidence.store import (
    EvidenceStore,
    EvidenceStoreFull,
    EvidenceStoreImmutable,
)

__all__ = [
    "BarEvidence",
    "EarningsCallEvidence",
    "EvidenceCausalityError",
    "EvidenceRecord",
    "EvidenceStore",
    "EvidenceStoreFull",
    "EvidenceStoreImmutable",
    "FilingEvidence",
    "INGEST_LAG_FLOOR_SECONDS",
    "MacroPrintEvidence",
    "NewsEvidence",
    "OptionChainEvidence",
    "SocialEvidence",
    "compute_available_at",
    "derive_evidence_id",
    "sha256_of_bytes",
    "sha256_of_json",
]
