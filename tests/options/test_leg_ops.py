"""tests/options/test_leg_ops.py — deterministic LEG-OPERATION rules (aegis-ml01).

These tests cover the three composite leg-operation DECISION functions that let AEGIS
either manage a multi-leg composite WHOLE or break it apart and manage each leg, WITHOUT
orphaning the composite_plays state machine (ADR-0098 §H1-H4, operator decision #5):

  * decompose_decision   — WHEN to break a composite into independently-managed legs.
  * convert_decision      — roll / leg-into another admissible structure (atomic or
                            H1-partial-guarded so a half-applied convert cannot strand a
                            naked / undefined-risk leg).
  * risk_adjust_decision  — close / roll / hedge ONE leg without orphaning the rest
                            (REJECT any adjust that would leave a side naked).

All three are DETERMINISTIC (no LLM), emit a structured decision the React layer would
execute, and DEFAULT-OFF behind HERMES_QUANT_COMPOSITE_LEG_OPS (byte-identical no-op when
unset). The state transition through CompositePlaysStore is the side effect the ACT helpers
drive — never an auto-close (H1).

Test plan (load-bearing first):
  1. DECOMPOSE + NO-ORPHAN: a leg breaching its own risk -> decompose, store transitions
     open->partial (or decomposed), detect_orphan() == False after. RED: a broken impl that
     closes the leg WITHOUT transitioning leaves detect_orphan() == True.
  2. CONVERT ATOMICITY: a convert whose add-leg half fails leaves NO naked / undefined-risk
     leg (rolled back, or a defined-risk intermediate). RED: a non-atomic convert strands a
     naked short.
  3. RISK-ADJUST NO-NAKED: a single-leg adjust that WOULD make a side naked is REJECTED.
  4. H1 partial-never-auto-close: a partial decompose -> 'partial', NEVER 'closed' auto.
  5. DEFAULT-OFF byte-identical: flag unset -> all decision fns return no_action -> no
     transitions driven.
  6. THESIS-INVALIDATION + ASSIGNMENT-LOOMS: triggers fire on the right options_exit signals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.options.data import OptionGreeksSnapshot, OptionLeg, StockLeg
from hermes_quant.options.leg_ops import (
    COMPOSITE_LEG_OPS_FLAG,
    ConvertExecutionError,
    LegRisk,
    apply_convert,
    apply_decompose,
    apply_risk_adjust,
    composite_has_naked_side,
    convert_decision,
    decompose_decision,
    leg_ops_enabled,
    risk_adjust_decision,
)
from hermes_quant.state.composite_plays import (
    STATE_DECOMPOSED,
    STATE_OPEN,
    STATE_PARTIAL,
    CompositePlaysStore,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(COMPOSITE_LEG_OPS_FLAG, "1")


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(COMPOSITE_LEG_OPS_FLAG, raising=False)


def _store(tmp_path: Path) -> CompositePlaysStore:
    return CompositePlaysStore(db_path=tmp_path / "composite_plays.db")


# OCC-21 symbols for AAPL options expiring 2026-07-17 (same expiry => same condor).
# Format: ROOT(6, left-padded) + YYMMDD + C/P + strike*1000 (8 digits).
_SHORT_PUT = "AAPL  260717P00190000"
_LONG_PUT = "AAPL  260717P00185000"
_SHORT_CALL = "AAPL  260717C00210000"
_LONG_CALL = "AAPL  260717C00215000"


def _greeks(delta: float) -> OptionGreeksSnapshot:
    return OptionGreeksSnapshot(delta=delta, gamma=0.0, theta=-0.01, vega=0.05)


def _leg(symbol: str, side: str, intent: str, delta: float) -> OptionLeg:
    return OptionLeg(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        position_intent=intent,  # type: ignore[arg-type]
        ratio_qty=1,
        greeks_at_decision=_greeks(delta),
        fill_price=1.0,
    )


def _iron_condor_legs() -> list[OptionLeg]:
    """A 4-leg iron condor: short put + long put (put wing) + short call + long call."""
    return [
        _leg(_SHORT_PUT, "sell", "sell_to_open", -0.30),
        _leg(_LONG_PUT, "buy", "buy_to_open", -0.15),
        _leg(_SHORT_CALL, "sell", "sell_to_open", 0.30),
        _leg(_LONG_CALL, "buy", "buy_to_open", 0.15),
    ]


def _bull_put_spread_legs() -> list[OptionLeg]:
    """A 2-leg bull put spread: short put + long protective put."""
    return [
        _leg(_SHORT_PUT, "sell", "sell_to_open", -0.30),
        _leg(_LONG_PUT, "buy", "buy_to_open", -0.15),
    ]


def _open_condor(store: CompositePlaysStore, mlid: str = "prop_ic_001") -> str:
    store.open_composite(
        multi_leg_id=mlid,
        underlying="AAPL",
        strategy_kind="iron_condor",
        opened_at="2026-06-17T10:00:00.000000Z",
        outer_qty=1,
        net_entry_price=2.00,
        fill_size_pct=0.05,
        expected_leg_count=4,
        max_loss=300.0,
    )
    return mlid


# ---------------------------------------------------------------------------
# 1. DECOMPOSE + NO-ORPHAN (load-bearing)
# ---------------------------------------------------------------------------


def test_decompose_leg_risk_breach_drives_transition_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leg breaching its OWN risk -> decompose_decision says decompose; apply_decompose
    drives open->partial (some legs still composite); detect_orphan() == False after.

    RED-PROOF (the cardinal H1 hazard): a broken impl that closes the leg WITHOUT
    transitioning the composite leaves the store in state='open' with active_leg_count <
    expected_leg_count -> detect_orphan() == True. We assert it is False, and assert the
    state actually moved off 'open'.
    """
    _enable(monkeypatch)
    store = _store(tmp_path)
    mlid = _open_condor(store)

    legs = _iron_condor_legs()
    # The short put leg (idx 0) breaches its own 2x loss cap (a single-leg risk breach).
    leg_signals = [
        LegRisk(leg_idx=0, breaches_own_risk=True, assignment_looms=False),
        LegRisk(leg_idx=1, breaches_own_risk=False, assignment_looms=False),
        LegRisk(leg_idx=2, breaches_own_risk=False, assignment_looms=False),
        LegRisk(leg_idx=3, breaches_own_risk=False, assignment_looms=False),
    ]

    decision = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=False
    )
    assert decision["decompose"] is True
    assert 0 in decision["legs_to_independently_manage"]
    assert decision["reason"]

    # ACT: drive the store transition (NOT all legs go independent -> partial).
    new_state = apply_decompose(
        store=store,
        multi_leg_id=mlid,
        decision=decision,
        legs_remaining_after=3,  # 3 legs still composite
    )
    assert new_state == STATE_PARTIAL

    # NO-ORPHAN: with 3 active legs vs 4 expected, an 'open' composite WOULD be an orphan;
    # because we transitioned to 'partial', detect_orphan() must be False.
    assert store.detect_orphan(mlid, active_leg_count=3) is False
    row = store.get(mlid)
    assert row is not None
    assert row.state == STATE_PARTIAL


