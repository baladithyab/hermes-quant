"""Parity grid for the kelly.py port into pdr_core (ADR-0092 Increment-1-cont).

STAGE 1 of the gate port: ``hermes_quant.risk.kelly`` is moved VERBATIM (pure
``git mv``) to ``hermes_quant.pdr_core.kelly`` and a back-compat re-export shim
is left at the old path. kelly.py is provably pure (imports only ``__future__``
and ``math``), so the move cannot perturb the purity gate.

This file is the safety proof for the move: a captured baseline matrix of
function outputs, snapshotted from the pre-move ``hermes_quant.risk.kelly``,
that the MOVED functions must reproduce BIT-FOR-BIT. It also proves:

  - the back-compat shim path still imports every public symbol, and
  - the shim re-exports the SAME function objects (identity), so every existing
    importer (risk/gate.py, bma, react fill-size invariant, ...) is unaffected.

RED-first contract: with ``hermes_quant.pdr_core.kelly`` absent, the module-level
import below fails at collection time → every test in this file errors. The
``git mv`` + shim turns it GREEN.

Baseline values were captured from the pre-move module on the host venv; floats
are stored as exact ``repr`` strings and compared with an exact ``==`` on the
re-parsed float (NaN handled explicitly) so any arithmetic drift is caught.
"""

from __future__ import annotations

import math

import pytest

# --- the MOVED module (new home) -------------------------------------------
from hermes_quant.pdr_core.kelly import (
    cost_gate_threshold,
    expected_log_return,
    expected_signed_edge,
    quarter_kelly_size,
    round_to_step,
)

# --- the back-compat shim (old home) ---------------------------------------
from hermes_quant.risk import kelly as kelly_shim

NAN = float("nan")
INF = float("inf")


def _f(token: str) -> float:
    """Re-parse a baseline token (handles nan/inf/-inf) into a float."""
    if token == "nan":
        return NAN
    if token == "inf":
        return INF
    if token == "-inf":
        return -INF
    return float(token)


def _eq(actual: float, expected: float) -> bool:
    """Exact float equality, but NaN == NaN (since the source can return NaN)."""
    if isinstance(expected, float) and math.isnan(expected):
        return isinstance(actual, float) and math.isnan(actual)
    return actual == expected


# ---------------------------------------------------------------------------
# Captured baselines (pre-move hermes_quant.risk.kelly, host venv).
# Each row's last element is the expected output as an exact repr token.
# ---------------------------------------------------------------------------

