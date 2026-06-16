"""hermes_quant.evidence.adapters.form4 — SEC EDGAR Form-4 insider adapter (B20).

A read-only producer of ``filing``-kind ``EvidenceRecord``s (the schema's
``FilingEvidence``, which the schema was pre-designed for and names ``'4'``
explicitly) from SEC EDGAR — the ONLY asof-honest source for insider
transactions.

WHY EDGAR, NOT yfinance (the load-bearing design decision):
  * The asof anchor MUST be the EDGAR **acceptance datetime / filing date** —
    the moment the Form 4 became public. Insiders have up to 2 business days
    (historically much longer for late filings) between transacting and the
    filing hitting EDGAR. Anchoring on the *transaction* date back-dates public
    availability and corrupts every backtest that cites the record.
  * yfinance carries ONLY the transaction date (``Start Date``) and has empty
    ``URL``/``Transaction`` columns on flagship tickers — it cannot supply the
    filing date or the accession number. It is therefore DISQUALIFIED as the
    EvidenceRecord anchor (research note B20 §1a/§5).

Mirrors the ``catalyst/ingest.py`` ingester contract exactly:
  * stdlib-only fetch (``urllib.request`` + ``json``), an SEC-mandated
    ``User-Agent`` of the form ``<name> <email>``;
  * an injectable ``fetcher(url, timeout) -> bytes`` so tests are
    offline-deterministic (no live network);
  * fail-closed / silence-by-default: ANY error (the documented SEC 403 from
    cloud egress, timeout, parse failure, empty body) -> ``([], latency)``,
    never raises — a blocked feed must not break the daily run or fabricate data;
  * a filing whose acceptance/filing timestamp is unparseable is SKIPPED, NEVER
    defaulted to ``now()`` (same rule as ``ingest.py:127`` / ``social.py``).

DEFAULT-OFF: the adapter is opt-in behind ``HERMES_QUANT_INSIDER_ENABLED`` read
AT CALL TIME by :func:`fetch_form4_filings`. OFF (the default) ⇒ ``([], 0.0)``
with no network touched. ``.env`` is tool-guarded — the agent emits the flag
line, the operator flips it after a connectivity smoke check + negative-control
eval (the same bar the catalyst subsystem had to clear; research note B20 §3
Rollout). The deterministic risk gate / sizing ladder / kill-switch are NEVER
touched by this module.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from hermes_quant.evidence.schema import (
    FilingEvidence,
    compute_available_at,
    derive_evidence_id,
    sha256_of_json,
)

logger = logging.getLogger(__name__)

# SEC-mandated User-Agent form: "<Sample Company Name> <AdminContact@example.com>"
# (name + contact email). A bare/abusive UA -> ~10-minute IP block (research
# note B20 §1b). Kept descriptive + research-tagged like the catalyst ingesters.
_UA = "HermesQuant/0.1 research hermes-quant@example.com"
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accno_nodash}/{primary_doc}"
_DEFAULT_TIMEOUT = 15.0
_SOURCE = "sec_edgar_form4"

# Default-OFF flag (read at call time). OFF -> adapter returns no filings.
_FLAG = "HERMES_QUANT_INSIDER_ENABLED"

# Root form "4" matches both "4" and its amendment "4/A" (EDGAR groups them).
_FORM4_KINDS = frozenset({"4", "4/A"})

# EDGAR clocks its acceptance/filing wall-clocks in US/Eastern. The civil offset
# is -05:00 in winter (EST, DST off ~Nov-Mar) and -04:00 in summer (EDT). We
# resolve it PER-DATE from the IANA zone (stdlib zoneinfo, no new dependency)
# rather than hard-coding one offset: a fixed -04:00 would anchor EST-season
# filings 1h too EARLY in UTC, fabricating earlier public availability and
# defeating the no-lookahead gate. -05:00 is the LATER (never-earlier) UTC
# instant for a given Eastern wall-clock, so the per-date resolution is the
# conservative, asof-honest choice.
_ET = ZoneInfo("America/New_York")


def insider_enabled() -> bool:
    """True iff the default-OFF insider flag is set to "1" RIGHT NOW.

    Read at call time (not import time) so the operator can flip ``.env`` /
    the environment without a reload — same convention as
    ``HERMES_QUANT_SEMANTIC_ENABLED`` (advisor.py) / ``HERMES_QUANT_CONVERGENCE``.
    """
    return os.environ.get(_FLAG, "0") == "1"


@dataclass(frozen=True)
class InsiderFiling:
    """One normalized Form-4 filing (the intermediate, mirrors ``CatalystItem``).

    ``filed_at`` is the SOLE asof anchor — the EDGAR acceptance datetime (or, as
    a conservative fallback, the filing date pushed to end-of-day ET so we never
    claim a filing was public EARLIER than it was). ``period_of_report`` is the
    transaction/event date and is METADATA ONLY — it must NEVER anchor asof.
    """

    accession_number: str  # e.g. "0000320193-25-000077"
    form_type: str  # "4" or "4/A"
    issuer_symbol: str | None
    issuer_cik: str  # 10-digit zero-padded
    filed_at: datetime  # tz-aware UTC — the acceptance datetime / filing-date anchor
    period_of_report: date | None  # transaction/event date — metadata, NOT the anchor
    primary_doc: str  # primary document filename (for the Archives URL)

    def payload(self) -> dict:
        """Canonical, deterministic payload for hashing/identity.

        No ``now()`` inside — identity is a pure function of the filing's own
        fields, so the same filing re-fetched yields the same ``payload_hash``
        and therefore the same evidence ``id`` (idempotent append).
        """
        return {
            "accession_number": self.accession_number,
            "form_type": self.form_type,
            "issuer_cik": self.issuer_cik,
            "issuer_symbol": self.issuer_symbol,
            "filed_at": self.filed_at.isoformat(),
            "period_of_report": self.period_of_report.isoformat()
            if self.period_of_report is not None
            else None,
            "primary_doc": self.primary_doc,
            "source": _SOURCE,
        }

    def archive_url(self) -> str:
        """The www.sec.gov Archives URL for the primary document (provenance)."""
        return _ARCHIVE.format(
            cik=str(int(self.issuer_cik)),  # Archives path uses the un-padded CIK
            accno_nodash=self.accession_number.replace("-", ""),
            primary_doc=self.primary_doc,
        )


# --------------------------------------------------------------------------- #
# Fetch + parse helpers (injectable, never raise — silence-by-default)
# --------------------------------------------------------------------------- #
def _fetch_raw(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed SEC host
        return resp.read()


def _pad_cik(cik: str | int) -> str:
    """Zero-pad a CIK to 10 digits (required by the data.sec.gov submissions URL)."""
    return f"{int(cik):010d}"


def _parse_acceptance_dt(s: str) -> datetime | None:
    """Parse an EDGAR ``acceptanceDateTime`` to tz-aware UTC. None on failure.

    EDGAR emits acceptance datetimes either as ISO-8601 with an offset
    (e.g. ``"2025-03-15T16:30:00-04:00"``) or as a compact ``YYYYMMDDHHMMSS``
    form in the legacy header. We try ``fromisoformat`` first (also accepts a
    trailing ``Z`` via normalization), then the compact form (assumed Eastern
    wall-clock, the EDGAR header convention). Anything unparseable -> None so the
    caller SKIPS the filing rather than fabricating a ``now()`` asof.
    """
    s = (s or "").strip()
    if not s:
        return None
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is None:
        # Compact legacy header form: "20250315163000" (Eastern wall-clock).
        try:
            naive = datetime.strptime(s, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        # Treat the compact header time as US/Eastern (EDGAR's clock). The civil
        # offset (EST -05:00 / EDT -04:00) is resolved PER-DATE from
        # America/New_York so a winter (EST) filing is never anchored 1h early —
        # the resulting available_at is never EARLIER than the true public moment.
        dt = naive.replace(tzinfo=_ET)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_filing_date_eod(s: str) -> datetime | None:
    """Parse an EDGAR ``filingDate`` (a bare date) to a CONSERVATIVE tz-aware UTC.

    Used only as a fallback when ``acceptanceDateTime`` is absent. A Form 4 is
    public no earlier than the END of its filing day, so we anchor at
    end-of-day Eastern (23:59:59 ET). The civil offset is resolved PER-DATE from
    America/New_York, so this is 04:59:59Z next day in winter (EST) and
    03:59:59Z next day in summer (EDT) — always a LATER bound. This guarantees
    we never claim the filing was public earlier than it actually was
    (no-lookahead). Unparseable -> None (caller SKIPS, never now()).
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    eod_et = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=_ET)
    return eod_et.astimezone(UTC)


