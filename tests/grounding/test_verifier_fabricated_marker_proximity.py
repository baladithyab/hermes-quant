"""tests/grounding/test_verifier_fabricated_marker_proximity.py

Regression coverage for the ClaimVerifier no-proximity contract (wave-7 / the ar65
successor). A numeric claim is cited IFF its value is traceable in the ground-truth
block text — there is NO proximity-to-marker fallback. This pins the loophole the
wave-7 fix closed: a fabricated number is NOT credited by sitting near a citation
marker, whether that marker is fabricated OR valid.

History: an earlier verifier credited a numeric claim under a "check (b)" proximity
fallback — if a valid citation appeared anywhere in the rationale AND any [gt_...]
bracket sat within 80 chars of the claim. A stale-base hunt agent (wave-15) re-found
the FABRICATED-marker variant of that bug on an old base; on the integration HEAD the
proximity fallback was already removed entirely (strictly stronger), so the fabricated
case is covered AND the valid-marker proximity-credit is intentionally gone. These tests
encode the HEAD contract, not the removed proximity behavior.
"""
from __future__ import annotations

from datetime import date, timedelta

from hermes_quant.grounding.data_grounding import Bar, build_ground_truth_block
from hermes_quant.grounding.verifier import ClaimVerifier
from hermes_quant.protocol import AnalystView


def _make_block(symbol: str = "AAPL", n_bars: int = 5):
    bars = []
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


def _make_view(rationale: str) -> AnalystView:
    return AnalystView(
        analyst="fabricated_marker_test",
        direction=1,
        magnitude=0.01,
        confidence=0.6,
        confidence_raw=0.75,
        horizon="1d",
        rationale=rationale,
    )


def test_fabricated_number_near_fabricated_marker_is_uncited():
    """A fabricated price next to a FABRICATED citation marker must NOT be credited,
    even though a separate legitimate citation exists elsewhere in the rationale."""
    block = _make_block("AAPL", n_bars=5)
    valid_cid = block.citation_ids[-1]
    last_close = block.ohlcv_60d[-1].close

    separator = "z" * 200
    rationale = (
        f"Close was {last_close:.4f} [{valid_cid}]. "
        f"{separator} "
        f"Our price target is 999.99 [gt_FAKE_99999999_close] strong buy."
    )
    view = _make_view(rationale)

    result = ClaimVerifier(threshold=1.0).verify(view, block)
    assert "999.99" in result.uncited_claims, (
        f"Fabricated 999.99 near a FABRICATED marker must be UNCITED. "
        f"uncited={result.uncited_claims}, reason={result.reason}"
    )
    assert result.accepted is False


def test_fabricated_number_near_valid_marker_is_also_uncited():
    """THE STRONGER HEAD CONTRACT (wave-7): a fabricated number NOT in the GT text is
    UNCITED even when parked next to a VALID citation marker. Proximity to a valid
    marker is NOT sufficient — block markers are coarse (one per close) and attest to a
    single value; crediting an arbitrary nearby number is the one-citation-covers-all
    loophole the proximity fallback was removed to close. (An earlier ar65-era verifier
    WOULD have credited this; HEAD does not.)"""
    block = _make_block("AAPL", n_bars=5)
    valid_cid = block.citation_ids[-1]
    # 888.88 is NOT in the GT close range (170-172) and not in render text.
    rationale = f"The implied fair value is 888.88 [{valid_cid}], confirming uptrend."
    view = _make_view(rationale)

    result = ClaimVerifier(threshold=1.0).verify(view, block)
    assert "888.88" in result.uncited_claims, (
        "888.88 is absent from the ground-truth text and must stay UNCITED even next "
        f"to a VALID marker (no proximity credit at HEAD). reason={result.reason}"
    )
    assert result.accepted is False


def test_number_present_in_gt_text_is_cited():
    """Non-vacuity: a number that IS in the ground-truth render text is correctly
    credited (the verifier isn't rejecting everything)."""
    block = _make_block("AAPL", n_bars=5)
    last_close = block.ohlcv_60d[-1].close  # e.g. 172.0, present in the render
    rationale = f"The latest close is {last_close:.4f} — momentum intact."
    view = _make_view(rationale)

    result = ClaimVerifier(threshold=1.0).verify(view, block)
    assert result.accepted is True, (
        f"a number present in the GT text must be cited+accepted. reason={result.reason}"
    )
    assert f"{last_close:.4f}".rstrip("0").rstrip(".") not in result.uncited_claims or not result.uncited_claims