# expected_log_return(p, m)
ELR_BASELINE = [
    (0.0, -0.01, "0.0"), (0.0, 0.0, "0.0"),
    (0.0, 0.0005, "-0.0005001250416822979"), (0.0, 0.001, "-0.0010005003335835335"),
    (0.0, 0.01, "-0.010050335853501442"), (0.0, 0.1, "-0.10536051565782631"),
    (0.0, 0.3, "-0.35667494393873234"), (0.0, 1.0, "-1.0"), (0.0, 1.5, "-1.5"),
    (0.0, NAN, "nan"), (0.0, INF, "-inf"),
    (0.1, -0.01, "0.0"), (0.1, 0.0, "0.0"),
    (0.1, 0.0005, "-0.00040012503334896333"), (0.1, 0.001, "-0.0008005002669168267"),
    (0.1, 0.01, "-0.00805026918283449"), (0.1, 0.1, "-0.08529344611161119"),
    (0.1, 0.3, "-0.29477102309811"), (0.1, 1.0, "-0.8"), (0.1, 1.5, "-1.2000000000000002"),
    (0.1, NAN, "nan"), (0.1, INF, "-inf"),
    (0.4, -0.01, "0.0"), (0.4, 0.0, "0.0"),
    (0.4, 0.0005, "-0.00010012500834895955"), (0.4, 0.001, "-0.00020050006691670682"),
    (0.4, 0.01, "-0.002050069170833632"), (0.4, 0.1, "-0.025092237472965837"),
    (0.4, 0.3, "-0.10905926057624298"), (0.4, 1.0, "-0.19999999999999996"),
    (0.4, 1.5, "-0.29999999999999993"), (0.4, NAN, "nan"), (0.4, INF, "-inf"),
    (0.5, -0.01, "0.0"), (0.5, 0.0, "0.0"),
    (0.5, 0.0005, "-1.250000156249962e-07"), (0.5, 0.001, "-5.000002500001738e-07"),
    (0.5, 0.01, "-5.000250016667929e-05"), (0.5, 0.1, "-0.005025167926750722"),
    (0.5, 0.3, "-0.04715533973562064"), (0.5, 1.0, "0.0"), (0.5, 1.5, "0.0"),
    (0.5, NAN, "nan"), (0.5, INF, "nan"),
    (0.6, -0.01, "0.0"), (0.6, 0.0, "0.0"),
    (0.6, 0.0005, "9.987500831770956e-05"), (0.6, 0.001, "0.00019950006641670641"),
    (0.6, 0.01, "0.0019500641705002732"), (0.6, 0.1, "0.015041901619464386"),
    (0.6, 0.3, "0.014748581105001685"), (0.6, 1.0, "0.19999999999999996"),
    (0.6, 1.5, "0.29999999999999993"), (0.6, NAN, "nan"), (0.6, INF, "inf"),
    (0.7, -0.01, "0.0"), (0.7, 0.0, "0.0"),
    (0.7, 0.0005, "0.00019987501665104412"), (0.7, 0.001, "0.000399500133083413"),
    (0.7, 0.01, "0.003950130841167225"), (0.7, 0.1, "0.03510897116567951"),
    (0.7, 0.3, "0.07665250194562402"), (0.7, 1.0, "0.3999999999999999"),
    (0.7, 1.5, "0.5999999999999999"), (0.7, NAN, "nan"), (0.7, INF, "inf"),
    (0.9, -0.01, "0.0"), (0.9, 0.0, "0.0"),
    (0.9, 0.0005, "0.0003998750333177134"), (0.9, 0.001, "0.0007995002664168265"),
    (0.9, 0.01, "0.00795026418250113"), (0.9, 0.1, "0.07524311025810974"),
    (0.9, 0.3, "0.20046034362686874"), (0.9, 1.0, "0.8"), (0.9, 1.5, "1.2000000000000002"),
    (0.9, NAN, "nan"), (0.9, INF, "inf"),
    (1.0, -0.01, "0.0"), (1.0, 0.0, "0.0"),
    (1.0, 0.0005, "0.000499875041651048"), (1.0, 0.001, "0.0009995003330835331"),
    (1.0, 0.01, "0.009950330853168083"), (1.0, 0.1, "0.09531017980432487"),
    (1.0, 0.3, "0.26236426446749106"), (1.0, 1.0, "1.0"), (1.0, 1.5, "1.5"),
    (1.0, NAN, "nan"), (1.0, INF, "inf"),
    # out-of-[0,1] probabilities are caller error; not validated in this layer
    (-0.1, -0.01, "0.0"), (-0.1, 0.0, "0.0"),
    (-0.1, 0.0005, "-0.0005001250416822979"), (-0.1, 0.001, "-0.0010005003335835335"),
    (-0.1, 0.01, "-0.010050335853501442"), (-0.1, 0.1, "-0.10536051565782631"),
    (-0.1, 0.3, "-0.35667494393873234"), (-0.1, 1.0, "-1.2"),
    (-0.1, 1.5, "-1.7999999999999998"), (-0.1, NAN, "nan"), (-0.1, INF, "-inf"),
    (1.1, -0.01, "0.0"), (1.1, 0.0, "0.0"),
    (1.1, 0.0005, "0.000499875041651048"), (1.1, 0.001, "0.0009995003330835331"),
    (1.1, 0.01, "0.009950330853168083"), (1.1, 0.1, "0.09531017980432487"),
    (1.1, 0.3, "0.26236426446749106"), (1.1, 1.0, "1.2000000000000002"),
    (1.1, 1.5, "1.8000000000000003"), (1.1, NAN, "nan"), (1.1, INF, "inf"),
]