def _parse_period(s: str) -> date | None:
    """Parse an EDGAR ``reportDate`` (period of report) to a date. None on failure.

    METADATA ONLY — this is the transaction/event date and must NEVER be used as
    the asof anchor. Failure to parse it does NOT drop the filing (the anchor is
    ``filed_at``, parsed separately).
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_submissions(
    raw: bytes, *, since: datetime | None = None
) -> list[InsiderFiling]:
    """Parse a data.sec.gov submissions JSON payload into Form-4 ``InsiderFiling``s.

    Keeps only ``form`` in {"4", "4/A"}. Anchors ``filed_at`` on
    ``acceptanceDateTime`` (preferred) else the end-of-day ``filingDate``
    fallback. A row whose acceptance/filing timestamp is unparseable is SKIPPED
    (never ``now()``). Optional ``since`` (tz-aware UTC) keeps only filings with
    ``filed_at >= since``. Never raises: a malformed body -> ``[]``.
    """
    items: list[InsiderFiling] = []
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("form4: submissions JSON parse error: %s", e)
        return items
    if not isinstance(doc, dict):
        return items

    cik_raw = doc.get("cik")
    try:
        cik10 = _pad_cik(cik_raw) if cik_raw is not None else ""
    except (ValueError, TypeError):
        cik10 = ""
    # Issuer ticker, if present (submissions carries a `tickers` array). For a
    # Form 4 the *subject company* is the issuer, so this is the issuer symbol.
    tickers = doc.get("tickers")
    symbol = tickers[0] if isinstance(tickers, list) and tickers else None

    filings = doc.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return items

    forms = recent.get("form") or []
    accnos = recent.get("accessionNumber") or []
    accept = recent.get("acceptanceDateTime") or []
    fdates = recent.get("filingDate") or []
    rdates = recent.get("reportDate") or []
    pdocs = recent.get("primaryDocument") or []
    n = len(forms)

    def _at(arr: list, i: int) -> str:
        return arr[i] if i < len(arr) and arr[i] is not None else ""

    for i in range(n):
        form = (_at(forms, i) or "").strip()
        if form not in _FORM4_KINDS:
            continue
        # Anchor: acceptance datetime preferred, end-of-day filingDate fallback.
        filed_at = _parse_acceptance_dt(_at(accept, i))
        if filed_at is None:
            filed_at = _parse_filing_date_eod(_at(fdates, i))
        if filed_at is None:
            # No parseable public timestamp -> cannot anchor asof honestly. SKIP.
            # (Never default to now(): that would fabricate freshness.)
            logger.debug("form4: skipping filing with no parseable filed_at (form=%s)", form)
            continue
        if since is not None and filed_at < since:
            continue
        accno = (_at(accnos, i) or "").strip()
        if not accno:
            continue  # accession number is the FilingEvidence identity field
        items.append(
            InsiderFiling(
                accession_number=accno,
                form_type=form,
                issuer_symbol=symbol,
                issuer_cik=cik10,
                filed_at=filed_at,
                period_of_report=_parse_period(_at(rdates, i)),
                primary_doc=(_at(pdocs, i) or "").strip(),
            )
        )
    return items


def fetch_form4_filings(
    cik: str | int,
    *,
    since: datetime | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    fetcher=None,
) -> tuple[list[InsiderFiling], float]:
    """Fetch recent Form-4 filings for a CIK from EDGAR. Returns (filings, latency).

    DEFAULT-OFF: if ``HERMES_QUANT_INSIDER_ENABLED`` is not "1" at call time,
    returns ``([], 0.0)`` with NO network touched (opt-in; research note B20 §3).

    ``fetcher`` is injectable (``fetcher(url, timeout) -> bytes``) for offline
    tests. On ANY network/parse failure — including the documented SEC 403 from
    cloud egress — returns ``([], latency)`` and NEVER raises (silence-by-default;
    a blocked feed must not break the daily run). The 403 is a NORMAL expected
    outcome from blocked egress, not an exception to surface.
    """
    if fetcher is None and not insider_enabled():
        # Flag OFF and no test fetcher injected -> stay silent, touch no network.
        return [], 0.0
    cik10 = _pad_cik(cik)
    url = _SUBMISSIONS.format(cik10=cik10)
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 - feed failure (incl. SEC 403) is non-fatal
        logger.warning("form4: fetch failed for CIK %s: %s", cik10, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    items = parse_submissions(raw, since=since)
    return items, latency


def to_filing_evidence(
    f: InsiderFiling, *, supersedes: str | None = None
) -> FilingEvidence:
    """Build a ``FilingEvidence`` from an ``InsiderFiling`` (deterministic identity).

    asof honesty (the whole point of B20):
      * ``published_at`` = ``f.filed_at`` — the EDGAR acceptance/filing moment,
        NEVER the transaction date / period_of_report.
      * ``available_at`` = ``compute_available_at("filing", published_at)`` — the
        ``filing`` ingest-lag floor is 0s, so ``available_at == published_at``.
      * ``ingested_at`` = wall-clock now (the only timestamp that legitimately
        may be now(); causality only constrains ``available_at >= published_at``,
        and it is OUTSIDE the identity triple so it never affects the id/hash).

    Identity is ``derive_evidence_id("filing", source, payload_hash)`` over a
    canonical ``sha256_of_json(payload)`` — same filing -> same UUID -> a re-append
    is a no-op (idempotent store). Pass ``supersedes`` (the prior record id) when
    emitting a ``4/A`` amendment so the store links it rather than overwriting
    (the store is append-only and raises on overwrite).
    """
    payload = f.payload()
    phash = sha256_of_json(payload)
    published = f.filed_at
    available = compute_available_at("filing", published)
    return FilingEvidence(
        id=derive_evidence_id("filing", _SOURCE, phash),
        kind="filing",
        symbol=f.issuer_symbol,
        source=_SOURCE,
        published_at=published,
        ingested_at=datetime.now(UTC),
        available_at=available,
        payload_ref=f.archive_url(),
        payload_hash=phash,
        supersedes=UUID(supersedes) if supersedes else None,
        accession_number=f.accession_number,
        form_type=f.form_type,
    )
