"""ob3 BESIDE-not-REPLACE guard: form4.py must be UNTOUCHED by aegis-ob3.

The ob3 OpenBB insider provider sits BESIDE the existing EDGAR Form-4 adapter
(``hermes_quant/evidence/adapters/form4.py``) and feeds the SAME ``filing``-kind
evidence series — it must NOT replace or modify form4's own ingestion. This test
pins form4's blob to its pre-ob3 content (the git blob at base 2dcb66d, sha
866cf1d) so any accidental edit to form4 by the ob3 lane fails loudly.

If form4.py is legitimately changed by ANOTHER lane/commit, update _EXPECTED_SHA
to the new ``git rev-parse HEAD:hermes_quant/evidence/adapters/form4.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FORM4_REL = "hermes_quant/evidence/adapters/form4.py"

# Blob sha of form4.py at base 2dcb66d (pre-ob3). ob3 adds files BESIDE form4;
# it never touches this file.
_EXPECTED_SHA = "866cf1dfc64a045182ac9e7c6246a24c7d032ac6"


def _git_blob_sha(rel: str) -> str:
    """Blob sha of the WORKING-TREE content of ``rel`` (hash-object), so an
    uncommitted edit is caught too."""
    out = subprocess.run(
        ["git", "hash-object", str(_REPO / rel)],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_form4_blob_unchanged_by_ob3():
    """form4.py working-tree content matches its pre-ob3 blob (untouched).

    RED-proof: if ob3 edited form4.py the working-tree blob sha would diverge
    from _EXPECTED_SHA and this assertion would fail.
    """
    try:
        actual = _git_blob_sha(_FORM4_REL)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:  # pragma: no cover
        pytest.skip(f"git hash-object unavailable: {e}")
    assert actual == _EXPECTED_SHA, (
        f"form4.py was modified by ob3 (BESIDE-not-REPLACE violation): "
        f"expected blob {_EXPECTED_SHA}, got {actual}"
    )


def test_form4_evidence_path_still_importable_and_intact():
    """form4's public surface (the contract ob3 mirrors but does not replace) is
    still importable and its asof anchor is still filed_at, not the trade date."""
    from datetime import UTC, date, datetime

    from hermes_quant.evidence.adapters.form4 import (
        InsiderFiling,
        to_filing_evidence,
    )

    f = InsiderFiling(
        accession_number="0000320193-25-000077",
        form_type="4",
        issuer_symbol="AAPL",
        issuer_cik="0000320193",
        filed_at=datetime(2025, 3, 17, 22, 30, tzinfo=UTC),
        period_of_report=date(2025, 3, 14),  # trade date — must NOT anchor
        primary_doc="xslF345X05/doc.xml",
    )
    ev = to_filing_evidence(f)
    # form4's anchor is the FILING moment, not the transaction date.
    assert ev.published_at == f.filed_at
    assert ev.source == "sec_edgar_form4"  # form4's own distinct source tag
