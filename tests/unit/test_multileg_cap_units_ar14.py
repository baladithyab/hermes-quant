"""ar14 — MultiLegPaperReactor gross-cap must normalize TRUE-UNIT Position.quantity
to a NAV-fraction (sibling of ar13 in PaperReactor).

The convergence review (wf wff4dts6m) flagged multileg.py:461-494: the cap builds
`pos_map[f'{ac}\\x1f{sym}'] = position.quantity` then feeds RiskPortfolioState, which
sums abs(.) for gross. But Position.quantity is UNIT-AMBIGUOUS — the ADR-0086/0088
true-unit path (reactor_metadata.quantity present) stores SIGNED SHARES/CONTRACTS.
A 100-share line read as a NAV-fraction = 10000% of NAV, so g_room collapses to <=0
and EVERY legitimate subsequent multi-leg family is silenced (fail-CLOSED over-count).

(The ar13 lane initially declared this seam "stale"; it is NOT — multileg.py:466
reads Position.quantity directly, exactly like the PaperReactor path ar13 fixed.)

RED  (pre-fix): a true-unit 100-share equity line (worth ~15% of a $100k NAV) makes
                the multileg cap read 10000% gross -> an in-cap CSP family is silenced.
GREEN (post-fix): position_gross_fraction normalizes the line to ~0.15, gross is ~15%,
                and the in-cap family fills.
Byte-identical: a pure NAV-fraction book (DummyPos / no unit_kind) is unchanged, and
                with the flag OFF the cap path is never consulted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import NetGreeks, OptionGreeksSnapshot, OptionLeg
from hermes_quant.options.multileg import MultiLegProposal
from hermes_quant.react.multileg import MultiLegPaperReactor
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket
from hermes_quant.state.portfolio_state import PortfolioState


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)


def _true_unit_book(tmp_path, monkeypatch, *, shares: float, price: float):
    """A DB-backed PortfolioState whose paper-default book holds ONE true-unit
    equity line (reactor_metadata.quantity = signed shares). With the ar13/ar14
    unit_kind marker, get_positions reports unit_kind='true_unit' so the cap must
    value it as shares*price/NAV (NOT read the raw share count as a NAV-fraction).
    """
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)
    ps.apply_execution(
        {
            "proposal_id": "seed_true_unit_aapl",
            "asset": "AAPL",
            "asset_class": "equity",
            "fill_size_pct": 0.0,  # ignored on the true-unit path
            "fill_price": price,
            "asof_execution": "2026-05-30T17:00:00+00:00",
            "reactor_metadata": {"quantity": shares, "role": "leg"},
        }
    )
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    return ps


def _snap(**kw) -> OptionGreeksSnapshot:
    base = dict(delta=0.25, gamma=0.01, theta=-0.02, vega=0.10, rho=0.01, iv=0.4)
    base.update(kw)
    return OptionGreeksSnapshot(**base)


def _admitted_gate(*, bucket, net_greeks, bpr_estimate=0.0) -> OptionsGateResult:
    return OptionsGateResult(
        admitted=True,
        bucket=bucket,
        reason=None,
        net_greeks=net_greeks,
        bpr_estimate=bpr_estimate,
        max_loss=None,
        contracts=1,
        warnings=(),
    )


def _csp(*, outer_qty: int = 1) -> MultiLegProposal:
    put = OptionLeg(
        symbol="NVDA260626P00130000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=-0.25),
        fill_price=3.10,
    )
    return MultiLegProposal.from_gate_result(
        gate_result=_admitted_gate(
            bucket=StructureBucket.CASH_SECURED_PUT,
            net_greeks=NetGreeks(delta=25.0),
            bpr_estimate=13000.0,
        ),
        proposal_id=f"prop_20260530T180000_NVDA_ar14csp{outer_qty}",
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="cash_secured_put",
        underlying="NVDA",
        option_legs=(put,),
        stock_leg=None,
        outer_qty=outer_qty,
        net_debit_credit=Decimal("-3.10"),
        max_gain=Decimal("310"),
        breakeven_underlying=(Decimal("126.90"),),
        rationale="csp",
        source_recipe_id="r_csp",
    )


def _read_family(bus):
    if not bus.exists():
        return []
    return [json.loads(ln) for ln in bus.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# RED -> GREEN: a true-unit book must NOT inflate gross and silence an in-cap family
# --------------------------------------------------------------------------- #
def test_true_unit_book_does_not_inflate_gross_and_silence_family(
    enabled, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    # 100 shares @ $150 = $15k against the $100k default NAV = 15% gross — WELL
    # under the 200% cap, so the tiny CSP family ($310 notional) MUST fill.
    # Pre-fix, the cap reads quantity=100 as 10000% gross => g_room<=0 => silenced.
    # (_true_unit_book installs the PortfolioState singleton via monkeypatch.)
    _true_unit_book(tmp_path, monkeypatch, shares=100.0, price=150.0)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)

    meta = parent.reactor_metadata or {}
    silenced = meta.get("no_fill") is True or meta.get("silenced") is True
    assert not silenced, (
        "ar14: an in-cap multi-leg family was wrongly silenced because a true-unit "
        "(100-share) book line was read as a 10000% NAV-fraction and inflated gross "
        f"past the cap. meta={meta}"
    )
    assert parent.fill_size_pct == pytest.approx(0.05)


def test_true_unit_book_still_silences_a_genuinely_over_cap_family(
    enabled, tmp_path, monkeypatch
) -> None:
    """The cap still BINDS: a true-unit line worth ~195% NAV leaves <5% headroom,
    so a family whose own gross dwarfs that (large outer_qty) is silenced. Proves the
    normalization did not simply disable the cap."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    # 13000 shares @ $150 = $1.95M = 1950% of $100k NAV — past the 200% cap.
    _true_unit_book(tmp_path, monkeypatch, shares=13000.0, price=150.0)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(outer_qty=1), fill_size_pct=0.05)

    meta = parent.reactor_metadata or {}
    silenced = meta.get("no_fill") is True or meta.get("silenced") is True
    assert silenced, "ar14: a genuinely over-cap true-unit book must still silence"
    assert _read_family(bus) == []


def test_pure_nav_fraction_book_unchanged(enabled, tmp_path, monkeypatch) -> None:
    """Byte-identical: a legacy NAV-fraction book (DummyPos, no unit_kind) behaves
    exactly as before — a 205%-gross book silences the family."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)

    class DummyPos:
        def __init__(self, quantity: float) -> None:
            self.quantity = quantity  # NAV-fraction, no unit_kind => verbatim

    real = ps.get_positions

    def _get_positions(account_id):
        out = dict(real(account_id))
        out[("equity", "SPY")] = DummyPos(2.05)
        return out

    ps.get_positions = _get_positions  # type: ignore[method-assign]
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)
    meta = parent.reactor_metadata or {}
    assert meta.get("no_fill") is True or meta.get("silenced") is True
    assert _read_family(bus) == []


def test_flag_off_does_not_consult_cap(enabled, tmp_path, monkeypatch) -> None:
    """Flag OFF: the cap path is never consulted; a true-unit book is irrelevant."""
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    _true_unit_book(tmp_path, monkeypatch, shares=13000.0, price=150.0)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_csp(), fill_size_pct=0.05)
    meta = parent.reactor_metadata or {}
    # With the cap off, the family is NOT silenced by a portfolio_cap reason.
    reason = str(meta.get("silence_reason", ""))
    assert "portfolio_cap_" not in reason
