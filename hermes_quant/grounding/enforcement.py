"""hermes_quant.grounding.enforcement — wire ClaimVerifier into the decision path.

Seed 24ba (REVIEW-SYNTHESIS-20260604, Tier-1 safety). ClaimVerifier
(``verifier.py``) was fully built + tested but had ZERO instantiating callers
outside ``grounding/`` — the citation HARD_RULE had no teeth. An analyst claim
with a fabricated / ungrounded numeric reached the decision ANNOTATED but not
DROPPED.

This module is the enforcement seam. ``enforce_grounding(views, ctx)`` runs at
the analyst-views → aggregator boundary (``advisor.py`` Step 5.5). For every
analyst view that OPTED INTO grounding, it verifies the view's full numeric
claim text against the ``GroundTruthBlock`` carried in
``ctx.extras['ground_truth_block']`` and DROPS the view from the vote when
verification fails.

Fail-CLOSED (money-software posture, [[hermes-quant-posture]])
--------------------------------------------------------------
A view whose numeric assertion fails grounding is REMOVED from the vote — never
trusted-but-annotated. We default toward NOT trading on ungrounded data. The
default citation threshold here is **strict (1.0)**: any single uncited number
in a grounded view drops the whole view.

ADDITIVE / byte-identical-when-clean
------------------------------------
The seam is a no-op on today's default advisor path, which does NOT populate
``ctx.extras['ground_truth_block']``:

  * No ``ground_truth_block`` in ctx.extras → identity passthrough.
  * A view that never opted into grounding (no grounding marker in its metadata)
    → never verified, never dropped. Deterministic analysts
    (ClassicalTA / Microstructure / Kronos) emit internal sub-scores in their
    rationale (e.g. ``rsi=+1@0.75``) that are NOT sourced numeric claims;
    verifying them against a price block would wrongly drop a legitimate analyst.
  * A grounded view whose every numeric claim traces to ground truth → kept
    as the SAME object (no copy), so the aggregate is byte-identical to today.

Kill-switch: ``HERMES_QUANT_GROUNDING_ENFORCE`` (read at call time, like the
other hermes-quant feature flags). Absent or ``"1"`` → enforce (DEFAULT-ON).
``"0"`` → identity passthrough.

Determinism / asof-honesty
--------------------------
Pure function of ``(views, ctx)``. No wall-clock ``now()``, no RNG. The verifier
reads the full semantic packet summary when available (falling back to
``view.rationale``) and the already as-of-sliced block, so the seam is
replay-stable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from hermes_quant.grounding.verifier import ClaimVerifier
from hermes_quant.protocol import AnalystView, MarketContext

logger = logging.getLogger(__name__)

# Feature flag (read at call time, never cached at import — mirrors advisor.py's
# HERMES_QUANT_EVENT_RISK / semantic.py's HERMES_QUANT_SATURATION pattern).
# Absent or "1" => enforce (DEFAULT-ON). "0" => identity passthrough.
_ENFORCE_FLAG = "HERMES_QUANT_GROUNDING_ENFORCE"

# Strict by default: any uncited numeric in a grounded view drops the view.
# Fail-CLOSED toward NOT trading on ungrounded data.
_DEFAULT_THRESHOLD = 1.0

# Metadata keys that mark a view as having OPTED INTO grounding. HermesSemanticAnalyst
# sets `with_grounding` (True on success, False on grounding-error) plus
# `ground_truth_symbol` / `citation_ids` when a block was injected. The presence of
# ANY of these is the participation signal — `with_grounding=False` still counts
# (a view that wanted grounding but errored must not get a free pass to vote with
# ungrounded numbers; fail-closed).
_GROUNDING_MARKERS = ("with_grounding", "ground_truth_symbol", "citation_ids")


def _opted_into_grounding(view: AnalystView) -> bool:
    """True iff *view* declared grounding participation in its metadata."""
    meta = view.metadata or {}
    return any(key in meta for key in _GROUNDING_MARKERS)


def _iter_semantic_packet_payloads(extras: dict[str, Any]) -> list[Any]:
    raw_packets = extras.get("semantic_packets")
    if raw_packets is None:
        raw_packet = extras.get("semantic_packet")
        raw_packets = [raw_packet] if raw_packet is not None else []
    if isinstance(raw_packets, dict):
        raw_packets = [raw_packets]
    if not isinstance(raw_packets, (list, tuple)):
        return []
    return list(raw_packets)


def _full_claim_text_for_view(view: AnalystView, ctx: MarketContext) -> str | None:
    """Return the untruncated semantic claim text for *view* when resolvable.

    HermesSemanticAnalyst stores the selected packet hash in metadata and leaves
    the raw packet in ``ctx.extras``. Use that packet summary for verification so
    a display-truncated rationale cannot hide a numeric claim.
    """
    meta = view.metadata or {}
    packet_hash = meta.get("packet_hash")
    if not isinstance(packet_hash, str) or not packet_hash:
        return None

    extras = dict(getattr(ctx, "extras", None) or {})
    payloads = _iter_semantic_packet_payloads(extras)
    if not payloads:
        return None

    from hermes_quant.semantic import parse_semantic_packet

    for raw in payloads:
        try:
            packet = parse_semantic_packet(raw)
        except Exception:  # noqa: BLE001
            continue
        if packet_hash in {packet.packet_hash, packet.computed_hash}:
            return packet.summary
    return None


def enforce_grounding(
    views: list[AnalystView],
    ctx: MarketContext,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
) -> tuple[list[AnalystView], list[dict[str, Any]]]:
    """Drop grounded analyst views whose numeric claims fail citation verification.

    Parameters
    ----------
    views     : the analyst views about to be aggregated.
    ctx       : the MarketContext; ``ctx.extras['ground_truth_block']`` (when
                present) is the GroundTruthBlock claims are verified against.
    threshold : citation-coverage floor passed to ClaimVerifier (default strict
                1.0 — any uncited number fails).

    Returns
    -------
    (kept_views, dropped_records)
        ``kept_views`` — the subset of *views* that survive (same objects, order
        preserved). ``dropped_records`` — one dict per dropped view with audit
        fields (analyst, citation_coverage, uncited_claims, reason) so a consumer
        can surface WHY a view didn't vote.

    Notes
    -----
    Identity passthrough (``kept == views``, ``dropped == []``) when:
      * the kill-switch is OFF (``HERMES_QUANT_GROUNDING_ENFORCE=0``), OR
      * no ``ground_truth_block`` is in ``ctx.extras`` (today's advisor default).
    """
    if os.environ.get(_ENFORCE_FLAG, "1") != "1":
        return list(views), []

    extras = getattr(ctx, "extras", None) or {}
    block = extras.get("ground_truth_block")
    if block is None:
        # Nothing to verify against — byte-identical to the no-verifier path.
        return list(views), []

    verifier = ClaimVerifier(threshold=threshold)

    kept: list[AnalystView] = []
    dropped: list[dict[str, Any]] = []
    for view in views:
        if not _opted_into_grounding(view):
            # Did not opt into grounding → not a sourced-claim view → never verified.
            kept.append(view)
            continue
        try:
            result = verifier.verify(
                view,
                block,
                claim_text=_full_claim_text_for_view(view, ctx),
            )
        except Exception as exc:  # noqa: BLE001
            # A verifier failure must FAIL CLOSED: a grounded view we cannot
            # verify is dropped, not trusted. (The verifier is pure and tested;
            # this guards a malformed-block edge, never the happy path.)
            logger.warning(
                "enforce_grounding: verifier raised for analyst %r (%s); "
                "dropping view fail-closed",
                view.analyst,
                exc,
            )
            dropped.append(
                {
                    "analyst": view.analyst,
                    "citation_coverage": 0.0,
                    "uncited_claims": [],
                    "reason": f"verifier_error: {exc}",
                }
            )
            continue
        if result.accepted:
            kept.append(view)
        else:
            dropped.append(
                {
                    "analyst": view.analyst,
                    "citation_coverage": result.citation_coverage,
                    "uncited_claims": list(result.uncited_claims),
                    "reason": result.reason,
                }
            )
            logger.info(
                "enforce_grounding: DROPPED analyst %r view from vote — %s",
                view.analyst,
                result.reason,
            )

    return kept, dropped
