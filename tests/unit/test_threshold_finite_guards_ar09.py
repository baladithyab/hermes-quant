"""ar09 — operator-YAML money thresholds must finite-guard (the ar08 family, completed).

archaeology-final-verify (wbomb8o6z) found ar08 (kill_switch_pct) had two un-fixed siblings in the
SAME family — operator-editable thresholds parsed via float() with no finite/bounds check, so a
`.nan`/`.inf`/<=0 value (valid YAML) silently FLIPS a money gate:

  - silence-bias FIRE gate (ALWAYS-ON): min_confidence / min_urgency. A NaN threshold makes
    `confidence < cfg.min_confidence` False, so a should-be-SILENCED low-confidence signal FIRES.
  - stop-loss backstop (opt-in): stopless_max_size_pct. A NaN limit makes `abs(kelly) > limit` False,
    so a full-size stopless position passes UNCAPPED.

FIX: finite-guard at the config-parse sites (_read_silence_bias_config, _read_safety_rails) — fall back
to the documented default + warn, never let NaN/inf/<=0 silently neuter the gate. Byte-identical for
any finite-positive configured value (the only legal shape; live config is finite).
"""

from __future__ import annotations

import math

import hermes_quant.autonomous as auto


def _cfg_with(silence_bias: dict | None = None, autonomous_extra: dict | None = None) -> dict:
    quant = {"autonomous": {**(autonomous_extra or {})}}
    if silence_bias is not None:
        quant["autonomous"]["silence_bias"] = silence_bias
    return {"quant": quant}


def test_silence_bias_nan_min_confidence_falls_back_to_default(monkeypatch) -> None:
    """A NaN min_confidence from YAML must fall back to the 0.65 default, not become NaN."""
    monkeypatch.setattr(auto, "_read_config", lambda: _cfg_with(silence_bias={"min_confidence": float("nan")}))
    cfg = auto._read_silence_bias_config()
    assert math.isfinite(cfg.min_confidence), "NaN min_confidence must not propagate into the gate config"
    assert cfg.min_confidence == 0.65


def test_silence_bias_inf_min_urgency_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(auto, "_read_config", lambda: _cfg_with(silence_bias={"min_urgency": float("inf")}))
    cfg = auto._read_silence_bias_config()
    assert math.isfinite(cfg.min_urgency)
    assert cfg.min_urgency == 0.5


def test_silence_bias_negative_threshold_falls_back(monkeypatch) -> None:
    """A negative confidence threshold (a NaN-equivalent would never silence) falls back to default."""
    monkeypatch.setattr(auto, "_read_config", lambda: _cfg_with(silence_bias={"min_confidence": -1.0}))
    cfg = auto._read_silence_bias_config()
    assert cfg.min_confidence == 0.65


def test_silence_bias_finite_positive_byte_identical(monkeypatch) -> None:
    """A legal finite-positive configured value is used verbatim (byte-identical)."""
    monkeypatch.setattr(
        auto, "_read_config",
        lambda: _cfg_with(silence_bias={"min_confidence": 0.8, "min_urgency": 0.3}),
    )
    cfg = auto._read_silence_bias_config()
    assert cfg.min_confidence == 0.8
    assert cfg.min_urgency == 0.3


def test_stopless_max_size_nan_falls_back_to_floor(monkeypatch) -> None:
    """A NaN stopless_max_size_pct must fall back to the 0.05 floor, not silently disable the backstop
    (a NaN limit makes `abs(kelly) > limit` False -> a full-size stopless position passes uncapped)."""
    monkeypatch.setattr(
        auto, "_read_config",
        lambda: _cfg_with(autonomous_extra={"stopless_max_size_pct": float("nan")}),
    )
    rails = auto._read_safety_rails()
    assert math.isfinite(rails["stopless_max_size_pct"])
    assert rails["stopless_max_size_pct"] == 0.05


def test_stopless_max_size_negative_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        auto, "_read_config",
        lambda: _cfg_with(autonomous_extra={"stopless_max_size_pct": -0.10}),
    )
    rails = auto._read_safety_rails()
    assert rails["stopless_max_size_pct"] == 0.05


def test_stopless_max_size_finite_positive_byte_identical(monkeypatch) -> None:
    monkeypatch.setattr(
        auto, "_read_config",
        lambda: _cfg_with(autonomous_extra={"stopless_max_size_pct": 0.08}),
    )
    rails = auto._read_safety_rails()
    assert rails["stopless_max_size_pct"] == 0.08
