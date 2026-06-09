"""Unit test for the multi-name ablation frame assembler.

The runner lives in scripts/ (operational tooling), but its pure frame-assembly
helper is load-bearing — it must produce the (field, symbol) MultiIndex-column
shape the WalkForwardEngine + AdvisorStrategy expect (engine reads
row[("close", sym)]; strategy slices lookback_data[(field, symbol)]). A regression
here would silently corrupt every multi-name flag verdict. Tested without network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quant-multiname-ablation.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("quant_multiname_ablation", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sym_frame(start, n, base=100.0):
    idx = pd.bdate_range(start=start, periods=n)
    rng = np.random.default_rng(abs(hash(str(start))) % (2**32))
    close = base * (1 + rng.normal(0, 0.01, n)).cumprod()
    return pd.DataFrame(
        {"open": close * 0.999, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": np.full(n, 1e6)},
        index=pd.DatetimeIndex(idx),
    )


def test_assemble_produces_field_symbol_multiindex():
    mod = _load_runner()
    frames = {"AAA": _sym_frame("2024-01-01", 100), "BBB": _sym_frame("2024-01-01", 100, base=50.0)}
    ohlcv, present = mod._assemble_multiindex(frames)
    assert isinstance(ohlcv.columns, pd.MultiIndex)
    assert set(present) == {"AAA", "BBB"}
    # Engine reads row[("close", sym)] — (field, symbol) ordering is REQUIRED.
    assert ("close", "AAA") in ohlcv.columns
    assert ("open", "BBB") in ohlcv.columns
    # Spot-check a value round-trips to the right symbol.
    assert ohlcv[("close", "AAA")].iloc[-1] == pytest.approx(frames["AAA"]["close"].iloc[-1])


def test_assemble_unions_days_and_ffills():
    mod = _load_runner()
    # BBB starts 5 business days later -> union index spans the earlier start; the
    # leading BBB gap is NOT back-filled (reindex+ffill only fills forward), but no
    # row is dropped for AAA's early days.
    frames = {"AAA": _sym_frame("2024-01-01", 60), "BBB": _sym_frame("2024-01-08", 55)}
    ohlcv, _ = mod._assemble_multiindex(frames)
    assert len(ohlcv) == len(set(frames["AAA"].index) | set(frames["BBB"].index))
    # AAA present on its own first day; BBB NaN there (no lookahead backfill).
    first = ohlcv.index.min()
    assert np.isfinite(ohlcv[("close", "AAA")].loc[first])


def test_assemble_requires_two_symbols():
    mod = _load_runner()
    with pytest.raises(ValueError):
        mod._assemble_multiindex({"AAA": _sym_frame("2024-01-01", 100)})
