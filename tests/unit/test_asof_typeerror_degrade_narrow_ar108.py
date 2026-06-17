"""ar108 — the as_of TypeError-degrade must be NARROW (only a genuine no-as_of provider
degrades; any other TypeError propagates) at every fetch_bars-with-as_of seam.

The idiom `except TypeError as exc: if "as_of" in str(exc) or "unexpected keyword" in
str(exc): retry-without-as_of` was over-broad: an unrelated TypeError naming SOME OTHER
unexpected keyword (or raised inside fetch_bars' body) was misclassified as a legacy
provider and silently retried WITHOUT as_of — DROPPING the no-lookahead bound (fail-OPEN).
This is the systemic family the review team flagged after the ar103-followup fixed the
data/base.py instance; ar108 narrows the 4 remaining sites:
  - hermes_quant/advisor.py (_fetch_bars_for_horizon ~:506, _fetch_with_as_of ~:941)
  - hermes_quant/perception/builder.py (_fetch_with_as_of ~:82)
  - hermes_quant/data/horizon_cache.py (~:239)

Fix: require BOTH the bad-signature shape (unexpected keyword / got multiple values) AND
the literal `as_of` token in the message.

This module pins the BEHAVIORAL CONTRACT by replicating the exact narrowed predicate the
four sites now use, exercised against (a) a genuine no-as_of legacy provider (degrades,
byte-identical back-compat) and (b) a provider that raises an UNRELATED TypeError (must
propagate, NOT silently drop as_of). A source-level guard against re-widening lives in
the AST check at the bottom (structure-based, not string-heuristic).
"""

from __future__ import annotations

import ast
import inspect

import pandas as pd
import pytest

_OHLCV = pd.DataFrame(
    {
        "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [10.0, 20.0],
    }
)


class _LegacyNoAsofProvider:
    """A genuine legacy provider whose fetch_bars has NO as_of kwarg -> must degrade."""

    def __init__(self):
        self.calls = []

    def fetch_bars(self, symbol, timeframe, start, end, **kwargs):
        if "as_of" in kwargs:
            raise TypeError("fetch_bars() got an unexpected keyword argument 'as_of'")
        self.calls.append((symbol, timeframe))
        return _OHLCV.copy()


class _UnrelatedTypeErrorProvider:
    """Accepts as_of but raises an UNRELATED TypeError (a DIFFERENT unexpected keyword)
    from its body. The narrow degrade must PROPAGATE this, never retry without as_of."""

    def __init__(self):
        self.calls = []

    def fetch_bars(self, symbol, timeframe, start, end, *, as_of=None):
        self.calls.append("with_asof" if as_of is not None else "without_asof")
        raise TypeError("some_helper() got an unexpected keyword argument 'frobnicate'")


def _degrade_seam(provider):
    """Replicates the EXACT narrowed predicate the four source sites now use."""
    start = pd.Timestamp("2026-01-01", tz="UTC")
    end = pd.Timestamp("2026-01-02", tz="UTC")
    asof_ts = end
    try:
        return provider.fetch_bars("X", "1d", start, end, as_of=asof_ts)
    except TypeError as exc:
        msg = str(exc)
        if "as_of" in msg and ("unexpected keyword" in msg or "got multiple values" in msg):
            return provider.fetch_bars("X", "1d", start, end)
        raise


def test_ar108_legacy_provider_degrades():
    """A genuine no-as_of provider still degrades (byte-identical back-compat)."""
    prov = _LegacyNoAsofProvider()
    df = _degrade_seam(prov)
    assert not df.empty
    assert prov.calls == [("X", "1d")]  # the degraded (no-as_of) retry succeeded


def test_ar108_unrelated_typeerror_propagates():
    """An unrelated TypeError (different kwarg) must PROPAGATE, NOT silently retry
    without as_of (which would drop the no-lookahead bound — the fail-open)."""
    prov = _UnrelatedTypeErrorProvider()
    with pytest.raises(TypeError, match="frobnicate"):
        _degrade_seam(prov)
    assert "without_asof" not in prov.calls, (
        "an unrelated TypeError was swallowed and retried WITHOUT as_of — the no-lookahead "
        "bound was silently dropped (the ar108 fail-open)"
    )


def test_ar108_source_sites_require_asof_in_the_and_clause():
    """SOURCE-BOUND guard against re-widening: at each site, the BoolOp that contains
    the "unexpected keyword" string must be an OR nested INSIDE an AND whose other
    operand checks `as_of` — i.e. the "unexpected keyword" OR is never the TOP of the
    degrade predicate. AST-structural (not a string heuristic), so comments are ignored.
    """
    from hermes_quant import advisor
    from hermes_quant.data import base as data_base
    from hermes_quant.data import horizon_cache
    from hermes_quant.perception import builder

    for mod in (advisor, data_base, horizon_cache, builder):
        tree = ast.parse(inspect.getsource(mod))
        # Structural check: every `"unexpected keyword"` constant must have an ancestor
        # BoolOp(And) that ALSO contains an `"as_of"` constant (the narrow guard).
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "unexpected keyword":
                # climb to the nearest enclosing And; it must contain an "as_of" const.
                cur = parents.get(id(node))
                guarded = False
                while cur is not None:
                    if isinstance(cur, ast.BoolOp) and isinstance(cur.op, ast.And):
                        consts = {
                            c.value for c in ast.walk(cur)
                            if isinstance(c, ast.Constant) and isinstance(c.value, str)
                        }
                        if any("as_of" in v for v in consts):
                            guarded = True
                            break
                    cur = parents.get(id(cur))
                assert guarded, (
                    f"{mod.__name__}: an 'unexpected keyword' degrade check is not nested "
                    "under an AND that also requires 'as_of' (over-broad re-widening)"
                )
