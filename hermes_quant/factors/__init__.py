"""hermes_quant.factors — Factor library with IC deduplication gate.

Exports:
    ICDedupGate   — gate that rejects factors whose IC correlation with the
                    existing library exceeds a configurable threshold (default
                    0.99 per consensus pattern C5, R&D-Agent NeurIPS 2025).
    ICDedupResult — structured result of .check()
    ic_metrics    — sub-module with compute_ic / compute_icir / factor_correlation
"""

from hermes_quant.factors.ic_dedup import ICDedupGate, ICDedupResult
from hermes_quant.factors import ic_metrics

__all__ = [
    "ICDedupGate",
    "ICDedupResult",
    "ic_metrics",
]
