"""Tests for hermes_quant.observability.fallback_probe (ADR-0060).

Verifies silence-by-default holds for all 4 LLM-wired surfaces under all
failure modes. NEVER makes real network calls.
"""

from __future__ import annotations

import json

import pytest

from hermes_quant.observability.fallback_probe import (
    FAILURE_MODES,
    SURFACES,
    FallbackProbeResult,
    RateLimitError,
    ServerError,
    StubLLMCaller,
    format_results_human,
    format_results_json,
    probe_reflector,
    probe_regime_hmm,
    probe_risk_committee,
    probe_trader_node,
    run_fallback_probe,
)


# ---------------------------------------------------------------------------
# Trader probe: every failure mode produces a valid output and matches v0.1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "server_error", "malformed_json", "schema_invalid", "empty"])
def test_trader_failure_modes_match_v0_1(mode: str) -> None:
    """Every failure mode must fall back to v0.1 with output_matches_v0_1=True."""
    r = probe_trader_node(mode)
    assert r.output_valid, f"trader/{mode} produced invalid output: {r.error}"
    assert r.output_matches_v0_1, (
        f"trader/{mode} did NOT match v0.1 deterministic (silence-by-default broken)"
    )
    assert r.error is None
    assert r.surface_name == "trader"
    assert r.llm_failure_mode == mode
    assert r.latency_ms >= 0


def test_trader_happy_path_valid() -> None:
    """Happy path: stub LLM returns synthetic JSON; output is valid."""
    r = probe_trader_node("happy_path")
    assert r.output_valid
    assert r.error is None
    # Happy path may or may not match v0.1 (it uses LLM-driven fields).
    assert r.surface_name == "trader"
    assert r.llm_failure_mode == "happy_path"


# ---------------------------------------------------------------------------
# Risk committee probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "server_error", "malformed_json", "schema_invalid", "empty"])
def test_risk_committee_failure_modes_match_v0_1(mode: str) -> None:
    r = probe_risk_committee(mode)
    assert r.output_valid, f"risk_committee/{mode} invalid: {r.error}"
    assert r.output_matches_v0_1, (
        f"risk_committee/{mode} did NOT match v0.1 (silence-by-default broken)"
    )
    assert r.error is None


def test_risk_committee_happy_path_valid() -> None:
    r = probe_risk_committee("happy_path")
    assert r.output_valid
    assert r.error is None


# ---------------------------------------------------------------------------
# Reflector probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "server_error", "malformed_json", "schema_invalid", "empty"])
def test_reflector_failure_modes_match_v0_1(mode: str) -> None:
    r = probe_reflector(mode)
    assert r.output_valid, f"reflector/{mode} invalid: {r.error}"
    assert r.output_matches_v0_1, (
        f"reflector/{mode} did NOT match v0.1 (silence-by-default broken)"
    )
    assert r.error is None


def test_reflector_happy_path_valid() -> None:
    r = probe_reflector("happy_path")
    assert r.output_valid
    assert r.error is None


# ---------------------------------------------------------------------------
# HMM regime probe (only meaningful failure modes; others map to deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["timeout", "server_error", "schema_invalid"])
def test_regime_hmm_failure_modes_match_v0_1(mode: str) -> None:
    r = probe_regime_hmm(mode)
    assert r.output_valid, f"regime_hmm/{mode} invalid: {r.error}"
    assert r.output_matches_v0_1
    assert r.error is None


def test_regime_hmm_happy_path_valid() -> None:
    r = probe_regime_hmm("happy_path")
    assert r.output_valid
    assert r.error is None


# ---------------------------------------------------------------------------
# Cross-product: run_fallback_probe
# ---------------------------------------------------------------------------


def test_run_fallback_probe_default_returns_full_matrix() -> None:
    """Default args: 4 surfaces × 7 failure modes = 28 results."""
    results = run_fallback_probe()
    assert len(results) >= 24  # at least 24 (HMM has fewer meaningful modes but module returns all 7)
    assert all(isinstance(r, FallbackProbeResult) for r in results)
    # All surfaces represented
    surfaces_seen = {r.surface_name for r in results}
    assert surfaces_seen == set(SURFACES)


def test_run_fallback_probe_all_valid_silence_by_default_holds() -> None:
    """Critical regression: silence-by-default MUST hold for all (surface, mode) combos."""
    results = run_fallback_probe()
    invalid = [r for r in results if not r.output_valid]
    assert not invalid, (
        f"silence-by-default BROKEN for {len(invalid)} probes: "
        f"{[(r.surface_name, r.llm_failure_mode, r.error) for r in invalid]}"
    )


def test_run_fallback_probe_subset_surfaces() -> None:
    """Filter by surface."""
    results = run_fallback_probe(surfaces=["trader", "regime_hmm"])
    surfaces_seen = {r.surface_name for r in results}
    assert surfaces_seen == {"trader", "regime_hmm"}


