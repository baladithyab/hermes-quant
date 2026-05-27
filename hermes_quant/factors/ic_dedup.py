"""hermes_quant.factors.ic_dedup — IC deduplication gate.

Prevents the "Correlation Red Sea" (F4) failure mode where generative factor
mining fills the library with near-identical signals, causing false confidence
in factor diversity.

References:
    F4 — Correlation Red Sea / Factor Redundancy in Mining Loops
         FactorMiner (THU, arxiv:2602.14670), R&D-Agent(Q) (NeurIPS 2025,
         arxiv:2505.15155), AlphaPROBE (arxiv:2602.11917).
    C5 — Factor/Signal Deduplication via IC Correlation Gating
         Consensus pattern: ICmax ≥ 0.99 → discard.

Configuration
~~~~~~~~~~~~~
    threshold:  Default 0.99.  Override via env var
                HERMES_QUANT_IC_DEDUP_THRESHOLD (float 0–1).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from hermes_quant.factors.ic_metrics import factor_correlation

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default threshold — configurable via env var (do NOT hardcode 0.99 as the
# only possible value; operators on different alpha-density regimes may want
# a tighter 0.95 or looser 0.999 threshold).
# ---------------------------------------------------------------------------
_DEFAULT_THRESHOLD = float(
    os.environ.get("HERMES_QUANT_IC_DEDUP_THRESHOLD", "0.99")
)


@dataclass(frozen=True)
class ICDedupResult:
    """Result returned by :class:`ICDedupGate`.check().

    Attributes:
        passes:         True if the new factor is *not* redundant and can be
                        admitted to the library.
        max_corr:       Maximum absolute Pearson correlation found between the
                        new factor and any factor already in the library.
                        NaN when the library is empty.
        correlated_with: Name of the most-correlated existing factor, or None
                         when the library is empty or all correlations are NaN.
        reason:         Human-readable explanation.
    """

    passes: bool
    max_corr: float
    correlated_with: str | None
    reason: str


class ICDedupGate:
    """Gate that enforces IC correlation deduplication on a factor library.

    Usage::

        gate = ICDedupGate(threshold=0.99)
        result = gate.check("momentum_12m", returns_array)
        if result.passes:
            gate.register("momentum_12m", returns_array)

    The library is held in-memory as a dict of ``{factor_name: np.ndarray}``.
    Persistence is via :meth:`save` / :meth:`load` which serialise factor
    return arrays as JSON lists (UTF-8, human-readable, diff-friendly).
    For large libraries consider a binary format; the interface is stable.

    Args:
        threshold:  Maximum permitted |correlation| for a new factor.
                    Values ≥ threshold → rejected.  Default from env var
                    HERMES_QUANT_IC_DEDUP_THRESHOLD (fallback 0.99).
    """

    def __init__(self, threshold: float | None = None) -> None:
        self.threshold: float = (
            threshold if threshold is not None else _DEFAULT_THRESHOLD
        )
        self._library: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Core gate logic
    # ------------------------------------------------------------------

    def check(
        self,
        new_factor_returns: np.ndarray,
        existing_library: dict[str, np.ndarray] | None = None,
        threshold: float | None = None,
    ) -> ICDedupResult:
        """Check whether a new factor is sufficiently distinct from the library.

        Args:
            new_factor_returns: Return series of the candidate factor.
            existing_library:   Override the gate's internal library for this
                                check only.  Defaults to ``self._library``.
            threshold:          Override the gate's instance threshold for this
                                call only.  Defaults to ``self.threshold``.

        Returns:
            ICDedupResult describing pass/fail and which factor (if any)
            was most correlated.
        """
        lib = existing_library if existing_library is not None else self._library
        thr = threshold if threshold is not None else self.threshold

        arr = np.asarray(new_factor_returns, dtype=float)

        if not lib:
            return ICDedupResult(
                passes=True,
                max_corr=float("nan"),
                correlated_with=None,
                reason="library_empty",
            )

        max_corr = float("-inf")
        most_correlated: str | None = None

        for name, existing in lib.items():
            corr = factor_correlation(arr, existing)
            if not np.isfinite(corr):
                continue
            abs_corr = abs(corr)
            if abs_corr > max_corr:
                max_corr = abs_corr
                most_correlated = name

        if max_corr == float("-inf"):
            # All correlations were NaN (e.g. constant series)
            return ICDedupResult(
                passes=True,
                max_corr=float("nan"),
                correlated_with=None,
                reason="all_correlations_nan",
            )

        passes = max_corr < thr
        if passes:
            reason = (
                f"max_corr={max_corr:.4f} < threshold={thr:.4f}"
            )
        else:
            reason = (
                f"rejected: max_corr={max_corr:.4f} >= threshold={thr:.4f} "
                f"(most similar: {most_correlated!r})"
            )

        return ICDedupResult(
            passes=passes,
            max_corr=max_corr,
            correlated_with=most_correlated,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Library management
    # ------------------------------------------------------------------

    def register(self, factor_name: str, factor_returns: np.ndarray) -> None:
        """Add a factor to the in-memory library unconditionally.

        Callers should call :meth:`check` first and only register on pass.
        Registering a duplicate name overwrites the previous series.
        """
        self._library[factor_name] = np.asarray(factor_returns, dtype=float)
        logger.debug("ICDedupGate: registered factor %r (n=%d)", factor_name, len(factor_returns))

    def remove(self, factor_name: str) -> bool:
        """Remove a factor from the library.  Returns True if it existed."""
        existed = factor_name in self._library
        self._library.pop(factor_name, None)
        return existed

    @property
    def library(self) -> dict[str, np.ndarray]:
        """Read-only view of the current factor library."""
        return dict(self._library)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Persist the factor library to a JSON file.

        Format: ``{factor_name: [float, ...]}``.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: arr.tolist() for name, arr in self._library.items()}
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        logger.info(
            "ICDedupGate: saved %d factors to %s", len(self._library), p
        )

    def load(self, path: str | Path) -> None:
        """Load factor library from a JSON file (replaces current library)."""
        p = Path(path)
        with open(p, encoding="utf-8") as fh:
            payload = json.load(fh)
        self._library = {
            name: np.array(vals, dtype=float) for name, vals in payload.items()
        }
        logger.info(
            "ICDedupGate: loaded %d factors from %s", len(self._library), p
        )

    def __len__(self) -> int:
        return len(self._library)

    def __contains__(self, factor_name: str) -> bool:
        return factor_name in self._library
