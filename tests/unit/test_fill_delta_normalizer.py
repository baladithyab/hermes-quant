"""Increment-0 §0.2 (seed ra01, ADR-0091 Option E): the ONE shared fill-delta
normalizer.

This is the hard architectural gate of the rearchitecture: exactly one module
converts an absolute-target fill stream into per-fill traded deltas, keyed by
(account_id, asset_class, asset), carrying its OWN running_net, with ONE canonical
ordering. Both consumers (the state.db fold and the settlement FIFO) will call THIS
— never a parallel reimplementation — so they cannot diverge (the cr00 two-views
failure mode).

The transform, per bucket, over the asof-ordered stream:
  - absolute-target record  -> delta = target - running_net ; running_net = target
  - re-affirmation (target unchanged) -> delta 0 (a no-op in every downstream fold)
  - genuine change 5%->7%   -> delta +2%
  - flip 5%->-5%            -> delta -10% (the signed value the ADR-0011 algebra needs)
  - true-delta record (future schema) -> passed through untouched (never re-differenced)
"""

from __future__ import annotations

from hermes_quant.react.base import SCHEMA_ABSOLUTE_TARGET
from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer


def _r(asset="AAPL", acct="paper-default", ac="equity", *, fill_size_pct=None, quantity=None,
       schema_version=None, asof="2026-06-13T00:00:00Z"):
    rec: dict = {
        "account_id": acct,
        "asset_class": ac,
        "asset": asset,
        "asof_execution": asof,
        "schema_version": schema_version,
    }
    if fill_size_pct is not None:
        rec["fill_size_pct"] = fill_size_pct
    if quantity is not None:
        rec["reactor_metadata"] = {"quantity": quantity}
    return rec


def test_reaffirmation_folds_to_zero_delta():
    n = FillDeltaNormalizer()
    # 12 re-affirmations of the same 0.05 target (the AAPL-12x scenario).
    deltas = [n.delta_for(_r(fill_size_pct=0.05)) for _ in range(12)]
    assert deltas[0] == 0.05, "first fire opens the position"
    assert all(abs(d) < 1e-12 for d in deltas[1:]), (
        f"re-affirmations must fold to delta 0, got {deltas[1:]}"
    )
    assert sum(deltas) == 0.05, "net of the whole stream is the single intended 0.05"


def test_genuine_change_emits_increment():
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct=0.05)) == 0.05
    assert abs(n.delta_for(_r(fill_size_pct=0.07)) - 0.02) < 1e-12  # +2% ADD
    assert abs(n.delta_for(_r(fill_size_pct=0.07)) - 0.0) < 1e-12   # re-affirm -> 0


def test_flip_emits_signed_delta():
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct=0.05)) == 0.05
    # 5% long -> 5% short = a -10% signed delta (the value ADR-0011 FLIP needs).
    assert abs(n.delta_for(_r(fill_size_pct=-0.05)) - (-0.10)) < 1e-12


def test_buckets_are_independent_per_account_assetclass_asset():
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(asset="AAPL", fill_size_pct=0.05)) == 0.05
    assert n.delta_for(_r(asset="MSFT", fill_size_pct=0.05)) == 0.05  # different bucket
    assert n.delta_for(_r(asset="AAPL", ac="us_option", fill_size_pct=0.05)) == 0.05
    # re-affirm AAPL/equity -> 0 (its own running_net is 0.05)
    assert abs(n.delta_for(_r(asset="AAPL", fill_size_pct=0.05))) < 1e-12


def test_quantity_lane_carries_forward_in_shares():
    # The det-equity path tracks the absolute target in reactor_metadata.quantity
    # (true shares). The normalizer must carry-forward in THAT field's own unit.
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(asset="AAPL", quantity=33.33)) == 33.33
    assert abs(n.delta_for(_r(asset="AAPL", quantity=33.33))) < 1e-12  # re-affirm -> 0 shares
    assert abs(n.delta_for(_r(asset="AAPL", quantity=50.0)) - 16.67) < 1e-9  # ADD 16.67 sh


def test_true_delta_record_passed_through_untouched():
    n = FillDeltaNormalizer()
    # A future true-delta-schema record is already a delta — do NOT re-difference it.
    d = n.delta_for(_r(fill_size_pct=0.05, schema_version="true-delta-v1"))
    assert d == 0.05
    d2 = n.delta_for(_r(fill_size_pct=0.05, schema_version="true-delta-v1"))
    assert d2 == 0.05, "true-delta records pass through every time (additive, not carry-forward)"


def test_absolute_target_schema_version_explicit_same_as_none():
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct=0.05, schema_version=SCHEMA_ABSOLUTE_TARGET)) == 0.05
    assert abs(n.delta_for(_r(fill_size_pct=0.05, schema_version=SCHEMA_ABSOLUTE_TARGET))) < 1e-12
