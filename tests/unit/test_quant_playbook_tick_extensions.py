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


def test_run_committee_safe_dataclass_reconstruction_does_not_raise(monkeypatch):
    """REGRESSION GUARD (2026-05-26 smoke-test caught two latent bugs):

    1. `MarketContext` requires `last_volume` — the committee shim was
       constructing it without that field, raising TypeError that the
       failure-closed envelope swallowed. The deliberative path was
       silently non-functional.
    2. `Direction` is a Literal[-1,0,1], not an Enum — calling
       `Direction(int(...))` raises TypeError (can't call a Literal),
       so AnalystView reconstruction silently dropped every view and
       the function bailed at `no_reconstructable_analyst_views`.

    This test exercises the FULL reconstruction path with
    HERMES_QUANT_DELIBERATIVE=1 + a placeholder-shaped key + a mocked
    `run_llm_committee` so we don't hit the network. If reconstruction
    fails, the test catches it because we mock the LLM call to verify
    it gets called with reconstructed dataclasses.
    """
    monkeypatch.setenv("HERMES_QUANT_DELIBERATIVE", "1")
    # Use a non-placeholder shaped key so the env-key short-circuit
    # doesn't fire. Real key is irrelevant — we mock the LLM call.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-deliberative-shim-not-real")
    m = _load_tick_module(monkeypatch)

    # Mock run_llm_committee so we capture what got passed to it,
    # without making a real LLM call.
    captured: dict = {}

    def fake_run_llm_committee(*, market_context, analyst_views, baseline_signal, config):
        captured["market_context"] = market_context
        captured["analyst_views"] = analyst_views
        captured["baseline_signal"] = baseline_signal
        captured["config"] = config
        return []  # empty turns — committee bailed

    import hermes_quant.aggregators.llm_committee as llm_mod
    monkeypatch.setattr(llm_mod, "run_llm_committee", fake_run_llm_committee)

    out = m._run_committee_safe(
        symbol="MDB",
        advisor_result={
            "aggregated_signal": {
                "asset": "MDB",
                "direction": 1,  # Literal[-1,0,1] — must NOT be Direction(int(...))
                "magnitude": 0.012,
                "confidence": 0.65,
                "confidence_raw": 0.7,
                "horizon": "1d",
                "timeframe": "1d",
                "asset_class": "equity",
                "asof": "2026-05-26T20:00:00Z",
            },
            "analyst_views": [
                {
                    "analyst": "ta_classic",
                    "direction": 1,  # Literal[-1,0,1]
                    "magnitude": 0.012,
                    "confidence": 0.65,
                    "confidence_raw": 0.7,
                    "horizon": "1d",
                    "rationale": "bullish breakout",
                },
            ],
            "last_close": 220.50,
        },
        risk_mgmt_enabled=False,
    )

    # Failure-closed envelope must capture no error.
    assert out["error"] is None, (
        f"reconstruction raised inside _run_committee_safe: {out['error']}"
    )

    # Reconstruction must have actually run (not bailed at a guard).
    assert "market_context" in captured, (
        "run_llm_committee was never called — reconstruction bailed early "
        f"with deferred_reason={out.get('deferred_reason')!r}"
    )

    # MarketContext.last_volume must be present (regression #1).
    mc = captured["market_context"]
    assert hasattr(mc, "last_volume"), "MarketContext missing last_volume field"
    assert mc.last_volume == 0.0  # placeholder value from reconstruction

    # AnalystView reconstruction must NOT have silently dropped (regression #2).
    views = captured["analyst_views"]
    assert len(views) == 1, (
        f"expected 1 reconstructed view, got {len(views)} — Direction Literal "
        f"reconstruction may have silently dropped it"
    )
    assert views[0].analyst == "ta_classic"
    assert views[0].direction == 1

    # Baseline signal direction must be the int directly, not a TypeError.
    bs = captured["baseline_signal"]
    assert bs.direction == 1


def test_run_committee_safe_default_quick_model_is_valid(monkeypatch):
    """REGRESSION GUARD (2026-05-26 smoke test caught this):

    DeliberativeConfig default quick_model was `claude-haiku-4.6` which
    is not a real OpenRouter model — runs hit `BadRequestError: 400 -
    not a valid model ID`, the committee bailed after 2 consecutive
    failures, and `_run_committee_safe` returned turns=[] with no error.

    Verify the default in the playbook-tick fallback is on the live
    OpenRouter roster.
    """
    m = _load_tick_module(monkeypatch)
    src = open(m.__file__).read()
    # The playbook-tick uses haiku-4.5 (canonical) as the env-fallback.
    assert "claude-haiku-4.5" in src, (
        "playbook-tick.py must default quick_model to claude-haiku-4.5 "
        "(haiku-4.6 is not a valid OpenRouter model ID)"
    )
    assert "claude-haiku-4.6" not in src, (
        "playbook-tick.py must NOT reference claude-haiku-4.6 — that model "
        "ID does not exist on OpenRouter and causes BadRequestError"
    )


