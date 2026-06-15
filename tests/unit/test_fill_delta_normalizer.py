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

import math

import pytest

from hermes_quant.react.base import SCHEMA_ABSOLUTE_TARGET
from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer, delta_from_net


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


def _meta_acct_rec(asset="AAPL", meta_acct="alpaca-paper", ac="equity", *, fill_size_pct):
    """A PERSISTED-shape record: account_id lives ONLY in reactor_metadata, never
    top-level — exactly what react/paper.py:_record_to_dict + alpaca_paper.py:413
    serialize to executions.jsonl. The top-level account_id is injected onto the
    in-memory dict at runtime (alpaca_paper.py:432) but is NOT written to the log, so a
    full rebuild reads this shape."""
    return {
        "asset_class": ac,
        "asset": asset,
        "asof_execution": "2026-06-13T00:00:00Z",
        "schema_version": None,
        "fill_size_pct": fill_size_pct,
        "reactor_metadata": {"account_id": meta_acct},
    }


def test_bucket_resolves_account_from_reactor_metadata_cs64():
    """cs64: the running-net bucket must resolve the account the SAME way the booking
    fold does (cs52 _resolve_account: top-level account_id, else reactor_metadata
    .account_id, else paper-default). The persisted log carries an alpaca-paper fill's
    account ONLY in reactor_metadata, so a bare top-level read would collapse it onto
    paper-default and re-pool the carry-forward net.

    RED before the fix: _bucket returned 'paper-default' for an alpaca-paper-in-metadata
    record, so a paper-default + alpaca-paper book sharing a symbol re-differenced the
    alpaca-paper target against paper-default's net (rebuild fold) — diverging from the
    per-account incremental fold.
    """
    from hermes_quant.state.fill_delta_normalizer import _bucket, _resolve_account
    from hermes_quant.state.portfolio_state import _resolve_account as ps_resolve_account

    rec = _meta_acct_rec(meta_acct="alpaca-paper", fill_size_pct=0.05)
    assert _bucket(rec) == ("alpaca-paper", "equity", "AAPL")
    # The normalizer's account resolution must be byte-identical to the cs52 booking
    # fold's resolution — that identity is the whole no-divergence guarantee.
    assert _resolve_account(rec) == ps_resolve_account(rec) == "alpaca-paper"


def test_metadata_only_accounts_are_independent_buckets_cs64():
    """cs64: two accounts (paper-default + alpaca-paper) sharing ONE symbol, each
    carrying its account only in reactor_metadata, must NOT share a running-net bucket.
    A re-affirm on alpaca-paper folds to delta 0 against its OWN 0.05 net, not against a
    re-pooled paper-default + alpaca-paper net."""
    n = FillDeltaNormalizer()
    # paper-default opens AAPL 0.05
    assert n.delta_for(_meta_acct_rec(meta_acct="paper-default", fill_size_pct=0.05)) == 0.05
    # alpaca-paper opens the SAME symbol — its own bucket starts at net 0, so delta 0.05
    # (NOT 0.05 - 0.05 == 0, which is what the collapsed bucket produced pre-fix).
    assert n.delta_for(_meta_acct_rec(meta_acct="alpaca-paper", fill_size_pct=0.05)) == 0.05
    # alpaca-paper re-affirms -> delta 0 against its own 0.05 net.
    assert abs(n.delta_for(_meta_acct_rec(meta_acct="alpaca-paper", fill_size_pct=0.05))) < 1e-12


def test_top_level_account_id_resolves_identically_byte_identical_cs64():
    """cs64 safety: a truthy top-level account_id resolves exactly as the old
    .get('account_id','paper-default') did, so a single-account / live-injected log is
    byte-identical (the resolution only CHANGES behavior for the reactor_metadata-only
    persisted shape)."""
    from hermes_quant.state.fill_delta_normalizer import _bucket

    rec = _r(asset="AAPL", acct="paper-default", fill_size_pct=0.05)
    assert _bucket(rec) == ("paper-default", "equity", "AAPL")
    # Top-level wins over a conflicting reactor_metadata account (runtime-injected shape).
    rec2 = dict(rec)
    rec2["reactor_metadata"] = {"account_id": "alpaca-paper"}
    assert _bucket(rec2) == ("paper-default", "equity", "AAPL")