def test_decompose_all_legs_independent_transitions_to_decomposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ALL legs become independently managed, the composite transitions to
    'decomposed' (not 'partial'), and detect_orphan() is False."""
    _enable(monkeypatch)
    store = _store(tmp_path)
    mlid = _open_condor(store)
    legs = _iron_condor_legs()
    leg_signals = [
        LegRisk(leg_idx=i, breaches_own_risk=True, assignment_looms=False)
        for i in range(4)
    ]
    decision = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=True
    )
    assert decision["decompose"] is True
    new_state = apply_decompose(
        store=store, multi_leg_id=mlid, decision=decision, legs_remaining_after=0
    )
    assert new_state == STATE_DECOMPOSED
    assert store.detect_orphan(mlid, active_leg_count=0) is False


# ---------------------------------------------------------------------------
# 2. CONVERT ATOMICITY
# ---------------------------------------------------------------------------


def test_convert_add_leg_failure_does_not_strand_naked_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A convert (bull-put-spread -> iron-condor) whose ADD-leg half fails must NOT leave a
    naked / undefined-risk leg.

    The convert adds a short-call + long-call wing. If the add-leg half fails, the original
    bull-put-spread legs must be untouched (still defined-risk) and the composite must NOT
    have advanced. We model the broker failure via an add_executor that raises; apply_convert
    must either (a) not mutate the store at all (rolled back) AND leave the composite in a
    defined-risk state, and (b) NEVER produce a leg-set with a naked side.

    RED-PROOF: a non-atomic convert that removes/transitions BEFORE confirming the add half
    would strand the composite mid-convert. We assert the composite state is unchanged (still
    'open') and the surviving leg-set has no naked side.
    """
    _enable(monkeypatch)
    store = _store(tmp_path)
    store.open_composite(
        multi_leg_id="prop_cv_001",
        underlying="AAPL",
        strategy_kind="bull_put_spread",
        opened_at="2026-06-17T10:00:00.000000Z",
        outer_qty=1,
        net_entry_price=1.00,
        fill_size_pct=0.03,
        expected_leg_count=2,
        max_loss=400.0,
    )
    current = _bull_put_spread_legs()
    # Convert by ADDING a bear-call wing (short call + long call) -> iron condor.
    legs_to_add = [
        _leg(_SHORT_CALL, "sell", "sell_to_open", 0.30),
        _leg(_LONG_CALL, "buy", "buy_to_open", 0.15),
    ]
    decision = convert_decision(
        current_legs=current,
        target_structure="iron_condor",
        legs_to_add=legs_to_add,
        legs_to_remove=[],
        reason="roll BPS to IC on rising IV",
    )
    assert decision["convert"] is True
    assert decision["target_structure"] == "iron_condor"

    # The broker ADD-leg order FAILS (raises). apply_convert must NOT strand a naked leg.
    def _failing_add_executor(legs: list[OptionLeg]) -> None:
        raise RuntimeError("broker rejected the MLEG add order")

    with pytest.raises(ConvertExecutionError):
        apply_convert(
            store=store,
            multi_leg_id="prop_cv_001",
            decision=decision,
            current_legs=current,
            add_executor=_failing_add_executor,
        )

    # ATOMICITY: the composite must be unchanged (still 'open') because the add failed and
    # nothing was removed first.
    row = store.get("prop_cv_001")
    assert row is not None
    assert row.state == STATE_OPEN

    # NO-NAKED: the surviving leg-set is the original defined-risk bull-put-spread.
    assert composite_has_naked_side(current) is False


