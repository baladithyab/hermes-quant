"""ar81 — silence-bias quorum/veto INT counts must fail CLOSED on a malformed operator value.

Found by the parallel find->fix workflow (wf_d7d2cc27). _read_silence_bias_config read
min_analysts_emitted / max_recent_rejections / salience_window_hours via bare int(raw.get(...)):
  - inf -> OverflowError, nan / float-form string -> ValueError: propagate out of the config read
    (no try/except at the tick's call site) and ABORT the entire tick before any gate/kill-switch.
  - a float-form token (1.9) silently truncates a 2-of-N quorum to 1.
  - 0 / negative makes `n_emitted < cfg` always False so the quorum NEVER silences -> a single-/
    zero-voice signal can FIRE autonomously (contradicts "single-voice is never enough").
Fix: route the three int counts through the ar61 _positive_int_count helper (finite, >=1 int, else the
conservative documented default; never abort, never neuter).
"""
from __future__ import annotations

import pytest

from hermes_quant import autonomous as auto


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 1.9, 0, -1, "abc", "1.9"])
def test_ar81_malformed_min_analysts_falls_back_to_default(monkeypatch, bad):
    """A malformed min_analysts_emitted must fall CLOSED to the default (2), never abort the
    config read or neuter the quorum below 1."""
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {"silence_bias": {"min_analysts_emitted": bad}}}
    })
    cfg = auto._read_silence_bias_config()  # MUST NOT raise
    assert cfg.min_analysts_emitted == 2, f"{bad!r} should fall back to default 2"
    assert cfg.min_analysts_emitted >= 1  # quorum never neutered below 1


def test_ar81_valid_int_quorum_honored(monkeypatch):
    """A valid YAML int count is honored (byte-identical to bare int on the good path)."""
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {"silence_bias": {
            "min_analysts_emitted": 3, "max_recent_rejections": 5, "salience_window_hours": 72,
        }}}
    })
    cfg = auto._read_silence_bias_config()
    assert cfg.min_analysts_emitted == 3
    assert cfg.max_recent_rejections == 5
    assert cfg.salience_window_hours == 72


@pytest.mark.parametrize("field,default", [
    ("max_recent_rejections", 3), ("salience_window_hours", 168),
])
def test_ar81_other_counts_fail_closed(monkeypatch, field, default):
    monkeypatch.setattr(auto, "_read_config", lambda: {
        "quant": {"autonomous": {"silence_bias": {field: float("inf")}}}
    })
    cfg = auto._read_silence_bias_config()  # MUST NOT raise (OverflowError pre-fix)
    assert getattr(cfg, field) == default
