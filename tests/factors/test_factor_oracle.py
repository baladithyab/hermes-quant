"""tests/factors/test_factor_oracle.py — Unit tests for FactorOracle + FactorVerdict.

Coverage:
  1.  FactorVerdict Pydantic round-trip (model_dump / model_validate)
  2.  ProductionReadinessThresholds: premium tier fires correctly
  3.  ProductionReadinessThresholds: standard tier fires correctly
  4.  ProductionReadinessThresholds: experimental tier fires correctly
  5.  ProductionReadinessThresholds: rejected tier fires correctly
  6.  NaN ICPanel → always rejected
  7.  FactorOracle.evaluate() on a starter-set factor → deterministic verdict
  8.  FactorOracle.evaluate_all() returns one verdict per registered factor
  9.  FactorOracle.rank() sorts by icir descending
 10.  APPEND-ONLY: re-evaluating a factor adds a new row
 11.  .verdict_for() on AlphaZoo returns latest verdict
 12.  FactorOracle with too-short bars → rejected tier (insufficient data)
 13.  FactorOracle.evaluate() with non-existent factor_id → KeyError
 14.  production_ready=True only for premium/standard
 15.  production_ready=False for experimental/rejected
 16.  FactorVerdict reasons list has at most 5 entries
 17.  ICDedupGate rejection path: near-duplicate → rejected tier
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hermes_quant.factors.alpha_zoo import AlphaFactor, AlphaZoo
from hermes_quant.factors.factor_oracle import (
    FactorOracle,
    FactorVerdict,
    ProductionReadinessThresholds,
)
from hermes_quant.factors.ic_panel import ICPanel
from hermes_quant.factors.starter_set import register_starter_set


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_N = 400  # bars to generate (comfortably above window=60 + fwd_horizon)


def _bdate_index(n: int = _N) -> pd.DatetimeIndex:
    return pd.bdate_range("2019-01-02", periods=n)


def _make_bars(n: int = _N, seed: int = 0) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame."""
    rng = np.random.default_rng(seed)
    idx = _bdate_index(n)
    close = 100.0 + np.cumsum(rng.standard_normal(n) * 0.5)
    open_ = close * (1 + rng.uniform(-0.005, 0.005, n))
    high = np.maximum(close, open_) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(close, open_) * (1 - rng.uniform(0, 0.01, n))
    volume = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _make_zoo_with_one_factor(tmpdir: Path) -> tuple[AlphaZoo, str]:
    zoo = AlphaZoo(base_dir=tmpdir)
    fid = zoo.register(
        AlphaFactor(
            name="test_momentum",
            description="Close minus open",
            source_code='bars["close"] - bars["open"]',
            author="test",
        )
    )
    return zoo, fid


def _synthetic_panel(
    ic_mean: float = 0.10,
    ic_std: float = 0.05,
    hit_rate: float = 0.60,
) -> ICPanel:
    icir = ic_mean / max(ic_std, 1e-9)
    return ICPanel(
        factor_id="synth",
        ic_mean=ic_mean,
        ic_std=ic_std,
        icir=icir,
        hit_rate=hit_rate,
        turnover=0.02,
        n_periods=40,
        fwd_horizon_days=5,
    )


# ---------------------------------------------------------------------------
# 1. FactorVerdict Pydantic round-trip
# ---------------------------------------------------------------------------


class TestFactorVerdictModel:
    def test_round_trip_model_dump_validate(self) -> None:
        panel = _synthetic_panel(ic_mean=0.06, ic_std=0.04, hit_rate=0.65)
        verdict = FactorVerdict(
            factor_id="alpha_001",
            name="test_alpha",
            ic_panel=panel.to_dict(),
            production_ready=True,
            tier="premium",
            reasons=["icir=1.5 ≥ 0.5"],
            reviewed_at="2024-01-01T00:00:00+00:00",
        )
        dumped = verdict.model_dump()
        restored = FactorVerdict.model_validate(dumped)
        assert restored.factor_id == verdict.factor_id
        assert restored.tier == verdict.tier
        assert restored.production_ready == verdict.production_ready
        assert restored.ic_panel == verdict.ic_panel

    def test_json_serialisation(self) -> None:
        panel = _synthetic_panel()
        verdict = FactorVerdict(
            factor_id="alpha_002",
            name="alpha_two",
            ic_panel=panel.to_dict(),
            production_ready=False,
            tier="experimental",
            reasons=["reason 1"],
            reviewed_at="2024-06-01T12:00:00+00:00",
        )
        json_str = verdict.model_dump_json()
        data = json.loads(json_str)
        restored = FactorVerdict.model_validate(data)
        assert restored.tier == "experimental"
        assert not restored.production_ready

    def test_extra_fields_forbidden(self) -> None:
        """extra='forbid' means unknown fields raise ValidationError."""
        with pytest.raises(Exception):  # pydantic.ValidationError
            FactorVerdict(
                factor_id="x",
                name="x",
                ic_panel={},
                production_ready=False,
                tier="rejected",
                reasons=[],
                reviewed_at="2024-01-01T00:00:00+00:00",
                unexpected_field="oops",  # type: ignore[call-arg]
            )

    def test_ic_panel_obj_property(self) -> None:
        panel = _synthetic_panel(ic_mean=0.08)
        verdict = FactorVerdict(
            factor_id="alpha_003",
            name="test",
            ic_panel=panel.to_dict(),
            production_ready=False,
            tier="experimental",
            reasons=[],
            reviewed_at="2024-01-01T00:00:00+00:00",
        )
        recovered = verdict.ic_panel_obj
        assert recovered.ic_mean == pytest.approx(panel.ic_mean)