def test_convert_rejects_when_target_would_be_naked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A convert whose target leg-set has a naked short (e.g. removing the long protective
    put from a bull-put-spread leaving a naked short put) is REJECTED at decision time.

    RED-PROOF: a convert that does not check the resulting leg-set would emit convert=True
    and could strand a naked short. We assert convert=False with a no-naked reason.
    """
    _enable(monkeypatch)
    current = _bull_put_spread_legs()
    # Remove the LONG protective put -> would leave a naked short put.
    decision = convert_decision(
        current_legs=current,
        target_structure="cash_secured_put",
        legs_to_add=[],
        legs_to_remove=[_LONG_PUT],
        reason="bad idea: strip the protection",
    )
    assert decision["convert"] is False
    assert "naked" in decision["reason"].lower()


# ---------------------------------------------------------------------------
# 3. RISK-ADJUST NO-NAKED
# ---------------------------------------------------------------------------


def test_risk_adjust_rejects_when_it_would_make_a_side_naked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-leg adjust that closes the LONG protective put of a bull-put-spread (leaving
    the short put naked) is REJECTED.

    RED-PROOF: an adjust that does not simulate the resulting leg-set would emit
    action='close' on the long put and strand a naked short. We assert action='reject'.
    """
    _enable(monkeypatch)
    current = _bull_put_spread_legs()
    decision = risk_adjust_decision(
        current_legs=current,
        leg_symbol=_LONG_PUT,  # closing the protection
        action="close",
        reason="thought it was free money",
    )
    assert decision["action"] == "reject"
    assert "naked" in decision["reason"].lower()