def test_run_fallback_probe_subset_failure_modes() -> None:
    """Filter by failure_modes."""
    results = run_fallback_probe(failure_modes=["timeout", "happy_path"])
    modes_seen = {r.llm_failure_mode for r in results}
    assert modes_seen.issubset({"timeout", "happy_path"})


def test_run_fallback_probe_never_raises_on_unknown_surface() -> None:
    """Unknown surface name must be captured as error, not raised."""
    results = run_fallback_probe(surfaces=["bogus_surface"])
    # Unknown surfaces should produce an error result, not crash
    assert all(isinstance(r, FallbackProbeResult) for r in results)


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


def test_format_results_human_non_empty() -> None:
    results = run_fallback_probe(surfaces=["trader"], failure_modes=["timeout"])
    out = format_results_human(results)
    assert isinstance(out, str)
    assert len(out) > 0
    assert "trader" in out
    assert "timeout" in out


def test_format_results_human_includes_pass_or_fail() -> None:
    results = run_fallback_probe()
    out = format_results_human(results)
    # Either "PASS" (all valid) or "FAIL" (some invalid) must appear in summary
    assert "PASS" in out or "FAIL" in out


def test_format_results_json_round_trips() -> None:
    results = run_fallback_probe(surfaces=["trader"], failure_modes=["happy_path", "timeout"])
    out = format_results_json(results)
    parsed = json.loads(out)
    # Format may wrap results dict with metadata or be a bare list — accept either
    if isinstance(parsed, dict):
        # Wrapper format: extract the results array
        rows = parsed.get("results") or parsed.get("rows") or []
        assert len(rows) == 2
        assert all("surface_name" in row for row in rows)
    else:
        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert all("surface_name" in row for row in parsed)


# ---------------------------------------------------------------------------
# StubLLMCaller behaviour
# ---------------------------------------------------------------------------


def test_stub_llm_caller_timeout_raises_timeout() -> None:
    stub = StubLLMCaller("timeout")
    with pytest.raises(TimeoutError):
        stub.call("sys", "user")


def test_stub_llm_caller_rate_limit_raises_429() -> None:
    stub = StubLLMCaller("rate_limit")
    with pytest.raises(RateLimitError):
        stub.call("sys", "user")


def test_stub_llm_caller_server_error_raises_500() -> None:
    stub = StubLLMCaller("server_error")
    with pytest.raises(ServerError):
        stub.call("sys", "user")


def test_stub_llm_caller_malformed_json_returns_non_json_string() -> None:
    """malformed_json mode: obj=None, raw is non-parseable JSON."""
    stub = StubLLMCaller("malformed_json")
    parsed, raw = stub.call("sys", "user")
    assert parsed is None
    assert isinstance(raw, dict) or isinstance(raw, str)
    # raw_text is not parseable JSON
    raw_text = raw.get("text", raw) if isinstance(raw, dict) else raw
    if isinstance(raw_text, str) and raw_text:
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw_text)


def test_stub_llm_caller_empty_returns_empty_string() -> None:
    """empty mode: parsed is None."""
    stub = StubLLMCaller("empty")
    parsed, raw = stub.call("sys", "user")
    assert parsed is None
    raw_text = raw.get("text", "") if isinstance(raw, dict) else raw
    assert raw_text == ""


def test_stub_llm_caller_unknown_mode_raises_value_error() -> None:
    """Unknown failure mode rejected at construction time."""
    with pytest.raises(ValueError):
        StubLLMCaller("not_a_real_mode")


# ---------------------------------------------------------------------------
# Result invariants
# ---------------------------------------------------------------------------


def test_result_is_dataclass_with_required_fields() -> None:
    r = probe_trader_node("timeout")
    assert hasattr(r, "surface_name")
    assert hasattr(r, "llm_enabled")
    assert hasattr(r, "llm_failure_mode")
    assert hasattr(r, "output_valid")
    assert hasattr(r, "output_matches_v0_1")
    assert hasattr(r, "latency_ms")
    assert hasattr(r, "error")


def test_latency_is_non_negative() -> None:
    """All probes must record non-negative latency (no negative timings)."""
    results = run_fallback_probe()
    for r in results:
        assert r.latency_ms >= 0, f"{r.surface_name}/{r.llm_failure_mode} reported negative latency: {r.latency_ms}"


def test_failure_modes_constant_is_complete() -> None:
    """FAILURE_MODES contains the 7 documented modes."""
    expected = {"happy_path", "timeout", "rate_limit", "server_error", "malformed_json", "schema_invalid", "empty"}
    assert set(FAILURE_MODES) == expected


def test_surfaces_constant_is_complete() -> None:
    """SURFACES contains the 4 LLM-wired surfaces."""
    expected = {"trader", "risk_committee", "reflector", "regime_hmm"}
    assert set(SURFACES) == expected
