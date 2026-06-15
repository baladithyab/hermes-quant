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

import logging
import math
from numbers import Real
from typing import Any

from hermes_quant.react.base import is_absolute_target_record

logger = logging.getLogger(__name__)

# Bucket key: the same grain the state.db fold and the settlement FIFO use.
_BucketKey = tuple[str, str, str]


class _PoisonedSizeError(ValueError):
    """A raw size field is non-finite / bool / non-numeric and would poison the
    carry-forward running_net. Raised by the dict-boundary coercion so the fold
    fails CLOSED on that one record (silence-by-default: abstain, do NOT fold a
    poisoned value into running_net)."""


def _coerce_size(value: Any, field: str) -> float:
    """cs84: the SINGLE guarded float() coercion at the raw-dict boundary, mirroring
    the fl1/cs82 ``Fill.__post_init__`` guard (pdr_core.contracts) — but applied HERE,
    where the production fold actually reads values. fl1/cs82 protect in-memory ``Fill``
    construction via ``__post_init__``; the normalizer's production fold
    (portfolio_state.py:662 rebuild + :979 incremental, under HERMES_QUANT_DELTA_NORMALIZER)
    consumes RAW executions.jsonl dicts and NEVER constructs a ``Fill``, so that guard
    never runs on this path. Without this, a NaN ``fill_size_pct`` poisons ``running_net``
    so every later valid fill in that bucket also returns NaN (the exact poisoning fl1
    cited), a ``bool`` target reads as a 100% NAV target (``True`` -> 1.0), and a string
    silently coerces — all into the gate-sized state.db NAV.

    Guard order matches cs82: reject ``bool`` FIRST (in Python ``bool`` subclasses
    ``int``, so ``True``/``False`` are finite and slip through ``isfinite``), then reject
    non-``Real`` (string/None/list) BEFORE float() can coerce them, then reject non-finite
    (NaN/inf). Any failure raises ``_PoisonedSizeError`` so the caller abstains on that
    record rather than folding the poison forward.
    """
    if isinstance(value, bool):
        raise _PoisonedSizeError(
            f"{field}={value!r} is a bool, not a real number (a bool slips through "
            "the isfinite checks and reads as a 100% NAV target -> poisons running_net)."
        )
    if not isinstance(value, Real):
        raise _PoisonedSizeError(
            f"{field}={value!r} is non-numeric ({type(value).__name__}); refusing to "
            "float()-coerce a raw size into the carry-forward running_net."
        )
    out = float(value)
    if not math.isfinite(out):
        raise _PoisonedSizeError(
            f"{field}={value!r} is non-finite (NaN/inf); a poisoned target would make "
            "every later fill in this bucket return NaN -> corrupts the state.db NAV."
        )
    return out


def _resolve_account(rec: dict[str, Any]) -> str:
    """Resolve the partition account for one record (cs64) — IDENTICAL to the cs52
    ``portfolio_state._resolve_account`` (and the cs24 daemon loader): top-level
    ``account_id`` if truthy, else ``reactor_metadata.account_id`` if truthy, else the
    ``"paper-default"`` sentinel.

    Inlined here (not imported from portfolio_state) deliberately: this module is the
    SINGLE shared normalizer and must not couple its import to the heavy portfolio_state
    module. Keep this body byte-for-byte in step with portfolio_state._resolve_account.

    Why it matters (cs64): the running-net carry-forward MUST partition on the same grain
    the BOOKING fold uses, or the two folds diverge. The persisted log serializes
    ``account_id`` ONLY inside ``reactor_metadata`` (react/paper.py:_record_to_dict,
    alpaca_paper.py:413) — the top-level ``account_id`` is injected at runtime onto the
    in-memory dict handed to apply_execution (react/paper.py:530, alpaca_paper.py:432)
    but is NOT written to the log. So on a full rebuild an alpaca-paper fill carries its
    account ONLY in reactor_metadata. Reading just the bare top-level field collapses every
    reactor_metadata-only account onto ``paper-default`` and re-pools the running net, while
    the incremental fold (apply_execution -> ``acct = portfolio_state._resolve_account`` ->
    reads ``old_qty WHERE account_id=acct``) keeps the per-account net. The carry-forward
    delta ``target - net`` then differs between folds whenever >1 named account shares a
    symbol — corrupting the gate-sized NAV. A truthy top-level account_id resolves
    identically to the old ``.get(..., "paper-default")``, so a single-account log is
    byte-identical (and the normalizer is itself default-OFF behind
    HERMES_QUANT_DELTA_NORMALIZER, so _bucket is unreachable when the flag is off).
    """
    acct = rec.get("account_id")
    if acct:
        return str(acct)
    meta_acct = (rec.get("reactor_metadata") or {}).get("account_id")
    if meta_acct:
        return str(meta_acct)
    return "paper-default"


