"""ar-killswitch-pct-guard — _read_safety_rails() kill_switch_pct bare float() TypeError fix.

DEFECT (line 363, autonomous.py):
  `"kill_switch_pct": float(auto.get("kill_switch_pct", 0.10))`
  Every other threshold in _read_safety_rails() (stopless_max_size_pct, per_position_stop_loss_pct)
  and in _read_silence_bias_config() (min_confidence, min_urgency) use `_finite_threshold()`.
  kill_switch_pct was the one un-guarded outlier.

  float({'bad': 'value'}) raises TypeError.
  float(None) raises TypeError.
  These propagate uncaught out of the config read and abort the tick.

  The unguarded call is at line 363 (inside _read_safety_rails), which is invoked at:
    - line 1529: the kill-switch check (wrapped in a try/except by ar08 — so the KS check is safe)
    - line 1585: the second unconditional call AFTER the kill-switch block (NOT wrapped)
  So a malformed kill_switch_pct crashes the tick AFTER the kill-switch check passes, during
  signal-evaluation setup. The docstring says "Raises: Nothing externally-visible" — violated.

FIX: replace `float(...)` with `_finite_threshold(...)` at line 363. Byte-identical for any
finite positive YAML value (the only legal shape). No flag gate needed.

RED->GREEN discipline: this test RED-proves on the pre-fix code by monkeypatching _read_config
to return a malformed kill_switch_pct and asserting that _read_safety_rails() does NOT raise
TypeError. On pre-fix code it DOES raise; on post-fix code it falls back to 0.10.
"""
from __future__ import annotations

import pytest

from hermes_quant import autonomous as auto


@pytest.mark.parametrize("bad", [{"bad": "value"}, None, object()])
def test_malformed_kill_switch_pct_does_not_raise(monkeypatch, bad):
    """Pre-fix: _read_safety_rails() raises TypeError on a non-numeric kill_switch_pct.
    Post-fix: it falls CLOSED to the documented 0.10 default.

    RED proof: on the bare-float() code, float({'bad': 'value'}) / float(None) / float(object())
    all raise TypeError — the call site at line 1585 is NOT wrapped in a try/except, so the tick
    aborts mid-signal-evaluation.
    """
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {"kill_switch_pct": bad}}
    })
    rails = auto._read_safety_rails()  # MUST NOT raise TypeError post-fix
    assert rails["kill_switch_pct"] == pytest.approx(0.10), (
        f"malformed kill_switch_pct={bad!r} must fall back to the 0.10 default, "
        "not raise TypeError or return a non-finite value"
    )


def test_valid_kill_switch_pct_is_honored(monkeypatch):
    """A valid finite-positive kill_switch_pct is returned unchanged (byte-identical path)."""
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {"kill_switch_pct": 0.15}}
    })
    rails = auto._read_safety_rails()
    assert rails["kill_switch_pct"] == pytest.approx(0.15)


def test_absent_kill_switch_pct_uses_default(monkeypatch):
    """When kill_switch_pct is absent (the live config), the default 0.10 is returned unchanged."""
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {}}
    })
    rails = auto._read_safety_rails()
    assert rails["kill_switch_pct"] == pytest.approx(0.10)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.10, 0.0])
def test_non_finite_or_nonpositive_kill_switch_pct_falls_to_default(monkeypatch, bad):
    """NaN/inf/<=0 must fall CLOSED to 0.10 (mirrors the ar08/ar09 finite-threshold family)."""
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {"kill_switch_pct": bad}}
    })
    rails = auto._read_safety_rails()
    assert rails["kill_switch_pct"] == pytest.approx(0.10), (
        f"kill_switch_pct={bad!r} (non-finite or <=0) must fall back to 0.10"
    )
