"""ADR-0097 — the paper-vs-live slippage haircut must be conservative + fail-closed."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hermes_quant.risk.slippage_haircut import (
    _DEFAULT_PRIOR,
    _LIVE_VS_PAPER_PRIOR,
    _MIN_SHADOW_SAMPLES,
    apply_edge_haircut,
    estimate_live_penalty,
    haircut_enabled,
)


def _write_shadow(path: Path, asset_class: str, n: int, div_ratio: float, syn_price: float = 100.0):
    """n rows for asset_class with |div|/syn ≈ div_ratio."""
    lines = []
    for i in range(n):
        lines.append(json.dumps({
            "asset_class": asset_class,
            "synthetic_fill_price": syn_price,
            "alpaca_fill_price": syn_price * (1 + div_ratio),
            "fill_price_divergence": syn_price * div_ratio,
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fail-closed: absent/thin data -> a POSITIVE prior, never 0.
# --------------------------------------------------------------------------- #
def test_absent_shadow_log_uses_prior(tmp_path):
    est = estimate_live_penalty("equity", shadow_log=tmp_path / "nope.jsonl")
    assert est.penalty_frac == _LIVE_VS_PAPER_PRIOR["equity"]
    assert est.basis == "prior"
    assert est.penalty_frac > 0.0  # NEVER a free pass


def test_thin_shadow_data_uses_prior(tmp_path):
    log = tmp_path / "shadow.jsonl"
    _write_shadow(log, "equity", _MIN_SHADOW_SAMPLES - 1, 0.05)  # below threshold
    est = estimate_live_penalty("equity", shadow_log=log)
    assert est.basis == "prior"
    assert est.penalty_frac == _LIVE_VS_PAPER_PRIOR["equity"]


def test_unknown_asset_class_worse_than_equity(tmp_path):
    est = estimate_live_penalty("weird", shadow_log=tmp_path / "nope.jsonl")
    assert est.penalty_frac == _DEFAULT_PRIOR
    assert _DEFAULT_PRIOR > _LIVE_VS_PAPER_PRIOR["equity"]


# --------------------------------------------------------------------------- #
# Measured component: high divergence -> measured > prior (we DO haircut more).
# --------------------------------------------------------------------------- #
def test_high_measured_divergence_dominates_prior(tmp_path):
    log = tmp_path / "shadow.jsonl"
    # 50 rows at 3% divergence — far above the 25bps equity prior.
    _write_shadow(log, "equity", 50, 0.03)
    est = estimate_live_penalty("equity", shadow_log=log)
    assert est.basis == "measured"
    assert est.penalty_frac > _LIVE_VS_PAPER_PRIOR["equity"]
    assert est.penalty_frac == pytest.approx(0.03, abs=2e-3)  # ~p90 of a constant 3%


def test_low_measured_divergence_floored_by_prior(tmp_path):
    """A SMALL measured divergence must NOT lower the penalty below the prior — the shadow
    log can't see the paper->live gap, so the prior is the floor (fail-closed)."""
    log = tmp_path / "shadow.jsonl"
    _write_shadow(log, "equity", 50, 0.0001)  # 1bp — tiny
    est = estimate_live_penalty("equity", shadow_log=log)
    assert est.penalty_frac == _LIVE_VS_PAPER_PRIOR["equity"]  # floored by prior
    assert est.basis == "measured+prior"


# --------------------------------------------------------------------------- #
# Options + multi-leg: materially larger than equity (ADR-0097 sl02).
# --------------------------------------------------------------------------- #
def test_single_option_penalty_exceeds_equity(tmp_path):
    opt = estimate_live_penalty("us_option", shadow_log=tmp_path / "nope.jsonl")
    eq = estimate_live_penalty("equity", shadow_log=tmp_path / "nope.jsonl")
    assert opt.penalty_frac > eq.penalty_frac


def test_multileg_penalty_at_least_sum_of_legs(tmp_path):
    # A 2-option-leg vertical: penalty >= 2x the per-option-leg prior.
    est = estimate_live_penalty(
        "us_option", structure_kind="vertical_spread",
        leg_asset_classes=("us_option", "us_option"),
        shadow_log=tmp_path / "nope.jsonl",
    )
    single = estimate_live_penalty("us_option", shadow_log=tmp_path / "nope.jsonl")
    assert est.penalty_frac >= 2 * 0.0080 - 1e-9
    assert est.penalty_frac > single.penalty_frac


def test_covered_call_combo_penalty_stock_plus_option(tmp_path):
    # stock leg + option leg.
    est = estimate_live_penalty(
        "us_option", structure_kind="covered_call",
        leg_asset_classes=("equity", "us_option"),
        shadow_log=tmp_path / "nope.jsonl",
    )
    assert est.penalty_frac == pytest.approx(0.0025 + 0.0080, abs=1e-9)


def test_n_legs_without_classes_assumes_option_legs(tmp_path):
    est = estimate_live_penalty("us_option", n_legs=4, shadow_log=tmp_path / "nope.jsonl")
    assert est.penalty_frac == pytest.approx(4 * 0.0080, abs=1e-9)


# --------------------------------------------------------------------------- #
# Edge haircut-toward-silence.
# --------------------------------------------------------------------------- #
def test_edge_haircut_silences_marginal_play(tmp_path):
    est = estimate_live_penalty("equity", shadow_log=tmp_path / "nope.jsonl")  # 25bps
    # A play with 20bps edge < 25bps penalty -> net negative -> silence.
    net = apply_edge_haircut(0.0020, est)
    assert net < 0
    # A play with 100bps edge survives.
    assert apply_edge_haircut(0.0100, est) > 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", None])
def test_edge_haircut_nonfinite_edge_fails_closed(tmp_path, bad):
    est = estimate_live_penalty("equity", shadow_log=tmp_path / "nope.jsonl")
    net = apply_edge_haircut(bad, est)
    assert net < 0  # non-finite edge -> treated as <=0 edge -> silence


def test_nan_divergence_rows_ignored_not_crash(tmp_path):
    log = tmp_path / "shadow.jsonl"
    rows = [json.dumps({"asset_class": "equity", "synthetic_fill_price": 100.0,
                        "fill_price_divergence": float("nan")}) for _ in range(50)]
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")
    est = estimate_live_penalty("equity", shadow_log=log)
    # all rows non-finite -> no usable samples -> prior (fail-closed), no crash
    assert est.basis == "prior"
    assert math.isfinite(est.penalty_frac) and est.penalty_frac > 0


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", raising=False)
    assert haircut_enabled() is False
    monkeypatch.setenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", "1")
    assert haircut_enabled() is True