def _bucket(rec: dict[str, Any]) -> _BucketKey:
    return (
        _resolve_account(rec),
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
            return _coerce_size(q, "reactor_metadata.quantity")
    return _coerce_size(rec.get("fill_size_pct", 0.0), "fill_size_pct")


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
                return _coerce_size(q, "reactor_metadata.quantity")
        return None

    def delta_for(self, rec: dict[str, Any]) -> float:
        """Return the traded delta to fold for this record, in the record's own
        size unit (shares if it uses the quantity lane, else NAV-fraction).

        For an absolute-target record this is ``target − running_net`` and the
        running net advances to the target. For a true-delta-schema record the
        size field is already a delta, so it is returned unchanged AND the running
        net advances BY that delta (``running_net += delta``) — mirroring the
        incremental fold, whose persisted qty advances ``old_qty + delta``. Both
        folds therefore carry the same base into the next absolute-target
        difference (the ms1 mixed-schema parity fix).
        """
        key = _bucket(rec)
        try:
            qty = self._quantity_of(rec)
            # Lane selection mirrors the existing fold: quantity wins when present.
            net_map = self._net_qty if qty is not None else self._net_pct
            absolute = (
                qty if qty is not None
                else _coerce_size(rec.get("fill_size_pct", 0.0), "fill_size_pct")
            )
        except _PoisonedSizeError as exc:
            # cs84: ABSTAIN on a poisoned raw size (NaN/inf/bool/non-numeric) — skip this
            # ONE record, return a 0.0 delta (a no-op in every downstream fold), and do
            # NOT advance running_net (so the next VALID fill in this bucket is unaffected;
            # without this a single NaN poisoned the carry-forward forever). Silence-by-
            # default for the valid stream; fail-CLOSED + loud on the bad record so it
            # never folds into the gate-sized state.db NAV.
            logger.warning(
                "FillDeltaNormalizer: abstaining on poisoned record in bucket %s: %s",
                key,
                exc,
            )
            return 0.0

        if not is_absolute_target_record(rec):
            # Already a traded delta — pass it through untouched (do NOT re-difference
            # it against the running net). But it MUST still advance running_net by that
            # delta, because the incremental fold's persisted qty does exactly that:
            # apply_execution computes new_qty = old_qty + delta (portfolio_state.py:984
            # via _update_position) and writes it back, so the next absolute-target
            # record there differences against (old_qty + delta). If the rebuild's
            # in-memory net did NOT advance here, a bucket MIXING a true-delta fill and a
            # later absolute-target fill would seed the carry-forward base from different
            # values in the two folds (rebuild net stale, incremental qty advanced) and
            # the next `target - net` derivation would diverge — the ms1 fold-divergence.
            # Advancing by the delta keeps the carry-forward base byte-identical to the
            # persisted qty in both folds. (running_net += delta is the in-memory mirror
            # of new_qty = old_qty + delta.) DORMANT today: no producer emits a non-
            # absolute-target schema_version, so net_map only ever sees absolute targets
            # and this branch is unreachable in production — an all-absolute-target bucket
            # is byte-identical because it never enters here.
            delta = delta_from_net(rec, net_map.get(key, 0.0))
            net_map[key] = net_map.get(key, 0.0) + delta
            return delta

        # Absolute-target: derive via the ONE shared derivation, then advance the
        # in-memory running net to the target for the next record in this bucket.
        running = net_map.get(key, 0.0)
        delta = delta_from_net(rec, running)
        net_map[key] = absolute
        return delta