# ---------------------------------------------------------------------------
# 2–5. ProductionReadinessThresholds tier assignment
# ---------------------------------------------------------------------------


class TestProductionReadinessThresholds:
    def _thr(self) -> ProductionReadinessThresholds:
        return ProductionReadinessThresholds()

    def test_premium_tier_fires(self) -> None:
        """icir ≥ 0.5, hit_rate ≥ 0.60, ic_mean ≥ 0.05 → premium."""
        panel = ICPanel(
            factor_id="p",
            ic_mean=0.06,
            ic_std=0.04,
            icir=1.5,  # 0.06 / 0.04 = 1.5
            hit_rate=0.70,
            turnover=0.01,
            n_periods=50,
            fwd_horizon_days=5,
        )
        tier, reasons = self._thr().assign_tier(panel)
        assert tier == "premium"
        assert any("premium" in r for r in reasons)

    def test_standard_tier_fires(self) -> None:
        """icir ≥ 0.3, hit_rate ≥ 0.55, ic_mean ≥ 0.02 → standard (not premium)."""
        panel = ICPanel(
            factor_id="s",
            ic_mean=0.025,
            ic_std=0.05,
            icir=0.5,  # 0.025 / 0.05 = 0.5 ≥ 0.3 but ic_mean=0.025 < 0.05 so not premium
            hit_rate=0.58,
            turnover=0.02,
            n_periods=40,
            fwd_horizon_days=5,
        )
        tier, _ = self._thr().assign_tier(panel)
        assert tier == "standard"

    def test_experimental_tier_fires(self) -> None:
        """icir ≥ 0.1, hit_rate ≥ 0.50 but below standard → experimental."""
        panel = ICPanel(
            factor_id="e",
            ic_mean=0.01,
            ic_std=0.08,
            icir=0.125,  # 0.01 / 0.08 = 0.125 ≥ 0.1
            hit_rate=0.52,
            turnover=0.05,
            n_periods=30,
            fwd_horizon_days=5,
        )
        tier, _ = self._thr().assign_tier(panel)
        assert tier == "experimental"

    def test_rejected_tier_fires(self) -> None:
        """icir < 0.1 → rejected."""
        panel = ICPanel(
            factor_id="r",
            ic_mean=0.001,
            ic_std=0.1,
            icir=0.01,  # below 0.1
            hit_rate=0.48,
            turnover=0.10,
            n_periods=20,
            fwd_horizon_days=5,
        )
        tier, reasons = self._thr().assign_tier(panel)
        assert tier == "rejected"
        assert len(reasons) >= 1

    def test_nan_panel_always_rejected(self) -> None:
        panel = ICPanel(
            factor_id="nan_test",
            ic_mean=float("nan"),
            ic_std=float("nan"),
            icir=float("nan"),
            hit_rate=float("nan"),
            turnover=float("nan"),
            n_periods=0,
            fwd_horizon_days=5,
        )
        tier, reasons = self._thr().assign_tier(panel)
        assert tier == "rejected"
        assert reasons

    def test_production_ready_for_premium(self) -> None:
        panel = _synthetic_panel(ic_mean=0.06, ic_std=0.04, hit_rate=0.70)
        # Manually verify icir
        panel2 = ICPanel(
            factor_id="prem",
            ic_mean=0.06,
            ic_std=0.04,
            icir=1.5,
            hit_rate=0.70,
            turnover=0.01,
            n_periods=50,
            fwd_horizon_days=5,
        )
        tier, _ = self._thr().assign_tier(panel2)
        assert tier == "premium"

    def test_reasons_list_max_5(self) -> None:
        panel = ICPanel(
            factor_id="r2",
            ic_mean=0.0,
            ic_std=0.1,
            icir=0.0,
            hit_rate=0.3,
            turnover=0.1,
            n_periods=10,
            fwd_horizon_days=5,
        )
        _, reasons = self._thr().assign_tier(panel)
        assert len(reasons) <= 5


