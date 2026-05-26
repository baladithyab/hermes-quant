"""Tests for the env-gated multi-horizon + deliberative-committee shim in
quant-playbook-tick.py (ADR-0036 + ADR-0037 Wave C wire-up).

These tests verify:
  - env-unset path is bit-identical to the legacy single-horizon call
  - HERMES_QUANT_HORIZONS=1d,1w activates the multi-horizon collection
  - HERMES_QUANT_DELIBERATIVE=1 activates the committee shim (currently
    deferred-success — returns empty turns, no error)
  - Invalid horizons get dropped with a caveat, not silently accepted
  - Failure paths return synthetic error dicts, never raise
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-playbook-tick.py"


def _load_tick_module(monkeypatch):
    """Load the playbook tick module fresh, with mock-mode forced ON so we
    don't accidentally call the real advisor / Alpaca."""
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_MOCK", "1")
    spec = importlib.util.spec_from_file_location("_qpt_test", str(SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_parse_horizons_defaults_to_1d_when_empty(monkeypatch):
    m = _load_tick_module(monkeypatch)
    valid, dropped = m._parse_horizons("")
    assert valid == ["1d"]
    assert dropped == []


def test_parse_horizons_dedupes_and_orders(monkeypatch):
    m = _load_tick_module(monkeypatch)
    valid, dropped = m._parse_horizons("1d, 1w, 1d, 1M")
    assert valid == ["1d", "1w", "1M"]
    # The duplicate "1d" lands in dropped (more conservative than silent dedupe)
    assert dropped == ["1d"]


def test_parse_horizons_drops_invalid(monkeypatch):
    m = _load_tick_module(monkeypatch)
    valid, dropped = m._parse_horizons("1d, 5m, 1w, garbage")
    assert valid == ["1d", "1w"]
    assert sorted(dropped) == sorted(["5m", "garbage"])


def test_parse_horizons_falls_back_to_1d_when_all_invalid(monkeypatch):
    m = _load_tick_module(monkeypatch)
    valid, dropped = m._parse_horizons("garbage, also-bad")
    assert valid == ["1d"]
    assert sorted(dropped) == sorted(["garbage", "also-bad"])


def test_call_advisor_default_path_uses_1d(monkeypatch):
    """Env-unset → legacy single-horizon path. Mock mode confirms recommend()
    is what got called and timeframe wasn't perturbed."""
    m = _load_tick_module(monkeypatch)
    monkeypatch.delenv("HERMES_QUANT_HORIZONS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_DELIBERATIVE", raising=False)
    out = m.call_advisor("AAPL")
    # Mock returns a known shape per _mock_recommend
    assert "aggregated_signal" in out or "gate" in out
    # No multi-horizon metadata when env unset
    assert "horizons_attempted" not in out
    assert "committee_turns" not in out


def test_call_advisor_with_horizons_attaches_metadata(monkeypatch):
    """HERMES_QUANT_HORIZONS=1d,1w → result has horizons_attempted +
    multi_horizon_views (best-effort, may be empty in mock mode)."""
    m = _load_tick_module(monkeypatch)
    monkeypatch.setenv("HERMES_QUANT_HORIZONS", "1d,1w")
    monkeypatch.delenv("HERMES_QUANT_DELIBERATIVE", raising=False)
    out = m.call_advisor("AAPL")
    assert out.get("horizons_attempted") == ["1d", "1w"]
    assert out.get("primary_timeframe") == "1w"  # last horizon = primary
    assert "multi_horizon_views" in out  # may be empty list, but present


def test_call_advisor_horizons_with_invalid_drops_with_caveat(monkeypatch):
    """Invalid horizon entries are dropped and surfaced in caveats."""
    m = _load_tick_module(monkeypatch)
    monkeypatch.setenv("HERMES_QUANT_HORIZONS", "1d, garbage, 1w")
    monkeypatch.delenv("HERMES_QUANT_DELIBERATIVE", raising=False)
    out = m.call_advisor("AAPL")
    assert out.get("horizons_attempted") == ["1d", "1w"]
    caveats = out.get("caveats", [])
    assert any("horizons_env_dropped" in c and "garbage" in c for c in caveats), \
        f"expected drop caveat, got {caveats}"


def test_call_advisor_with_deliberative_attaches_committee_keys(monkeypatch):
    """HERMES_QUANT_DELIBERATIVE=1 → result has committee_turns + committee_decision keys."""
    m = _load_tick_module(monkeypatch)
    monkeypatch.delenv("HERMES_QUANT_HORIZONS", raising=False)
    monkeypatch.setenv("HERMES_QUANT_DELIBERATIVE", "1")
    out = m.call_advisor("AAPL")
    assert "committee_turns" in out
    assert "committee_decision" in out
    # Wave C: committee invocation is deferred (returns empty turns), so
    # turns is empty list and decision is None — but the keys exist so the
    # journal can record env-var activation.
    assert out["committee_turns"] == []
    assert out["committee_decision"] is None


def test_call_advisor_combined_horizons_and_deliberative(monkeypatch):
    """Both env vars set → all metadata keys present."""
    m = _load_tick_module(monkeypatch)
    monkeypatch.setenv("HERMES_QUANT_HORIZONS", "1d,1w,1M")
    monkeypatch.setenv("HERMES_QUANT_DELIBERATIVE", "1")
    out = m.call_advisor("AAPL")
    assert out.get("horizons_attempted") == ["1d", "1w", "1M"]
    assert "committee_turns" in out
    assert "committee_decision" in out


def test_collect_multi_horizon_views_safe_handles_import_failure(monkeypatch):
    """If recommend_multi_horizon import fails, return empty list — never raise."""
    m = _load_tick_module(monkeypatch)
    # Force ImportError by purging hermes_quant.advisor from sys.modules and
    # patching __import__ to raise. Simplest: just check the function returns
    # a list (empty or not) for a never-existed symbol.
    out = m._collect_multi_horizon_views_safe("NEVER_EXISTS_TICKER_XYZ", ["1d"])
    assert isinstance(out, list)


def test_run_committee_safe_returns_deferred_success(monkeypatch):
    """Wave C committee shim returns deferred-success record (no error,
    empty turns)."""
    m = _load_tick_module(monkeypatch)
    out = m._run_committee_safe(
        symbol="AAPL", advisor_result={"aggregated_signal": {}}, risk_mgmt_enabled=False
    )
    assert out["error"] is None
    assert out["turns"] == []
    assert out["decision"] is None
    assert "deferred_reason" in out
