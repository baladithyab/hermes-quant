"""ar57 — multi-leg per-leg over-weighting masks a family loss on the ADR-0016 rail.

Distinct from ar19 (the family-PARENT phantom skip) and ar47 (partially-filled-parent
lost-fills): this is the per-leg CHILD over-weighting / double-count of the realized-P&L
basis the kill-switch reads.

MultiLegPaperReactor._build_records writes each per-leg child with
``fill_size_pct == F`` — the WHOLE family's NAV fraction — for EVERY leg (the equity
child verbatim, every option child via ``_signed_frac = sgn*abs(F)``). There is NO
per-leg ratio / leg-count / NAV-weight division; the docstring on ``_signed_frac``
itself calls the fraction a "proxy" and declares ``reactor_metadata.quantity`` (the
signed TRUE units) authoritative. But settlement_loop._normalize_exec_record derives
``qty = abs(fill_size_pct) = F`` and never reads ``reactor_metadata.quantity``. So
join_exit_fills buckets each distinct leg symbol into its OWN round-trip with qty=F,
and compute_cumulative_realized_pnl_pct sums ``realized_return × F`` once PER LEG.

Two failures compound:
  1. Each leg is over-weighted at the WHOLE-family fraction F (Σ over a 2-leg family
     contributes F twice, i.e. 2×F of NAV, not F).
  2. Both legs carry the SAME weight F despite vastly different TRUE notionals — so a
     small option leg with a large per-price-basis realized_return (premium kept,
     +98%) at equal weight MASKS the large stock leg's loss.

Worked example (F=0.05, NAV=$100k): a covered call = long 100 sh @100 (drops to 90,
ret=-10%, true notional $10,000) + short 1 call @5.00 (buy-to-close @0.10, ret=+98%,
true notional $500). True net family P&L = -$1000 + $490 = -$510 = -0.0051 of NAV (a
LOSS). The buggy basis reports (-0.10 + 0.98)·0.05 = +0.044 — a spurious +4.4% NAV
GAIN — so a genuine realized loss biases the rail POSITIVE and the kill-switch fails
to trip = fail-OPEN on the ADR-0016 capital-preservation rail.

Fix: weight each multi-leg leg by its TRUE per-leg NAV fraction
(abs(reactor_metadata.quantity) × fill_price × contract_multiplier / NAV), so Σ over a
family's legs equals the family's true net realized NAV-fraction P&L — not F×leg_count
at equal weight. Single-leg / equity fills with no multi_leg_id stay byte-identical
(the ar25/ar34 realized_return×qty fast-path).

MultiLegPaperReactor.execute() is default-OFF behind HERMES_QUANT_MULTILEG_REACTOR=1
(react/dispatch.py MultiLegReactorDisabled), so this is latent until the multileg flag
is enabled — but the kill-switch reads the shared executions.jsonl unconditionally.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto


def _write_bus(tmp_path: Path, fills: list[dict]) -> Path:
    p = tmp_path / "executions.jsonl"
    p.write_text("\n".join(json.dumps(f) for f in fills) + "\n", encoding="utf-8")
    return p


def _child(asset, asset_class, pct, price, asof, pid, *, quantity, multi_leg_id, role):
    """A per-leg multi-leg CHILD execution record, exactly as
    MultiLegPaperReactor._build_records emits: fill_size_pct == the WHOLE family NAV
    fraction (signed per leg), with the authoritative signed TRUE units in
    reactor_metadata.quantity. No account_id => settlement assigns 'paper-default'."""
    return {
        "asset": asset,
        "asset_class": asset_class,
        "timeframe": "",
        "fill_size_pct": pct,
        "fill_price": price,
        "decision_price": price,
        "asof_execution": asof,
        "asof_decision": asof,
        "bar_ts": None,
        "proposal_id": pid,
        "signal_id": None,
        "reactor_name": "multileg-paper",
        "human_in_the_loop": True,
        "play_tag": "advisor",
        "reactor_metadata": {
            "paper": True,
            "multi_leg_id": multi_leg_id,
            "quantity": quantity,
            "role": role,
        },
        "target_position_pct": pct,
        "approver_user_id": "test",
    }


def _covered_call_family(fam_frac: float) -> list[dict]:
    """A closed covered-call family at whole-family NAV fraction ``fam_frac``:

      equity leg : long 100 sh @ 100 -> sell @ 90   (ret -10%, true notional $10,000)
      option leg : short 1 call @ 5.00 -> buy-to-close @ 0.10  (ret +98%, true $500)

    Both legs carry fill_size_pct == ±fam_frac (the family fraction), as
    _build_records writes (the WHOLE-family proxy, repeated per leg).
    """
    f = fam_frac
    return [
        # open
        _child("AAPL", "equity", +f, 100.0, "2026-06-01T15:00:00Z", "open",
               quantity=+100.0, multi_leg_id="mlg1", role="equity_leg"),
        _child("AAPL260620C00100000", "us_option", -f, 5.00, "2026-06-01T15:00:00Z", "open",
               quantity=-1.0, multi_leg_id="mlg1", role="leg"),
        # close
        _child("AAPL", "equity", -f, 90.0, "2026-06-05T15:00:00Z", "close",
               quantity=-100.0, multi_leg_id="mlg1", role="equity_leg"),
        _child("AAPL260620C00100000", "us_option", +f, 0.10, "2026-06-05T15:00:00Z", "close",
               quantity=+1.0, multi_leg_id="mlg1", role="leg"),
    ]


def test_ar57_covered_call_loss_is_not_reported_as_gain(tmp_path, monkeypatch):
    """A closed covered call that LOST money must contribute a NEGATIVE NAV-fraction to
    the kill-switch basis. The buggy equal-F per-leg weighting reports a spurious GAIN
    (+0.044) because the small offsetting option leg is over-weighted to the same F as
    the much larger stock leg, masking the real stock loss -> fail-OPEN."""
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    bus = _write_bus(tmp_path, _covered_call_family(fam_frac=0.05))

    frac = auto.compute_cumulative_realized_pnl_pct(bus)

    # True net family realized P&L: stock -$1000 + call +$490 = -$510 on $100k NAV.
    assert frac < 0.0, (
        "ar57: a covered-call family that realized a NET LOSS reported a spurious GAIN "
        f"on the kill-switch basis (got {frac:+.5f}); the per-leg children are over-"
        "weighted at the whole-family fraction F instead of their TRUE per-leg NAV "
        "fractions, so the small +98% option leg masks the large -10% stock loss = "
        "fail-OPEN on the ADR-0016 rail."
    )
    # And it must be the TRUE notional-weighted net (-$510 / $100k), not a fabricated value.
    assert frac == pytest.approx(-0.0051, abs=5e-4), (
        f"ar57: contribution {frac:+.5f} must equal the family's true notional-weighted "
        "net realized NAV-fraction (-0.0051)."
    )


def test_ar57_family_contribution_true_weighted_not_equal_frac(tmp_path, monkeypatch):
    """Sanity on the over-count axis: a covered call where BOTH legs lose by the same
    per-price-basis return must contribute the family's true net once — its magnitude is
    bounded by the per-leg NAV-fraction weights, NOT the equal whole-family-fraction
    weighting (which would inflate to fam_frac × leg_count at the wrong per-leg weight)."""
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    f = 0.05  # whole-family NAV fraction (the proxy _build_records repeats per leg)
    # Both legs DOWN 10% on their own price basis (no offsetting sign to mask).
    fam = [
        _child("AAPL", "equity", +f, 100.0, "2026-06-01T15:00:00Z", "open",
               quantity=+100.0, multi_leg_id="mlg2", role="equity_leg"),
        _child("AAPL260620C00100000", "us_option", +f, 5.00, "2026-06-01T15:00:00Z", "open",
               quantity=+1.0, multi_leg_id="mlg2", role="leg"),
        _child("AAPL", "equity", -f, 90.0, "2026-06-05T15:00:00Z", "close",
               quantity=-100.0, multi_leg_id="mlg2", role="equity_leg"),
        _child("AAPL260620C00100000", "us_option", -f, 4.50, "2026-06-05T15:00:00Z", "close",
               quantity=-1.0, multi_leg_id="mlg2", role="leg"),
    ]
    bus = _write_bus(tmp_path, fam)
    frac = auto.compute_cumulative_realized_pnl_pct(bus)
    # True: stock -10% on $10,000 = -$1000; long call -10% on $500 = -$50; net -$1050 ->
    # -0.0105 of $100k NAV. The buggy equal-F basis reads (-0.10 + -0.10)·0.05 = -0.010
    # (a close-but-WRONG equal weighting). Tolerance is tight enough to REJECT -0.010.
    assert frac == pytest.approx(-0.0105, abs=1e-4), (
        f"ar57: both-legs-down family must contribute its true notional-weighted net "
        f"(-0.0105 of NAV), not the equal-F per-leg weighting -0.010 (got {frac:+.5f})."
    )


def test_ar57_single_leg_equity_byte_identical(tmp_path, monkeypatch):
    """Non-regression: a plain single-leg equity fill (no multi_leg_id) must be
    UNCHANGED by the fix — it stays on the ar25/ar34 realized_return×qty fast-path."""
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)

    def _equity(asset, pct, price, asof, pid):
        return {
            "asset": asset, "asset_class": "equity", "timeframe": "1d",
            "fill_size_pct": pct, "fill_price": price, "decision_price": price,
            "asof_execution": asof, "asof_decision": asof, "bar_ts": asof,
            "proposal_id": pid, "signal_id": None, "reactor_name": "paper",
            "human_in_the_loop": True, "play_tag": "autonomous",
            "reactor_metadata": {"paper": True}, "target_position_pct": pct,
            "approver_user_id": "test",
        }

    bus = _write_bus(tmp_path, [
        _equity("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _equity("ASTS", -0.2, 80.0, "2026-06-02T15:00:00Z", "p2"),
    ])
    # 0.2 NAV-fraction lot, -20% return -> -0.04 of NAV (ar25 fast-path, NAV cancels).
    assert auto.compute_cumulative_realized_pnl_pct(bus) == pytest.approx(-0.04)
