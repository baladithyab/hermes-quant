"""ar16 — TOCTOU double-fire regression fence for quant_approve.

The HITL approve flow (hermes_quant/tools.py quant_approve) historically did a
non-atomic check-then-act:

    proposal = store.get(...)         # 1. read state == 'pending'
    if proposal.state != 'pending': ...# 2. gate on the read state
    reactor.execute(...)              # 3. FIRE the real paper fill
    store.approve(...)                # 4. advance state, AFTER the fire

Two CONCURRENT quant_approve calls for the SAME proposal_id both pass the
pending-check at step 2 (state hasn't advanced yet), so BOTH reach the
reactor.execute at step 3. The reactor stamps a FRESH asof_execution = now per
call, and the only idempotency in PortfolioState is keyed on
(proposal_id, asof_execution, ...) — so two distinct asof_execution values
produce two distinct dedup keys and BOTH fills are recorded. Capital moves
TWICE; only the second store.approve raises (state already advanced), but the
money already moved.

These tests reproduce the interleave deterministically (no real threads): the
reactor's first execute() RE-ENTERS quant_approve for the same proposal_id,
which is exactly the window where caller #2's pending-check runs before caller
#1 advances state. On the unfixed code the reactor fires TWICE (two real fills
on the executions bus). After the fix (atomic claim-before-fire) only ONE fill
lands; the re-entrant approve is rejected because the proposal was already
claimed out of `pending`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_quant.proposals import ProposalStore

# ---------------------------------------------------------------------------
# Fixtures / helpers (mirror tests/test_react_fill_size_invariant.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _paper_reactor_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")


def _advisor_result(kelly: float = 0.05) -> dict[str, Any]:
    return {
        "as_of": "2026-06-04T00:00:00Z",
        "decision_price": 200.0,
        "signal_id": "sig-toctou",
        "risk_gate": {
            "pass": True,
            "kelly_fraction": kelly,
            "recommended_action": "long_with_stop",
        },
        "caveats": [],
    }


def _bus_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProposalStore:
    import hermes_quant.proposals as proposals_module

    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    monkeypatch.setattr(proposals_module, "_default_store", store)
    return store


def _patch_executions_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import hermes_quant.daemon.signal_bus as bus_module
    import hermes_quant.react.paper as paper_module

    monkeypatch.setattr(paper_module, "EXECUTION_BUS_PATH", path)
    monkeypatch.setattr(bus_module, "EXECUTION_BUS_PATH", path)


def _set_pdr_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg_dir = tmp_path / ".hermes"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(f"quant:\n  pdr:\n    mode: {mode}\n")


# ---------------------------------------------------------------------------
# RED: concurrent double-approve must fire the reactor exactly once
# ---------------------------------------------------------------------------


def test_concurrent_quant_approve_same_proposal_fires_reactor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two interleaved approves of ONE proposal => exactly ONE real fill.

    Reproduces the TOCTOU window structurally: the reactor's FIRST execute()
    re-enters quant_approve for the same proposal_id (the interleave where the
    2nd caller's pending-check runs before the 1st advances state). On the
    unfixed code this drives the reactor twice -> two fills on the bus. The
    fix claims the proposal out of 'pending' ATOMICALLY before firing, so the
    re-entrant call is rejected and the reactor fires once.
    """
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)

    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id

    from hermes_quant.react import dispatch as dispatch_module
    from hermes_quant.tools import quant_approve

    real_select = dispatch_module.select_reactor
    fire_count = {"n": 0}
    reentrant_result: dict[str, Any] = {}

    def _counting_select(prop):  # noqa: ANN001
        reactor = real_select(prop)
        real_execute = reactor.execute

        def _execute(*a, **kw):  # noqa: ANN002, ANN003
            # On the FIRST fire, before doing the real work, re-enter
            # quant_approve for the SAME proposal_id. This is the concurrent
            # caller #2 whose pending-check happens inside caller #1's
            # check->fire->approve gap. We snapshot its result, then proceed
            # with the real fire for caller #1.
            fire_count["n"] += 1
            if fire_count["n"] == 1 and not reentrant_result:
                reentrant_result["out"] = quant_approve({"proposal_id": pid})
            return real_execute(*a, **kw)

        reactor.execute = _execute  # type: ignore[method-assign]
        return reactor

    monkeypatch.setattr(dispatch_module, "select_reactor", _counting_select)

    out = quant_approve({"proposal_id": pid})
    parsed = json.loads(out)

    # The reactor must have fired EXACTLY ONCE across both interleaved approves.
    assert fire_count["n"] == 1, (
        f"reactor fired {fire_count['n']} times for one proposal — TOCTOU "
        "double-fire (capital moved twice)"
    )
    # And exactly ONE real fill on the executions bus.
    records = _bus_records(bus)
    assert len(records) == 1, (
        f"{len(records)} fills on the executions bus for one proposal — "
        "TOCTOU double-fire"
    )

    # Exactly one of the two interleaved approves succeeds; the proposal ends
    # up in a terminal claimed/approved state (never back to pending).
    inner = reentrant_result.get("out")
    assert inner is not None
    inner_parsed = json.loads(inner)
    successes = [p for p in (parsed, inner_parsed) if p.get("success") is True]
    assert len(successes) == 1, (
        "exactly one of two concurrent approves must succeed; got "
        f"outer={parsed.get('success')} inner={inner_parsed.get('success')}"
    )
    assert store.get(pid).state == "approved"