# ── cs84: guard the raw-dict float() coercion so a poisoned size never folds into the
# gate-sized state.db NAV (fl1/cs82 guard Fill.__post_init__, but the production fold
# reads RAW dicts at portfolio_state.py:662/979 and never constructs a Fill). ──────────


def test_cs84_nan_fill_size_pct_does_not_poison_running_net_forever():
    """RED before the fix: delta_for({'fill_size_pct': NaN}) returned NaN AND poisoned
    the bucket's running_net so the NEXT valid 0.05 fill ALSO returned NaN forever (the
    exact carry-forward poisoning fl1 cited). GREEN: the NaN record abstains (delta 0.0,
    running_net NOT advanced) and the next valid fill is unaffected."""
    n = FillDeltaNormalizer()
    d_nan = n.delta_for(_r(fill_size_pct=float("nan")))
    assert d_nan == 0.0 and not math.isnan(d_nan), "poisoned NaN record must abstain to 0"
    # The NEXT valid fill in the SAME bucket must be a clean open (0.05), NOT NaN —
    # i.e. running_net was never poisoned by the abstained record.
    d_next = n.delta_for(_r(fill_size_pct=0.05))
    assert d_next == 0.05, f"running_net poisoned: next valid fill returned {d_next!r}"


def test_cs84_inf_fill_size_pct_abstains():
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct=float("inf"))) == 0.0
    assert n.delta_for(_r(fill_size_pct=0.05)) == 0.05  # bucket recovered


def test_cs84_bool_fill_size_pct_is_not_a_100pct_nav_target():
    """RED before the fix: delta_for({'fill_size_pct': True}) == 1.0 — a bool read as a
    100% NAV target (bool subclasses int, so True slips past isfinite). GREEN: abstain."""
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct=True)) == 0.0
    assert n.delta_for(_r(fill_size_pct=False)) == 0.0
    # And the bucket is unpoisoned: a real subsequent fill opens cleanly.
    assert n.delta_for(_r(fill_size_pct=0.05)) == 0.05


def test_cs84_bool_quantity_is_not_a_1_contract_target():
    """RED before the fix: reactor_metadata.quantity=True yielded a 1.0-contract delta
    (cs82 numeric verdict). GREEN: the quantity lane abstains on the bool too."""
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(asset="AAPL", quantity=True)) == 0.0
    assert n.delta_for(_r(asset="AAPL", quantity=50.0)) == 50.0  # bucket clean afterwards


def test_cs84_string_fill_size_pct_not_silently_coerced():
    """RED before the fix: delta_for({'fill_size_pct': '0.05'}) silently coerced to 0.05.
    GREEN: a non-numeric raw value abstains (no silent string->float into the NAV)."""
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct="0.05")) == 0.0
    assert n.delta_for(_r(fill_size_pct=0.05)) == 0.05  # real value still folds


def test_cs84_incremental_delta_from_net_fails_closed_on_poison():
    """The incremental fold (portfolio_state.py:979) calls the module-level delta_from_net
    on a RAW dict. A poisoned size there must fail CLOSED (raise) — NOT silently fold NaN
    or a bool-1.0 into the persisted position. The rebuild loop's delta_for abstains; the
    incremental caller surfaces the raise (it is not a sustained in-memory stream)."""
    from hermes_quant.state.fill_delta_normalizer import _PoisonedSizeError, delta_from_net

    for bad in (float("nan"), float("inf"), True, "0.05"):
        with pytest.raises(_PoisonedSizeError):
            delta_from_net(_r(fill_size_pct=bad), 0.0)


def test_cs84_valid_numeric_stream_is_byte_identical():
    """The ONLY valid production case (real numeric absolute targets) must be untouched by
    the guard — re-affirm folds to 0, genuine change to the increment, flip to the signed
    delta. Byte-identical to the pre-guard behavior."""
    n = FillDeltaNormalizer()
    assert n.delta_for(_r(fill_size_pct=0.05)) == 0.05
    assert abs(n.delta_for(_r(fill_size_pct=0.05))) < 1e-12   # re-affirm -> 0
    assert abs(n.delta_for(_r(fill_size_pct=0.07)) - 0.02) < 1e-12  # +2% ADD
    assert abs(n.delta_for(_r(fill_size_pct=-0.05)) - (-0.12)) < 1e-12  # flip 7%->-5% = -12%
    # quantity lane likewise unaffected
    m = FillDeltaNormalizer()
    assert m.delta_for(_r(asset="MSFT", quantity=33.33)) == 33.33
    assert abs(m.delta_for(_r(asset="MSFT", quantity=33.33))) < 1e-12


