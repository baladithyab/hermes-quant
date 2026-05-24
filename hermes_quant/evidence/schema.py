"""hermes_quant.evidence.schema — EvidenceRecord + per-kind subtypes (ADR-0033 D1+D2).

The three-timestamp invariant (published_at, ingested_at, available_at) is
load-bearing: it operationalizes FutureSim chronological replay. Causal
sanity (available_at >= published_at) is enforced at construction time.

Per-kind ``ingest_lag_floor`` values implement the ADR-0033 D2 table; an
analyst at backtest tick T may consume a record only if
``record.available_at <= T``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import NAMESPACE_OID, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

EvidenceKind = Literal[
    "bar",
    "news",
    "filing",
    "social",
    "option_chain",
    "earnings_call",
    "macro_print",
]

# Per-kind ingest_lag_floor in seconds (ADR-0033 D2).
INGEST_LAG_FLOOR_SECONDS: Final[dict[str, int]] = {
    "bar": 60,
    "news": 30,  # baseline; provider-specific override may be higher
    "filing": 0,
    "social": 0,
    "option_chain": 60,  # ADR-0028 D5 NBBO snapshot lag
    "earnings_call": 0,
    "macro_print": 0,
}


class EvidenceCausalityError(ValueError):
    """Raised when available_at < published_at (causality violation) or when
    timestamps are not timezone-aware, or when ``id`` does not match the
    deterministic hash of (kind, source, payload_hash)."""


class EvidenceRecord(BaseModel):
    """Base record. Per-kind subtypes extend with structured fields.

    Identity: ``id`` MUST equal ``derive_evidence_id(kind, source, payload_hash)``;
    this collapses semantically duplicate fetches at write time and is
    enforced by ``_check_causality``.
    """

    model_config = ConfigDict(
        frozen=True,  # immutable by construction
        extra="forbid",  # reject unknown fields at the boundary
    )

    id: UUID
    kind: EvidenceKind
    symbol: str | None = None  # None for macro_print
    source: str

    published_at: datetime
    ingested_at: datetime
    available_at: datetime

    payload_ref: str  # path or URI to actual data; SHA-256 in payload_hash
    payload_hash: str
    schema_version: int = 1
    supersedes: UUID | None = None

    @model_validator(mode="after")
    def _check_causality(self) -> EvidenceRecord:
        # All three datetimes MUST be tz-aware; reject naive datetimes.
        for fname in ("published_at", "ingested_at", "available_at"):
            v = getattr(self, fname)
            if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
                raise EvidenceCausalityError(
                    f"{fname} must be timezone-aware (UTC); got naive datetime."
                )
        if self.available_at < self.published_at:
            raise EvidenceCausalityError(
                f"available_at ({self.available_at.isoformat()}) < "
                f"published_at ({self.published_at.isoformat()}); causal violation."
            )
        # payload_hash is the canonical identity; sanity-check it matches the
        # configured deterministic UUID rule.
        expected = derive_evidence_id(self.kind, self.source, self.payload_hash)
        if self.id != expected:
            raise EvidenceCausalityError(
                f"id ({self.id}) does not match deterministic hash from "
                f"(kind, source, payload_hash). Use derive_evidence_id()."
            )
        return self


def derive_evidence_id(kind: str, source: str, payload_hash: str) -> UUID:
    """Deterministic UUID from (kind, source, payload_hash).

    Stable across machines: same triple -> same UUID. Used for dedup at
    write time. ``payload_hash`` should be the SHA-256 hex digest of the
    canonical payload bytes.
    """
    name = f"{kind}:{source}:{payload_hash}"
    return uuid5(NAMESPACE_OID, name)


def compute_available_at(
    kind: EvidenceKind,
    published_at: datetime,
    override_lag_seconds: int | None = None,
) -> datetime:
    """Compute the available_at field per ADR-0033 D2.

    available_at = published_at + max(0, ingest_lag_floor[kind] OR override).
    """
    lag = (
        override_lag_seconds
        if override_lag_seconds is not None
        else INGEST_LAG_FLOOR_SECONDS[kind]
    )
    return published_at + timedelta(seconds=max(0, lag))


def sha256_of_bytes(b: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(b).hexdigest()


def sha256_of_json(payload: Any) -> str:
    """SHA-256 hex digest of a JSON-serializable payload, using a canonical
    encoding (sorted keys, no whitespace) so the same logical payload yields
    the same hash regardless of Python dict ordering."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


# ---- Per-kind subtypes (ADR-0033 D1) ----


class BarEvidence(EvidenceRecord):
    kind: Literal["bar"] = "bar"
    open: float
    high: float
    low: float
    close: float
    volume: float


class NewsEvidence(EvidenceRecord):
    kind: Literal["news"] = "news"
    headline: str
    body: str | None = None  # may be None when truncated to headers-only
    url: str | None = None


class FilingEvidence(EvidenceRecord):
    kind: Literal["filing"] = "filing"
    accession_number: str  # SEC EDGAR accession
    form_type: str  # '10-K', '8-K', '4', etc.


class SocialEvidence(EvidenceRecord):
    kind: Literal["social"] = "social"
    platform: str  # 'reddit', 'x', 'stocktwits'
    text: str
    score: float | None = None  # platform-specific (upvotes, likes, ...)


class OptionChainEvidence(EvidenceRecord):
    kind: Literal["option_chain"] = "option_chain"
    underlying: str
    expiry: str  # ISO date
    chain_payload_path: str  # parquet path to full chain snapshot


class EarningsCallEvidence(EvidenceRecord):
    kind: Literal["earnings_call"] = "earnings_call"
    transcript_text: str
    fiscal_period: str  # 'Q3FY26'


class MacroPrintEvidence(EvidenceRecord):
    kind: Literal["macro_print"] = "macro_print"
    series_id: str  # 'CPIAUCSL', 'UNRATE', etc.
    value: float
    units: str