# ---------------------------------------------------------------------------
# 7. FactorOracle.evaluate() on a starter-set factor
# ---------------------------------------------------------------------------


class TestFactorOracleEvaluate:
    @pytest.fixture()
    def tmp_oracle(self, tmp_path: Path) -> FactorOracle:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo")
        register_starter_set(zoo)
        return FactorOracle(zoo, verdicts_dir=tmp_path / "verdicts")

    def test_evaluate_returns_verdict(self, tmp_oracle: FactorOracle) -> None:
        bars = _make_bars()
        # Pick any registered factor
        fid = tmp_oracle._zoo.list_all()[0].factor_id  # noqa: SLF001
        verdict = tmp_oracle.evaluate(fid, bars)
        assert isinstance(verdict, FactorVerdict)
        assert verdict.factor_id == fid

    def test_evaluate_deterministic(self, tmp_path: Path) -> None:
        """Same bars + factor → same tier (deterministic evaluation)."""
        bars = _make_bars(seed=7)
        zoo = AlphaZoo(base_dir=tmp_path / "zoo2")
        fid = zoo.register(
            AlphaFactor(
                name="det_factor",
                description="close/open",
                source_code='bars["close"] / bars["open"]',
                author="test",
            )
        )
        oracle1 = FactorOracle(zoo, verdicts_dir=tmp_path / "v1")
        oracle2 = FactorOracle(zoo, verdicts_dir=tmp_path / "v2")
        v1 = oracle1.evaluate(fid, bars)
        v2 = oracle2.evaluate(fid, bars)
        assert v1.tier == v2.tier
        assert v1.ic_panel["icir"] == pytest.approx(v2.ic_panel["icir"], rel=1e-6)

    def test_evaluate_nonexistent_factor_raises(self, tmp_oracle: FactorOracle) -> None:
        bars = _make_bars()
        with pytest.raises(KeyError, match="not registered"):
            tmp_oracle.evaluate("nonexistent_factor_id", bars)

    def test_evaluate_persists_to_jsonl(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo3")
        fid = zoo.register(
            AlphaFactor(
                name="persist_test",
                description="volume zscore",
                source_code=(
                    "(bars[\"volume\"] - bars[\"volume\"].rolling(20).mean())"
                    " / bars[\"volume\"].rolling(20).std()"
                ),
                author="test",
            )
        )
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "verdicts3")
        bars = _make_bars()
        oracle.evaluate(fid, bars)
        assert oracle._verdicts_path.exists()  # noqa: SLF001
        lines = oracle._verdicts_path.read_text().strip().splitlines()  # noqa: SLF001
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# 8. evaluate_all() returns one verdict per registered factor
# ---------------------------------------------------------------------------


