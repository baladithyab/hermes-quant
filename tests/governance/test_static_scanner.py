"""Tests for hermes_quant.governance.static_scanner (ADR-0031, Vibe-Trading pattern)."""

from __future__ import annotations

import pytest

from hermes_quant.governance.static_scanner import (
    ScanFinding,
    ScanResult,
    StaticScannerError,
    require_clean,
    scan_text,
)


def test_scan_clean_text_returns_no_findings():
    text = "def add(a, b):\n    return a + b\n"
    result = scan_text(text, mode="research")
    assert result.findings == ()
    assert result.blocked is False
    assert result.blocking_findings == ()


def test_scan_alpaca_submit_order_blocks_in_research():
    text = "client = alpaca.submit_order(symbol='AAPL', qty=1)"
    result = scan_text(text, mode="research")
    assert result.blocked is True
    assert any("submit_order" in f.matched_text for f in result.findings)


def test_scan_alpaca_submit_order_warns_in_live():
    text = "client = alpaca.submit_order(symbol='AAPL', qty=1)"
    result = scan_text(text, mode="live")
    # finding is recorded but not blocking under 'live' (warn_research only blocks in research/paper)
    assert len(result.findings) >= 1
    assert result.blocked is False


def test_scan_alpaca_submit_order_blocks_in_paper():
    text = "client = alpaca.submit_order(symbol='AAPL', qty=1)"
    result = scan_text(text, mode="paper")
    assert result.blocked is True


def test_scan_live_broker_import_always_blocks():
    text = "from hermes_quant.react.live import LiveBroker\n"
    for mode in ("research", "paper", "shadow", "live"):
        result = scan_text(text, mode=mode)  # type: ignore[arg-type]
        assert result.blocked is True, f"should block in mode={mode}"


def test_scan_naked_http_post_to_alpaca_blocks():
    text = "requests.post('https://paper-api.alpaca.markets/v2/orders', json={})"
    result = scan_text(text, mode="live")
    assert result.blocked is True
    # 'block' severity holds even in live mode
    assert any(f.severity == "block" for f in result.findings)


def test_scan_bypass_risk_gate_blocks():
    text = "bypass_risk_gate(True)"
    result = scan_text(text, mode="research")
    assert result.blocked is True


def test_scan_skip_immutables_blocks():
    text = "skip_immutable = True\nskip_immutables = False\n"
    result = scan_text(text, mode="research")
    assert result.blocked is True
    # both 'skip_immutable' and 'skip_immutables' should match
    assert len(result.findings) >= 2


def test_scan_moon_dev_substring_match_pattern_blocks():
    for variant in ("disable", "skip", "ignore"):
        text = f"if '{variant}' in user_input:\n    return\n"
        result = scan_text(text, mode="research")
        assert result.blocked is True, f"variant={variant} should block"


def test_require_clean_raises_on_blocked():
    text = "alpaca.submit_order(symbol='AAPL')"
    with pytest.raises(StaticScannerError) as exc_info:
        require_clean(text, mode="research")
    assert "submit_order" in str(exc_info.value) or "alpaca" in str(exc_info.value)


def test_require_clean_passes_on_clean_text():
    text = "def safe_function():\n    return 42\n"
    assert require_clean(text, mode="research") is None


def test_scan_finding_includes_line_number_and_matched_text():
    text = "\n".join(
        [
            "import os",  # line 1
            "import sys",  # line 2
            "",  # line 3
            "def f():",  # line 4
            "    alpaca.submit_order(qty=1)",  # line 5
        ]
    )
    result = scan_text(text, mode="research")
    submit_findings = [f for f in result.findings if "submit_order" in f.matched_text]
    assert len(submit_findings) == 1
    assert submit_findings[0].line_number == 5
    assert "submit_order" in submit_findings[0].matched_text


def test_scan_collects_multiple_findings_in_one_pass():
    text = "alpaca.submit_order(qty=1)\nbypass_risk_gate(True)\n"
    result = scan_text(text, mode="research")
    assert len(result.findings) == 2
    severities = {f.severity for f in result.findings}
    assert "warn_research" in severities
    assert "block" in severities
    assert result.blocked is True