# ── cs85: the two folds must share ONE running-net per bucket. The rebuild kept TWO
# parallel maps (_net_pct + _net_qty) selected per-record by lane, so a bucket that
# mixed a qty-lane fill and a later pct-lane fill differenced the pct fill against a
# STALE _net_pct=0 (the qty fill never advanced it) — diverging from the incremental
# fold, whose single persisted positions.quantity column (portfolio_state.py:960) is
# advanced by EVERY delta regardless of lane. Collapse the two maps into one net so
# both folds carry the same base. (Fold-CONSISTENCY only; the qty/pct unit-mixing in
# that single column is the pre-existing cr00 unit-unification dependency, out of scope.)
# ────────────────────────────────────────────────────────────────────────────────────


def test_cs85_mixed_lane_bucket_folds_to_one_net():
    """RED before the fix: one bucket (paper-default, equity, AAPL) receives a qty-lane
    absolute target (quantity=10.0) then a pct-lane absolute target (fill_size_pct=0.05).
    The rebuild's two-map normalizer differences the pct fill against _net_pct=0 -> 0.05,
    but the incremental fold's single column (already advanced to 10.0 by the qty fill)
    differences it against 10.0 -> -9.95. GREEN: the one-net collapse makes the rebuild
    delta_for stream EQUAL the incremental single-column delta_from_net stream.
    """
    fill_a = _r(asset="AAPL", quantity=10.0)        # qty-lane absolute target = 10 shares
    fill_b = _r(asset="AAPL", fill_size_pct=0.05)   # pct-lane absolute target = 0.05 NAV-frac

    # Rebuild fold (the in-memory running-net normalizer).
    n = FillDeltaNormalizer()
    reb = [n.delta_for(fill_a), n.delta_for(fill_b)]

    # Incremental fold (single persisted column): old_qty starts 0 and advances by each
    # fold delta, exactly as apply_execution's new_qty = old_qty + pos_delta does
    # (portfolio_state.py:960/979/985). delta_from_net is the SAME shared derivation.
    net = 0.0
    inc = []
    for rec in (fill_a, fill_b):
        d = delta_from_net(rec, net)
        inc.append(d)
        net += d

    # The gate: the rebuild delta stream equals the incremental single-column stream.
    assert reb == pytest.approx(inc), (
        f"cs85 fold divergence: rebuild {reb} != incremental {inc}"
    )
    # And the rebuild's accumulated net (sum of its deltas, = the single positions
    # column the rebuild writes) equals the incremental's accumulated net.
    assert sum(reb) == pytest.approx(net)


def test_cs85_single_lane_pct_byte_identical():
    """The pure-pct production stream (autonomous-tick PaperReactor) must be BYTE-
    IDENTICAL to the captured two-map baseline [0.05, 0.0, 0.03]: open 0.05, re-affirm
    0.05 (-> 0), change to 0.08 (-> +0.03)."""
    n = FillDeltaNormalizer()
    deltas = [n.delta_for(_r(asset="AAPL", fill_size_pct=p)) for p in (0.05, 0.05, 0.08)]
    assert deltas[0] == pytest.approx(0.05)
    assert deltas[1] == pytest.approx(0.0)
    assert deltas[2] == pytest.approx(0.03)


def test_cs85_single_lane_qty_byte_identical():
    """The pure-qty production stream (det-equity true-shares) must be BYTE-IDENTICAL
    to the captured two-map baseline [33.33, 0.0, 16.67]."""
    n = FillDeltaNormalizer()
    deltas = [n.delta_for(_r(asset="AAPL", quantity=q)) for q in (33.33, 33.33, 50.0)]
    assert deltas[0] == pytest.approx(33.33)
    assert deltas[1] == pytest.approx(0.0)
    assert deltas[2] == pytest.approx(16.67)