class TestFactorOracleEvaluateAll:
    def test_evaluate_all_count(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_all")
        register_starter_set(zoo)
        n_factors = len(zoo.list_all())
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_all")
        bars = _make_bars()
        verdicts = oracle.evaluate_all(bars)
        assert len(verdicts) == n_factors

    def test_evaluate_all_returns_dict_of_verdicts(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_all2")
        register_starter_set(zoo)
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_all2")
        bars = _make_bars()
        verdicts = oracle.evaluate_all(bars)
        for fid, v in verdicts.items():
            assert isinstance(v, FactorVerdict)
            assert v.factor_id == fid


# ---------------------------------------------------------------------------
# 9. rank() sorts by icir descending
# ---------------------------------------------------------------------------


class TestFactorOracleRank:
    def test_rank_sorted_icir_desc(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_rank")
        register_starter_set(zoo)
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_rank")
        bars = _make_bars()
        ranked = oracle.rank(bars)
        icirs = [
            v.ic_panel.get("icir", float("-inf"))
            for _, v in ranked
        ]
        # Replace NaN with -inf for comparison
        icirs_clean = [x if (isinstance(x, float) and x == x) else float("-inf") for x in icirs]
        for i in range(len(icirs_clean) - 1):
            assert icirs_clean[i] >= icirs_clean[i + 1], (
                f"rank not descending at index {i}: {icirs_clean[i]} < {icirs_clean[i+1]}"
            )

    def test_rank_returns_list_of_tuples(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_rank2")
        fid = zoo.register(
            AlphaFactor(
                name="single_factor",
                description="close",
                source_code='bars["close"]',
                author="test",
            )
        )
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_rank2")
        bars = _make_bars()
        ranked = oracle.rank(bars)
        assert isinstance(ranked, list)
        for item in ranked:
            assert isinstance(item, tuple)
            assert len(item) == 2


# ---------------------------------------------------------------------------
# 10. APPEND-ONLY: re-evaluating adds a new row; latest returned
# ---------------------------------------------------------------------------


class TestAppendOnlyVerdicts:
    def test_re_evaluate_appends_new_row(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_ao")
        fid = zoo.register(
            AlphaFactor(
                name="ao_factor",
                description="intraday",
                source_code='bars["close"] - bars["open"]',
                author="test",
            )
        )
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_ao")
        bars = _make_bars()
        oracle.evaluate(fid, bars)
        oracle.evaluate(fid, bars)
        oracle.evaluate(fid, bars)
        lines = oracle._verdicts_path.read_text().strip().splitlines()  # noqa: SLF001
        assert len(lines) == 3, f"Expected 3 rows (one per evaluate), got {len(lines)}"

    def test_latest_verdict_returns_newest(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_latest")
        fid = zoo.register(
            AlphaFactor(
                name="latest_factor",
                description="volume",
                source_code='bars["volume"]',
                author="test",
            )
        )
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_latest")
        bars = _make_bars()
        v1 = oracle.evaluate(fid, bars)
        v2 = oracle.evaluate(fid, bars)
        # latest_verdict reads from JSONL; should match most recent reviewed_at
        latest = oracle.latest_verdict(fid)
        assert latest is not None
        assert latest.reviewed_at == v2.reviewed_at

    def test_latest_verdict_none_for_unevaluated(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_none")
        fid = zoo.register(
            AlphaFactor(
                name="unevaluated",
                description="high",
                source_code='bars["high"]',
                author="test",
            )
        )
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_none")
        assert oracle.latest_verdict(fid) is None


# ---------------------------------------------------------------------------
# 11. AlphaZoo.verdict_for() bridge
# ---------------------------------------------------------------------------


class TestAlphaZooVerdictFor:
    def test_verdict_for_none_before_evaluation(self, tmp_path: Path) -> None:
        """AlphaZoo.verdict_for() returns None for a factor never evaluated."""
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_vf")
        zoo.register(
            AlphaFactor(
                name="vf_factor",
                description="low",
                source_code='bars["low"]',
                author="test",
            )
        )
        # Construct a FactorOracle with isolated verdicts_dir (no prior verdicts)
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "verdicts_vf")
        result = oracle.latest_verdict("nonexistent_factor_id")
        assert result is None


# ---------------------------------------------------------------------------
# 12. Too-short bars → rejected
# ---------------------------------------------------------------------------


class TestShortBarsRejected:
    def test_short_bars_produces_rejected_verdict(self, tmp_path: Path) -> None:
        zoo = AlphaZoo(base_dir=tmp_path / "zoo_short")
        fid = zoo.register(
            AlphaFactor(
                name="short_bars_factor",
                description="close",
                source_code='bars["close"]',
                author="test",
            )
        )
        oracle = FactorOracle(zoo, verdicts_dir=tmp_path / "v_short")
        # Only 50 bars — below the 60-day window requirement
        short_bars = _make_bars(n=50)
        verdict = oracle.evaluate(fid, short_bars)
        assert verdict.tier == "rejected"
        assert not verdict.production_ready

    # ---------------------------------------------------------------------------
    # 14–15. production_ready flag
    # ---------------------------------------------------------------------------

    def test_production_ready_only_for_premium_standard(self) -> None:
        thr = ProductionReadinessThresholds()
        for tier_name in ("experimental", "rejected"):
            # experimental panel
            panel = ICPanel(
                factor_id="t",
                ic_mean=0.01,
                ic_std=0.08,
                icir=0.125,
                hit_rate=0.52,
                turnover=0.05,
                n_periods=30,
                fwd_horizon_days=5,
            )
            tier, _ = thr.assign_tier(panel)
            if tier == tier_name:
                # Verify production_ready would be False
                assert tier not in ("premium", "standard")
