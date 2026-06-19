"""ar10 — operator env-FLOAT coercions must reject non-finite values (the ar08/ar09 family, finished).

archaeology-convergence (wf_319d0dde) found ar08/ar09 hardened the autonomous.py thresholds but missed
THREE OTHER operator-env float() coercions that catch only ValueError — so `inf` / `1e400` (silently
overflows to inf, a realistic operator typo) / `nan` slip through:

  - _default_initial_cash (portfolio_state.py): the NAV source. A non-finite NAV crashes the tick via
    math.floor(inf) OverflowError in the admissibility path, OR bypasses the multileg gross cap
    (gross/inf == 0.0 -> "nothing to cap"). LIVE-MONEY rail. (P2)
  - tick_lock._default_timeout_s: inf -> deadline = monotonic()+inf -> the poll loop never terminates
    (spin-forever hang). (P3)
  - llm_budget._env_float: nan -> `projected > nan` is always False -> the LLM cost gate never trips. (P3)

FIX: reject non-finite at each coercion, falling back to the documented default (NAV/timeout) or None
(budget). Byte-identical for any finite configured value.
"""

from __future__ import annotations

import math

import pytest


def test_initial_cash_inf_falls_back_to_default(monkeypatch) -> None:
    import hermes_quant.state.portfolio_state as ps
    monkeypatch.setenv(ps._INITIAL_CASH_ENV, "inf")
    val = ps._default_initial_cash()
    assert math.isfinite(val), "a non-finite HERMES_QUANT_PAPER_INITIAL_CASH must not propagate"
    assert val == ps._DEFAULT_INITIAL_CASH


def test_initial_cash_overflow_typo_falls_back(monkeypatch) -> None:
    """`1e400` parses to inf without ValueError — the realistic operator typo."""
    import hermes_quant.state.portfolio_state as ps
    monkeypatch.setenv(ps._INITIAL_CASH_ENV, "1e400")
    val = ps._default_initial_cash()
    assert math.isfinite(val)
    assert val == ps._DEFAULT_INITIAL_CASH


def test_initial_cash_nan_and_nonpositive_fall_back(monkeypatch) -> None:
    import hermes_quant.state.portfolio_state as ps
    for bad in ("nan", "-100", "0"):
        monkeypatch.setenv(ps._INITIAL_CASH_ENV, bad)
        val = ps._default_initial_cash()
        assert val == ps._DEFAULT_INITIAL_CASH, f"{bad!r} must fall back to the default NAV"


def test_initial_cash_finite_positive_byte_identical(monkeypatch) -> None:
    import hermes_quant.state.portfolio_state as ps
    monkeypatch.setenv(ps._INITIAL_CASH_ENV, "50000")
    assert ps._default_initial_cash() == 50000.0


def test_tick_lock_timeout_inf_falls_back(monkeypatch) -> None:
    import hermes_quant.daemon.tick_lock as tl
    monkeypatch.setenv(tl._TIMEOUT_ENV, "inf")
    val = tl._default_timeout_s()
    assert math.isfinite(val), "a non-finite tick-lock timeout must not produce a never-terminating deadline"
    assert val == tl.DEFAULT_TIMEOUT_S


def test_tick_lock_timeout_finite_byte_identical(monkeypatch) -> None:
    import hermes_quant.daemon.tick_lock as tl
    monkeypatch.setenv(tl._TIMEOUT_ENV, "2.5")
    assert tl._default_timeout_s() == 2.5


def test_llm_budget_nan_ceiling_is_none(monkeypatch) -> None:
    import hermes_quant.agents.llm_budget as lb
    monkeypatch.setenv("HERMES_QUANT_LLM_BUDGET_PER_DECISION_USD", "nan")
    val = lb._env_float("HERMES_QUANT_LLM_BUDGET_PER_DECISION_USD")
    assert val is None, "a NaN budget ceiling must be treated as 'not set', not silently disable the gate"


def test_llm_budget_finite_byte_identical(monkeypatch) -> None:
    import hermes_quant.agents.llm_budget as lb
    monkeypatch.setenv("HERMES_QUANT_LLM_BUDGET_PER_DECISION_USD", "5.0")
    assert lb._env_float("HERMES_QUANT_LLM_BUDGET_PER_DECISION_USD") == 5.0
