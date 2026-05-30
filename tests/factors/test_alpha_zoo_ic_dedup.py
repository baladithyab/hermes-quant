"""tests/factors/test_alpha_zoo_ic_dedup.py — B38 IC-dedup wire at register.

Default-OFF behind HERMES_QUANT_IC_DEDUP_AT_INGEST. Uses HERMES_QUANT_ALPHA_ZOO_DIR
→ tmp via monkeypatch + a clean-source factor. No network.
"""

from __future__ import annotations

import numpy as np
import pytest

from hermes_quant.factors.alpha_zoo import (
    AlphaFactor,
    AlphaZoo,
    RedundantFactorError,
)
from hermes_quant.factors.ast_purity import PurityViolation
from hermes_quant.factors.ic_dedup import ICDedupGate

_CLEAN_SRC = 'bars["close"] - bars["open"]'


def _factor(name: str, src: str = _CLEAN_SRC) -> AlphaFactor:
    return AlphaFactor(
        name=name,
        description="test factor",
        source_code=src,
        author="test",
    )


@pytest.fixture
def zoo(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_ALPHA_ZOO_DIR", str(tmp_path))
    return AlphaZoo(base_dir=tmp_path)


def test_flag_off_no_dedup(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", raising=False)
    z = AlphaZoo(base_dir=tmp_path)
    rets = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    z.register(_factor("f_a"), factor_returns=rets)
    z.register(_factor("f_b"), factor_returns=rets.copy())
    assert len(z.list_all()) == 2


def test_flag_off_no_returns_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    z = AlphaZoo(base_dir=tmp_path)
    # No returns supplied → gate is a no-op.
    z.register(_factor("f_a"))
    z.register(_factor("f_b"))
    assert len(z.list_all()) == 2


def test_flag_on_rejects_near_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    z = AlphaZoo(base_dir=tmp_path)
    rng = np.random.default_rng(0)
    rets_a = rng.normal(size=200)
    z.register(_factor("factor_a"), factor_returns=rets_a)
    rets_b = rets_a + 1e-9 * rng.normal(size=200)
    with pytest.raises(RedundantFactorError) as exc:
        z.register(_factor("factor_b"), factor_returns=rets_b)
    assert exc.value.result.max_corr >= 0.99
    assert exc.value.result.correlated_with == "factor_a"


def test_flag_on_accepts_orthogonal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    z = AlphaZoo(base_dir=tmp_path)
    rng = np.random.default_rng(1)
    z.register(_factor("factor_a"), factor_returns=rng.normal(size=300))
    z.register(_factor("factor_b"), factor_returns=rng.normal(size=300))
    assert len(z.list_all()) == 2


def test_rejected_factor_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    z = AlphaZoo(base_dir=tmp_path)
    rng = np.random.default_rng(2)
    rets_a = rng.normal(size=200)
    z.register(_factor("factor_a"), factor_returns=rets_a)
    jsonl = tmp_path / "alpha_zoo.jsonl"
    n_lines_before = len(jsonl.read_text().strip().splitlines())

    dup = _factor("factor_b", src='bars["high"] - bars["low"]')
    with pytest.raises(RedundantFactorError):
        z.register(dup, factor_returns=rets_a + 1e-12)

    n_lines_after = len(jsonl.read_text().strip().splitlines())
    assert n_lines_after == n_lines_before
    assert dup.factor_id not in {f.factor_id for f in z.list_all()}


def test_threshold_env_respected(tmp_path, monkeypatch):
    # The gate's threshold env (HERMES_QUANT_IC_DEDUP_THRESHOLD) is read at
    # ic_dedup module-import time, so we pass the resolved value straight to the
    # gate constructor (= what the env would yield) and inject it. This proves
    # AlphaZoo honors the gate's configured threshold, not a hardcoded 0.99.
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_THRESHOLD", "0.5")
    gate = ICDedupGate(threshold=0.5)
    z = AlphaZoo(base_dir=tmp_path, ic_dedup_gate=gate)
    rng = np.random.default_rng(3)
    base = rng.normal(size=500)
    z.register(_factor("factor_a"), factor_returns=base)
    # B correlated ~0.6 with A: B = base + noise of comparable scale.
    noisy = base + rng.normal(size=500) * 1.1
    # confirm correlation lands above 0.5 but below 0.99
    corr = abs(np.corrcoef(base, noisy)[0, 1])
    assert 0.5 < corr < 0.99
    with pytest.raises(RedundantFactorError):
        z.register(_factor("factor_b", src='bars["high"] - bars["low"]'),
                   factor_returns=noisy)


def test_purity_gate_still_runs_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    z = AlphaZoo(base_dir=tmp_path)
    bad = _factor("evil", src='__import__("os").system("echo hi")')
    with pytest.raises(PurityViolation):
        z.register(bad, factor_returns=np.array([1.0, 2.0, 3.0]))


def test_injected_gate_used(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    gate = ICDedupGate()
    z = AlphaZoo(base_dir=tmp_path, ic_dedup_gate=gate)
    rng = np.random.default_rng(4)
    z.register(_factor("factor_a"), factor_returns=rng.normal(size=100))
    assert "factor_a" in gate.library
