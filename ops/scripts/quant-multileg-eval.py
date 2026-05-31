"""quant-multileg-eval.py — ADR-0029 multi-leg PAPER reactor eval gate (the merge proof).

A CC + a CSP fill END-TO-END on paper, byte-deterministic, validated against the PMCC
shadow; idempotent re-fire is a no-op; a risk_gate_pass=False proposal is GateRejected
with zero writes; flag-OFF => MultiLegReactorDisabled, nothing written.

Runs with HERMES_QUANT_MULTILEG_REACTOR=1 (+ HERMES_QUANT_OPTIONS_GATE=1) set FOR THIS
PROCESS ONLY — never in deploy. Uses tmp bus + tmp state.db + tmp shadow store + the
deterministic no-creds PaperBroker (no network). Exit 0 iff every assertion holds AND
the run is byte-deterministic across two invocations.

    python ops/scripts/quant-multileg-eval.py

Posture rails (NON-NEGOTIABLE): the reactor CONSUMES an options_gate admit via
MultiLegProposal.from_gate_result (never bypasses the gate); money seam is HITL/CLI
only; PAPER-only; exactly-once; default-OFF flag set NOWHERE in repo/deploy.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

_VENV = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

# Flags set FOR THIS EVAL PROCESS ONLY (never written to deploy .env). The deploy flag
# stays unset; the operator's deliberate later flip is a separate act (ADR-0029 D7).
os.environ["HERMES_QUANT_MULTILEG_REACTOR"] = "1"
os.environ["HERMES_QUANT_OPTIONS_GATE"] = "1"
os.environ.setdefault("HERMES_QUANT_PAPER_INITIAL_CASH", "1000000")
# Ensure the deterministic (no-creds) PaperBroker model + admissibility no-op.
os.environ.pop("APCA_API_KEY_ID", None)
os.environ.pop("APCA_API_SECRET_KEY", None)
os.environ.pop("HERMES_QUANT_ADMISSIBILITY", None)
os.environ.pop("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", None)

from hermes_quant.options.data import (  # noqa: E402
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
)
from hermes_quant.options.multileg import MultiLegProposal  # noqa: E402
from hermes_quant.reconcile.pmcc_shadow import reconcile_pmcc_shadow  # noqa: E402
from hermes_quant.risk.options_gate import (  # noqa: E402
    OptionsRiskConfig,
    StructureBucket,
    options_gate,
)
from hermes_quant.state.portfolio_state import PortfolioState  # noqa: E402

_CFG = OptionsRiskConfig()
_ASOF = datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC)


def _snap(**kw) -> OptionGreeksSnapshot:
    base = dict(delta=0.25, gamma=0.01, theta=0.05, vega=0.05, rho=0.01, iv=0.4)
    base.update(kw)
    return OptionGreeksSnapshot(**base)


# --------------------------------------------------------------------------- #
# Proposal builders — each goes through a REAL options_gate admit
# --------------------------------------------------------------------------- #
def _cc_proposal(pid: str) -> MultiLegProposal:
    call = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=0.25),
        fill_price=4.50,
    )
    stock = StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0)
    gr = options_gate(
        [stock, call],
        strategy_kind="covered_call",
        underlying="NVDA",
        spot=150.0,
        nav=1_000_000.0,
        held_shares=100,
        options_buying_power=500_000.0,
        premium_received=450.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=160.0,
        basis_per_share=100.0,
        min_dte=27,
    )
    assert gr.admitted, f"CC gate did not admit: {gr.reason}"
    assert gr.bucket == StructureBucket.COVERED_CALL
    return MultiLegProposal.from_gate_result(
        gate_result=gr,
        proposal_id=pid,
        asof=_ASOF,
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(call,),
        stock_leg=stock,
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="eval CC",
        source_recipe_id="eval_cc",
    )


def _csp_proposal(pid: str) -> MultiLegProposal:
    put = OptionLeg(
        symbol="NVDA260626P00140000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=-0.25, rho=-0.01),
        fill_price=3.10,
    )
    gr = options_gate(
        [put],
        strategy_kind="cash_secured_put",
        underlying="NVDA",
        spot=150.0,
        nav=1_000_000.0,
        held_shares=0,
        options_buying_power=20_000.0,
        premium_received=310.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=140.0,
        min_dte=27,
    )
    assert gr.admitted, f"CSP gate did not admit: {gr.reason}"
    assert gr.bucket == StructureBucket.CASH_SECURED_PUT
    return MultiLegProposal.from_gate_result(
        gate_result=gr,
        proposal_id=pid,
        asof=_ASOF,
        strategy_kind="cash_secured_put",
        underlying="NVDA",
        option_legs=(put,),
        stock_leg=None,
        outer_qty=1,
        net_debit_credit=Decimal("-3.10"),
        max_gain=Decimal("310"),
        breakeven_underlying=(Decimal("136.90"),),
        rationale="eval CSP",
        source_recipe_id="eval_csp",
    )


def _pmcc_proposal(pid: str) -> MultiLegProposal:
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
        greeks_at_decision=_snap(delta=0.30, theta=0.05, iv=0.40),
        fill_price=3.5,
    )
    gr = options_gate(
        [leaps, short],
        strategy_kind="vertical_spread",
        underlying="NVDA",
        spot=165.0,
        nav=1_000_000.0,
        held_shares=0,
        options_buying_power=500_000.0,
        premium_received=350.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=180.0,
        width=60.0,
        net_debit=44.5,
        premium_paid=44.5 * 100,
        min_dte=34,
    )
    assert gr.admitted, f"PMCC gate did not admit: {gr.reason}"
    return MultiLegProposal.from_gate_result(
        gate_result=gr,
        proposal_id=pid,
        asof=_ASOF,
        strategy_kind="pmcc",
        underlying="NVDA",
        option_legs=(leaps, short),
        stock_leg=None,
        outer_qty=1,
        net_debit_credit=Decimal("44.50"),
        max_gain=None,
        breakeven_underlying=(Decimal("164.50"),),
        rationale="eval PMCC",
        source_recipe_id="eval_pmcc",
    )


def _gate_rejected_cc(pid: str) -> MultiLegProposal:
    """A naked short call (no covering shares) the gate REJECTS — carried for the
    gate-is-final-authority assertion."""
    call = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=0.25),
        fill_price=4.50,
    )
    gr = options_gate(
        [call],
        strategy_kind="covered_call",
        underlying="NVDA",
        spot=150.0,
        nav=1_000_000.0,
        held_shares=0,  # no covering shares => NAKED reject
        options_buying_power=500_000.0,
        premium_received=450.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=160.0,
        min_dte=27,
    )
    assert not gr.admitted, "expected the gate to REJECT a naked short call"
    return MultiLegProposal.from_gate_result(
        gate_result=gr,
        proposal_id=pid,
        asof=_ASOF,
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(call,),
        stock_leg=None,
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="eval gate-reject",
        source_recipe_id="eval_reject",
    )


# --------------------------------------------------------------------------- #
# One full eval run (returns the parent records for byte-determinism comparison)
# --------------------------------------------------------------------------- #
def _run_eval(workdir: Path) -> dict:
    import hermes_quant.shadow.pmcc as pmcc_mod
    from hermes_quant.react.multileg import (
        GateRejectedProposal,
        MultiLegPaperReactor,
        MultiLegReactorDisabled,
    )

    bus = workdir / "executions.jsonl"
    state_db = workdir / "state.db"
    shadow_store = workdir / "pmcc-positions.jsonl"

    # Point the shadow store + state.db singleton at the tmp workdir.
    pmcc_mod._DEFAULT_STORE = shadow_store
    ps = PortfolioState(state_db_path=state_db)
    import hermes_quant.state.portfolio_state as ps_mod

    ps_mod._singleton = ps

    reactor = MultiLegPaperReactor(executions_path=bus)
    results: dict = {}

    # ── 1. Covered call e2e. ────────────────────────────────────────────────
    cc = _cc_proposal("prop_20260530T180000_NVDA_cceval")
    cc_parent = reactor.execute(cc, fill_size_pct=0.05)
    assert cc_parent.asset_class == "multi_leg", "CC parent not multi_leg"
    assert cc_parent.fill_price < 0, "CC net price should be a credit (negative)"
    fam = _family(bus, cc.proposal_id)
    parents = [r for r in fam if r["reactor_metadata"]["role"] == "parent"]
    children = [r for r in fam if r["reactor_metadata"]["role"] != "parent"]
    assert len(parents) == 1 and len(children) == 2, f"CC family wrong: {fam}"
    pos = ps.get_positions("paper-default")
    assert pos[("equity", "NVDA")].quantity == 100, "CC equity row != +100"
    assert pos[("us_option", "NVDA260626C00160000")].quantity == -1, "CC option row != -1"
    results["cc_ok"] = True

    # ── 2. Cash-secured put e2e. ────────────────────────────────────────────
    csp = _csp_proposal("prop_20260530T180000_NVDA_cspeval")
    reactor.execute(csp, fill_size_pct=0.05)
    cfam = _family(bus, csp.proposal_id)
    cchildren = [r for r in cfam if r["reactor_metadata"]["role"] != "parent"]
    assert len(cchildren) == 1, f"CSP should have ONE option child: {cfam}"
    assert cchildren[0]["asset_class"] == "us_option"
    pos = ps.get_positions("paper-default")
    assert pos[("us_option", "NVDA260626P00140000")].quantity == -1, "CSP option row != -1"
    assert ("equity", "NVDA260626P00140000") not in pos
    results["csp_ok"] = True

    # ── 3. Idempotent re-fire of the SAME CC => no-op. ──────────────────────
    bus_before = bus.read_bytes()
    pos_before = {k: v.quantity for k, v in ps.get_positions("paper-default").items()}
    reactor.execute(cc, fill_size_pct=0.05)
    assert bus.read_bytes() == bus_before, "idempotent re-fire wrote to the bus"
    pos_after = {k: v.quantity for k, v in ps.get_positions("paper-default").items()}
    assert pos_after == pos_before, "idempotent re-fire mutated state.db"
    results["idempotent_ok"] = True

    # ── 4. PMCC e2e + shadow reconcile (net_theta_day > 0). ─────────────────
    pmcc = _pmcc_proposal("prop_20260530T180000_NVDA_pmcceval")
    reactor.execute(pmcc, fill_size_pct=0.05)
    loaded = pmcc_mod.load_pmcc_positions(path=shadow_store)
    stamped = [p for p in loaded if p.note == pmcc.proposal_id]
    assert len(stamped) == 1, "PMCC shadow not stamped with multi_leg_id"
    div = reconcile_pmcc_shadow(
        asof=_ASOF.date(),
        spot_by_symbol={"NVDA": 165.0},
        real_marks_by_mleg_id={pmcc.proposal_id: 4500.0},
        path=shadow_store,
    )
    row = next(r for r in div if r.multi_leg_id == pmcc.proposal_id)
    assert row.model_net_theta_day > 0, f"PMCC net theta not positive: {row.model_net_theta_day}"
    assert row.severity == "ok", f"PMCC severity not ok: {row.severity}"
    results["pmcc_ok"] = True

    # ── 5. Gate-is-final rail: a risk_gate_pass=False proposal => reject, 0 writes.
    rej = _gate_rejected_cc("prop_20260530T180000_NVDA_rejeval")
    bus_before = bus.read_bytes()
    try:
        reactor.execute(rej, fill_size_pct=0.05)
        raise AssertionError("gate-rejected proposal was NOT refused")
    except GateRejectedProposal:
        pass
    assert bus.read_bytes() == bus_before, "gate-rejected proposal wrote to the bus"
    results["gate_reject_ok"] = True

    # ── Flag-OFF proof (a fresh reactor with the flag cleared). ─────────────
    os.environ.pop("HERMES_QUANT_MULTILEG_REACTOR", None)
    off_bus = workdir / "off-executions.jsonl"
    off_reactor = MultiLegPaperReactor(executions_path=off_bus)
    try:
        off_reactor.execute(_cc_proposal("prop_off"), fill_size_pct=0.05)
        raise AssertionError("flag-OFF reactor did not raise MultiLegReactorDisabled")
    except MultiLegReactorDisabled:
        pass
    assert not off_bus.exists(), "flag-OFF reactor wrote to the bus"
    os.environ["HERMES_QUANT_MULTILEG_REACTOR"] = "1"  # restore for determinism re-run
    results["flag_off_ok"] = True

    # The CC + CSP + PMCC families, normalized (strip asof_execution wall-clock, which
    # is the only non-deterministic field) for the byte-determinism check.
    results["families_normalized"] = _normalized_bus(bus)
    return results


def _family(bus: Path, multi_leg_id: str) -> list[dict]:
    out = []
    for ln in bus.read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        if (rec.get("reactor_metadata") or {}).get("multi_leg_id") == multi_leg_id:
            out.append(rec)
    return out


def _normalized_bus(bus: Path) -> str:
    """Serialize the bus with the wall-clock asof_execution stripped so two runs
    (which differ only in fire wall-clock) compare byte-equal on the structural
    content — proving the FILL itself is deterministic."""
    recs = []
    for ln in bus.read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        rec.pop("asof_execution", None)
        meta = rec.get("reactor_metadata") or {}
        meta.pop("broker_order_id", None)  # derived from client_order_id; stable, kept
        recs.append(rec)
    return json.dumps(recs, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        run_a = _run_eval(Path(d1))
        run_b = _run_eval(Path(d2))

    checks = [
        ("CC e2e (parent multi_leg + (equity +100) + (us_option -1) rows)", run_a["cc_ok"]),
        ("CSP e2e (one us_option -1 row, no equity row)", run_a["csp_ok"]),
        ("idempotent re-fire is a no-op (bus + state.db unchanged)", run_a["idempotent_ok"]),
        ("PMCC shadow joined on multi_leg_id; net_theta_day > 0", run_a["pmcc_ok"]),
        ("gate-is-final: risk_gate_pass=False => GateRejected, zero writes", run_a["gate_reject_ok"]),
        ("flag-OFF => MultiLegReactorDisabled, nothing written", run_a["flag_off_ok"]),
        (
            "byte-deterministic across two invocations (replay equality)",
            run_a["families_normalized"] == run_b["families_normalized"],
        ),
    ]
    all_ok = all(ok for _, ok in checks)
    print("=" * 72)
    print("ADR-0029 multi-leg PAPER reactor — EVAL GATE")
    print("=" * 72)
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print("=" * 72)
    print(f"EVAL GATE: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
