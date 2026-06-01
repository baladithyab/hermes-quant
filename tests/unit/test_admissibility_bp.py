"""Unit tests for RR8 (review-reconcile): admissibility buying-power fail-closed branches.

Two seams the review proved regression-blind:

  (1) oracle.live_buying_power() — the live paper-account BP fetch. It is FAIL-CLOSED:
      missing creds, any client/API error, or a non-positive value all return None so the
      caller's BP hard check fails-closed (MISSING_ACCOUNT_CONTEXT) rather than admitting a
      short on a fabricated sufficiency (ADR-0077 D77). Branches under test:
        * creds absent          -> None  (RuntimeError from _resolve_client, swallowed)
        * positive BP           -> the float value
        * zero / negative BP    -> None  (the `bp if bp > 0 else None` guard)
        * get_account() raises  -> None  (broad fail-closed except)

  (2) gate_order.admit_or_reject(available_bp=None) — with the flag ON and a live-like ETB
      oracle, a short whose account_equity clears the floor but whose available_bp is absent
      REJECTS with MISSING_ACCOUNT_CONTEXT (the documented BP gap; never an assumed pass).

Deterministic: no network. The live client is faked via monkeypatch; the oracle is injected.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import hermes_quant.admissibility.gate_order as gate_order
import hermes_quant.admissibility.oracle as oracle_mod
from hermes_quant.admissibility import (
    AdmissibilityContext,
    AdmissibilityState,
    admit_or_reject,
    evaluate_admissibility,
)
from hermes_quant.admissibility.oracle import live_buying_power

_ASOF = datetime(2026, 5, 30, tzinfo=UTC)

_CRED_KEYS = (
    "ALPACA_API_KEY",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET",
    "ALPACA_API_SECRET_KEY",
)


def _clear_creds(monkeypatch):
    for k in _CRED_KEYS:
        monkeypatch.delenv(k, raising=False)


class _FakeAccount:
    def __init__(self, buying_power):
        self.buying_power = buying_power


class _FakeClientOracle:
    """Stands in for AlpacaShortabilityOracle inside live_buying_power(): a _resolve_client()
    returning a fake client with get_account(). buying_power is the value the test injects."""

    def __init__(self, *args, **kwargs):
        self._buying_power = _FakeClientOracle._next_bp
        self._raise = _FakeClientOracle._next_raise

    # class-level slots set by the helper below (live_buying_power builds the oracle itself,
    # so we can't pass ctor args — stash on the class for the next construction).
    _next_bp = None
    _next_raise = None

    def _resolve_client(self):
        if self._raise is not None:
            raise self._raise

        outer_bp = self._buying_power

        class _Client:
            def get_account(self):
                return _FakeAccount(outer_bp)

        return _Client()


def _install_fake_oracle(monkeypatch, *, buying_power=None, raise_on_client=None):
    _FakeClientOracle._next_bp = buying_power
    _FakeClientOracle._next_raise = raise_on_client
    monkeypatch.setattr(oracle_mod, "AlpacaShortabilityOracle", _FakeClientOracle)


# --------------------------------------------------------------------------- #
# (1) live_buying_power() fail-closed branches
# --------------------------------------------------------------------------- #


def test_live_bp_creds_absent_returns_none(monkeypatch):
    """No ALPACA creds => _resolve_client raises RuntimeError => fail-closed None. This is
    the real production default (creds live only in the live daemon env)."""
    _clear_creds(monkeypatch)
    # Use the real AlpacaShortabilityOracle (no fake): _resolve_client raises on missing creds.
    assert live_buying_power() is None


def test_live_bp_positive_returns_value(monkeypatch):
    """A positive buying_power off get_account() is returned verbatim as a float."""
    _install_fake_oracle(monkeypatch, buying_power="125000.50")
    assert live_buying_power() == pytest.approx(125000.50)


def test_live_bp_zero_returns_none(monkeypatch):
    """Zero BP => None (the `bp if bp > 0 else None` guard): a zero-BP account cannot fund a
    short, so it fails-closed rather than reporting 0.0 as a usable buying power."""
    _install_fake_oracle(monkeypatch, buying_power="0")
    assert live_buying_power() is None


def test_live_bp_negative_returns_none(monkeypatch):
    """Negative BP (margin call / debit) => None, fail-closed."""
    _install_fake_oracle(monkeypatch, buying_power="-500.0")
    assert live_buying_power() is None


def test_live_bp_account_fetch_error_returns_none(monkeypatch):
    """Any error fetching the account => None (the broad fail-closed except). Never raises."""
    _install_fake_oracle(monkeypatch, raise_on_client=RuntimeError("API 500"))
    assert live_buying_power() is None


# --------------------------------------------------------------------------- #
# (2) admit_or_reject(available_bp=None) rejects a short with MISSING_ACCOUNT_CONTEXT
# --------------------------------------------------------------------------- #


class _LiveLikeETBOracle:
    """An ETB asset delegating to the REAL deterministic core with the fail-closed default,
    so the ONLY thing that changes the verdict is the account/quote context the seam supplies."""

    def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
        populated = AdmissibilityContext(
            tradable=True,
            marginable=True,
            shortable=True,
            easy_to_borrow=True,
            current_ask=ctx.current_ask,
            account_equity=ctx.account_equity,
            available_bp=ctx.available_bp,
        )
        return evaluate_admissibility(symbol, side, qty, asof, populated)


def test_admit_or_reject_bp_none_rejects_short_missing_account_context(monkeypatch):
    """Flag ON + live-like ETB oracle: account_equity clears the < $2k floor (step 5), but
    available_bp=None (the documented gap) fails-closed on the BP hard check (step 8b) ->
    REJECTED / MISSING_ACCOUNT_CONTEXT, adjusted target flattened to 0.0. The short is never
    admitted on an assumed-sufficient BP."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _LiveLikeETBOracle())

    verdict = admit_or_reject(
        "GME", "short", -0.20, 100_000.0, 200.0, _ASOF,
        account_equity=100_000.0,  # available_bp left None: the gap
    )

    assert verdict.admitted is False
    assert verdict.state is AdmissibilityState.REJECTED
    assert verdict.reason == "MISSING_ACCOUNT_CONTEXT"
    assert verdict.adjusted_target_pct == 0.0  # REJECT-only -> flatten, never amplified
