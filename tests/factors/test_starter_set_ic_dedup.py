"""tests/factors/test_starter_set_ic_dedup.py — B38 starter-set IC-dedup wiring.

Verifies ``register_starter_set`` forwards a per-factor ``factor_returns``
mapping into ``AlphaZoo.register`` so the IC-dedup gate can run at ingest.

Default-OFF: with no mapping (every current call-site) the path is
byte-identical to the prior two-gate-only behavior — all factors register and
the IC gate never runs. Offline/deterministic; uses HERMES_QUANT_ALPHA_ZOO_DIR
→ tmp via monkeypatch. No network.
"""

from __future__ import annotations

import numpy as np
import pytest

from hermes_quant.factors.alpha_zoo import (
    AlphaZoo,
    RedundantFactorError,
)
from hermes_quant.factors.ic_dedup import ICDedupGate
from hermes_quant.factors.starter_set import (
    _STARTER_FACTORS,
    compute_starter_factor_returns,
    register_starter_set,
)

_N = len(_STARTER_FACTORS)


@pytest.fixture
def zoo(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_ALPHA_ZOO_DIR", str(tmp_path))
    return AlphaZoo(base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Flag-OFF: byte-identical to the prior path — all factors register.
# ---------------------------------------------------------------------------


def test_flag_off_no_mapping_registers_all(zoo, monkeypatch):
    """No mapping + flag unset → every starter factor registers (back-compat)."""
    monkeypatch.delenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", raising=False)
    ids = register_starter_set(zoo)
    assert len(ids) == _N
    assert len(zoo.list_all()) == _N


def test_flag_off_with_mapping_still_registers_all(zoo, monkeypatch):
    """A mapping is supplied but the flag is OFF → gate is a no-op, all register.

    Even with deliberately-identical return series for two factors, the gate
    does not run while the flag is unset, so no factor is rejected.
    """
    monkeypatch.delenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", raising=False)
    dup = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    mapping = {defn["name"]: dup.copy() for defn in _STARTER_FACTORS}
    ids = register_starter_set(zoo, factor_returns=mapping)
    assert len(ids) == _N
    assert len(zoo.list_all()) == _N


def test_flag_on_no_mapping_registers_all(zoo, monkeypatch):
    """Flag ON but no mapping → gate is a no-op (no returns), all register."""
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    ids = register_starter_set(zoo)
    assert len(ids) == _N
    assert len(zoo.list_all()) == _N


# ---------------------------------------------------------------------------
# Flag-ON: the gate runs and rejects a near-duplicate; orthogonal passes.
# ---------------------------------------------------------------------------


def test_flag_on_rejects_near_duplicate(zoo, monkeypatch):
    """Flag ON + a near-duplicate (IC ≥ 0.99) factor is rejected at ingest."""
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    rng = np.random.default_rng(0)
    base = rng.normal(size=200)

    names = [defn["name"] for defn in _STARTER_FACTORS]
    first, second = names[0], names[1]
    # First factor gets an independent series; second is first + tiny noise
    # (correlation ≥ 0.99) so the gate must reject it.
    mapping = {
        first: base,
        second: base + 1e-9 * rng.normal(size=200),
    }

    with pytest.raises(RedundantFactorError) as exc:
        register_starter_set(zoo, factor_returns=mapping)

    assert exc.value.result.max_corr >= 0.99
    assert exc.value.result.correlated_with == first
    # The first factor persisted; the rejected duplicate did not — and the
    # rejection short-circuits the loop before any later factor is reached.
    assert len(zoo.list_all()) == 1


def test_flag_on_accepts_independent_factor(zoo, monkeypatch):
    """Flag ON + two orthogonal series → both register (gate passes)."""
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    rng = np.random.default_rng(1)
    names = [defn["name"] for defn in _STARTER_FACTORS]
    # Independent random series for the first two; the rest have no mapping
    # entry → registered with factor_returns=None (gate no-op for them).
    mapping = {
        names[0]: rng.normal(size=300),
        names[1]: rng.normal(size=300),
    }
    ids = register_starter_set(zoo, factor_returns=mapping)
    assert len(ids) == _N
    assert len(zoo.list_all()) == _N


def test_flag_on_partial_mapping_only_gates_mapped_factors(zoo, monkeypatch):
    """Factors with no mapping entry are registered with the gate as a no-op.

    A duplicate pair is given series; the remaining factors have NO entry, so
    they pass straight through (factor_returns=None). The duplicate is still
    rejected — proving per-factor forwarding, not all-or-nothing.
    """
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    rng = np.random.default_rng(2)
    base = rng.normal(size=200)
    names = [defn["name"] for defn in _STARTER_FACTORS]
    mapping = {
        names[0]: base,
        names[1]: base + 1e-12,  # near-perfect duplicate of names[0]
    }
    with pytest.raises(RedundantFactorError):
        register_starter_set(zoo, factor_returns=mapping)
    # Only the first (independent) factor got persisted before the reject.
    assert len(zoo.list_all()) == 1


def test_flag_on_uses_injected_gate(tmp_path, monkeypatch):
    """An injected ICDedupGate is the one populated by the starter-set ingest."""
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    gate = ICDedupGate()
    z = AlphaZoo(base_dir=tmp_path, ic_dedup_gate=gate)
    rng = np.random.default_rng(3)
    names = [defn["name"] for defn in _STARTER_FACTORS]
    mapping = {names[0]: rng.normal(size=100)}
    register_starter_set(z, factor_returns=mapping)
    # The mapped factor's returns were registered into the injected gate.
    assert names[0] in gate.library


# ---------------------------------------------------------------------------
# compute_starter_factor_returns — the production-caller input builder (ar23).
# Produces the {name: returns} mapping the IC-dedup gate consumes at ingest.
# ---------------------------------------------------------------------------


def _ohlcv(n: int = 100, seed: int = 0):
    import pandas as pd

    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(size=n))
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": rng.integers(1_000_000, 2_000_000, n).astype(float),
        },
        index=idx,
    )


