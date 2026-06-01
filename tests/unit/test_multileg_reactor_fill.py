"""Unit tests for hermes_quant.react.multileg.MultiLegPaperReactor — the fill heart.

Deterministic, no network. Uses the no-creds PaperBroker model + a tmp bus + a tmp
state.db (the singleton is patched). Covers CC / CSP / PMCC e2e, idempotency, gate
refusal, broker reject -> no-fill, admissibility, and slippage asymmetry.
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
    GateRejectedProposal,
    MultiLegPaperReactor,
)
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket
from hermes_quant.state.portfolio_state import PortfolioState


def _admitted_gate(
    *,
    bucket: StructureBucket,
    net_greeks: NetGreeks,
    bpr_estimate: float = 0.0,
    max_loss: float | None = None,
) -> OptionsGateResult:
    """A minimal admitted gate result so builders mint a passing proposal via the
    blessed ``MultiLegProposal.from_gate_result`` seam (risk_gate_pass=True is
    unrepresentable by direct construction — ADR-0029/#38). The gate copies
    bucket/net_greeks/bpr/max_loss verbatim onto the proposal, so these values
    reproduce the previously hand-built field values exactly."""
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


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
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


def _pmcc(*, pid="prop_20260530T180000_NVDA_pmcc01") -> MultiLegProposal:
    leaps = OptionLeg(
        symbol="NVDA271217C00120000",
        side="buy",
        position_intent="buy_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=0.82, theta=-0.01, iv=0.45),
        fill_price=48.0,
    )
    short = OptionLeg(
        symbol="NVDA260703C00180000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=0.30, theta=-0.05, iv=0.40),
        fill_price=3.5,
    )
    return MultiLegProposal.from_gate_result(
        gate_result=_admitted_gate(
            bucket=StructureBucket.DEFINED_RISK,
            net_greeks=NetGreeks(delta=52.0, theta=4.0),
            bpr_estimate=4450.0,
            max_loss=4450.0,
        ),
        proposal_id=pid,
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="pmcc",
        underlying="NVDA",
        option_legs=(leaps, short),
        stock_leg=None,
        outer_qty=1,
        net_debit_credit=Decimal("44.50"),  # net DEBIT (positive)
        max_gain=None,
        breakeven_underlying=(Decimal("164.50"),),
        rationale="pmcc",
        source_recipe_id="r_pmcc",
    )


def _read_family(bus):
    return [json.loads(ln) for ln in bus.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Covered call e2e
# --------------------------------------------------------------------------- #
def test_covered_call_e2e(enabled, state_db, tmp_path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    p = _cc()
    parent = reactor.execute(p, fill_size_pct=0.05)

    assert parent.asset_class == "multi_leg"
    assert parent.fill_price < 0  # net credit (negative)
    assert (parent.reactor_metadata or {})["strategy_kind"] == "covered_call"
    assert (parent.reactor_metadata or {})["multi_leg_id"] == p.proposal_id

    family = _read_family(bus)
    parents = [r for r in family if r["reactor_metadata"]["role"] == "parent"]
    children = [r for r in family if r["reactor_metadata"]["role"] != "parent"]
    assert len(parents) == 1
    assert len(children) == 2
    eq = next(c for c in children if c["asset_class"] == "equity")
    opt = next(c for c in children if c["asset_class"] == "us_option")
    assert eq["reactor_metadata"]["quantity"] == 100
    assert opt["reactor_metadata"]["quantity"] == -1

    positions = state_db.get_positions("paper-default")
    assert positions[("equity", "NVDA")].quantity == 100
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1


# --------------------------------------------------------------------------- #
# Cash-secured put e2e
# --------------------------------------------------------------------------- #
def test_cash_secured_put_e2e(enabled, state_db, tmp_path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    reactor.execute(_csp(), fill_size_pct=0.05)

    family = _read_family(bus)
    children = [r for r in family if r["reactor_metadata"]["role"] != "parent"]
    assert len(children) == 1
    assert children[0]["asset_class"] == "us_option"
    assert children[0]["reactor_metadata"]["quantity"] == -1

    positions = state_db.get_positions("paper-default")
    assert positions[("us_option", "NVDA260626P00130000")].quantity == -1
    assert ("equity", "NVDA") not in positions


# --------------------------------------------------------------------------- #
# PMCC e2e + shadow record
# --------------------------------------------------------------------------- #
def test_pmcc_e2e_records_shadow(enabled, state_db, tmp_path, monkeypatch) -> None:
    bus = tmp_path / "executions.jsonl"
    shadow_store = tmp_path / "pmcc-positions.jsonl"
    import hermes_quant.shadow.pmcc as pmcc_mod

    monkeypatch.setattr(pmcc_mod, "_DEFAULT_STORE", shadow_store, raising=False)

    reactor = MultiLegPaperReactor(executions_path=bus)
    p = _pmcc()
    reactor.execute(p, fill_size_pct=0.05)

    family = _read_family(bus)
    children = [r for r in family if r["reactor_metadata"]["role"] != "parent"]
    assert len(children) == 2  # two option legs
    assert all(c["asset_class"] == "us_option" for c in children)

    # Shadow row stamped with note == multi_leg_id
    loaded = pmcc_mod.load_pmcc_positions(path=shadow_store)
    assert len(loaded) == 1
    assert loaded[0].note == p.proposal_id

    # Structural sanity: net_theta_day from the model is POSITIVE for a PMCC.
    mark = pmcc_mod.mark_pmcc(loaded[0], spot=165.0)
    assert mark.net_theta_day > 0


# --------------------------------------------------------------------------- #
# Idempotency (ADR-0078)
# --------------------------------------------------------------------------- #
def test_idempotent_refire_is_noop(enabled, state_db, tmp_path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    p = _cc()
    first = reactor.execute(p, fill_size_pct=0.05)
    family_after_first = _read_family(bus)

    second = reactor.execute(p, fill_size_pct=0.05)
    family_after_second = _read_family(bus)

    # Second is a no-op returning the existing parent; the bus is unchanged.
    assert family_after_second == family_after_first
    assert second.proposal_id == first.proposal_id
    assert (second.reactor_metadata or {})["multi_leg_id"] == p.proposal_id

    # state.db not double-applied.
    positions = state_db.get_positions("paper-default")
    assert positions[("equity", "NVDA")].quantity == 100
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1


# --------------------------------------------------------------------------- #
# Gate-rejected refusal
# --------------------------------------------------------------------------- #
def test_gate_rejected_writes_nothing(enabled, state_db, tmp_path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    p = _cc()
    bad = MultiLegProposal(
        **{
            **{k: getattr(p, k) for k in p.__dataclass_fields__},
            "risk_gate_pass": False,
            "risk_gate_reason": "naked_short_call",
        }
    )
    with pytest.raises(GateRejectedProposal):
        reactor.execute(bad, fill_size_pct=0.05)
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


# --------------------------------------------------------------------------- #
# Broker reject -> no-fill record
# --------------------------------------------------------------------------- #
def test_broker_reject_yields_nofill(enabled, state_db, tmp_path, monkeypatch) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    # Force the single-leg fill to return status="rejected".
    from hermes_quant.react.mleg_fill import LegFill, PaperBroker

    def _rej(self, leg, *, qty, limit_price, tif="day", client_order_id):
        return LegFill(
            symbol=leg.symbol,
            filled_avg_price=0.0,
            filled_qty=0.0,
            status="rejected",
            position_intent=leg.position_intent,
        )

    monkeypatch.setattr(PaperBroker, "submit_single_leg_option", _rej)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)
    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert parent.fill_size_pct == 0.0
    # No bus write, no positions (never fabricate a fill).
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


# --------------------------------------------------------------------------- #
# Admissibility on a short-stock collar leg (flag ON)
# --------------------------------------------------------------------------- #
def test_admissibility_short_stock_leg_flag_on(enabled, state_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    p = _cc()
    # Re-mint a passing proposal that differs only in the (short) stock leg.
    # risk_gate_pass=True is unrepresentable by a direct re-spread (ADR-0029/#38),
    # so route through the same blessed seam the builder uses.
    collar = MultiLegProposal.from_gate_result(
        gate_result=_admitted_gate(
            bucket=StructureBucket.COVERED_CALL,
            net_greeks=p.net_greeks,
            bpr_estimate=float(p.bpr_estimate),
            max_loss=None if p.max_loss is None else float(p.max_loss),
        ),
        proposal_id=p.proposal_id,
        asof=p.asof,
        strategy_kind=p.strategy_kind,
        underlying=p.underlying,
        option_legs=p.option_legs,
        stock_leg=StockLeg(underlying="NVDA", qty=-100, basis_per_share=160.0),
        outer_qty=p.outer_qty,
        net_debit_credit=p.net_debit_credit,
        max_gain=p.max_gain,
        breakeven_underlying=p.breakeven_underlying,
        rationale=p.rationale,
        source_recipe_id=p.source_recipe_id,
    )
    # The live oracle fails-closed (MISSING_ACCOUNT_CONTEXT) for a short -> REJECT.
    parent = reactor.execute(collar, fill_size_pct=0.05)
    assert (parent.reactor_metadata or {}).get("admissibility_rejected") is True
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


def test_admissibility_flag_off_cc_fills(enabled, state_db, tmp_path, monkeypatch) -> None:
    # CC's +100 long leg is admissible by construction; flag OFF is a no-op anyway.
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc(), fill_size_pct=0.05)
    assert (parent.reactor_metadata or {}).get("admissibility_rejected") is None
    assert bus.exists()


# --------------------------------------------------------------------------- #
# Slippage asymmetry
# --------------------------------------------------------------------------- #
def test_slippage_asymmetry(enabled, state_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    reactor.execute(_cc(), fill_size_pct=0.20)

    family = _read_family(bus)
    children = [r for r in family if r["reactor_metadata"]["role"] != "parent"]
    eq = next(c for c in children if c["asset_class"] == "equity")
    opt = next(c for c in children if c["asset_class"] == "us_option")
    # Equity leg slipped (fill_price != decision basis 160.0); option passthrough.
    assert eq["fill_price"] != 160.0
    assert opt["fill_price"] == 4.50  # passthrough at the broker mid


# --------------------------------------------------------------------------- #
# Determinism UNDER the v0.2 slippage model (#23.4)
# --------------------------------------------------------------------------- #
# The eval gate (ops/scripts/quant-multileg-eval.py) proves byte-replay-equality
# with the slippage model OFF (v0.1 passthrough). This closes the complementary
# contract: with the v0.2 envelope ON, the SAME inputs must still produce the
# SAME slipped output across two runs. The v0.2 model is deterministic-given-seed
# where seed = sha256(proposal_id | asof_execution) (slippage_model.seed_for_fill),
# so the ONLY entropy source that could leak is the reactor's wall-clock
# datetime.now() (multileg.py step 3 -> asof_execution). We pin that clock to a
# fixed instant for both runs; byte-equal families then prove no Date.now/random
# leak survives into the slipped fill. The v0.2 model is implemented in
# hermes_quant/react/slippage_model.py, so this runs as a hard assertion; the
# importorskip guard keeps it honest (becomes skip, never a false PASS, if the
# model is ever removed).
def _normalized_family(bus) -> str:
    """Serialize a bus family with the wall-clock asof_execution stripped (it is
    pinned identically across runs here, but stripping mirrors the eval gate's
    _normalized_bus so the comparison is on the structural FILL content)."""
    recs = []
    for ln in bus.read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        rec.pop("asof_execution", None)
        recs.append(rec)
    return json.dumps(recs, separators=(",", ":"), sort_keys=True)


def _run_slipped_cc(workdir, monkeypatch) -> str:
    """One isolated v0.2-slipped CC execution; returns the normalized family.

    Each call gets its own bus + state.db singleton so the two runs are fully
    independent (not an idempotency no-op on a shared bus)."""
    import hermes_quant.state.portfolio_state as ps_mod

    workdir.mkdir(parents=True, exist_ok=True)
    bus = workdir / "executions.jsonl"
    ps = PortfolioState(state_db_path=workdir / "state.db")
    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)

    reactor = MultiLegPaperReactor(executions_path=bus)
    reactor.execute(_cc(), fill_size_pct=0.20)
    return _normalized_family(bus)


def test_slippage_v02_determinism_across_two_runs(enabled, tmp_path, monkeypatch) -> None:
    """v0.2 slippage ON: two runs of identical inputs are byte-identical (no
    Date.now/random leak into the slipped fill). Complements the eval gate's
    slippage-OFF replay-equality assertion (#23.4)."""
    # The v0.2 model must exist for this to be a real assertion (not a fabricated
    # contract). Import-guard so a future removal surfaces as skip, not a false PASS.
    pytest.importorskip("hermes_quant.react.slippage_model")
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")

    # Pin the reactor's wall-clock so asof_execution (the slippage RNG seed input)
    # is identical across both runs — isolating the slippage model as the only
    # thing under test for determinism.
    import hermes_quant.react.multileg as mleg_mod

    fixed = datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - matches datetime.now signature
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(mleg_mod, "datetime", _FrozenDatetime)

    run_a = _run_slipped_cc(tmp_path / "a", monkeypatch)
    run_b = _run_slipped_cc(tmp_path / "b", monkeypatch)

    assert run_a == run_b, "v0.2 slippage fill is non-deterministic across runs"

    # Sanity: the slippage actually fired (equity leg moved off the 160.0 basis),
    # so the determinism assertion is exercising the SLIPPED path, not passthrough.
    a_family = _read_family(tmp_path / "a" / "executions.jsonl")
    eq = next(r for r in a_family if r["asset_class"] == "equity")
    assert eq["fill_price"] != 160.0
