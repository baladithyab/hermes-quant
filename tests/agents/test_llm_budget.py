"""tests/agents/test_llm_budget.py — LLMBudgetGuard unit tests (B41-a).

ADR-4665 §7.1/§7b: per-stage LLM cost ceiling + zero-call kill-switch.

Coverage (maps to lane deliverables):
  (b) per-tick AND per-decision ceiling both enforced
  (c) durability — cumulative spend survives a simulated restart (reload)
  (d) corrupt spend file → fail-closed (treated as exhausted)
  + max_tokens clamp / reject-when-omitted
  + child-cost-counts-against-parent (sub-calls share one decision bucket)
  + zero-call kill-switch (ceiling == 0 blocks the first call)

All tests are offline and write only into ``tmp_path`` — never the real
``~/.hermes/quant``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.agents.llm_budget import (
    BudgetCeilings,
    LLMBudgetGuard,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guard(
    tmp_path: Path,
    *,
    per_decision_usd: float | None = None,
    per_tick_usd: float | None = None,
    per_decision_tokens: int | None = None,
    per_tick_tokens: int | None = None,
) -> LLMBudgetGuard:
    """Construct a guard with a known, cheap price so cost math is predictable.

    The price table is pinned so 1,000 prompt tokens + 1,000 completion tokens
    cost exactly $0.002 (=$0.001/1k in + $0.001/1k out) for the test model.
    """
    return LLMBudgetGuard(
        ceilings=BudgetCeilings(
            per_decision_usd=per_decision_usd,
            per_tick_usd=per_tick_usd,
            per_decision_tokens=per_decision_tokens,
            per_tick_tokens=per_tick_tokens,
        ),
        price_table={"test/model": (0.001, 0.001)},
        default_price=(1.0, 1.0),  # expensive unknown-model fallback (fail-safe)
        path=tmp_path / "spend.json",
    )


# ---------------------------------------------------------------------------
# (b) per-decision ceiling enforced
# ---------------------------------------------------------------------------


def test_per_decision_usd_ceiling_blocks_second_call(tmp_path: Path) -> None:
    # $0.0011 per-decision ceiling. One call of 1k prompt + 500 completion
    # tokens = (1000/1000)*0.001 + (500/1000)*0.001 = $0.0015 worst-case
    # projection. That already exceeds $0.0011 on the FIRST call.
    g = _guard(tmp_path, per_decision_usd=0.0011)
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1000,
        max_tokens=500,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.reason == "decision_usd"


def test_per_decision_usd_ceiling_allows_then_blocks_after_record(tmp_path: Path) -> None:
    # Ceiling $0.003. First call projects $0.0015 → allowed. After recording
    # actual $0.002, a second identical call would project to $0.0035 cumulative
    # → blocked.
    g = _guard(tmp_path, per_decision_usd=0.003)
    chk1 = g.check(
        model_id="test/model",
        prompt_tokens=1000,
        max_tokens=500,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk1.allowed is True
    g.record(
        model_id="test/model",
        prompt_tokens=1000,
        completion_tokens=1000,  # actual $0.002
        decision_id="dec-1",
        tick_id="tick-1",
    )
    chk2 = g.check(
        model_id="test/model",
        prompt_tokens=1000,
        max_tokens=500,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk2.allowed is False
    assert chk2.reason == "decision_usd"


# ---------------------------------------------------------------------------
# (b) per-tick ceiling enforced ACROSS decisions
# ---------------------------------------------------------------------------


def test_per_tick_usd_ceiling_spans_multiple_decisions(tmp_path: Path) -> None:
    # Per-tick ceiling $0.0025; NO per-decision ceiling. Decision A spends
    # $0.002; decision B (same tick) then projects over the tick ceiling.
    g = _guard(tmp_path, per_tick_usd=0.0025)
    g.record(
        model_id="test/model",
        prompt_tokens=1000,
        completion_tokens=1000,  # $0.002 on the tick
        decision_id="dec-A",
        tick_id="tick-1",
    )
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1000,
        max_tokens=1000,  # projects +$0.002 → $0.004 tick cumulative
        decision_id="dec-B",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.reason == "tick_usd"


def test_per_tick_token_ceiling_clamps_allowed_max_tokens(tmp_path: Path) -> None:
    # Token ceiling 1200 per tick; 1000 already spent → 200 tokens remain.
    # The new call's 50 prompt tokens also count against the ceiling, so the
    # completion cap (max_tokens) is clamped to 200 - 50 = 150.
    g = _guard(tmp_path, per_tick_tokens=1200)
    g.record(
        model_id="test/model",
        prompt_tokens=600,
        completion_tokens=400,  # 1000 tokens on the tick
        decision_id="dec-A",
        tick_id="tick-1",
    )
    chk = g.check(
        model_id="test/model",
        prompt_tokens=50,
        max_tokens=800,  # requests 800 but only 150 fit under the ceiling
        decision_id="dec-B",
        tick_id="tick-1",
    )
    assert chk.allowed is True
    assert chk.allowed_max_tokens == 150


def test_token_ceiling_exhausted_blocks(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_tick_tokens=1000)
    g.record(
        model_id="test/model",
        prompt_tokens=600,
        completion_tokens=400,
        decision_id="dec-A",
        tick_id="tick-1",
    )
    chk = g.check(
        model_id="test/model",
        prompt_tokens=10,
        max_tokens=100,
        decision_id="dec-B",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.reason == "tick_tokens"


# ---------------------------------------------------------------------------
# Zero-call kill-switch: a ceiling of 0 blocks the very first call.
# ---------------------------------------------------------------------------


def test_zero_usd_ceiling_is_kill_switch(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_tick_usd=0.0)
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1,
        max_tokens=1,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.allowed_max_tokens == 0


def test_zero_token_ceiling_is_kill_switch(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_tick_tokens=0)
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1,
        max_tokens=1,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False


# ---------------------------------------------------------------------------
# max_tokens enforcement: a call that omits max_tokens is rejected.
# ---------------------------------------------------------------------------


def test_missing_max_tokens_is_rejected(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_tick_usd=1.0)
    chk = g.check(
        model_id="test/model",
        prompt_tokens=10,
        max_tokens=None,  # caller omitted it
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.reason == "no_max_tokens"


def test_nonpositive_max_tokens_is_rejected(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_tick_usd=1.0)
    chk = g.check(
        model_id="test/model",
        prompt_tokens=10,
        max_tokens=0,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False


# ---------------------------------------------------------------------------
# (c) durability — cumulative spend survives a simulated restart.
# ---------------------------------------------------------------------------


def test_spend_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    g1 = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=0.003),
        price_table={"test/model": (0.001, 0.001)},
        path=path,
    )
    g1.record(
        model_id="test/model",
        prompt_tokens=1000,
        completion_tokens=1000,  # $0.002
        decision_id="dec-1",
        tick_id="tick-1",
    )

    # Simulate a process restart: a brand-new guard pointed at the same file
    # must observe the previously-recorded spend.
    g2 = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=0.003),
        price_table={"test/model": (0.001, 0.001)},
        path=path,
    )
    chk = g2.check(
        model_id="test/model",
        prompt_tokens=1000,
        max_tokens=1000,  # +$0.002 → $0.004 cumulative > $0.003
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.spent_tick_usd == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# (d) corrupt spend file → fail-closed (treated as exhausted).
# ---------------------------------------------------------------------------


def test_corrupt_spend_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    path.write_text("{ this is not valid json ::::", encoding="utf-8")
    g = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=1.0),  # generous ceiling
        price_table={"test/model": (0.001, 0.001)},
        path=path,
    )
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1,
        max_tokens=1,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.reason == "fail_closed"
    assert chk.allowed_max_tokens == 0


def test_unreadable_spend_file_fails_closed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "spend.json"
    path.write_text('{"ticks":{},"decisions":{}}', encoding="utf-8")
    g = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=1.0),
        price_table={"test/model": (0.001, 0.001)},
        path=path,
    )

    def _boom(*_a, **_k):
        raise OSError("disk gone")

    # Force the read to fail at call time.
    monkeypatch.setattr(Path, "read_text", _boom)
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1,
        max_tokens=1,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False
    assert chk.reason == "fail_closed"


# ---------------------------------------------------------------------------
# Child-cost-counts-against-parent: N sub-calls under one decision_id all bill
# to the same decision + tick bucket.
# ---------------------------------------------------------------------------


def test_child_costs_accumulate_against_parent_decision(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_decision_usd=0.01, per_tick_usd=0.05)
    # 4 debate sub-calls, all under decision "parent" / tick "t1".
    for _ in range(4):
        g.record(
            model_id="test/model",
            prompt_tokens=500,
            completion_tokens=500,  # $0.001 each → $0.004 after 4
            decision_id="parent",
            tick_id="t1",
        )
    snap = g.snapshot(decision_id="parent", tick_id="t1")
    assert snap["decision_usd"] == pytest.approx(0.004)
    assert snap["tick_usd"] == pytest.approx(0.004)
    assert snap["decision_calls"] == 4


# ---------------------------------------------------------------------------
# Unknown model uses the (expensive) default price → fail-safe.
# ---------------------------------------------------------------------------


def test_unknown_model_uses_expensive_default_price(tmp_path: Path) -> None:
    g = _guard(tmp_path, per_tick_usd=0.5)
    # default_price=(1.0, 1.0) → 1k prompt + 1k completion = $2.00, well over
    # the $0.5 ceiling. An unknown model must be billed conservatively.
    chk = g.check(
        model_id="mystery/model-9000",
        prompt_tokens=1000,
        max_tokens=1000,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is False


# ---------------------------------------------------------------------------
# No ceilings configured → guard is inert (everything allowed, max_tokens kept).
# ---------------------------------------------------------------------------


def test_no_ceilings_allows_with_requested_max_tokens(tmp_path: Path) -> None:
    g = _guard(tmp_path)  # all ceilings None
    chk = g.check(
        model_id="test/model",
        prompt_tokens=1000,
        max_tokens=777,
        decision_id="dec-1",
        tick_id="tick-1",
    )
    assert chk.allowed is True
    assert chk.allowed_max_tokens == 777


# ---------------------------------------------------------------------------
# from_env: default-OFF; only constructs a guard when the flag is set.
# ---------------------------------------------------------------------------


def test_from_env_returns_none_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_LLM_BUDGET", raising=False)
    assert LLMBudgetGuard.from_env() is None


def test_from_env_builds_guard_when_flag_on(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_QUANT_LLM_BUDGET", "1")
    monkeypatch.setenv("HERMES_QUANT_LLM_BUDGET_PER_TICK_USD", "0.25")
    monkeypatch.setenv("HERMES_QUANT_LLM_BUDGET_DIR", str(tmp_path / "budget"))
    g = LLMBudgetGuard.from_env()
    assert g is not None
    assert g.ceilings.per_tick_usd == pytest.approx(0.25)
