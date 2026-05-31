"""hermes_quant.perception.convergence — cross-SOURCE require_ensemble (ADR-0079 PDR-3, GAP-B).

The Camillo VALIDATE step relocated to the PERCEPTION layer: a trend is real only
when it shows across >=2 INDEPENDENT source families. Complementary to BMA's
cross-ANALYST require_ensemble (aggregators/bma.py:498-519); a social-arb signal
must clear BOTH. PURE + evidence-only: returns a score Mapping, never gates by
itself. The flag (HERMES_QUANT_CONVERGENCE) is read by the CALLER (synthesize), so
the scorer stays deterministic and offline-testable.

Rails: PerceptionFrame is a container (this fills .convergence); the deterministic
gate stays final; it can only SUBTRACT (haircut/drop a single-source packet),
never amplify. asof honesty: it reads only the items present at decision time.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hermes_quant.catalyst.ingest import CatalystItem

# -- family taxonomy (plan §1.1) --------------------------------------------
_FAMILY_REDDIT = "reddit"
_FAMILY_TRENDS = "google_trends"
_FAMILY_NEWS = "news_rss"
_FAMILY_WEB = "web_traffic"
_FAMILY_UNKNOWN = "unknown"  # never counts

CONVERGENCE_MIN_FAMILIES = 2  # the >=2 independent-origin bar (the taxonomy home)

# raw source string -> origin (the true independence unit, plan section 1.2 rule 3).
# Today each family == a distinct origin; the map only bites at B08.
_FAMILY_ORIGIN = {
    _FAMILY_REDDIT: "reddit",
    _FAMILY_TRENDS: "google",        # Google-owned
    _FAMILY_NEWS: "news_rss",        # syndicated news (collapsed within family already)
    _FAMILY_WEB: "web_traffic",      # B08 placeholder; set to "google" if a Google web-traffic feed lands
}

# press-wire / aggregator publisher substrings that are NOT independent reporting
# (plan section 1.2 rule 2). They still collapse into news_rss (no behavior change) but
# are FLAGGED so an operator audit sees when convergence rested on wire noise.
_SHARED_UPSTREAM = (
    "yahoo", "msn", "google news", "prnewswire", "pr newswire",
    "businesswire", "business wire", "globenewswire", "globe newswire",
    "accesswire", "newsfile",
)


def source_family(source: str) -> str:
    """Normalize a raw CatalystItem.source string to a source FAMILY.

    reddit/  -> reddit; google_trends -> google_trends; anything from the
    GN-RSS ingester (a bare publisher name) -> news_rss; recognized non-feed
    sources (sign-eval, phase0-label) -> unknown (never counts).
    """
    s = (source or "").strip().lower()
    if not s:
        return _FAMILY_UNKNOWN
    if s.startswith("reddit/"):
        return _FAMILY_REDDIT
    if s.startswith("google_trends"):
        return _FAMILY_TRENDS
    if s.startswith("web_traffic/") or s.startswith("similarweb"):
        return _FAMILY_WEB
    # non-feed / synthetic harness sources prove nothing about real convergence
    if s in {"sign-eval", "phase0-label", "n/a"} or s.startswith("test"):
        return _FAMILY_UNKNOWN
    # everything else is a GN-RSS publisher name (ingest.py:130) -> news family
    return _FAMILY_NEWS


@dataclass(frozen=True)
class ConvergenceResult:
    """Cross-source convergence evidence for one symbol's CatalystItem set.

    A CONTAINER of evidence, not an authority. ``validated`` is True iff
    ``n_independent >= min_families``.
    """
    n_items: int
    n_families: int                 # distinct families seen (excluding unknown)
    n_independent: int              # distinct ORIGINS (the true independence unit)
    families: tuple[str, ...]       # sorted distinct families (excluding unknown)
    validated: bool
    shared_upstream_collapsed: tuple[str, ...] = ()  # wire-republisher names seen
    min_families: int = 2

    def as_evidence(self) -> dict[str, Any]:
        """The Mapping stamped on PerceptionFrame.convergence (adapter.py:53)."""
        return {
            "n_items": self.n_items,
            "n_families": self.n_families,
            "n_independent": self.n_independent,
            "families": list(self.families),
            "validated": self.validated,
            "shared_upstream_collapsed": list(self.shared_upstream_collapsed),
            "min_families": self.min_families,
        }


def validate_convergence(
    items: Sequence[CatalystItem],
    *,
    min_families: int = CONVERGENCE_MIN_FAMILIES,
) -> ConvergenceResult:
    """PURE: count independent source ORIGINS across ``items`` for one symbol.

    >=min_families distinct origins (excluding 'unknown') => validated. This is
    cross-SOURCE require_ensemble: it asks "is this trend real?" and is
    COMPLEMENTARY to BMA's cross-ANALYST guard (a social-arb signal must clear
    BOTH). Reads only the items handed in (asof honesty: the caller filters the
    item set to <= decision time before calling).
    """
    families: set[str] = set()
    origins: set[str] = set()
    wires: list[str] = []
    for it in items:
        fam = source_family(it.source)
        if fam == _FAMILY_UNKNOWN:
            continue
        families.add(fam)
        origins.add(_FAMILY_ORIGIN.get(fam, fam))
        if fam == _FAMILY_NEWS:
            low = (it.source or "").lower()
            for w in _SHARED_UPSTREAM:
                if w in low:
                    wires.append(it.source)
                    break
    n_independent = len(origins)
    return ConvergenceResult(
        n_items=len(items),
        n_families=len(families),
        n_independent=n_independent,
        families=tuple(sorted(families)),
        validated=(n_independent >= min_families),
        shared_upstream_collapsed=tuple(sorted(set(wires))),
        min_families=min_families,
    )


__all__ = ["source_family", "validate_convergence", "ConvergenceResult", "CONVERGENCE_MIN_FAMILIES"]
