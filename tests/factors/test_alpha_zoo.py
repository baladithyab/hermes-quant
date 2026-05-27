"""tests/factors/test_alpha_zoo.py — Integration tests for AlphaZoo + starter set.

Covers:
  - AlphaFactor model validation (extra fields, tag limits, param limits)
  - AlphaZoo register / read round-trip
  - Malicious source raises PurityViolation
  - shift(-1) source raises LookaheadDetected
  - compute() returns pd.Series on synthetic OHLCV
  - truncate() raises AppendOnlyViolation
  - update() raises AppendOnlyViolation
  - starter_set: all 15 alphas register cleanly and compute non-empty Series
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hermes_quant.factors.alpha_zoo import (
    AlphaFactor,
    AlphaZoo,
    AppendOnlyViolation,
    FactorExecutionError,
)
from hermes_quant.factors.ast_purity import PurityViolation
from hermes_quant.factors.lookahead_sentinel import LookaheadDetected
from hermes_quant.factors.starter_set import register_starter_set


# ---------------------------------------------------------------------------
# Synthetic OHLCV fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def bars() -> pd.DataFrame:
    """100-row synthetic OHLCV DataFrame with a DatetimeIndex."""
    rng = np.random.default_rng(42)
    n = 100
    close = 100.0 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.1, 2.0, n)
    low = close - rng.uniform(0.1, 2.0, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(100_000, 1_000_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture()
def zoo(tmp_path: Path) -> AlphaZoo:
    """AlphaZoo backed by a temp directory (isolated per test)."""
    return AlphaZoo(base_dir=tmp_path)


# ---------------------------------------------------------------------------
# AlphaFactor model validation
# ---------------------------------------------------------------------------


class TestAlphaFactorModel:
    def test_factor_id_auto_generated(self):
        f = AlphaFactor(name="test", description="d", source_code='bars["close"]')
        assert f.factor_id.startswith("alpha_")
        assert len(f.factor_id) == 12  # "alpha_" + 6 hex chars

    def test_created_at_auto_set(self):
        f = AlphaFactor(name="test", description="d", source_code='bars["close"]')
        assert "T" in f.created_at  # ISO8601 contains T separator

    def test_deterministic_factor_id(self):
        f1 = AlphaFactor(name="same", description="d", source_code="x")
        f2 = AlphaFactor(name="same", description="d", source_code="x")
        assert f1.factor_id == f2.factor_id

    def test_different_source_different_id(self):
        f1 = AlphaFactor(name="a", description="d", source_code="x")
        f2 = AlphaFactor(name="a", description="d", source_code="y")
        assert f1.factor_id != f2.factor_id

    def test_tags_limit_10(self):
        with pytest.raises(Exception):
            AlphaFactor(
                name="t",
                description="d",
                source_code="x",
                tags=["t"] * 11,
            )

    def test_params_limit_10(self):
        with pytest.raises(Exception):
            AlphaFactor(
                name="t",
                description="d",
                source_code="x",
                params={str(i): i for i in range(11)},
            )

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            AlphaFactor(
                name="t",
                description="d",
                source_code="x",
                unknown_extra_field="bad",  # type: ignore[call-arg]
            )

    def test_name_max_length(self):
        with pytest.raises(Exception):
            AlphaFactor(name="a" * 129, description="d", source_code="x")

    def test_description_max_length(self):
        with pytest.raises(Exception):
            AlphaFactor(name="n", description="d" * 513, source_code="x")


# ---------------------------------------------------------------------------
# AlphaZoo register / read round-trip
# ---------------------------------------------------------------------------


class TestAlphaZooRegister:
    def test_register_returns_factor_id(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="test_factor",
            description="simple test",
            source_code='bars["close"] - bars["open"]',
        )
        fid = zoo.register(f)
        assert fid == f.factor_id

    def test_read_after_register(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="read_test",
            description="round-trip",
            source_code='bars["close"].pct_change(1)',
        )
        fid = zoo.register(f)
        retrieved = zoo.read(fid)
        assert retrieved is not None
        assert retrieved.name == "read_test"
        assert retrieved.source_code == 'bars["close"].pct_change(1)'

    def test_read_unknown_returns_none(self, zoo: AlphaZoo):
        assert zoo.read("alpha_000000") is None

    def test_list_all_returns_registered(self, zoo: AlphaZoo):
        f1 = AlphaFactor(name="f1", description="d", source_code='bars["close"]')
        f2 = AlphaFactor(name="f2", description="d", source_code='bars["open"]')
        zoo.register(f1)
        zoo.register(f2)
        all_factors = zoo.list_all()
        ids = {f.factor_id for f in all_factors}
        assert f1.factor_id in ids
        assert f2.factor_id in ids

    def test_jsonl_persisted(self, tmp_path: Path):
        """Re-loading from disk should recover registered factors."""
        zoo1 = AlphaZoo(base_dir=tmp_path)
        f = AlphaFactor(
            name="persist_test",
            description="persistence",
            source_code='bars["close"]',
        )
        fid = zoo1.register(f)

        zoo2 = AlphaZoo(base_dir=tmp_path)
        retrieved = zoo2.read(fid)
        assert retrieved is not None
        assert retrieved.name == "persist_test"


# ---------------------------------------------------------------------------
# Gate rejections
# ---------------------------------------------------------------------------


class TestGateRejections:
    def test_malicious_source_raises_purity_violation(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="evil",
            description="trying to import os",
            source_code="import os\nos.system('ls')",
        )
        with pytest.raises(PurityViolation) as exc_info:
            zoo.register(f)
        assert exc_info.value.violation_kind in {
            "import_statement",
            "forbidden_name",
            "forbidden_name_ref",
        }

    def test_eval_source_raises_purity_violation(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="eval_evil",
            description="uses eval",
            source_code="eval('bars[\"close\"]')",
        )
        with pytest.raises(PurityViolation):
            zoo.register(f)

    def test_negative_shift_raises_lookahead_detected(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="lookahead_factor",
            description="peeks into future",
            source_code='bars["close"].shift(-1)',
        )
        with pytest.raises(LookaheadDetected) as exc_info:
            zoo.register(f)
        assert "negative_shift" in exc_info.value.violation_kind

    def test_exec_raises_purity_violation(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="exec_evil",
            description="uses exec",
            source_code="exec('import os')",
        )
        with pytest.raises(PurityViolation):
            zoo.register(f)

    def test_to_csv_raises_purity_violation(self, zoo: AlphaZoo):
        f = AlphaFactor(
            name="io_evil",
            description="writes to csv",
            source_code='bars.to_csv("/tmp/leak.csv")',
        )
        with pytest.raises(PurityViolation):
            zoo.register(f)


# ---------------------------------------------------------------------------
# compute()
# ---------------------------------------------------------------------------


class TestAlphaZooCompute:
    def test_compute_returns_series(self, zoo: AlphaZoo, bars: pd.DataFrame):
        f = AlphaFactor(
            name="compute_test",
            description="simple",
            source_code='bars["close"] - bars["open"]',
        )
        fid = zoo.register(f)
        result = zoo.compute(fid, bars)
        assert isinstance(result, pd.Series)
        assert len(result) == len(bars)

    def test_compute_index_matches_bars(self, zoo: AlphaZoo, bars: pd.DataFrame):
        f = AlphaFactor(
            name="idx_test",
            description="index check",
            source_code='bars["close"].pct_change(1)',
        )
        fid = zoo.register(f)
        result = zoo.compute(fid, bars)
        assert list(result.index) == list(bars.index)

    def test_compute_unknown_factor_raises(self, zoo: AlphaZoo, bars: pd.DataFrame):
        with pytest.raises(KeyError):
            zoo.compute("alpha_zzzzzz", bars)

    def test_compute_runtime_error_wrapped(self, zoo: AlphaZoo, bars: pd.DataFrame):
        """A factor that errors at runtime should raise FactorExecutionError."""
        f = AlphaFactor(
            name="broken_runtime",
            description="divides by zero constant",
            source_code="bars['close'] / 0",
        )
        fid = zoo.register(f)
        # Division by zero in pandas produces inf/nan (not an exception),
        # but a truly broken expression should raise FactorExecutionError.
        # We test with an expression that accesses a non-existent column.
        f2 = AlphaFactor(
            name="missing_col",
            description="accesses nonexistent col",
            source_code='bars["nonexistent_column_xyz"]',
        )
        fid2 = zoo.register(f2)
        with pytest.raises(FactorExecutionError):
            zoo.compute(fid2, bars)

    def test_compute_no_builtins_escape(self, zoo: AlphaZoo, bars: pd.DataFrame):
        """The sandboxed eval should strip __builtins__. We verify indirectly
        that a pure pandas factor runs correctly (sandbox is not too restrictive)."""
        f = AlphaFactor(
            name="sandbox_test",
            description="should run in sandbox",
            source_code='bars["close"].rolling(5).mean()',
        )
        fid = zoo.register(f)
        result = zoo.compute(fid, bars)
        assert result.notna().any()


# ---------------------------------------------------------------------------
# Append-only protection
# ---------------------------------------------------------------------------


class TestAppendOnly:
    def test_truncate_raises(self, zoo: AlphaZoo):
        with pytest.raises(AppendOnlyViolation):
            zoo.truncate()

    def test_update_raises(self, zoo: AlphaZoo):
        with pytest.raises(AppendOnlyViolation):
            zoo.update()


# ---------------------------------------------------------------------------
# Starter set
# ---------------------------------------------------------------------------


class TestStarterSet:
    def test_all_starter_factors_register(self, zoo: AlphaZoo):
        ids = register_starter_set(zoo)
        assert len(ids) >= 10, f"Expected at least 10 starter factors; got {len(ids)}"

    def test_starter_factor_ids_are_strings(self, zoo: AlphaZoo):
        ids = register_starter_set(zoo)
        for fid in ids:
            assert isinstance(fid, str)
            assert fid.startswith("alpha_")

    def test_starter_factors_retrievable(self, zoo: AlphaZoo):
        ids = register_starter_set(zoo)
        for fid in ids:
            f = zoo.read(fid)
            assert f is not None, f"Could not retrieve {fid!r}"

    def test_starter_factors_compute_on_synthetic_ohlcv(
        self, zoo: AlphaZoo, bars: pd.DataFrame
    ):
        """All starter factors must produce non-empty, non-all-NaN Series."""
        ids = register_starter_set(zoo)
        failures = []
        for fid in ids:
            try:
                series = zoo.compute(fid, bars)
                if series.empty:
                    failures.append(f"{fid}: empty Series")
                elif series.notna().sum() == 0:
                    failures.append(f"{fid}: all-NaN Series")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{fid}: exception {type(exc).__name__}: {exc}")
        assert not failures, "Starter set compute failures:\n" + "\n".join(failures)

    def test_starter_factors_return_correct_type(
        self, zoo: AlphaZoo, bars: pd.DataFrame
    ):
        ids = register_starter_set(zoo)
        for fid in ids:
            result = zoo.compute(fid, bars)
            assert isinstance(result, pd.Series), f"{fid!r} returned {type(result)}"

    def test_starter_set_idempotent_on_second_call(self, zoo: AlphaZoo):
        """Calling register_starter_set twice should not raise (re-registers)."""
        ids1 = register_starter_set(zoo)
        ids2 = register_starter_set(zoo)
        assert set(ids1) == set(ids2)

    def test_starter_factors_all_pass_purity_gate(self):
        """Verify starter factor source code passes purity gate directly."""
        from hermes_quant.factors.ast_purity import check_factor_purity
        from hermes_quant.factors.starter_set import _STARTER_FACTORS

        failures = []
        for defn in _STARTER_FACTORS:
            result = check_factor_purity(defn["source_code"])
            if not result.passes:
                failures.append(
                    f"{defn['name']}: {result.violations}"
                )
        assert not failures, "Starter factors failing purity gate:\n" + "\n".join(failures)

    def test_starter_factors_all_pass_lookahead_gate(self):
        """Verify starter factor source code passes lookahead sentinel directly."""
        from hermes_quant.factors.lookahead_sentinel import check_no_lookahead
        from hermes_quant.factors.starter_set import _STARTER_FACTORS

        failures = []
        for defn in _STARTER_FACTORS:
            result = check_no_lookahead(defn["source_code"])
            if not result.passes:
                failures.append(
                    f"{defn['name']}: {result.suspicions}"
                )
        assert not failures, "Starter factors failing lookahead gate:\n" + "\n".join(failures)
