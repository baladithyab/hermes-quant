"""tests/grounding/test_verifier.py — Unit tests for ClaimVerifier.

Wave 5 acceptance tests:
  - ClaimVerifier accepts views with all-cited claims
  - ClaimVerifier rejects views with un-cited fabricated prices
  - ≥95% rejection on a synthetic batch of 20 fabricated views (DROP-RATE TARGET)
  - Regex handles all required numerical formats
"""
from __future__ import annotations

import pytest

from hermes_quant.grounding.data_grounding import Bar, build_ground_truth_block
from hermes_quant.grounding.verifier import (
    ClaimVerifier,
    VerificationResult,
    extract_numerical_claims,
    extract_citation_markers,
)
from hermes_quant.protocol import AnalystView


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_block(symbol: str = "AAPL", n_bars: int = 10):
    """Build a small GroundTruthBlock with predictable prices."""
    bars = []
    from datetime import date, timedelta

    d = date(2026, 5, 1)
    for i in range(n_bars):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = 170.00 + i * 0.50
        bars.append(
            Bar(
                date_str=d.isoformat(),
                open=close - 0.25,
                high=close + 0.50,
                low=close - 0.50,
                close=close,
                volume=5_000_000,
            )
        )
        d += timedelta(days=1)
    return build_ground_truth_block(symbol, "2026-05-27", ohlcv_bars=bars)


def _make_view(rationale: str, analyst: str = "test_analyst") -> AnalystView:
    return AnalystView(
        analyst=analyst,
        direction=1,
        magnitude=0.01,
        confidence=0.6,
        confidence_raw=0.75,
        horizon="1d",
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Test: regex numerical extraction
# ---------------------------------------------------------------------------


def test_extract_plain_decimal():
    claims = extract_numerical_claims("The stock is at 1.23 today")
    assert "1.23" in claims


def test_extract_currency():
    claims = extract_numerical_claims("Price moved to $185.50")
    assert "$185.50" in claims


def test_extract_percentage_positive():
    claims = extract_numerical_claims("Up +2.50% on the day")
    assert "+2.50%" in claims


def test_extract_percentage_negative():
    claims = extract_numerical_claims("Down -0.75% from close")
    assert "-0.75%" in claims


def test_extract_comma_thousands():
    claims = extract_numerical_claims("Volume was 1,234,567 shares")
    # The regex handles comma-thousands groups
    combined = "".join(claims)
    assert "1,234" in combined or "1234" in combined or "234,567" in combined


def test_extract_currency_comma_thousands():
    claims = extract_numerical_claims("Market cap is $2,345.67 billion")
    assert "$2,345.67" in claims


def test_extract_no_false_positives_on_plain_words():
    claims = extract_numerical_claims("The market is bullish today")
    assert claims == []


def test_extract_citation_markers():
    text = "Close [gt_AAPL_20260527_close] confirmed the breakout"
    markers = extract_citation_markers(text)
    assert "gt_AAPL_20260527_close" in markers


# ---------------------------------------------------------------------------
# Test: accept views with cited claims
# ---------------------------------------------------------------------------


def test_accept_no_numerical_claims():
    """Views with zero numerical claims must be trivially accepted."""
    block = _make_block()
    view = _make_view("The stock looks bullish based on technical patterns.")
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)
    assert result.accepted is True
    assert result.citation_coverage == 1.0
    assert result.uncited_claims == []


def test_accept_view_with_citation_marker():
    """Views that cite via [gt_...] marker must be accepted."""
    block = _make_block("AAPL")
    # Use a real citation ID from the block
    cid = block.citation_ids[-1]  # last bar's close
    last_bar = block.ohlcv_60d[-1]
    rationale = (
        f"The closing price was {last_bar.close:.2f} [{cid}], confirming the uptrend."
    )
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)
    assert result.accepted is True, f"Expected accepted but got: {result.reason}"


def test_accept_price_present_in_gt_text():
    """A price that literally appears in ground-truth render must be accepted."""
    block = _make_block("AAPL")
    last_close = block.ohlcv_60d[-1].close
    rationale = f"Price is {last_close:.4f} based on ground truth."
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)
    assert result.accepted is True, f"Expected accepted. Reason: {result.reason}"