# ---------------------------------------------------------------------------
# GREEN guard: the normal single approve still fires exactly once + advances
# ---------------------------------------------------------------------------


def test_single_quant_approve_fires_once_and_advances_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path unchanged: one approve => one fill, state -> approved."""
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)

    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id})
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed["state"] == "approved"
    assert len(_bus_records(bus)) == 1
    assert store.get(proposal.proposal_id).state == "approved"


def test_quant_approve_already_approved_is_rejected_and_no_extra_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SECOND, sequential approve of an already-approved proposal must not
    fire again (no extra fill) and must report state_mismatch."""
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)

    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )

    from hermes_quant.tools import quant_approve

    first = json.loads(quant_approve({"proposal_id": proposal.proposal_id}))
    assert first["success"] is True
    assert len(_bus_records(bus)) == 1

    second = json.loads(quant_approve({"proposal_id": proposal.proposal_id}))
    assert second["success"] is False
    assert second["error"] == "state_mismatch"
    # Still exactly one fill — the second approve did NOT re-fire.
    assert len(_bus_records(bus)) == 1


# ---------------------------------------------------------------------------
# Store-level primitive: two claim_for_approval calls => exactly one winner
# ---------------------------------------------------------------------------


def test_claim_for_approval_is_compare_and_set_single_winner(tmp_path: Path) -> None:
    """The atomic claim primitive: only the FIRST caller advances the proposal
    out of `pending`; the second raises ProposalStateError (lost the claim)."""
    from hermes_quant.proposals import ProposalStateError

    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id

    claimed = store.claim_for_approval(pid)
    assert claimed.state == "approved"

    with pytest.raises(ProposalStateError):
        store.claim_for_approval(pid)

    # The proposal is claimed exactly once; state is terminal-approved.
    assert store.get(pid).state == "approved"


def test_release_claim_only_re_pends_unfired_claim(tmp_path: Path) -> None:
    """release_claim restores an unfired claim to pending, but NEVER re-pends a
    proposal that already carries an execution (would resurrect a fired fill)."""
    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(),
    )
    pid = proposal.proposal_id

    # Unfired claim -> re-pend succeeds.
    store.claim_for_approval(pid)
    re_pended = store.release_claim(pid)
    assert re_pended is not None
    assert re_pended.state == "pending"
    assert store.get(pid).state == "pending"

    # Re-claim then attach an execution -> release refuses (fired).
    store.claim_for_approval(pid)
    store.record_execution(pid, execution={"asof_execution": "2026-06-04T00:00:01Z"})
    assert store.release_claim(pid) is None
    assert store.get(pid).state == "approved"
