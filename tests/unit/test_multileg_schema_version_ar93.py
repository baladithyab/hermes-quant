"""ar93 — multileg _record_to_dict must serialize schema_version (mirror paper.py).

react/multileg.py defines its OWN ExecutionRecord serializer (`_record_to_dict`, used by
`_write_family` to write every multi-leg parent + child to executions.jsonl). It omitted
the `schema_version` field that the canonical react/paper.py:_record_to_dict serializes
(line 79) — only the multileg copy diverged (alpaca_paper imports paper's version).

`schema_version` is the ADR-0091 Option-E tag that
`FillDeltaNormalizer.is_absolute_target_record()` keys off. With a non-None
schema_version (the documented Option-E "new records stamp the version explicitly" path),
paper's serializer keeps the key (is_absolute_target=False, correct) while multileg's
dropped it → reads back as None → is_absolute_target_record returns True → the normalizer
would DOUBLE-DIFFERENCE the legs → wrong qty → wrong NAV / kill-switch basis.

Currently DORMANT (no producer stamps a non-None schema_version today, so both serializers
read None and behavior is byte-identical) — a latent forward-compat defect on the
immutable money-log. FIX (ar93): serialize record.schema_version in the multileg dict too.
"""

from __future__ import annotations

from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.multileg import _record_to_dict as multileg_to_dict
from hermes_quant.react.paper import _record_to_dict as paper_to_dict
from hermes_quant.state.fill_delta_normalizer import is_absolute_target_record


def _record(schema_version: str | None) -> ExecutionRecord:
    return ExecutionRecord(
        proposal_id="p1", signal_id="s1", asset="AAPL260620C00100000",
        asset_class="us_option", timeframe="1d",
        asof_decision="2026-05-13T20:00:00Z", asof_execution="2026-05-13T20:00:00Z",
        target_position_pct=0.05, decision_price=200.0, fill_price=5.0, fill_size_pct=0.05,
        reactor_name="multi_leg_paper", human_in_the_loop=False,
        schema_version=schema_version,
    )


def test_ar93_multileg_serializes_nonnull_schema_version():
    """A non-None schema_version (Option-E stamped) must survive the multileg
    serializer and agree with the canonical paper serializer."""
    rec = _record("true-delta-v2")
    md = multileg_to_dict(rec)
    pd = paper_to_dict(rec)
    assert md.get("schema_version") == "true-delta-v2", (
        "multileg serializer dropped schema_version — the FillDeltaNormalizer would "
        "then mis-classify the record and double-difference the legs"
    )
    assert md.get("schema_version") == pd.get("schema_version"), "serializers must agree"


def test_ar93_normalizer_agrees_across_serializers():
    """is_absolute_target_record must return the SAME verdict on both serializers'
    output — the divergence this fix closes."""
    rec = _record("true-delta-v2")
    assert is_absolute_target_record(multileg_to_dict(rec)) == is_absolute_target_record(
        paper_to_dict(rec)
    ), "multileg vs paper serializer disagree on is_absolute_target (schema_version drop)"
    # Specifically: a stamped true-delta record must read as NOT-absolute on both.
    assert is_absolute_target_record(multileg_to_dict(rec)) is False


def test_ar93_none_schema_version_byte_identical():
    """Non-vacuity / byte-identity: the default None schema_version round-trips
    identically on both serializers (the dormant common case — unchanged behavior)."""
    rec = _record(None)
    md = multileg_to_dict(rec)
    pd = paper_to_dict(rec)
    assert md.get("schema_version") is None
    assert md.get("schema_version") == pd.get("schema_version")
    assert is_absolute_target_record(md) == is_absolute_target_record(pd)