# ---------------------------------------------------------------------------
# Test: reject views with un-cited fabricated prices
# ---------------------------------------------------------------------------


def test_reject_fabricated_price():
    """A fabricated price not in ground truth must cause rejection at threshold=0.5."""
    block = _make_block("AAPL")
    # Price 999.99 is NOT in the block (closes are 170.00–174.50)
    rationale = "The target price is 999.99 and the resistance is at 1050.00."
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)
    assert result.accepted is False, (
        f"Expected rejection of fabricated price but got accepted. Reason: {result.reason}"
    )
    assert len(result.uncited_claims) > 0


def test_reject_fabricated_percentage():
    """A fabricated percentage return not derivable from ground truth must be rejected."""
    block = _make_block("AAPL")
    rationale = "Expected return is +42.00% based on our model."
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)
    assert result.accepted is False, (
        f"Expected rejection of fabricated percentage but got: {result.reason}"
    )


def test_reject_fully_uncited_view():
    """A view with all claims fabricated must have coverage=0.0 and be rejected."""
    block = _make_block("AAPL")
    # All prices are outside the block's close range
    rationale = (
        "At $5,000.00 resistance, we see +127.50% upside to $6,350.00. "
        "Volume target is 99,999,999."
    )
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)
    assert result.accepted is False
    assert result.citation_coverage < 0.5


# ---------------------------------------------------------------------------
# Test: ≥95% DROP-RATE on synthetic batch of 20 fabricated views
# ---------------------------------------------------------------------------


def test_drop_rate_95pct_on_20_fabricated_views():
    """ClaimVerifier must reject ≥95% of 20 synthetic views with un-cited claims.

    Wave 5 acceptance criterion: verifier rejects ≥95% of un-cited views.
    This test constructs 20 views with entirely fabricated numerical claims
    (prices far outside the 170–174.50 ground-truth range) and asserts that
    at most 1 view slips through (floor(20*0.05) = 1).
    """
    block = _make_block("AAPL", n_bars=10)
    verifier = ClaimVerifier(threshold=0.5)

    # 20 fabricated views — prices/percentages nowhere near the GT block
    fabricated_rationales = [
        "Price target $500.00, upside +210.00%",
        "Resistance at 800.00, support at 750.00",
        "Expected return: -35.50%, stop at $600.00",
        "Volume spike: 99,999,999 shares, price $450.25",
        "Fair value: $1,200.50 with +400.00% upside",
        "Moving average at 380.00, RSI at 72.50",
        "Fibonacci target 620.00, stop loss 580.00",
        "Implied volatility 45.50%, delta 0.75",
        "Short float 25.30%, days to cover 12.50",
        "EPS $3.75, P/E ratio 35.20",
        "Revenue $450.00B, growth +18.50%",
        "Debt/equity 2.35, interest coverage 5.75",
        "Beta 1.85, Sharpe 2.10 annualized",
        "52-week high $520.00, low $310.00",
        "Breakout above 490.00 targeting 550.00",
        "Gap fill at 475.00, then 510.00",
        "Options implied move ±6.50%, gamma 0.025",
        "Analyst consensus $535.00 PT, buy rating 78.30%",
        "Institutional ownership 89.20%, insider buying $2.5M",
        "After-hours: +3.75% to $495.00 on earnings beat",
    ]

    rejected = 0
    for rationale in fabricated_rationales:
        view = _make_view(rationale)
        result = verifier.verify(view, block)
        if not result.accepted:
            rejected += 1

    rejection_rate = rejected / len(fabricated_rationales)
    assert rejection_rate >= 0.95, (
        f"DROP-RATE FAILURE: ClaimVerifier only rejected {rejected}/20 "
        f"({rejection_rate:.0%}) un-cited fabricated views. "
        f"Wave 5 requires ≥95% rejection rate."
    )


# ---------------------------------------------------------------------------
# Test: threshold configurability
# ---------------------------------------------------------------------------


