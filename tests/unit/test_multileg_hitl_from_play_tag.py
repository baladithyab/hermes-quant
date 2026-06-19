"""aegis-agdec2 (criterion 4) — human_in_the_loop is DERIVED from play_tag.

Provenance correctness fix (NOT behind a flag): a multi-leg family that was
ORIGINATED autonomously must NOT be stamped ``human_in_the_loop=True``. Before
this fix the multileg reactor hardcoded ``human_in_the_loop=True`` on the parent,
every option/equity child, and the no-fill parent — so an autonomously-originated
fire read back through the retro/settlement loop as a human override. The flag is
now derived ONCE from play_tag:

  * ``autonomous`` / ``autonomous_options`` => HITL False (no human in the loop);
  * ``advisor`` / ``playbook`` / None / unknown => HITL True (fail-safe: an
    unknown origin is treated as needing human oversight — conservative, and
    byte-identical to the prior hardcoded-True behavior for these paths).

The autonomous options origination path stamps ``play_tag="autonomous_options"``
(autonomous.py), so the predicate MUST cover BOTH spellings or option families
stay wrongly HITL=True.

Deterministic, no network: no-creds DeterministicBackend + tmp bus + tmp state.db.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
)
from hermes_quant.options.multileg import MultiLegProposal
from hermes_quant.react.multileg import (
    MultiLegPaperReactor,
    _hitl_from_play_tag,
)
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket
from hermes_quant.state.portfolio_state import PortfolioState


# --------------------------------------------------------------------------- #
# Fixtures / builders (mirror test_multileg_reactor_fill.py idiom)
# --------------------------------------------------------------------------- #
def _admitted_gate(
    *,
    bucket: StructureBucket,
    net_greeks: NetGreeks,
    bpr_estimate: float = 0.0,
    max_loss: float | None = None,
) -> OptionsGateResult:
    return OptionsGateResult(
        admitted=True,
        bucket=bucket,
        reason=None,
        net_greeks=net_greeks,
        bpr_estimate=bpr_estimate,
        max_loss=max_loss,
        contracts=1,
        warnings=(),
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    return ps


def _snap(**kw) -> OptionGreeksSnapshot:
    base = dict(delta=0.25, gamma=0.01, theta=-0.02, vega=0.10, rho=0.01, iv=0.4)
    base.update(kw)
    return OptionGreeksSnapshot(**base)


def _cc(*, pid="prop_20260530T180000_NVDA_cc0001") -> MultiLegProposal:
    """Covered call: one option leg + one equity leg => parent + 2 children
    (exercises BOTH the us_option child and the equity child record builders)."""
    call = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(),
        fill_price=4.50,
    )
    return MultiLegProposal.from_gate_result(
        gate_result=_admitted_gate(
            bucket=StructureBucket.COVERED_CALL,
            net_greeks=NetGreeks(delta=75.0, gamma=-1.0, theta=3.0, vega=-10.0),
            bpr_estimate=0.0,
            max_loss=None,
        ),
        proposal_id=pid,
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(call,),
        stock_leg=StockLeg(underlying="NVDA", qty=100, basis_per_share=160.0),
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="cc",
        source_recipe_id="r_cc",
    )


def _csp(*, pid="prop_20260530T180000_NVDA_csp001") -> MultiLegProposal:
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
            max_loss=None,
        ),
        proposal_id=pid,
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="cash_secured_put",
        underlying="NVDA",
        option_legs=(put,),
        stock_leg=None,
        outer_qty=1,
        net_debit_credit=Decimal("-3.10"),
        max_gain=Decimal("310"),
        breakeven_underlying=(Decimal("126.90"),),
        rationale="csp",
        source_recipe_id="r_csp",
    )


def _read_family(bus):
    return [json.loads(ln) for ln in bus.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# The pure predicate
# --------------------------------------------------------------------------- #
def test_hitl_predicate_autonomous_is_false():
    assert _hitl_from_play_tag("autonomous") is False
    assert _hitl_from_play_tag("autonomous_options") is False


def test_hitl_predicate_human_paths_are_true():
    assert _hitl_from_play_tag("advisor") is True
    assert _hitl_from_play_tag("playbook") is True


def test_hitl_predicate_none_or_unknown_is_true():
    # fail-safe: an unknown / absent origin is treated as needing human oversight.
    assert _hitl_from_play_tag(None) is True
    assert _hitl_from_play_tag("") is True
    assert _hitl_from_play_tag("something_new") is True


# --------------------------------------------------------------------------- #
# Parent + leg records: autonomous => HITL False
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag", ["autonomous", "autonomous_options"])
def test_autonomous_family_is_not_hitl(enabled, state_db, tmp_path, tag):
    """An autonomously-originated CC: parent AND every child (us_option + equity)
    record human_in_the_loop=False — on the returned record AND the bus line."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc(), fill_size_pct=0.05, play_tag=tag)

    assert parent.human_in_the_loop is False
    assert parent.play_tag == tag

    family = _read_family(bus)
    assert family, "expected a written family"
    assert all(r["human_in_the_loop"] is False for r in family)
    # cover both child asset classes explicitly
    classes = {r["asset_class"] for r in family}
    assert {"multi_leg", "us_option", "equity"} <= classes


