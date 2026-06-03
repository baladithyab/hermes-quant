"""tests/ops/test_quant_daily_interim_cap_target.py — advisor cap target-weight resolution.

Regression lock for the 2026-06-03 plumbing bug:

  The advisor cap gate (added 2026-06-02 after the leverage-runaway incident)
  read `actionable["target_position_pct"]` to size each fire. But the
  actionable-builder NEVER sets that key — it populates `kelly_fraction`
  (signed) + `trader_size_fraction` + `risk_silence_multiplier`. So the cap
  read None -> 0.0 and silenced EVERY pick as `zero_target`, which looked like
  "cap full" but was actually a missing-size plumbing break. The advisor layer
  fired 0 trades for ~24h while masquerading as healthy.

  The fix is `_resolve_target_weight()`: resolve the signed weight from the
  fields that actually exist, and a LOUD `size_field_missing` guard so a real
  plumbing break can never again be laundered into a benign cap silence.

These tests use the REAL actionable shape (the dict the builder emits), which
is exactly what the original cap test did NOT do — that gap is why the bug
shipped. The cap *math* was well-tested in tests/unit/test_portfolio_normalize.py;
this file tests the *input plumbing* the math depends on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_interim_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-daily-interim.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_daily_interim", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # neutralize the script's execv guard
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod


@pytest.fixture(scope="module")
def interim():
    return _load_interim_module()


# ---------------------------------------------------------------------------
# The canonical path: kelly_fraction (signed) x risk_silence_multiplier.
# This is the EXACT shape of today's 26 real actionables.
# ---------------------------------------------------------------------------


def test_resolves_short_from_kelly_and_risk_mult(interim):
    """MDB-shaped: kelly=-0.2, size=0.2, mult=0.5 -> -0.10, kelly path."""
    v = {
        "symbol": "MDB",
        "direction": -1,
        "kelly_fraction": -0.2,
        "trader_size_fraction": 0.2,
        "risk_silence_multiplier": 0.5,
    }
    weight, src = interim._resolve_target_weight(v)
    assert weight == pytest.approx(-0.10)
    assert src == "kelly_x_riskmult"


def test_resolves_long_from_kelly_and_risk_mult(interim):
    """B-shaped: kelly=+0.2, mult=0.5 -> +0.10."""
    v = {
        "symbol": "B",
        "direction": 1,
        "kelly_fraction": 0.2,
        "trader_size_fraction": 0.2,
        "risk_silence_multiplier": 0.5,
    }
    weight, src = interim._resolve_target_weight(v)
    assert weight == pytest.approx(0.10)
    assert src == "kelly_x_riskmult"


def test_no_risk_mult_defaults_to_full_kelly(interim):
    """Absent risk_silence_multiplier -> multiplier 1.0 (no shrink)."""
    v = {"symbol": "X", "direction": -1, "kelly_fraction": -0.2}
    weight, src = interim._resolve_target_weight(v)
    assert weight == pytest.approx(-0.2)
    assert src == "kelly_x_riskmult"


def test_risk_mult_clamped_to_silence_only(interim):
    """Committee can only silence (<=1.0); a >1 multiplier must be clamped."""
    v = {"symbol": "X", "direction": 1, "kelly_fraction": 0.2, "risk_silence_multiplier": 3.0}
    weight, _ = interim._resolve_target_weight(v)
    assert weight == pytest.approx(0.2)  # clamped to 1.0x, NOT amplified to 0.6


# ---------------------------------------------------------------------------
# Explicit override + trader_size fallback.
# ---------------------------------------------------------------------------


def test_explicit_target_position_pct_wins(interim):
    """If a caller set target_position_pct explicitly, use it verbatim."""
    v = {"symbol": "X", "direction": -1, "kelly_fraction": -0.2, "target_position_pct": -0.07}
    weight, src = interim._resolve_target_weight(v)
    assert weight == pytest.approx(-0.07)
    assert src == "explicit"


def test_trader_size_fallback_when_no_kelly(interim):
    """No kelly but a trader size + direction -> sign(direction)*|size|*mult."""
    v = {"symbol": "X", "direction": -1, "trader_size_fraction": 0.2, "risk_silence_multiplier": 0.5}
    weight, src = interim._resolve_target_weight(v)
    assert weight == pytest.approx(-0.10)
    assert src == "tradersize_x_riskmult"


# ---------------------------------------------------------------------------
# THE GUARD — the heart of the regression. A missing size field must NOT be
# laundered into a 0.0 weight (which the cap reads as benign `zero_target`).
# ---------------------------------------------------------------------------


def test_missing_all_size_fields_returns_none_source(interim):
    """No kelly, no trader size, no explicit -> (0.0, None). Source None is the
    signal the caller turns into a LOUD size_field_missing error, never a silence."""
    v = {"symbol": "BROKEN", "direction": -1}
    weight, src = interim._resolve_target_weight(v)
    assert weight == 0.0
    assert src is None  # <- the bug: this used to be indistinguishable from zero_target


def test_zero_kelly_with_no_fallback_is_missing(interim):
    """kelly literally 0.0 and no trader size -> unresolvable (None), not a fake fire."""
    v = {"symbol": "X", "kelly_fraction": 0.0}
    weight, src = interim._resolve_target_weight(v)
    assert weight == 0.0
    assert src is None


def test_garbage_size_values_dont_crash(interim):
    """Non-numeric junk in size fields degrades to None, never raises."""
    v = {"symbol": "X", "direction": "?", "kelly_fraction": "nan-ish", "trader_size_fraction": None}
    weight, src = interim._resolve_target_weight(v)
    assert weight == 0.0
    assert src is None
