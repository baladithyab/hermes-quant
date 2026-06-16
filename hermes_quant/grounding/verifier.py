"""hermes_quant.grounding.verifier — ClaimVerifier with Citation HARD RULE enforcement.

Wave 5 (ADR-0038 §W5). TauricResearch v0.2.5 pattern: forces tool calls before
synthesis to eliminate empty-memory hallucination and fabricated sentiment posts.

Drop-rate target: ClaimVerifier rejects ≥95% of AnalystViews whose rationale
contains un-cited numerical claims (tested in tests/grounding/).

Regex coverage
--------------
The single combined pattern handles:
  - Plain integers/decimals:           1.23, 123, 1234.56
  - Currency prefixed:                 $1.23, $1,234.56
  - Percentages (pos/neg/plain):       1.23%, +1.23%, -0.45%
  - Comma-thousands:                   1,234.56, 12,345

Post-match normalization strips punctuation ($ , %) for lookup in ground-truth text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hermes_quant.grounding.data_grounding import GroundTruthBlock, render_for_prompt
from hermes_quant.protocol import AnalystView

# ---------------------------------------------------------------------------
# Numerical claim extraction regex
# ---------------------------------------------------------------------------

# Matches: $1,234.56  |  +1.23%  |  -0.45%  |  1.23%  |  1,234.56  |  1.23
_NUM_PATTERN = re.compile(
    r"""
    (?:
        (?:\$)                                 # optional currency prefix
        (?:\d{1,3}(?:,\d{3})*|\d+)            # integer part with optional comma-thousands
        (?:\.\d+)?                             # optional decimal
    )
    |
    (?:
        [+-]?                                  # optional sign
        (?:\d{1,3}(?:,\d{3})*|\d+)            # integer part
        (?:\.\d+)?                             # optional decimal
        %                                      # percentage suffix required
    )
    |
    (?:
        (?:\d{1,3}(?:,\d{3})+)                # comma-separated thousands (no %)
        (?:\.\d+)?
    )
    |
    (?:
        \d+\.\d+                               # plain decimal (no suffix)
    )
    """,
    re.VERBOSE,
)

# Citation marker: [gt_SYMBOL_YYYYMMDD_field]  — field names like 'close', 'quote' are lowercase
_CITATION_PATTERN = re.compile(r"\[gt_[A-Za-z0-9_]+\]")


def _normalize_number(raw: str) -> str:
    """Strip punctuation ($, ,, %) for lookup in ground-truth rendered text."""
    return raw.replace("$", "").replace(",", "").replace("%", "").lstrip("+")


def _number_in_gt_text(norm: str, gt_text: str) -> bool:
    """True if *norm* appears as a standalone number in *gt_text*.

    Uses word-boundary-aware regex to prevent '0.75' matching inside '170.7500'.
    Accepts leading/trailing whitespace, brackets, comma, newline, or end-of-string.
    """
    if not norm:
        return False
    # Escape the normalized number for regex use
    escaped = re.escape(norm)
    # Word boundaries: not preceded/followed by digit or decimal point
    pattern = rf"(?<![0-9.]){escaped}(?![0-9.])"
    return bool(re.search(pattern, gt_text))


def extract_numerical_claims(text: str) -> list[str]:
    """Return list of raw numerical strings found in *text*."""
    return _NUM_PATTERN.findall(text)


def extract_citation_markers(text: str) -> set[str]:
    """Return set of citation IDs referenced in *text* (bare IDs, no brackets)."""
    return {m.strip("[]") for m in _CITATION_PATTERN.findall(text)}


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """Result of ClaimVerifier.verify().

    Attributes
    ----------
    accepted          : True if citation_coverage >= threshold
    citation_coverage : fraction of claims that are cited (0.0–1.0)
    uncited_claims    : raw numerical strings that could not be traced
    reason            : human-readable verdict summary
    """

    accepted: bool
    citation_coverage: float
    uncited_claims: list[str]
    reason: str | None = None


# ---------------------------------------------------------------------------
# ClaimVerifier
# ---------------------------------------------------------------------------


class ClaimVerifier:
    """Verifies that every numerical claim in an AnalystView is cited.

    A numerical claim is considered cited iff its normalized value appears as a
    standalone number in ``render_for_prompt(block)`` (check (a)).

    A nearby valid citation marker is NOT sufficient on its own to credit a number
    that is absent from the block text. Block citation markers are coarse — one
    marker per bar close (``gt_SYMBOL_YYYYMMDD_close``) — so the marker
    ``[gt_AAPL_20260507_close]`` attests to exactly one value (that day's close) and
    cannot stand in for an arbitrary unrelated number printed beside it. Crediting
    any number within 80 chars of a valid marker is precisely the
    "one-citation-covers-all-fabricated-numbers" loophole this module exists to
    eliminate (F3: LLM-fabricated price levels). Proximity to a valid marker is a
    NECESSARY-but-not-SUFFICIENT signal; the number itself must be traceable to the
    block, so the substring check (a) is the sole gate for numeric claims.

    Parameters
    ----------
    threshold : citation_coverage floor for acceptance (default 0.5).
                Wave 5 acceptance: tests assert ≥95% rejection of un-cited views.
    """

    def __init__(self, threshold: float = 0.5) -> None:
        if not (0.0 <= threshold <= 1.0):
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")
        self.threshold = threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(
        self,
        view: AnalystView,
        block: GroundTruthBlock,
        *,
        claim_text: str | None = None,
    ) -> VerificationResult:
        """Verify that the full claim text cites numbers from *block*.

        Returns VerificationResult with accepted=True iff citation_coverage
        meets self.threshold.
        """
        rationale = claim_text if claim_text is not None else (view.rationale or "")

        claims = extract_numerical_claims(rationale)

        if not claims:
            # No numerical claims → nothing to cite → trivially accepted
            return VerificationResult(
                accepted=True,
                citation_coverage=1.0,
                uncited_claims=[],
                reason="No numerical claims found; trivially accepted.",
            )

        gt_text = render_for_prompt(block)

        cited: list[str] = []
        uncited: list[str] = []

        for raw_claim in claims:
            norm = _normalize_number(raw_claim)
            # A numeric claim is cited IFF its normalized value appears as a
            # standalone number in the ground-truth rendered text (uses
            # word-boundary matching to prevent '0.75' matching inside '170.7500').
            #
            # A nearby valid citation marker is deliberately NOT sufficient: block
            # markers are coarse (one per close) and attest to exactly one value, so
            # crediting any number within 80 chars of a valid marker is the
            # "one-citation-covers-all-fabricated-numbers" loophole (F3). A number
            # absent from the block stays UNCITED regardless of an adjacent marker.
            if _number_in_gt_text(norm, gt_text):
                cited.append(raw_claim)
                continue
            uncited.append(raw_claim)

        total = len(claims)
        n_cited = len(cited)
        coverage = n_cited / total if total > 0 else 1.0
        accepted = coverage >= self.threshold

        reason = (
            f"citation_coverage={coverage:.2f} ({'PASS' if accepted else 'FAIL'}, "
            f"threshold={self.threshold:.2f}). "
            f"{n_cited}/{total} claims cited. "
            + (f"Uncited: {uncited[:5]}" if uncited else "All claims cited.")
        )

        return VerificationResult(
            accepted=accepted,
            citation_coverage=round(coverage, 4),
            uncited_claims=uncited,
            reason=reason,
        )

    # NOTE: there is intentionally NO proximity-to-marker fallback for numeric
    # claims. Block citation markers are coarse (one per bar close) and attest to a
    # single value; crediting any number that merely sits near a valid marker is the
    # "one-citation-covers-all-fabricated-numbers" loophole (F3). A numeric claim is
    # cited iff its value is traceable in the ground-truth block text (the check
    # above). This subsumes the earlier ar65 valid-marker-proximity guard, which
    # still admitted a fabricated number adjacent to a valid marker — proximity
    # itself was the hole.