def test_risk_adjust_allows_closing_short_when_protection_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the SHORT put of a bull-put-spread leaves only a long put (a defined-risk long
    option, no naked side) -> the adjust is ALLOWED."""
    _enable(monkeypatch)
    current = _bull_put_spread_legs()
    decision = risk_adjust_decision(
        current_legs=current,
        leg_symbol=_SHORT_PUT,
        action="close",
        reason="lock in the short-leg gain",
    )
    assert decision["action"] == "close"
    assert decision["leg"] == _SHORT_PUT


# ---------------------------------------------------------------------------
# 4. H1 partial-never-auto-close
# ---------------------------------------------------------------------------


def test_h1_partial_decompose_never_auto_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial decompose transitions to 'partial' and NEVER to 'closed' automatically.

    apply_decompose with some legs still composite must produce 'partial'; a follow-up
    apply_decompose that does NOT explicitly signal all-legs-done must STAY 'partial' (the
    store's H1 guard). We assert it never silently becomes 'closed'.
    """
    _enable(monkeypatch)
    store = _store(tmp_path)
    mlid = _open_condor(store, "prop_h1_001")
    legs = _iron_condor_legs()
    leg_signals = [LegRisk(leg_idx=0, breaches_own_risk=True, assignment_looms=False)]
    decision = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=False
    )
    s1 = apply_decompose(
        store=store, multi_leg_id=mlid, decision=decision, legs_remaining_after=3
    )
    assert s1 == STATE_PARTIAL
    # A subsequent decompose step on the partial composite must NOT auto-close.
    s2 = apply_decompose(
        store=store, multi_leg_id=mlid, decision=decision, legs_remaining_after=2
    )
    assert s2 == STATE_PARTIAL
    row = store.get(mlid)
    assert row is not None
    assert row.state == STATE_PARTIAL  # never 'closed'


# ---------------------------------------------------------------------------
# 5. DEFAULT-OFF byte-identical
# ---------------------------------------------------------------------------


def test_default_off_all_decision_fns_return_no_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With HERMES_QUANT_COMPOSITE_LEG_OPS unset, all three decision functions return a no-op
    (no_action) so the composite is managed WHOLE — byte-identical to today.

    RED-PROOF: if the decision logic ran regardless of the flag, decompose_decision on a
    breaching leg would return decompose=True. We assert it is False (no_action) when the
    flag is unset.
    """
    _disable(monkeypatch)
    assert leg_ops_enabled() is False

    legs = _iron_condor_legs()
    leg_signals = [
        LegRisk(leg_idx=0, breaches_own_risk=True, assignment_looms=True)
    ]
    d = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=True
    )
    assert d["decompose"] is False
    assert d["reason"] == "no_action"
    assert d["legs_to_independently_manage"] == []

    c = convert_decision(
        current_legs=_bull_put_spread_legs(),
        target_structure="iron_condor",
        legs_to_add=[
            _leg(_SHORT_CALL, "sell", "sell_to_open", 0.30),
            _leg(_LONG_CALL, "buy", "buy_to_open", 0.15),
        ],
        legs_to_remove=[],
        reason="x",
    )
    assert c["convert"] is False
    assert c["reason"] == "no_action"

    r = risk_adjust_decision(
        current_legs=_bull_put_spread_legs(),
        leg_symbol=_LONG_PUT,
        action="close",
        reason="x",
    )
    assert r["action"] == "no_action"


def test_default_off_apply_decompose_drives_no_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag unset, apply_decompose drives NO store transition (the composite stays
    'open' / managed whole)."""
    _disable(monkeypatch)
    store = _store(tmp_path)
    mlid = _open_condor(store, "prop_off_001")
    legs = _iron_condor_legs()
    leg_signals = [LegRisk(leg_idx=0, breaches_own_risk=True, assignment_looms=False)]
    decision = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=False
    )
    new_state = apply_decompose(
        store=store, multi_leg_id=mlid, decision=decision, legs_remaining_after=3
    )
    assert new_state == STATE_OPEN  # unchanged
    row = store.get(mlid)
    assert row is not None
    assert row.state == STATE_OPEN


