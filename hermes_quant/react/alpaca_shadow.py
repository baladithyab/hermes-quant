"""hermes_quant.react.alpaca_shadow — non-blocking SHADOW validation hook.

When ``HERMES_QUANT_ALPACA_SHADOW=1``, the REAL fill still goes through the
synthetic ``PaperReactor`` (the ``paper-default`` book is written EXACTLY as
today), but the proposal is ALSO submitted to Alpaca paper and the divergence
between Alpaca's real fill and the synthetic decision-price fill is logged to
``~/.hermes/quant/alpaca-shadow-divergence.jsonl``.

Rails:
  * NON-BLOCKING / fail-closed. Shadow must NEVER block, alter, or fail the
    synthetic fill — like the Wave-4 reflection hook, every error is swallowed
    with a warning. The synthetic record is returned to the caller untouched.
  * DEFAULT-OFF behind ``HERMES_QUANT_ALPACA_SHADOW``. With the flag unset this
    module is never invoked (the dispatch path is a bit-for-bit no-op).
  * READ vs WRITE: shadow DOES submit a real Alpaca paper order (that's how it
    gets a real fill to compare). The OFFLINE comparison harness
    (ops/scripts/quant-alpaca-shadow-compare.py) is read-only and only reads the
    log this hook writes.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

QUANT_HOME = Path.home() / ".hermes" / "quant"
SHADOW_DIVERGENCE_PATH = QUANT_HOME / "alpaca-shadow-divergence.jsonl"

_SHADOW_FLAG = "HERMES_QUANT_ALPACA_SHADOW"


def shadow_enabled() -> bool:
    """True iff the shadow flag is set. Default-OFF."""
    return os.environ.get(_SHADOW_FLAG, "0") == "1"


def run_shadow(
    proposal: Any,
    synthetic_record: Any,
    *,
    fill_size_pct: float,
    divergence_path: Path | None = None,
    reactor: Any | None = None,
) -> dict[str, Any] | None:
    """Submit to Alpaca paper IN SHADOW and log divergence vs the synthetic fill.

    MUST NEVER raise — every failure path is swallowed and logged so the
    synthetic fill (already returned to the caller) is never disturbed.

    Args:
        proposal: the equity Proposal that was just filled synthetically.
        synthetic_record: the ExecutionRecord PaperReactor returned (the REAL fill).
        fill_size_pct: the signed NAV fraction that was requested.
        divergence_path: override for the divergence log (tests).
        reactor: an injectable AlpacaPaperReactor (tests pass a fake-client one);
            built lazily when None.

    Returns:
        The divergence dict that was logged, or None if shadow was skipped /
        failed (so callers/tests can assert without depending on the file).
    """
    if not shadow_enabled():
        return None
    try:
        return _run_shadow_unsafe(
            proposal,
            synthetic_record,
            fill_size_pct=fill_size_pct,
            divergence_path=divergence_path,
            reactor=reactor,
        )
    except Exception as exc:  # noqa: BLE001 — shadow is strictly non-blocking
        logger.warning("alpaca-shadow: divergence run failed (non-blocking): %s", exc)
        return None


def _run_shadow_unsafe(
    proposal: Any,
    synthetic_record: Any,
    *,
    fill_size_pct: float,
    divergence_path: Path | None,
    reactor: Any | None,
) -> dict[str, Any] | None:
    from .alpaca_paper import AlpacaPaperReactor

    path = divergence_path or SHADOW_DIVERGENCE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if reactor is None:
        # Lazily build a reactor that writes to a throwaway executions path so
        # the shadow fill does NOT pollute the canonical executions bus (the
        # synthetic fill already owns that). We DO still let it reconcile to the
        # alpaca-paper state partition (separate book) — that is intentional.
        reactor = AlpacaPaperReactor(
            executions_path=QUANT_HOME / "alpaca-shadow-executions.jsonl"
        )

    alpaca_record = reactor.execute(
        proposal,
        fill_size_pct=fill_size_pct,
        approver_user_id=None,
        play_tag="shadow",
    )

    divergence = _build_divergence(
        proposal,
        synthetic_record,
        alpaca_record,
        fill_size_pct=fill_size_pct,
    )
    line = json.dumps(divergence, separators=(",", ":"), sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    logger.info(
        "alpaca-shadow: %s asset=%s synth_fill=%.4f alpaca_fill=%.4f "
        "price_div=%.4f size_div=%.4f",
        proposal.proposal_id,
        proposal.symbol,
        divergence["synthetic_fill_price"],
        divergence["alpaca_fill_price"],
        divergence["fill_price_divergence"],
        divergence["fill_size_divergence"],
    )
    return divergence


def _build_divergence(
    proposal: Any,
    synthetic_record: Any,
    alpaca_record: Any,
    *,
    fill_size_pct: float,
) -> dict[str, Any]:
    syn_price = float(getattr(synthetic_record, "fill_price", 0.0) or 0.0)
    alp_price = float(getattr(alpaca_record, "fill_price", 0.0) or 0.0)
    syn_size = float(getattr(synthetic_record, "fill_size_pct", 0.0) or 0.0)
    alp_size = float(getattr(alpaca_record, "fill_size_pct", 0.0) or 0.0)
    alp_meta = getattr(alpaca_record, "reactor_metadata", None) or {}
    return {
        "asof": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposal_id": proposal.proposal_id,
        "asset": proposal.symbol,
        "asset_class": proposal.asset_class,
        "requested_fill_size_pct": fill_size_pct,
        # Synthetic (paper-default) side
        "synthetic_fill_price": syn_price,
        "synthetic_fill_size_pct": syn_size,
        # Alpaca (alpaca-paper) side — broker truth
        "alpaca_fill_price": alp_price,
        "alpaca_fill_size_pct": alp_size,
        "alpaca_order_id": alp_meta.get("alpaca_order_id"),
        "alpaca_status": alp_meta.get("alpaca_status"),
        "alpaca_filled_qty": alp_meta.get("filled_qty"),
        "alpaca_account_equity": alp_meta.get("account_equity"),
        "alpaca_unfilled_timeout": alp_meta.get("unfilled_timeout", False),
        # Divergence (Alpaca - synthetic)
        "fill_price_divergence": alp_price - syn_price,
        "fill_size_divergence": alp_size - syn_size,
    }
