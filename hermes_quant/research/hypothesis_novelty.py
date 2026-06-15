"""hermes_quant.research.hypothesis_novelty — textual novelty/dedup gate (W3, ADR-0080).

Sibling of ``factors/ic_dedup.py``: same gate SHAPE (``check()`` → frozen Result,
env-tunable threshold, empty-library passes) but the similarity metric is TEXTUAL
(token-set Jaccard over normalized claim strings) instead of NUMERIC (IC correlation).

Why a new module and NOT a 1:1 import of ic_dedup: ic_dedup compares factor RETURN
arrays (numpy, Pearson corr ≥0.99 → reject). The monthly meta-retro needs to avoid
re-proposing a near-duplicate of an existing hypothesis *claim* — a string, not a
return series. The concept (a similarity gate that rejects near-duplicates and admits
novel candidates, env-tunable) is reused; the implementation is independent.

PROPOSE-ONLY / advisory-plane: this gate only decides whether a CANDIDATE hypothesis
is novel enough to register ``status="open"``. It never touches a limit, a size, the
risk gate, or live policy.

Configuration
~~~~~~~~~~~~~
    threshold:  Default 0.85. Override via env var
                HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD (float 0–1).
                A candidate with max token-Jaccard >= threshold is REJECTED
                (too similar to an existing claim).
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default threshold — configurable via env var (mirrors ic_dedup's idiom; do NOT
# hardcode 0.85 as the only possible value).
# ---------------------------------------------------------------------------
_NOVELTY_ENV = "HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD"
_NOVELTY_DEFAULT = 0.85


def _default_threshold() -> float:
    """Resolve the default threshold at call time so tests that monkey-patch the
    env var are respected (mirrors audit_log._audit_path resolve-each-call idiom).

    ar12: reject non-finite (mirrors ic_dedup._finite_threshold). A bare ``float(...)``
    catches only ValueError, so ``"1e400"`` (overflows to ``inf`` without raising),
    ``"inf"``, and ``"nan"`` slip through. With ``passes = max_sim < thr``, ``thr=inf``
    makes every claim read as "novel" (defeats the dedup — re-propose near-duplicate
    hypotheses forever); ``nan`` makes nothing ever novel. Fall back to the default.
    """
    # Env default is a quoted literal ("0.85") — not str(_NOVELTY_DEFAULT) — so the
    # flag-inventory scanner (_VIA_CONST regex) keeps detecting this read; a computed
    # default silently drops the flag from FLAG-INVENTORY.md. _NOVELTY_DEFAULT mirrors
    # the literal and is the post-parse fallback value.
    raw = os.environ.get(_NOVELTY_ENV, "0.85")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "hypothesis_novelty: ignoring non-numeric %s=%r; using default %s",
            _NOVELTY_ENV, raw, _NOVELTY_DEFAULT,
        )
        return _NOVELTY_DEFAULT
    if not math.isfinite(val):
        logger.warning(
            "hypothesis_novelty: ignoring non-finite %s=%r; using default %s",
            _NOVELTY_ENV, raw, _NOVELTY_DEFAULT,
        )
        return _NOVELTY_DEFAULT
    return val


# Backwards-compatible module-level constant (snapshot at import; the live default
# is resolved via _default_threshold()).
_DEFAULT_THRESHOLD = _default_threshold()

# Minimal English stopword set — kept small + deterministic (no NLTK dependency).
# Dropping stopwords prevents two claims from looking "similar" merely because they
# share filler words ("the", "a", "to", "on").
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
        "have", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this",
        "to", "was", "were", "will", "with", "over", "than", "then", "when",
        "into", "via", "per",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class NoveltyResult:
    """Result returned by :func:`check_novelty`. Mirrors ICDedupResult's shape.

    Attributes:
        passes:        True == novel == admissible (max_sim < threshold). A new
                       candidate may only be registered when this is True.
        max_sim:       Max token-set Jaccard similarity to any existing claim.
                       0.0 when the existing library is empty.
        nearest_claim: The most-similar existing claim, or None when the library
                       is empty / all similarities are 0.
        reason:        Human-readable explanation.
    """

    passes: bool
    max_sim: float
    nearest_claim: str | None
    reason: str


def _normalize(claim: str) -> frozenset[str]:
    """Lowercase, strip punctuation, drop stopwords; return the token SET.

    A set (not a bag) is deliberate: Jaccard over sets is symmetric, in [0,1], and
    insensitive to token repetition — two paraphrases of the same claim collapse to
    near-identical token sets regardless of word count.
    """
    if not claim:
        return frozenset()
    tokens = _TOKEN_RE.findall(claim.lower())
    return frozenset(t for t in tokens if t not in _STOPWORDS)


def token_jaccard(a: str, b: str) -> float:
    """|A∩B| / |A∪B| over normalized token sets. 0.0 when either side is empty."""
    sa, sb = _normalize(a), _normalize(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return inter / union


def check_novelty(
    candidate_claim: str,
    existing_claims: list[str],
    threshold: float | None = None,
) -> NoveltyResult:
    """Reject (passes=False) when max token-Jaccard >= threshold.

    Mirrors :meth:`ICDedupGate.check` shape: textual (Jaccard) instead of numeric
    (IC corr). An empty existing library -> passes=True (nothing to dedup against),
    exactly like ICDedupGate returns passes on ``library_empty``.

    Args:
        candidate_claim:  The proposed hypothesis claim string.
        existing_claims:  Claims already in the registry (read once by the caller).
        threshold:        Override the env/default cutoff for this call only.

    Returns:
        NoveltyResult describing pass/fail and the nearest existing claim.
    """
    thr = threshold if threshold is not None else _default_threshold()

    if not existing_claims:
        return NoveltyResult(
            passes=True,
            max_sim=0.0,
            nearest_claim=None,
            reason="library_empty",
        )

    max_sim = 0.0
    nearest: str | None = None
    # Deterministic iteration order (sorted) so the nearest_claim tie-break is stable.
    for existing in sorted(existing_claims):
        sim = token_jaccard(candidate_claim, existing)
        if sim > max_sim:
            max_sim = sim
            nearest = existing

    passes = max_sim < thr
    if passes:
        reason = f"max_sim={max_sim:.4f} < threshold={thr:.4f}"
    else:
        reason = (
            f"rejected: max_sim={max_sim:.4f} >= threshold={thr:.4f} "
            f"(nearest: {nearest!r})"
        )

    return NoveltyResult(
        passes=passes,
        max_sim=round(max_sim, 6),
        nearest_claim=nearest,
        reason=reason,
    )