# ---------------------------------------------------------------------------
# 6. THESIS-INVALIDATION + ASSIGNMENT-LOOMS triggers (reuse options_exit signals)
# ---------------------------------------------------------------------------


def test_thesis_invalidation_triggers_full_decompose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composite-level thesis invalidation triggers a decompose of ALL legs (the whole
    structure is no longer wanted as a combo)."""
    _enable(monkeypatch)
    legs = _iron_condor_legs()
    # No per-leg risk breach, but the composite thesis is invalidated.
    leg_signals = [
        LegRisk(leg_idx=i, breaches_own_risk=False, assignment_looms=False)
        for i in range(4)
    ]
    d = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=True
    )
    assert d["decompose"] is True
    assert sorted(d["legs_to_independently_manage"]) == [0, 1, 2, 3]
    assert "thesis" in d["reason"].lower()


def test_assignment_looms_on_short_leg_triggers_decompose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assignment looming on a SHORT leg (Rule 4 delta-breach / Rule 5 extrinsic-floor from
    options_exit) triggers a decompose that independently manages that short leg."""
    _enable(monkeypatch)
    legs = _iron_condor_legs()
    leg_signals = [
        LegRisk(leg_idx=0, breaches_own_risk=False, assignment_looms=True),
        LegRisk(leg_idx=1, breaches_own_risk=False, assignment_looms=False),
        LegRisk(leg_idx=2, breaches_own_risk=False, assignment_looms=False),
        LegRisk(leg_idx=3, breaches_own_risk=False, assignment_looms=False),
    ]
    d = decompose_decision(
        legs=legs, leg_signals=leg_signals, thesis_invalidated=False
    )
    assert d["decompose"] is True
    assert d["legs_to_independently_manage"] == [0]
    assert "assignment" in d["reason"].lower()


def test_leg_risk_from_options_exit_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LegRisk.from_exit_decision adapter maps a real OptionsExitDecision (the tp3/tp1-2
    core) into the leg-op signal so the decompose reuses options_exit's verdicts rather than
    duplicating exit logic."""
    from hermes_quant.risk.options_exit import ShortLegState, evaluate_options_exit

    # A short leg with a delta breach -> options_exit fires Rule 4 (delta_breach).
    exit_decision = evaluate_options_exit(
        net_pnl=-10.0,
        initial_credit=200.0,
        dte=30,
        short_legs=[ShortLegState(delta=0.55, extrinsic_value=1.0)],
    )
    assert exit_decision.should_close is True
    assert exit_decision.which_rule == "delta_breach"

    lr = LegRisk.from_exit_decision(leg_idx=0, decision=exit_decision)
    # A delta_breach on a short leg is an assignment-risk signal.
    assert lr.assignment_looms is True
    assert lr.leg_idx == 0


def test_composite_has_naked_side_detects_naked_short() -> None:
    """A short put with no protective long put is a naked side; a covered short (spread) is
    not. This is the no-naked predicate the convert + risk-adjust guards rely on."""
    # Naked short put (no long put).
    naked = [_leg(_SHORT_PUT, "sell", "sell_to_open", -0.30)]
    assert composite_has_naked_side(naked) is True
    # Bull put spread (short put + long put) -> covered, not naked.
    assert composite_has_naked_side(_bull_put_spread_legs()) is False
    # Covered call (short call covered by stock) -> not naked.
    covered_call = [
        StockLeg(underlying="AAPL", qty=100, basis_per_share=200.0),
        _leg(_SHORT_CALL, "sell", "sell_to_open", 0.30),
    ]
    assert composite_has_naked_side(covered_call) is False
