"""Tests for hermes_quant.evidence.schema (ADR-0033 D1, D2)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from hermes_quant.evidence.schema import (
    INGEST_LAG_FLOOR_SECONDS,
    BarEvidence,
    EvidenceCausalityError,
    NewsEvidence,
    compute_available_at,
    derive_evidence_id,
    sha256_of_bytes,
    sha256_of_json,
)

# ---- helpers ----


def _bar_kwargs(
    *,
    payload_hash: str | None = None,
    source: str = "yfinance",
    published_at: datetime | None = None,
    available_at: datetime | None = None,
    ingested_at: datetime | None = None,
    open_: float = 100.0,
    close: float = 101.0,
    high: float = 102.0,
    low: float = 99.5,
    volume: float = 1_000_000.0,
) -> dict:
    if published_at is None:
        published_at = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    if available_at is None:
        available_at = compute_available_at("bar", published_at)
    if ingested_at is None:
        ingested_at = published_at + timedelta(seconds=5)
    if payload_hash is None:
        payload_hash = sha256_of_bytes(b"OHLCV row 1")
    eid = derive_evidence_id("bar", source, payload_hash)
    return {
        "id": eid,
        "kind": "bar",
        "symbol": "AAPL",
        "source": source,
        "published_at": published_at,
        "ingested_at": ingested_at,
        "available_at": available_at,
        "payload_ref": f"blobs/{payload_hash}.json",
        "payload_hash": payload_hash,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


# ---- tests ----


def test_evidence_record_three_timestamp_required():
    """Pydantic must reject a record missing any of the three timestamps."""
    base = _bar_kwargs()
    for missing in ("published_at", "ingested_at", "available_at"):
        kw = {k: v for k, v in base.items() if k != missing}
        with pytest.raises(ValidationError):
            BarEvidence(**kw)


def test_evidence_record_available_at_geq_published_at():
    """available_at < published_at must raise EvidenceCausalityError."""
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    bad_avail = pub - timedelta(seconds=10)
    kw = _bar_kwargs(published_at=pub, available_at=bad_avail)
    with pytest.raises((EvidenceCausalityError, ValidationError)):
        BarEvidence(**kw)


def test_evidence_id_is_deterministic_hash():
    """Same (kind, source, payload_hash) -> same UUID."""
    h = sha256_of_bytes(b"identical-payload")
    a = derive_evidence_id("bar", "yfinance", h)
    b = derive_evidence_id("bar", "yfinance", h)
    assert a == b
    # different input -> different UUID
    h2 = sha256_of_bytes(b"different-payload")
    c = derive_evidence_id("bar", "yfinance", h2)
    assert c != a


def test_evidence_id_changes_when_payload_hash_changes():
    """Different payload_hash with same (kind, source) -> different UUID."""
    a = derive_evidence_id("news", "edgar", "0" * 64)
    b = derive_evidence_id("news", "edgar", "1" * 64)
    assert a != b


def test_evidence_id_changes_when_source_changes():
    """Different source with same (kind, payload_hash) -> different UUID
    (semantic guard required by D1: hash includes source so semantically
    identical records from two sources do not collide)."""
    h = sha256_of_bytes(b"shared-payload")
    a = derive_evidence_id("bar", "yfinance", h)
    b = derive_evidence_id("bar", "alpaca", h)
    assert a != b


def test_evidence_record_rejects_naive_datetimes():
    """All three timestamps must be tz-aware."""
    naive_pub = datetime(2026, 5, 24, 14, 30, 0)  # no tzinfo
    aware = datetime(2026, 5, 24, 14, 31, 0, tzinfo=UTC)
    kw = _bar_kwargs()
    kw["published_at"] = naive_pub
    kw["available_at"] = aware
    kw["ingested_at"] = aware
    with pytest.raises((EvidenceCausalityError, ValidationError)):
        BarEvidence(**kw)


def test_evidence_record_rejects_unknown_extra_fields():
    """ConfigDict(extra='forbid') must reject unknown fields at the boundary."""
    kw = _bar_kwargs()
    kw["mystery_field"] = "should be rejected"
    with pytest.raises(ValidationError):
        BarEvidence(**kw)


def test_compute_available_at_uses_per_kind_lag_floor():
    """available_at == published_at + ingest_lag_floor[kind]."""
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    avail_bar = compute_available_at("bar", pub)
    assert avail_bar == pub + timedelta(seconds=INGEST_LAG_FLOOR_SECONDS["bar"])
    assert avail_bar == pub + timedelta(seconds=60)

    avail_filing = compute_available_at("filing", pub)
    assert avail_filing == pub  # 0s lag

    avail_macro = compute_available_at("macro_print", pub)
    assert avail_macro == pub  # 0s lag


def test_compute_available_at_override_overrides_default():
    """An explicit override_lag_seconds replaces the default."""
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    avail = compute_available_at("news", pub, override_lag_seconds=300)
    assert avail == pub + timedelta(seconds=300)
    # negative override clamped to 0 (causality)
    avail_neg = compute_available_at("news", pub, override_lag_seconds=-99)
    assert avail_neg == pub


def test_bar_evidence_has_ohlcv_fields():
    rec = BarEvidence(**_bar_kwargs(open_=100.0, high=110.0, low=99.0, close=105.0, volume=1234.0))
    assert rec.open == 100.0
    assert rec.high == 110.0
    assert rec.low == 99.0
    assert rec.close == 105.0
    assert rec.volume == 1234.0
    assert rec.kind == "bar"


def test_news_evidence_supports_optional_body():
    """NewsEvidence body may be None when truncated to headers-only."""
    pub = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    avail = compute_available_at("news", pub)
    payload_hash = sha256_of_json({"headline": "Something happened"})
    eid = derive_evidence_id("news", "rss", payload_hash)
    rec = NewsEvidence(
        id=eid,
        kind="news",
        symbol="AAPL",
        source="rss",
        published_at=pub,
        ingested_at=pub + timedelta(seconds=5),
        available_at=avail,
        payload_ref=f"blobs/{payload_hash}.json",
        payload_hash=payload_hash,
        headline="Something happened",
        body=None,  # explicitly null
        url=None,
    )
    assert rec.body is None
    assert rec.url is None
    assert rec.headline == "Something happened"


def test_evidence_record_id_must_match_deterministic_hash():
    """If id != derive_evidence_id(kind, source, payload_hash), construction
    must raise to prevent silent identity drift."""
    kw = _bar_kwargs()
    # Tamper: take an unrelated UUID
    kw["id"] = derive_evidence_id("bar", "alpaca", "0" * 64)
    with pytest.raises((EvidenceCausalityError, ValidationError)):
        BarEvidence(**kw)


def test_evidence_record_is_frozen():
    """Records are immutable; assignment after construction must fail."""
    rec = BarEvidence(**_bar_kwargs())
    with pytest.raises(ValidationError):
        rec.close = 999.0  # type: ignore[misc]


def test_sha256_helpers_are_stable():
    """sha256_of_json yields the same hash regardless of dict ordering."""
    a = sha256_of_json({"a": 1, "b": 2})
    b = sha256_of_json({"b": 2, "a": 1})
    assert a == b
    # bytes helper roundtrips
    assert sha256_of_bytes(b"abc") == sha256_of_bytes(b"abc")
    assert sha256_of_bytes(b"abc") != sha256_of_bytes(b"abd")


def test_all_seven_kinds_have_lag_floor_entry():
    """Every kind in the EvidenceKind Literal must have a lag floor."""
    expected_kinds = {
        "bar",
        "news",
        "filing",
        "social",
        "option_chain",
        "earnings_call",
        "macro_print",
    }
    assert set(INGEST_LAG_FLOOR_SECONDS.keys()) == expected_kinds


def test_evidence_record_does_not_accept_unknown_kind():
    """The Literal['bar', ...] guard must reject unknown kinds."""
    kw = _bar_kwargs()
    kw["kind"] = "unknown_kind"
    with pytest.raises(ValidationError):
        BarEvidence(**kw)
