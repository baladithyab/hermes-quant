"""The ONE shared fill-delta normalizer (ADR-0091 Option E, Increment-0 §0.2).

Problem (ADR-0091): both reactors write the ABSOLUTE post-fill target into the
per-fill size field (``fill_size_pct``, and ``reactor_metadata.quantity`` on the
det-equity path), but every downstream fold treats that field as an *incremental
traded delta* (``new_qty = old_qty + delta``). So re-affirming an unchanged target N
times inflates the derived position to N×target (the AAPL 5%×12 → 60% / BA −0.2×6 →
−0.8 incident).

Option E fixes this at FOLD time, not at the producer (a producer that read derived
state to emit a delta would be a read-modify-write race into the immutable log — the
rejected Option B). The producers stay unchanged; this module derives the traded
delta from the absolute target by carrying a running net per bucket:

    delta = current_target − running_net ;   running_net ← current_target

So a re-affirmation yields delta 0 (a no-op in every downstream fold), a genuine
change yields the true increment, and a flip yields the signed delta the ADR-0011
OPEN/ADD/REDUCE/FLIP algebra needs.

**This is the hard architectural gate.** It must be the SINGLE place the carry-forward
is computed, with ONE canonical per-bucket ordering, and it owns its OWN ``running_net``
state — it does NOT reuse state.db's running-net nor the settlement FIFO's lot list.
Both consumers (the state.db rebuild/incremental fold and the settlement FIFO pre-pass)
call THIS, so they cannot diverge into the two-views failure mode.

Scope note (cr00): the carry-forward is computed in the *unit of whichever size field
the record uses* — NAV-fraction for the ``fill_size_pct`` lane, true shares for the
``reactor_metadata.quantity`` lane. A single ``(account, asset_class, asset)`` bucket
that receives BOTH unit regimes (e.g. det-equity true-shares and paper NAV-fraction on
the same symbol/account) cannot be reconciled here without a read-time mark-injection
seam (qty×mark/equity); that unit-unification is the cr00 follow-up and is out of this
module's scope. Within a single unit regime the carry-forward is exact.
"""

from __future__ import annotations

from typing import Any

from hermes_quant.react.base import is_absolute_target_record

# Bucket key: the same grain the state.db fold and the settlement FIFO use.
_BucketKey = tuple[str, str, str]


def _bucket(rec: dict[str, Any]) -> _BucketKey:
    return (
        rec.get("account_id", "paper-default"),
        rec.get("asset_class", "equity"),
        rec.get("asset", ""),
    )


def _absolute_size_of(rec: dict[str, Any]) -> float:
    """The record's per-fill size in its own lane unit: shares if the
    reactor_metadata.quantity lane is present, else the fill_size_pct
    NAV-fraction. Mirrors the fold's ``pos_delta = leg_quantity if not None
    else fill_size_pct`` lane selection."""
    rmeta = rec.get("reactor_metadata") or {}
    if isinstance(rmeta, dict):
        q = rmeta.get("quantity")
        if q is not None:
            return float(q)
    return float(rec.get("fill_size_pct", 0.0))


def delta_from_net(rec: dict[str, Any], current_net: float) -> float:
    """THE single carry-forward derivation, shared by both folds (the gate).

    Given a fill record and the bucket's CURRENT net position (in the record's
    own lane unit), return the traded delta to apply:
      - absolute-target record  -> target - current_net (re-affirmation -> 0)
      - true-delta-schema record -> the size field unchanged (already a delta;
        current_net is ignored, no re-difference).

    The rebuild fold (FillDeltaNormalizer, which carries an in-memory running net
    per bucket) and the incremental fold (apply_execution, which reads current_net
    from the persisted positions row) BOTH call this — so they cannot diverge on
    how the delta is computed. That single-derivation property is the Option-E
    no-two-views guarantee.
    """
    absolute = _absolute_size_of(rec)
    if not is_absolute_target_record(rec):
        return absolute  # already a delta — pass through, net irrelevant
    return absolute - current_net


class FillDeltaNormalizer:
    """Stream-ordered absolute-target → traded-delta transform.

    Construct once per fold pass, then call :meth:`delta_for` on each record in
    the fold's canonical order. The instance carries the per-bucket running net;
    do not share one instance across two independently-ordered passes.

    Two parallel running-net maps are kept because the two size lanes are in
    different units and must never be mixed in one accumulator:
      - ``_net_pct``  for the ``fill_size_pct`` (NAV-fraction) lane;
      - ``_net_qty``  for the ``reactor_metadata.quantity`` (true-shares) lane.
    A record uses exactly one lane (quantity if present, else fill_size_pct),
    matching the existing fold's ``pos_delta = leg_quantity if not None else
    fill_size_pct`` selection.
    """

    def __init__(self) -> None:
        self._net_pct: dict[_BucketKey, float] = {}
        self._net_qty: dict[_BucketKey, float] = {}

    @staticmethod
    def _quantity_of(rec: dict[str, Any]) -> float | None:
        rmeta = rec.get("reactor_metadata") or {}
        if isinstance(rmeta, dict):
            q = rmeta.get("quantity")
            if q is not None:
                return float(q)
        return None

    def delta_for(self, rec: dict[str, Any]) -> float:
        """Return the traded delta to fold for this record, in the record's own
        size unit (shares if it uses the quantity lane, else NAV-fraction).

        For an absolute-target record this is ``target − running_net`` and the
        running net advances to the target. For a true-delta-schema record the
        size field is already a delta, so it is returned unchanged (and does NOT
        advance the carry-forward, since the producer is reporting increments).
        """
        key = _bucket(rec)
        qty = self._quantity_of(rec)
        # Lane selection mirrors the existing fold: quantity wins when present.
        net_map = self._net_qty if qty is not None else self._net_pct
        absolute = qty if qty is not None else float(rec.get("fill_size_pct", 0.0))

        if not is_absolute_target_record(rec):
            # Already a traded delta — pass through untouched, do not re-difference.
            # (Do not advance running_net: the producer is reporting increments, so
            # a carry-forward derived from a partial stream would be wrong.)
            return delta_from_net(rec, net_map.get(key, 0.0))

        # Absolute-target: derive via the ONE shared derivation, then advance the
        # in-memory running net to the target for the next record in this bucket.
        running = net_map.get(key, 0.0)
        delta = delta_from_net(rec, running)
        net_map[key] = absolute
        return delta