def test_threshold_strict_rejects_partial_citations():
    """At threshold=1.0, a view with any un-cited claim must be rejected."""
    block = _make_block("AAPL", n_bars=5)
    cid = block.citation_ids[-1]
    last_close = block.ohlcv_60d[-1].close
    # The fabricated number 999.99 is placed far from any citation marker —
    # more than 80 chars away — so it will not get proximity credit.
    separator = "x" * 100  # force > 80 char gap
    rationale = (
        f"Close was {last_close:.4f} [{cid}]. "
        f"{separator}"
        f"But our target is 999.99"
    )
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=1.0)
    result = verifier.verify(view, block)
    assert result.accepted is False, (
        "strict threshold=1.0 should reject when any claim is un-cited. "
        f"Got: {result.reason}"
    )


def test_threshold_strict_rejects_fabricated_marker_proximity():
    """Phantom price next to a FABRICATED (out-of-block) citation marker must be rejected.

    F3 regression: an LLM can append a syntactically-valid-but-fabricated marker
    next to a fabricated price to steal proximity credit. The marker
    ``[gt_AAPL_99999999_close]`` is NOT in block.citation_ids — placing 999.99
    adjacent to it must NOT credit the claim. A separate, REAL citation appears
    far away (>80 chars) so ``has_valid_explicit_citation`` is True, exposing the
    bug where the proximity fallback matches ANY marker rather than a valid one.
    """
    block = _make_block("AAPL", n_bars=5)
    real_cid = block.citation_ids[-1]
    last_close = block.ohlcv_60d[-1].close
    fabricated_marker = "gt_AAPL_99999999_close"
    assert fabricated_marker not in set(block.citation_ids)
    separator = "x" * 100  # force the real marker > 80 chars from the phantom price
    rationale = (
        f"Close was {last_close:.4f} [{real_cid}]. "
        f"{separator}"
        f"But our target is 999.99 [{fabricated_marker}]"
    )
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=1.0)
    result = verifier.verify(view, block)
    assert result.accepted is False, (
        "A fabricated price adjacent to an out-of-block citation marker must be "
        f"rejected at strict threshold. Got: {result.reason}"
    )
    assert "999.99" in result.uncited_claims, (
        f"999.99 should be flagged uncited. uncited={result.uncited_claims}"
    )


def test_threshold_lenient_accepts_partial_citations():
    """At threshold=0.1, a view with ≥10% citation coverage must be accepted."""
    block = _make_block("AAPL", n_bars=5)
    cid = block.citation_ids[-1]
    last_close = block.ohlcv_60d[-1].close
    # One real price, many fabricated — coverage should be ~25%+ depending on regex
    rationale = (
        f"Close {last_close:.4f} [{cid}]. Targets: 999.00, 1000.00, 1001.00."
    )
    view = _make_view(rationale)
    verifier = ClaimVerifier(threshold=0.1)
    result = verifier.verify(view, block)
    assert result.accepted is True, (
        f"lenient threshold=0.1 should accept ≥10% coverage. Got: {result.reason}"
    )


def test_invalid_threshold_raises():
    """ClaimVerifier must raise ValueError for threshold outside [0, 1]."""
    with pytest.raises(ValueError):
        ClaimVerifier(threshold=1.5)
    with pytest.raises(ValueError):
        ClaimVerifier(threshold=-0.1)


# ---------------------------------------------------------------------------
# Test: VerificationResult structure
# ---------------------------------------------------------------------------


def test_verification_result_fields():
    """VerificationResult must have expected fields with correct types."""
    block = _make_block("AAPL")
    view = _make_view("Price is 999.99")
    verifier = ClaimVerifier(threshold=0.5)
    result = verifier.verify(view, block)

    assert isinstance(result, VerificationResult)
    assert isinstance(result.accepted, bool)
    assert isinstance(result.citation_coverage, float)
    assert isinstance(result.uncited_claims, list)
    assert result.reason is None or isinstance(result.reason, str)
    assert 0.0 <= result.citation_coverage <= 1.0