def test_compute_starter_factor_returns_keys_by_name_dropna(monkeypatch, tmp_path):
    """Returns are keyed by factor NAME (the key register_starter_set looks up),
    each value is a finite (dropna'd) >=2-length series, and computing the probe
    set does NOT write to the real registry dir."""
    import hermes_quant.factors.alpha_zoo as az

    monkeypatch.setattr(az, "_DEFAULT_DIR", tmp_path)
    mapping = compute_starter_factor_returns(_ohlcv())
    names = {defn["name"] for defn in _STARTER_FACTORS}
    assert set(mapping).issubset(names)
    assert len(mapping) >= 1
    for name, arr in mapping.items():
        assert name in names
        assert np.isfinite(arr).all()
        assert arr.size >= 2


def test_compute_then_register_gates_real_duplicate_starters(monkeypatch, tmp_path):
    """End-to-end of the production wiring on a duplicate starter set: compute
    returns -> register with the flag ON -> the near-duplicate is rejected."""
    import hermes_quant.factors.alpha_zoo as az
    import hermes_quant.factors.starter_set as ss

    monkeypatch.setattr(az, "_DEFAULT_DIR", tmp_path)
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_AT_INGEST", "1")
    monkeypatch.setattr(
        ss,
        "_STARTER_FACTORS",
        [
            {"name": "d_a", "description": "x", "source_code": 'bars["close"] - bars["open"]', "tags": []},
            {"name": "d_b", "description": "x", "source_code": 'bars["close"] - bars["open"] + 1e-12', "tags": []},
        ],
    )
    bars = _ohlcv()
    mapping = compute_starter_factor_returns(bars)
    z = AlphaZoo(base_dir=tmp_path)
    with pytest.raises(RedundantFactorError):
        register_starter_set(z, factor_returns=mapping)
