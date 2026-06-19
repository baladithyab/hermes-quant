"""sv1 + fl1 (Increment-1 critique): the pdr_core.Fill contract and the fold's
is_absolute_target_record classifier MUST agree on the schema_version sentinel, and
Fill must validate its money fields.

The bug (sv1): pdr_core.Fill stamped schema_version=int 1, but
react.base.is_absolute_target_record only recognized None or the string
'absolute-target-v1'. So a Fill-driven record classified as TRUE-DELTA, and the
normalizer passed the absolute target through as a raw per-fill delta — re-inflating
N re-affirmations to N*target (the exact AAPL-12x defect, inside the contract built
to fix it).

The bug (fl1): Fill had no __post_init__, so NaN/negative fill_price and NaN
fill_size_pct were accepted and would poison FillDeltaNormalizer.running_net for the
whole bucket.
"""

from __future__ import annotations

import math

import pytest

from hermes_quant.pdr_core.contracts import FILL_SCHEMA_VERSION, Fill
from hermes_quant.react.base import is_absolute_target_record


def _fill(**over):
    base = dict(
        proposal_id="p1", asset="AAPL", asset_class="equity",
        fill_price=100.0, fill_size_pct=0.05, asof_execution="2026-06-13T00:00:00Z",
    )
    base.update(over)
    return Fill(**base)


# ---- sv1: the sentinel the Fill stamps must classify as absolute-target ----

def test_fill_schema_version_classifies_as_absolute_target():
    f = _fill()
    rec = {"schema_version": f.schema_version}
    assert is_absolute_target_record(rec) is True, (
        f"Fill default schema_version={f.schema_version!r} is NOT recognized as "
        "absolute-target by the fold classifier — a Fill-driven record would be "
        "treated as a true-delta and re-inflate."
    )


def test_default_fill_schema_version_is_absolute_target_sentinel():
    # The default must round-trip through is_absolute_target_record as absolute-target.
    assert is_absolute_target_record({"schema_version": FILL_SCHEMA_VERSION}) is True


# ---- fl1: Fill must reject non-finite / non-positive money fields ----

def test_fill_rejects_nan_fill_size_pct():
    with pytest.raises((ValueError, TypeError)):
        _fill(fill_size_pct=float("nan"))


def test_fill_rejects_nan_fill_price():
    with pytest.raises((ValueError, TypeError)):
        _fill(fill_price=float("nan"))


def test_fill_rejects_nonpositive_fill_price():
    with pytest.raises((ValueError, TypeError)):
        _fill(fill_price=0.0)
    with pytest.raises((ValueError, TypeError)):
        _fill(fill_price=-5.0)


def test_valid_fill_constructs():
    f = _fill()
    assert math.isfinite(f.fill_price) and f.fill_price > 0
    assert math.isfinite(f.fill_size_pct)