# expected_signed_edge(direction, p, m)
ESE_BASELINE = [
    (-1, 0.4, -0.01, "0.002050069170833632"), (-1, 0.4, 0.0, "-0.0"),
    (-1, 0.4, 0.01, "0.002050069170833632"), (-1, 0.4, 0.1, "0.025092237472965837"),
    (-1, 0.5, -0.01, "5.000250016667929e-05"), (-1, 0.5, 0.0, "-0.0"),
    (-1, 0.5, 0.01, "5.000250016667929e-05"), (-1, 0.5, 0.1, "0.005025167926750722"),
    (-1, 0.6, -0.01, "-0.0019500641705002732"), (-1, 0.6, 0.0, "-0.0"),
    (-1, 0.6, 0.01, "-0.0019500641705002732"), (-1, 0.6, 0.1, "-0.015041901619464386"),
    (-1, 0.7, -0.01, "-0.003950130841167225"), (-1, 0.7, 0.0, "-0.0"),
    (-1, 0.7, 0.01, "-0.003950130841167225"), (-1, 0.7, 0.1, "-0.03510897116567951"),
    (0, 0.4, -0.01, "0.0"), (0, 0.4, 0.0, "0.0"), (0, 0.4, 0.01, "0.0"), (0, 0.4, 0.1, "0.0"),
    (0, 0.5, -0.01, "0.0"), (0, 0.5, 0.0, "0.0"), (0, 0.5, 0.01, "0.0"), (0, 0.5, 0.1, "0.0"),
    (0, 0.6, -0.01, "0.0"), (0, 0.6, 0.0, "0.0"), (0, 0.6, 0.01, "0.0"), (0, 0.6, 0.1, "0.0"),
    (0, 0.7, -0.01, "0.0"), (0, 0.7, 0.0, "0.0"), (0, 0.7, 0.01, "0.0"), (0, 0.7, 0.1, "0.0"),
    (1, 0.4, -0.01, "-0.002050069170833632"), (1, 0.4, 0.0, "0.0"),
    (1, 0.4, 0.01, "-0.002050069170833632"), (1, 0.4, 0.1, "-0.025092237472965837"),
    (1, 0.5, -0.01, "-5.000250016667929e-05"), (1, 0.5, 0.0, "0.0"),
    (1, 0.5, 0.01, "-5.000250016667929e-05"), (1, 0.5, 0.1, "-0.005025167926750722"),
    (1, 0.6, -0.01, "0.0019500641705002732"), (1, 0.6, 0.0, "0.0"),
    (1, 0.6, 0.01, "0.0019500641705002732"), (1, 0.6, 0.1, "0.015041901619464386"),
    (1, 0.7, -0.01, "0.003950130841167225"), (1, 0.7, 0.0, "0.0"),
    (1, 0.7, 0.01, "0.003950130841167225"), (1, 0.7, 0.1, "0.03510897116567951"),
]

# round_to_step(value, step)
RTS_BASELINE = [
    (0.0, 0.05, "0.0"), (0.0, 0.0, "0.0"), (0.0, -0.01, "0.0"), (0.0, 0.1, "0.0"),
    (0.073, 0.05, "0.05"), (0.073, 0.0, "0.073"), (0.073, -0.01, "0.073"), (0.073, 0.1, "0.1"),
    (0.078, 0.05, "0.1"), (0.078, 0.0, "0.078"), (0.078, -0.01, "0.078"), (0.078, 0.1, "0.1"),
    (-0.073, 0.05, "-0.05"), (-0.073, 0.0, "-0.073"), (-0.073, -0.01, "-0.073"), (-0.073, 0.1, "-0.1"),
    (-0.078, 0.05, "-0.1"), (-0.078, 0.0, "-0.078"), (-0.078, -0.01, "-0.078"), (-0.078, 0.1, "-0.1"),
    (0.025, 0.05, "0.0"), (0.025, 0.0, "0.025"), (0.025, -0.01, "0.025"), (0.025, 0.1, "0.0"),
    (0.2, 0.05, "0.2"), (0.2, 0.0, "0.2"), (0.2, -0.01, "0.2"), (0.2, 0.1, "0.2"),
    (-0.2, 0.05, "-0.2"), (-0.2, 0.0, "-0.2"), (-0.2, -0.01, "-0.2"), (-0.2, 0.1, "-0.2"),
    (0.999, 0.05, "1.0"), (0.999, 0.0, "0.999"), (0.999, -0.01, "0.999"), (0.999, 0.1, "1.0"),
]

# quarter_kelly_size(edge, variance, *, quarter_kelly, max_position_pct, action_step, direction)
# row: (edge_token, variance, direction, quarter_kelly, max_position_pct, action_step, expected_token)
QKS_BASELINE = [
    ("0.001", 0.0001, 1, 0.25, 0.20, 0.05, "0.2"),
    ("0.0", 0.0001, 1, 0.25, 0.20, 0.05, "0.0"),
    ("0.001", 0.0001, 0, 0.25, 0.20, 0.05, "0.0"),
    ("0.002", 0.0001, 1, 0.25, 0.20, 0.05, "0.2"),
    ("1.0", 0.0001, 1, 0.25, 0.10, 0.05, "0.1"),
    ("-1.0", 0.0001, -1, 0.25, 0.10, 0.05, "-0.1"),
    ("1e-09", 0.01, 1, 0.25, 0.20, 0.05, "0.0"),
    ("0.001", -1.0, 1, 0.25, 0.20, 0.05, "0.2"),
    ("nan", 0.0001, 1, 0.25, 0.20, 0.05, "0.0"),
    ("0.001", INF, 1, 0.25, 0.20, 0.05, "0.0"),
    ("inf", 0.0001, 1, 0.25, 0.20, 0.05, "0.0"),
    ("0.0006", 0.0001, 1, 0.25, 0.20, 0.05, "0.2"),
    ("-0.0006", 0.0001, -1, 0.25, 0.20, 0.05, "-0.2"),
]

