"""RED->GREEN: a non-finite (NaN/inf) forward return must NOT poison the catalyst
profitability verdict.

`measure_profitability` joins the propagation log against realized forward returns
(injected fetcher) and reports a per-relation-class verdict (PROFITABLE /
UNPROFITABLE_CONSIDER_PRUNE / MARGINAL_HOLD). That verdict is the LIVE feedback
decision that tells the operator whether to RAISE the consumer-trend confidence
haircut toward 1.0 or prune the edges (profitability.format_report ACTION line).

The unscored-row guard was ``fwd is None or fwd == 0`` — but ``nan == 0`` is False
and ``inf == 0`` is False, so a single non-finite forward-return bar (a corrupt /
delisted / data-glitch close) slips past the guard and is summed into
``sum_signed_return``. One NaN poisons the WHOLE class's ``mean_signed_return`` to
NaN, so ``mean_signed_return > 0`` is False -> a class that is genuinely PROFITABLE
(high hit-rate, all-positive signed returns) silently degrades to MARGINAL_HOLD, and
the edge that EARNED a haircut-raise is told to hold. This is the
NaN-defeats-every-comparison money-software family: finite-guard every money input.

Sibling modules already finite-guard their return inputs (eval._is_finite_number,
onboarding._is_finite_number); profitability (the live haircut-decision join) did not.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from hermes_quant.catalyst.profitability import MIN_SAMPLE, measure_profitability


def _write_log(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "propagation-log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _brand_self_rows(n: int) -> list[dict]:
    # symbol_sign=+1 (predicted UP); shape matches propagate(..., log=) + asof stamp.
    return [
        {
            "symbol": "CELH",
            "source": "celsius energy",
            "relation": "brand_self",
            "effect_sign": -1,
            "weight": 0.9,
            "symbol_sign": 1,
            "catalyst_sign": 1,
            "asof": "2024-01-02T13:00:00+00:00",
        }
        for _ in range(n)
    ]


def test_nan_forward_return_does_not_poison_profitable_verdict(tmp_path):
    """A genuinely PROFITABLE class with ONE NaN forward-return bar must still
    report PROFITABLE — the NaN bar is unscored, not summed into the mean."""
    rows = _brand_self_rows(MIN_SAMPLE + 1)
    log = _write_log(tmp_path, rows)

    calls = {"n": 0}

    def fetcher(sym, dt):
        calls["n"] += 1
        if calls["n"] == 5:
            return float("nan")  # one corrupt bar
        return +3.0  # predicted up (sym_sign=+1), went up -> a genuine WIN

    stats = measure_profitability(fetcher, path=log)
    bs = stats["brand_self"]

    # non-vacuity: the class actually scored a real, profitable sample.
    assert bs.n_scored == MIN_SAMPLE  # the NaN bar is excluded, not scored
    assert bs.hits == MIN_SAMPLE
    assert bs.hit_rate >= 0.6
    # the mean must be FINITE and positive (not NaN-poisoned).
    assert math.isfinite(bs.mean_signed_return)
    assert bs.mean_signed_return > 0
    # the verdict the operator acts on must be the honest one.
    assert bs.verdict == "PROFITABLE"


def test_inf_forward_return_is_unscored(tmp_path):
    """An inf forward-return bar (e.g. a divide-by-near-zero entry price upstream)
    is also unscored, not summed -> the mean stays finite."""
    rows = _brand_self_rows(MIN_SAMPLE + 1)
    log = _write_log(tmp_path, rows)

    calls = {"n": 0}

    def fetcher(sym, dt):
        calls["n"] += 1
        if calls["n"] == 3:
            return float("inf")
        return +2.0

    stats = measure_profitability(fetcher, path=log)
    bs = stats["brand_self"]
    assert bs.n_scored == MIN_SAMPLE
    assert math.isfinite(bs.mean_signed_return)
    assert bs.mean_signed_return > 0
    assert bs.verdict == "PROFITABLE"


def test_finite_zero_and_none_still_unscored(tmp_path):
    """Regression: the original None / exact-zero unscored behavior is preserved
    (the fix only ADDS the non-finite case)."""
    rows = _brand_self_rows(4)
    log = _write_log(tmp_path, rows)

    seq = [None, 0.0, +2.0, +2.0]
    calls = {"n": 0}

    def fetcher(sym, dt):
        v = seq[calls["n"]]
        calls["n"] += 1
        return v

    stats = measure_profitability(fetcher, path=log)
    bs = stats["brand_self"]
    # only the two +2.0 rows score; None and 0.0 are unscored as before.
    assert bs.n_scored == 2
    assert bs.hits == 2
    assert math.isfinite(bs.mean_signed_return)