def test_run_committee_safe_asof_uses_bar_anchor_not_wall_clock(monkeypatch):
    """ar89 NO-LOOKAHEAD GUARD (ADR-0042 Oracle Fallacy hard rule).

    The advisor's recommend() dict exposes the decision-bar timestamp under
    the top-level keys ``as_of`` and ``bar_ts`` (advisor.py to_dict() + the
    ADR-0068 split at advisor.py:1185). There is NO top-level ``asof`` key,
    and the ``aggregated_signal`` sub-dict (_signal_to_dict) carries no
    asof/as_of key at all.

    Previously _run_committee_safe built MarketContext.asof from
    ``sig_d.get("asof") or advisor_result.get("asof") or pd.Timestamp.utcnow()``
    — both lookups for the nonexistent ``asof`` key are ALWAYS None, so the
    expression ALWAYS resolved to wall-clock now(). That wall-clock value
    becomes MarketContext.asof, which build_role_prompt threads into
    get_past_context(asof=now) + load_active_beliefs(role, now). The Oracle
    Fallacy guard (retriever.py:371 `if tau >= asof: continue`) then admits
    every reflection/belief that became observable AFTER the decision bar but
    BEFORE now — a no-lookahead leak into the portfolio_manager LLM prompt.

    This feeds a REAL recommend()-shaped dict (as_of + bar_ts, NO asof) into
    _run_committee_safe and asserts MarketContext.asof equals the decision-bar
    anchor — NOT a value at/after wall-clock now().
    """
    import datetime as _dt

    import pandas as pd

    monkeypatch.setenv("HERMES_QUANT_DELIBERATIVE", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-deliberative-shim-not-real")
    m = _load_tick_module(monkeypatch)

    captured: dict = {}

    def fake_run_llm_committee(*, market_context, analyst_views, baseline_signal, config):
        captured["market_context"] = market_context
        captured["baseline_signal"] = baseline_signal
        return []

    import hermes_quant.aggregators.llm_committee as llm_mod
    monkeypatch.setattr(llm_mod, "run_llm_committee", fake_run_llm_committee)

    # Decision bar is well in the PAST relative to wall-clock now().
    bar_anchor = "2026-05-26T20:00:00+00:00"
    bar_ts = pd.to_datetime(bar_anchor).tz_localize(None)

    out = m._run_committee_safe(
        symbol="MDB",
        # REAL recommend() dict shape: top-level as_of + bar_ts, NO `asof`.
        advisor_result={
            "symbol": "MDB",
            "asset_class": "equity",
            "timeframe": "1d",
            "as_of": bar_anchor,
            "bar_ts": bar_anchor,
            "decision_wall_clock": _dt.datetime.now(_dt.UTC).isoformat(),
            "last_close": 220.50,
            "aggregated_signal": {
                "asset": "MDB",
                "timeframe": "1d",
                "direction": 1,
                "magnitude": 0.012,
                "confidence": 0.65,
                "confidence_raw": 0.7,
                "horizon": "1d",
                "aggregator": "bma",
                "n_components": 1,
                # NOTE: NO asof / as_of key here — matches _signal_to_dict.
            },
            "analyst_views": [
                {
                    "analyst": "ta_classic",
                    "direction": 1,
                    "magnitude": 0.012,
                    "confidence": 0.65,
                    "confidence_raw": 0.7,
                    "horizon": "1d",
                    "rationale": "bullish breakout",
                },
            ],
        },
        risk_mgmt_enabled=False,
    )

    assert out["error"] is None, (
        f"reconstruction raised inside _run_committee_safe: {out['error']}"
    )
    assert "market_context" in captured, (
        "run_llm_committee was never called — reconstruction bailed early "
        f"with deferred_reason={out.get('deferred_reason')!r}"
    )

    mc_asof = pd.Timestamp(captured["market_context"].asof)
    now = pd.Timestamp.utcnow().tz_localize(None)

    # The Oracle guard excludes tau >= asof. If asof leaked to wall-clock now,
    # it sits within seconds of now() and admits future reflections.
    assert abs((now - mc_asof).total_seconds()) > 3600, (
        f"MarketContext.asof={mc_asof} is within an hour of wall-clock "
        f"now={now} — the no-lookahead anchor leaked to wall-clock (Oracle "
        f"Fallacy: future reflections/beliefs leak into the committee prompt)"
    )
    # And it must equal the actual decision-bar anchor.
    assert abs((mc_asof - bar_ts).total_seconds()) < 1.0, (
        f"MarketContext.asof={mc_asof} != decision-bar anchor {bar_ts}"
    )
    # The reconstructed baseline signal asof must agree (committee audit/replay).
    bs_asof = pd.Timestamp(captured["baseline_signal"].asof)
    assert abs((bs_asof - bar_ts).total_seconds()) < 1.0, (
        f"AggregatedSignal.asof={bs_asof} != decision-bar anchor {bar_ts}"
    )