# --------------------------------------------------------------------------- #
# Parent + leg records: human paths => HITL True (byte-identical to prior)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag", ["advisor", "playbook"])
def test_human_family_stays_hitl(enabled, state_db, tmp_path, tag):
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc(), fill_size_pct=0.05, play_tag=tag)

    assert parent.human_in_the_loop is True
    family = _read_family(bus)
    assert family
    assert all(r["human_in_the_loop"] is True for r in family)


def test_default_play_tag_stays_hitl(enabled, state_db, tmp_path):
    """Omitting play_tag (default 'advisor') keeps the family HITL=True —
    byte-identical to the prior hardcoded-True behavior for non-autonomous callers."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc(), fill_size_pct=0.05)

    assert parent.human_in_the_loop is True
    family = _read_family(bus)
    assert family
    assert all(r["human_in_the_loop"] is True for r in family)


# --------------------------------------------------------------------------- #
# No-fill parent path also derives from play_tag
# --------------------------------------------------------------------------- #
def test_nofill_parent_autonomous_is_not_hitl(enabled, state_db, tmp_path, monkeypatch):
    """A broker-reject no-fill parent for an autonomous fire is HITL=False too
    (the audit record must not mislabel the autonomous origin as a human override)."""
    from hermes_quant.react.backend import FillResult
    from hermes_quant.react.backends.deterministic_backend import DeterministicBackend

    def _rej(self, leg, *, qty, limit_price, client_order_id):
        return FillResult(
            symbol=leg.symbol,
            filled_avg_price=0.0,
            filled_qty=0.0,
            status="rejected",
            position_intent=leg.position_intent,
            source="deterministic",
        )

    monkeypatch.setattr(DeterministicBackend, "submit_option_single", _rej)

    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_csp(), fill_size_pct=0.05, play_tag="autonomous_options")

    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert parent.human_in_the_loop is False
    assert parent.play_tag == "autonomous_options"


def test_nofill_parent_human_stays_hitl(enabled, state_db, tmp_path, monkeypatch):
    from hermes_quant.react.backend import FillResult
    from hermes_quant.react.backends.deterministic_backend import DeterministicBackend

    def _rej(self, leg, *, qty, limit_price, client_order_id):
        return FillResult(
            symbol=leg.symbol,
            filled_avg_price=0.0,
            filled_qty=0.0,
            status="rejected",
            position_intent=leg.position_intent,
            source="deterministic",
        )

    monkeypatch.setattr(DeterministicBackend, "submit_option_single", _rej)

    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_csp(), fill_size_pct=0.05)  # default advisor

    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert parent.human_in_the_loop is True
