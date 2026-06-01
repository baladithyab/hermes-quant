"""hermes_quant.evidence.adapters — read-only EvidenceRecord producers (B20+).

Each adapter fetches from a public, asof-honest source and normalizes into a
per-kind ``EvidenceRecord`` subtype (``evidence.schema``) ready for
``EvidenceStore.append``. Adapters follow the ``catalyst/ingest.py`` contract:
stdlib-only fetch, an injectable ``fetcher(url, timeout) -> bytes`` for offline
tests, fail-closed/silence-by-default (any error -> empty result, never raises),
and an asof anchor on the REAL publication timestamp (never wall-clock now()).

Public surface:
    form4 — SEC EDGAR Form-4 insider-transactions adapter (default-OFF).
"""

from __future__ import annotations
