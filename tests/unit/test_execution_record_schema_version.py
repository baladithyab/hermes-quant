"""Increment-0 §0.1 (seed ra01): ExecutionRecord gains a nullable schema_version.

Option-E (ADR-0091) keeps the producers UNCHANGED — they keep writing the ABSOLUTE
target into the per-fill size field. The delta is derived at FOLD time by the shared
normalizer. To let the fold tell an absolute-target record from a (future) true-delta
record, ExecutionRecord carries a schema_version:
  - None / absent  -> legacy absolute-target (every historical record IS this)
  - SCHEMA_ABSOLUTE_TARGET -> explicit absolute-target (new records stamp this)

The field is nullable + defaulted (same back-compat pattern as bar_ts / play_tag), so
old records read back cleanly and existing serialized readers are unaffected.
"""

from __future__ import annotations

from hermes_quant.react.base import (
    SCHEMA_ABSOLUTE_TARGET,
    ExecutionRecord,
    is_absolute_target_record,
)


def _rec(**over):
    base = dict(
        proposal_id="p1",
        signal_id=None,
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-06-13T00:00:00Z",
        asof_execution="2026-06-13T00:00:00Z",
        target_position_pct=0.05,
        decision_price=100.0,
        fill_price=100.0,
        fill_size_pct=0.05,
        reactor_name="paper",
        human_in_the_loop=True,
    )
    base.update(over)
    return ExecutionRecord(**base)


def test_schema_version_defaults_none_back_compat():
    # A record built without schema_version reads back as None — old persisted
    # records (which lack the field) deserialize the same way.
    rec = _rec()
    assert rec.schema_version is None


def test_schema_version_can_be_stamped_absolute_target():
    rec = _rec(schema_version=SCHEMA_ABSOLUTE_TARGET)
    assert rec.schema_version == SCHEMA_ABSOLUTE_TARGET


def test_is_absolute_target_record_treats_none_as_absolute_target():
    # The fold must interpret a legacy/None-version record as absolute-target,
    # because every historical record IS an absolute-target record.
    assert is_absolute_target_record({"schema_version": None}) is True
    assert is_absolute_target_record({}) is True  # field absent entirely
    assert is_absolute_target_record({"schema_version": SCHEMA_ABSOLUTE_TARGET}) is True


def test_is_absolute_target_record_false_for_explicit_true_delta():
    # A future true-delta-version record must NOT be re-differenced by the fold.
    assert is_absolute_target_record({"schema_version": "true-delta-v1"}) is False