# cost_gate_threshold(commission, spread, slippage, cost_multiple)
CGT_BASELINE = [
    (0.001, 0.0008, 0.0012, 2.0, "0.0052"),
    (0.0, 0.0, 0.0, 2.0, "0.0"),
    (0.001, 0.0008, 0.0012, 1.0, "0.0026"),
    (0.001, 0.0008, 0.0012, 3.0, "0.0078"),
]


# ---------------------------------------------------------------------------
# Parity grid: MOVED functions reproduce the captured baseline bit-for-bit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("p,m,expected", ELR_BASELINE)
def test_expected_log_return_parity(p, m, expected):
    got = expected_log_return(p, m)
    exp = _f(expected)
    assert _eq(got, exp), f"expected_log_return({p},{m}) -> {got!r} != {exp!r}"


@pytest.mark.parametrize("d,p,m,expected", ESE_BASELINE)
def test_expected_signed_edge_parity(d, p, m, expected):
    got = expected_signed_edge(d, p, m)
    exp = _f(expected)
    assert _eq(got, exp), f"expected_signed_edge({d},{p},{m}) -> {got!r} != {exp!r}"


@pytest.mark.parametrize("v,s,expected", RTS_BASELINE)
def test_round_to_step_parity(v, s, expected):
    got = round_to_step(v, s)
    exp = _f(expected)
    assert _eq(got, exp), f"round_to_step({v},{s}) -> {got!r} != {exp!r}"


@pytest.mark.parametrize(
    "edge_tok,variance,direction,qk,mx,step,expected", QKS_BASELINE
)
def test_quarter_kelly_size_parity(edge_tok, variance, direction, qk, mx, step, expected):
    edge = _f(edge_tok)
    got = quarter_kelly_size(
        edge,
        variance,
        quarter_kelly=qk,
        max_position_pct=mx,
        action_step=step,
        direction=direction,
    )
    exp = _f(expected)
    assert _eq(got, exp), (
        f"quarter_kelly_size({edge!r},{variance!r},qk={qk},mx={mx},"
        f"step={step},dir={direction}) -> {got!r} != {exp!r}"
    )


@pytest.mark.parametrize("c,sp,sl,cm,expected", CGT_BASELINE)
def test_cost_gate_threshold_parity(c, sp, sl, cm, expected):
    got = cost_gate_threshold(c, sp, sl, cost_multiple=cm)
    exp = _f(expected)
    assert _eq(got, exp), f"cost_gate_threshold({c},{sp},{sl},cm={cm}) -> {got!r} != {exp!r}"


# ---------------------------------------------------------------------------
# Back-compat shim: old import path still works AND is the same objects.
# ---------------------------------------------------------------------------

PUBLIC_SYMBOLS = (
    "expected_log_return",
    "expected_signed_edge",
    "round_to_step",
    "quarter_kelly_size",
    "cost_gate_threshold",
)


def test_shim_exports_every_public_symbol():
    """hermes_quant.risk.kelly must still expose every public callable."""
    for name in PUBLIC_SYMBOLS:
        assert hasattr(kelly_shim, name), f"shim missing {name}"
        assert callable(getattr(kelly_shim, name)), f"shim.{name} not callable"


def test_shim_reexports_same_function_objects():
    """The shim re-exports the MOVED function objects (identity), so existing
    importers (risk/gate.py:43, bma, react fill-size invariant) bind to the
    exact same implementation — no behavioral fork is possible."""
    from hermes_quant.pdr_core import kelly as kelly_core

    for name in PUBLIC_SYMBOLS:
        assert getattr(kelly_shim, name) is getattr(kelly_core, name), (
            f"shim.{name} is not the moved pdr_core.kelly.{name} (identity broke)"
        )


def test_legacy_importer_signature_still_resolves():
    """The exact symbols risk/gate.py imports from hermes_quant.risk.kelly must
    resolve through the shim (cost_gate_threshold, expected_signed_edge,
    quarter_kelly_size) AND compute identically to the core path."""
    _cgt = kelly_shim.cost_gate_threshold
    _ese = kelly_shim.expected_signed_edge
    _qks = kelly_shim.quarter_kelly_size

    assert _ese(1, 0.6, 0.01) == expected_signed_edge(1, 0.6, 0.01)
    assert _qks(0.002, 0.0001, direction=1) == quarter_kelly_size(
        0.002, 0.0001, direction=1
    )
    assert _cgt(0.001, 0.0008, 0.0012) == cost_gate_threshold(0.001, 0.0008, 0.0012)
