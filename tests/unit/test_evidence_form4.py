"""Tests for hermes_quant.evidence.adapters.form4 (SEC EDGAR Form-4 adapter, B20).

Offline-deterministic via injected fetchers — mirrors the GN-RSS / social ingester
test pattern. NO live network: every fetch is a captured-real submissions-JSON
fixture or a simulated failure. Verifies the load-bearing asof-honesty invariant:
published_at == the EDGAR ACCEPTANCE datetime / filing date, NEVER the transaction
date (period_of_report). Also: only Form 4/4-A kept, unparseable-acceptance rows
SKIPPED (never now()), fail-closed on 403/parse error, deterministic idempotent
FilingEvidence round-trip through the store, and the lookahead gate protecting any
view that cites a stored filing.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hermes_quant.evidence.adapters import form4
from hermes_quant.evidence.adapters.form4 import (
    fetch_form4_filings,
    parse_submissions,
    to_filing_evidence,
)
from hermes_quant.evidence.lookahead_gate import check_view_lookahead
from hermes_quant.evidence.schema import FilingEvidence
from hermes_quant.evidence.store import EvidenceStore
from hermes_quant.protocol import AnalystView

# --- fixtures ----------------------------------------------------------------
# Captured-real shape of the live data.sec.gov submissions feed
# (https://data.sec.gov/submissions/CIK0000320193.json): columnar `filings.recent`
# arrays. CIK 320193 = Apple. The `form` array mixes 4 / 4/A / 8-K so we prove the
# Form-4 filter. The `acceptanceDateTime` values are FIXED PAST datetimes WITH an
# Eastern offset, and they DIFFER from `reportDate` (the transaction/event date) —
# this is what proves published_at is anchored on acceptance, NOT the trade date.
# Row index 3 has an EMPTY acceptanceDateTime AND empty filingDate -> must be
# SKIPPED (no now() fabrication). Row index 4 is an 8-K (wrong form) -> dropped.
_SUBMISSIONS_PAYLOAD = json.dumps(
    {
        "cik": 320193,
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-25-000077",  # Form 4, has acceptance dt
                    "0000320193-25-000078",  # Form 4/A amendment, has acceptance dt
                    "0000320193-25-000079",  # Form 4, only filingDate (no acceptance)
                    "0000320193-25-000080",  # Form 4, NO acceptance, NO filingDate -> SKIP
                    "0000320193-25-000081",  # 8-K (wrong form) -> dropped
                ],
                "form": ["4", "4/A", "4", "4", "8-K"],
                # acceptanceDateTime: when it went PUBLIC (the asof anchor).
                "acceptanceDateTime": [
                    "2025-03-17T18:30:05.000-04:00",
                    "2025-03-18T09:15:00.000-04:00",
                    "",  # missing -> fall back to filingDate
                    "",  # missing AND filingDate missing -> SKIP
                    "2025-03-19T16:00:00.000-04:00",
                ],
                "filingDate": [
                    "2025-03-17",
                    "2025-03-18",
                    "2025-03-19",  # used as end-of-day fallback for row 2
                    "",  # missing -> row 3 has no anchor -> SKIP
                    "2025-03-19",
                ],
                # reportDate = the TRANSACTION/event date. Deliberately EARLIER
                # than acceptanceDateTime (insiders file up to 2 business days
                # late). If the adapter ever anchored on this, the asof test fails.
                "reportDate": [
                    "2025-03-13",
                    "2025-03-13",
                    "2025-03-14",
                    "2025-03-15",
                    "2025-03-19",
                ],
                "primaryDocument": [
                    "xslF345X05/wk-form4_1.xml",
                    "xslF345X05/wk-form4_2.xml",
                    "xslF345X05/wk-form4_3.xml",
                    "xslF345X05/wk-form4_4.xml",
                    "aapl-8k.htm",
                ],
            }
        },
    }
).encode()

# 2025-03-17 18:30:05 -04:00 == 2025-03-17 22:30:05 UTC (acceptance of row 0)
_ROW0_ACCEPTANCE_UTC = datetime(2025, 3, 17, 22, 30, 5, tzinfo=UTC)


def _ok_fetcher(url, timeout):
    return _SUBMISSIONS_PAYLOAD


def _boom_fetcher(url, timeout):
    raise ConnectionError("simulated SEC failure")


def _forbidden_fetcher(url, timeout):
    # Mimic the documented SEC 403 from cloud egress (urllib raises HTTPError).
    import urllib.error

    raise urllib.error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)


# --- parse correctness + Form-4 filter ---------------------------------------
def test_parse_keeps_only_form4_and_amendments():
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD)
    # rows 0,1,2 kept (4, 4/A, 4-with-filingdate); row 3 skipped (no anchor);
    # row 4 dropped (8-K wrong form).
    assert len(filings) == 3
    forms = {f.form_type for f in filings}
    assert forms == {"4", "4/A"}
    assert all(f.form_type != "8-K" for f in filings)


def test_published_at_is_acceptance_not_transaction_date():
    """THE asof-honesty test: filed_at == acceptance datetime, NOT reportDate."""
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD)
    row0 = next(f for f in filings if f.accession_number == "0000320193-25-000077")
    # filed_at is the ACCEPTANCE datetime in UTC ...
    assert row0.filed_at == _ROW0_ACCEPTANCE_UTC
    # ... and is STRICTLY LATER than the transaction date (period_of_report),
    # exactly the late-filing gap that disqualifies the trade date as the anchor.
    assert row0.period_of_report == datetime(2025, 3, 13).date()
    assert row0.filed_at.date() > row0.period_of_report
    # The evidence built from it anchors published_at on filed_at (acceptance).
    ev = to_filing_evidence(row0)
    assert ev.published_at == _ROW0_ACCEPTANCE_UTC
    # filing ingest-lag floor is 0s -> available_at == published_at.
    assert ev.available_at == ev.published_at


def test_filing_date_eod_fallback_is_conservative():
    """Row with only a filingDate anchors at END-of-day ET (a LATER, safe bound)."""
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD)
    row2 = next(f for f in filings if f.accession_number == "0000320193-25-000079")
    # 2025-03-19 23:59:59 -04:00 == 2025-03-20 03:59:59 UTC
    assert row2.filed_at == datetime(2025, 3, 20, 3, 59, 59, tzinfo=UTC)
    # never earlier than the filing day, never now()
    assert row2.filed_at > datetime(2025, 3, 19, tzinfo=UTC)


# --- EST/EDT timezone correctness (the DST defect) ---------------------------
# The Eastern offset is -05:00 in winter (EST, DST off ~Nov-Mar) and -04:00 in
# summer (EDT). A hard-coded -04:00 anchors EST-season filings 1h too EARLY in
# UTC, which fabricates earlier public availability -> lookahead. These cases
# exercise WINTER dates (Jan/Feb) so the resolved offset must be -05:00.
def test_compact_acceptance_in_winter_is_est_not_edt():
    """Compact-legacy acceptance during EST season anchors at -05:00, not -04:00."""
    # 2025-01-15 16:30:00 ET (EST, DST off) == 2025-01-15 21:30:00 UTC.
    # The buggy hard-coded -04:00 would yield 20:30:00 UTC (1h too early).
    payload = json.dumps(
        {
            "cik": 320193,
            "tickers": ["AAPL"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000200"],
                    "form": ["4"],
                    # Compact legacy header form (no offset) -> Eastern wall-clock.
                    "acceptanceDateTime": ["20250115163000"],
                    "filingDate": ["2025-01-15"],
                    "reportDate": ["2025-01-13"],
                    "primaryDocument": ["xslF345X05/wk-form4_w.xml"],
                }
            },
        }
    ).encode()
    filings = parse_submissions(payload)
    assert len(filings) == 1
    assert filings[0].filed_at == datetime(2025, 1, 15, 21, 30, 0, tzinfo=UTC)


def test_filing_date_eod_fallback_in_winter_is_est():
    """End-of-day filingDate fallback during EST season anchors at -05:00."""
    # 2025-02-10 23:59:59 ET (EST) == 2025-02-11 04:59:59 UTC.
    # The buggy hard-coded -04:00 would yield 2025-02-11 03:59:59 UTC (1h early).
    payload = json.dumps(
        {
            "cik": 320193,
            "tickers": ["AAPL"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000201"],
                    "form": ["4"],
                    "acceptanceDateTime": [""],  # absent -> EOD filingDate fallback
                    "filingDate": ["2025-02-10"],
                    "reportDate": ["2025-02-08"],
                    "primaryDocument": ["xslF345X05/wk-form4_w2.xml"],
                }
            },
        }
    ).encode()
    filings = parse_submissions(payload)
    assert len(filings) == 1
    assert filings[0].filed_at == datetime(2025, 2, 11, 4, 59, 59, tzinfo=UTC)


def test_summer_acceptance_unchanged_edt():
    """Sanity: an EDT (summer) compact acceptance is still -04:00 (no regression)."""
    # 2025-07-15 16:30:00 ET (EDT) == 2025-07-15 20:30:00 UTC — same under both.
    payload = json.dumps(
        {
            "cik": 320193,
            "tickers": ["AAPL"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000202"],
                    "form": ["4"],
                    "acceptanceDateTime": ["20250715163000"],
                    "filingDate": ["2025-07-15"],
                    "reportDate": ["2025-07-13"],
                    "primaryDocument": ["xslF345X05/wk-form4_s.xml"],
                }
            },
        }
    ).encode()
    filings = parse_submissions(payload)
    assert len(filings) == 1
    assert filings[0].filed_at == datetime(2025, 7, 15, 20, 30, 0, tzinfo=UTC)


def test_winter_lookahead_gate_rejects_asof_in_the_phantom_early_hour(tmp_path: Path):
    """D5 gate: an asof in the 1h window the bug fabricated must be flagged.

    The buggy -04:00 anchored the EST filing at 20:30Z; the true public moment is
    21:30Z. An asof of 21:00Z is BEFORE the filing was actually public, so the
    lookahead gate MUST flag it. Under the bug the gate would have falsely passed
    (avail 20:30Z <= asof 21:00Z), consuming the record up to 1h early.
    """
    payload = json.dumps(
        {
            "cik": 320193,
            "tickers": ["AAPL"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000203"],
                    "form": ["4"],
                    "acceptanceDateTime": ["20250115163000"],  # 16:30 EST
                    "filingDate": ["2025-01-15"],
                    "reportDate": ["2025-01-13"],
                    "primaryDocument": ["xslF345X05/wk-form4_g.xml"],
                }
            },
        }
    ).encode()
    row = parse_submissions(payload)[0]
    ev = to_filing_evidence(row)
    # available_at must be the TRUE public moment (21:30Z), not the phantom 20:30Z.
    assert ev.available_at == datetime(2025, 1, 15, 21, 30, 0, tzinfo=UTC)

    store = EvidenceStore(root=tmp_path / "evidence_store")
    store.append(ev)
    view = AnalystView(
        analyst="insider_winter",
        direction="long",
        magnitude=0.01,
        confidence=0.5,
        confidence_raw=0.7,
        horizon="1d",
        evidence_ids=(str(ev.id),),
    )
    # asof at 21:00Z — inside the bug's phantom-early hour (20:30Z..21:30Z).
    phantom = datetime(2025, 1, 15, 21, 0, 0, tzinfo=UTC)
    res = check_view_lookahead(view, phantom, store)
    assert res.ok is False  # gate fires: filing not yet public at 21:00Z
    assert len(res.violations) == 1


def test_skips_filing_with_no_parseable_timestamp():
    """A Form 4 with NO acceptance AND NO filingDate is SKIPPED (never now())."""
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD)
    accnos = {f.accession_number for f in filings}
    assert "0000320193-25-000080" not in accnos  # the no-anchor row is gone
    # none of the surviving filings was defaulted to ~now()
    now = datetime.now(UTC)
    assert all(f.filed_at < now for f in filings)


def test_issuer_symbol_carried_from_tickers():
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD)
    assert all(f.issuer_symbol == "AAPL" for f in filings)
    assert all(f.issuer_cik == "0000320193" for f in filings)


def test_since_filter_drops_earlier_filings():
    cutoff = datetime(2025, 3, 18, tzinfo=UTC)
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD, since=cutoff)
    assert all(f.filed_at >= cutoff for f in filings)
    # row 0 (accepted 2025-03-17) is dropped by the cutoff
    assert all(f.accession_number != "0000320193-25-000077" for f in filings)


def test_since_filter_accepts_naive_cutoff_without_raising():
    """A NAIVE ``since`` (bare-date / tzinfo=None) must NOT raise (docstring: 'Never raises').

    Regression: ``filed_at`` is tz-aware UTC; comparing it against a naive
    ``since`` raised TypeError("can't compare offset-naive and offset-aware
    datetimes"). parse_submissions must treat a naive cutoff as UTC and
    DATE-FILTER, not blow up (which previously dropped EVERY filing behind an
    opaque generic error).
    """
    # datetime.fromisoformat("2025-03-18") -> naive midnight, tzinfo is None.
    naive_cutoff = datetime(2025, 3, 18)  # noqa: DTZ001 - intentionally naive
    assert naive_cutoff.tzinfo is None
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD, since=naive_cutoff)
    # Treated as UTC: same result as the tz-aware cutoff above (row 0 dropped,
    # rows accepted on/after 2025-03-18 kept) — NOT all-dropped, NOT an exception.
    aware = parse_submissions(_SUBMISSIONS_PAYLOAD, since=datetime(2025, 3, 18, tzinfo=UTC))
    assert {f.accession_number for f in filings} == {f.accession_number for f in aware}
    assert len(filings) >= 1  # the date filter kept some, did not drop everything
    assert all(f.accession_number != "0000320193-25-000077" for f in filings)


# --- fail-closed / silence-by-default ----------------------------------------
def test_fetch_silences_on_feed_failure():
    """A network failure returns ([], latency), never raises."""
    filings, lat = fetch_form4_filings("320193", fetcher=_boom_fetcher)
    assert filings == []
    assert lat >= 0.0


def test_fetch_silences_on_sec_403():
    """The documented SEC 403 from cloud egress degrades to [] (NORMAL outcome)."""
    filings, lat = fetch_form4_filings("320193", fetcher=_forbidden_fetcher)
    assert filings == []
    assert lat >= 0.0


def test_fetch_silences_on_malformed_json():
    def _garbage(url, timeout):
        return b"<<< not json at all )]}',{"

    filings, lat = fetch_form4_filings("320193", fetcher=_garbage)
    assert filings == []
    assert lat >= 0.0


def test_parse_empty_body_returns_empty():
    assert parse_submissions(b"") == []
    assert parse_submissions(b"null") == []
    assert parse_submissions(b"[]") == []  # JSON but not the expected object


def test_fetch_builds_padded_cik_url():
    seen = {}

    def cap(url, timeout):
        seen["url"] = url
        return _SUBMISSIONS_PAYLOAD

    fetch_form4_filings("320193", fetcher=cap)
    assert "CIK0000320193.json" in seen["url"]  # zero-padded to 10 digits


# --- default-OFF gate --------------------------------------------------------
def test_default_off_no_fetcher_returns_empty_without_network(monkeypatch):
    """Flag OFF + no injected fetcher -> ([], 0.0), touching no network."""
    monkeypatch.delenv("HERMES_QUANT_INSIDER_ENABLED", raising=False)
    called = {"n": 0}

    def _should_not_be_called(url, timeout):  # pragma: no cover - asserts it isn't
        called["n"] += 1
        return _SUBMISSIONS_PAYLOAD

    monkeypatch.setattr(form4, "_fetch_raw", _should_not_be_called)
    filings, lat = fetch_form4_filings("320193")
    assert filings == []
    assert lat == 0.0
    assert called["n"] == 0  # OFF must not reach the real fetcher


def test_injected_fetcher_works_even_when_flag_off(monkeypatch):
    """An explicitly injected fetcher (tests) bypasses the flag gate."""
    monkeypatch.delenv("HERMES_QUANT_INSIDER_ENABLED", raising=False)
    filings, _ = fetch_form4_filings("320193", fetcher=_ok_fetcher)
    assert len(filings) == 3  # flag OFF but injected fetcher still parses


def test_insider_enabled_reads_flag_at_call_time(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_INSIDER_ENABLED", raising=False)
    assert form4.insider_enabled() is False
    monkeypatch.setenv("HERMES_QUANT_INSIDER_ENABLED", "1")
    assert form4.insider_enabled() is True
    monkeypatch.setenv("HERMES_QUANT_INSIDER_ENABLED", "0")
    assert form4.insider_enabled() is False


# --- FilingEvidence build + deterministic idempotent store round-trip --------
def test_to_filing_evidence_is_valid_filing_record():
    row0 = parse_submissions(_SUBMISSIONS_PAYLOAD)[0]
    ev = to_filing_evidence(row0)
    assert isinstance(ev, FilingEvidence)
    assert ev.kind == "filing"
    assert ev.source == "sec_edgar_form4"
    assert ev.accession_number == row0.accession_number
    assert ev.form_type == row0.form_type
    assert ev.symbol == "AAPL"
    # payload_ref is the Archives URL (provenance), with the un-padded CIK.
    assert "edgar/data/320193/" in ev.payload_ref


def test_evidence_id_is_deterministic_for_same_filing():
    row0 = parse_submissions(_SUBMISSIONS_PAYLOAD)[0]
    ev_a = to_filing_evidence(row0)
    ev_b = to_filing_evidence(row0)
    # ingested_at (now()) differs but is OUTSIDE the identity triple -> same id.
    assert ev_a.id == ev_b.id
    assert ev_a.payload_hash == ev_b.payload_hash


def test_store_round_trip_and_idempotent_reappend(tmp_path: Path):
    store = EvidenceStore(root=tmp_path / "evidence_store")
    row0 = parse_submissions(_SUBMISSIONS_PAYLOAD)[0]
    ev = to_filing_evidence(row0)
    store.append(ev)
    got = store.get(ev.id)
    assert got is not None
    assert got["accession_number"] == row0.accession_number
    assert got["form_type"] == "4"
    # available_at persisted is the acceptance moment (asof honesty survives
    # storage). Compare parsed instants — the store may re-serialize the tz as
    # "Z" rather than "+00:00", which is the same UTC instant.
    stored_avail = datetime.fromisoformat(str(got["available_at"]).replace("Z", "+00:00"))
    assert stored_avail == ev.available_at
    # re-append the SAME filing -> idempotent no-op (same deterministic id).
    store.append(to_filing_evidence(row0))
    # still exactly one row for that id.
    chain = store.supersedes_chain(ev.id)
    assert len(chain) == 1


def test_amendment_can_supersede_original(tmp_path: Path):
    store = EvidenceStore(root=tmp_path / "evidence_store")
    filings = parse_submissions(_SUBMISSIONS_PAYLOAD)
    orig = next(f for f in filings if f.form_type == "4")
    amend = next(f for f in filings if f.form_type == "4/A")
    ev_orig = to_filing_evidence(orig)
    store.append(ev_orig)
    ev_amend = to_filing_evidence(amend, supersedes=str(ev_orig.id))
    store.append(ev_amend)
    chain = store.supersedes_chain(ev_amend.id)
    # walk amendment -> original
    assert [c["accession_number"] for c in chain] == [
        amend.accession_number,
        orig.accession_number,
    ]


# --- lookahead gate protects any view citing a stored filing -----------------
def test_lookahead_gate_flags_view_before_available_at(tmp_path: Path):
    store = EvidenceStore(root=tmp_path / "evidence_store")
    row0 = parse_submissions(_SUBMISSIONS_PAYLOAD)[0]
    ev = to_filing_evidence(row0)
    store.append(ev)

    view = AnalystView(
        analyst="insider_test",
        direction="long",
        magnitude=0.01,
        confidence=0.5,
        confidence_raw=0.7,
        horizon="1d",
        evidence_ids=(str(ev.id),),
    )
    # asof BEFORE the filing was public -> lookahead violation flagged.
    before = ev.available_at - timedelta(hours=1)
    res_before = check_view_lookahead(view, before, store)
    assert res_before.ok is False
    assert res_before.n_evidence_checked == 1
    assert len(res_before.violations) == 1

    # asof AT/AFTER the filing's available_at -> clean.
    after = ev.available_at + timedelta(seconds=1)
    res_after = check_view_lookahead(view, after, store)
    assert res_after.ok is True
    assert len(res_after.violations) == 0


# --- tool surface: default-OFF silence ---------------------------------------
def test_quant_insider_tool_silent_when_flag_off(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_INSIDER_ENABLED", raising=False)
    from hermes_quant.tools import quant_insider

    out = json.loads(quant_insider({"cik": "320193"}))
    assert out["success"] is True
    assert out["enabled"] is False
    assert out["filings"] == []


def test_quant_insider_tool_bare_date_since_filters_not_drops(monkeypatch):
    """Flag ON + a natural bare-date ``since`` must DATE-FILTER, not opaque-fail.

    Regression: ``datetime.fromisoformat('2025-01-01')`` is NAIVE; the tool only
    caught ValueError, so the naive value flowed into parse_submissions where the
    tz-aware ``filed_at < since`` comparison raised TypeError, propagated to the
    tool's catch-all and returned ``{success: False}`` with EVERY filing dropped
    behind a generic error. The fix coerces ``since`` to tz-aware UTC at the
    boundary so the common bare-date input works.
    """
    monkeypatch.setenv("HERMES_QUANT_INSIDER_ENABLED", "1")
    # Inject the offline fixture via the adapter's real fetch path (the tool calls
    # fetch_form4_filings without a fetcher, which uses _fetch_raw when ON).
    monkeypatch.setattr(form4, "_fetch_raw", _ok_fetcher)
    from hermes_quant.tools import quant_insider

    # Bare ISO date -> naive datetime. Before the fix this triggered the TypeError.
    out = json.loads(quant_insider({"cik": "320193", "since": "2025-03-18"}))
    assert out["success"] is True, out
    assert out["enabled"] is True
    # Date filter applied (treated as UTC): row 0 (accepted 2025-03-17) dropped,
    # not ALL filings — count must be > 0 and the early accession absent.
    accnos = {f["accession_number"] for f in out["filings"]}
    assert "0000320193-25-000077" not in accnos
    assert out["count"] >= 1

    # Same with an explicit naive datetime form (T00:00:00, still tzinfo=None).
    out2 = json.loads(quant_insider({"cik": "320193", "since": "2025-03-18T00:00:00"}))
    assert out2["success"] is True, out2
    assert out2["count"] == out["count"]
