"""tests/unit/test_target_weight.py — direct, importable resolver tests (P2-3).

Durable test surface for `hermes_quant.risk.target_weight.resolve_target_weight`.

Unlike tests/ops/test_quant_daily_interim_cap_target.py — which loads the
deployed ops script via importlib.util.spec_from_file_location and a
sys.executable swap to neutralize the script's execv guard — this file imports
the resolver DIRECTLY from the package. That harness was clever but fragile:
the original 2026-06-03 bug was a test/prod input divergence, and importing prod
code via a file-load harness reintroduces a thinner version of the SAME risk
(prod and tests resolving to different objects). Hoisting the resolver into an
importable module (review finding P2-3) lets prod and tests import the identical
object with no execv dance — this is that durable surface.

These are the 9 cases ported verbatim from the ops test, using the REAL
actionable shape the builder emits (signed kelly_fraction + trader_size_fraction
+ risk_silence_multiplier). The logic is a pure behavior-preserving hoist, so
the assertions are identical.
"""

from __future__ import annotations

import pytest

from hermes_quant.risk.target_weight import resolve_target_weight


# ---------------------------------------------------------------------------
# The canonical path: kelly_fraction (signed) x risk_silence_multiplier.
# This is the EXACT shape of today's real actionables.
# ---------------------------------------------------------------------------


def test_resolves_short_from_kelly_and_risk_mult():
    """MDB-shaped: kelly=-0.2, size=0.2, mult=0.5 -> -0.10, kelly path."""
    v = {
        "symbol": "MDB",
        "direction": -1,
        "kelly_fraction": -0.2,
        "trader_size_fraction": 0.2,
        "risk_silence_multiplier": 0.5,
    }
    weight, src = resolve_target_weight(v)
    assert weight == pytest.approx(-0.10)
    assert src == "kelly_x_riskmult"


def test_resolves_long_from_kelly_and_risk_mult():
    """B-shaped: kelly=+0.2, mult=0.5 -> +0.10."""
    v = {
        "symbol": "B",
        "direction": 1,
        "kelly_fraction": 0.2,
        "trader_size_fraction": 0.2,
        "risk_silence_multiplier": 0.5,
    }
    weight, src = resolve_target_weight(v)
    assert weight == pytest.approx(0.10)
    assert src == "kelly_x_riskmult"


def test_no_risk_mult_defaults_to_full_kelly():
    """Absent risk_silence_multiplier -> multiplier 1.0 (no shrink)."""
    v = {"symbol": "X", "direction": -1, "kelly_fraction": -0.2}
    weight, src = resolve_target_weight(v)
    assert weight == pytest.approx(-0.2)
    assert src == "kelly_x_riskmult"


def test_risk_mult_clamped_to_silence_only():
    """Committee can only silence (<=1.0); a >1 multiplier must be clamped."""
    v = {"symbol": "X", "direction": 1, "kelly_fraction": 0.2, "risk_silence_multiplier": 3.0}
    weight, _ = resolve_target_weight(v)
    assert weight == pytest.approx(0.2)  # clamped to 1.0x, NOT amplified to 0.6


# ---------------------------------------------------------------------------
# Explicit override + trader_size fallback.
# ---------------------------------------------------------------------------


def test_explicit_target_position_pct_wins():
    """If a caller set target_position_pct explicitly, use it verbatim."""
    v = {"symbol": "X", "direction": -1, "kelly_fraction": -0.2, "target_position_pct": -0.07}
    weight, src = resolve_target_weight(v)
    assert weight == pytest.approx(-0.07)
    assert src == "explicit"


def test_trader_size_fallback_when_no_kelly():
    """No kelly but a trader size + direction -> sign(direction)*|size|*mult."""
    v = {"symbol": "X", "direction": -1, "trader_size_fraction": 0.2, "risk_silence_multiplier": 0.5}
    weight, src = resolve_target_weight(v)
    assert weight == pytest.approx(-0.10)
    assert src == "tradersize_x_riskmult"


# ---------------------------------------------------------------------------
# THE GUARD — the heart of the regression. A missing size field must NOT be
# laundered into a 0.0 weight (which the cap reads as benign `zero_target`).
# ---------------------------------------------------------------------------


def test_missing_all_size_fields_returns_none_source():
    """No kelly, no trader size, no explicit -> (0.0, None). Source None is the
    signal the caller turns into a LOUD size_field_missing error, never a silence."""
    v = {"symbol": "BROKEN", "direction": -1}
    weight, src = resolve_target_weight(v)
    assert weight == 0.0
    assert src is None  # <- the bug: this used to be indistinguishable from zero_target


def test_zero_kelly_with_no_fallback_is_missing():
    """kelly literally 0.0 and no trader size -> unresolvable (None), not a fake fire."""
    v = {"symbol": "X", "kelly_fraction": 0.0}
    weight, src = resolve_target_weight(v)
    assert weight == 0.0
    assert src is None


def test_garbage_size_values_dont_crash():
    """Non-numeric junk in size fields degrades to None, never raises."""
    v = {"symbol": "X", "direction": "?", "kelly_fraction": "nan-ish", "trader_size_fraction": None}
    weight, src = resolve_target_weight(v)
    assert weight == 0.0
    assert src is None
