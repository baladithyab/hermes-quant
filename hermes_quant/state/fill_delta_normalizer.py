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
        if qty is not None:
            lane, net_map, absolute = qty, self._net_qty, qty
        else:
            absolute = float(rec.get("fill_size_pct", 0.0))
            lane, net_map = absolute, self._net_pct

        if not is_absolute_target_record(rec):
            # Already a traded delta — pass through untouched, do not re-difference.
            # (Do not advance running_net: the producer is reporting increments, so
            # a carry-forward derived from a partial stream would be wrong.)
            return lane

        running = net_map.get(key, 0.0)
        delta = absolute - running
        net_map[key] = absolute
        return delta
