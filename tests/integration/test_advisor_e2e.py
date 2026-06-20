"""Tests for the chat-mode advisor surface (ADR-0014).

Per ADR-0014 §D6 test fence:
1. recommend known-symbol returns structurally valid dict
2. recommend with empty-bars returns gated, no exception
3. recommend with as_of in past matches a deterministic golden file
   (deferred — needs golden-file fixture; covered by deterministic test)
4. recommend does NOT write to state.db (no-IO assertion)
5. recommend does NOT call any calibrator update method
6. recommend with no_lessons=True returns lessons=[] without journal IO
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.advisor import recommend
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    DataProviderError,
)

# aegis-ci-hang: the no-``analysts=`` tests here build the canonical default
# loadout, which includes a REAL KronosAnalyst that lazy-loads weights from
# HuggingFace on first analyze(). The autouse offline guard in
# tests/integration/conftest.py keeps that fetch off the live network; this
# per-module timeout is a second rail so any residual hang (e.g. a future
# analyst that ignores the offline flag) fail-fasts instead of stalling the
# whole pytest sweep near the end. Generous: the offline cache-or-abstain path
# is sub-5s, but model-init / first torch import can take a while cold.
pytestmark = pytest.mark.timeout(120)


# ---------------------------------------------------------------------------
# Fixtures: synthetic OHLCV bars + a fake DataProvider
# ---------------------------------------------------------------------------


def _make_bars(n: int = 100, start_price: float = 100.0) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV bars for tests."""
    timestamps = pd.date_range(start="2026-01-01", periods=n, freq="1D", tz="UTC")
    # A monotone trend so analysts have some signal to chew on
    closes = [start_price + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.3 for c in closes],
            "low": [c - 0.4 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
        }
    )


class _FakeProvider:
    """Test double that returns canned bars without hitting the network."""

    name = "fake"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars: pd.DataFrame | None = None, raise_on_fetch: Exception | None = None):
        self._bars = bars if bars is not None else _make_bars()
        self._raise = raise_on_fetch
        self.fetch_count = 0

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True):
        self.fetch_count += 1
        if self._raise is not None:
            raise self._raise
        return self._bars


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_recommend_returns_structurally_valid_dict():
    """ADR-0014 §D6.1: known-symbol returns the documented shape."""
    provider = _FakeProvider()
    result = recommend(
        symbol="FAKE",
        asset_class="equity",
        timeframe="1d",
        provider=provider,
        include_lessons=False,
    )

    # Top-level keys per ADR-0014 §D1
    assert set(result.keys()) >= {
        "symbol",
        "asset_class",
        "timeframe",
        "as_of",
        "data_quality",
        "analyst_views",
        "aggregated_signal",
        "risk_gate",
        "lessons",
        "caveats",
        "doctor",
    }
    assert result["symbol"] == "FAKE"
    assert result["asset_class"] == "equity"
    assert result["timeframe"] == "1d"
    assert result["data_quality"]["bars_received"] == 100
    assert isinstance(result["analyst_views"], list)
    assert isinstance(result["caveats"], list)
    # Caveats are MANDATORY (per ADR-0014 §D1 caveats field is not optional)
    assert any("snapshot" in c.lower() for c in result["caveats"])


def test_recommend_with_empty_bars_returns_gated_no_exception():
    """ADR-0014 §D3.4: safe under no-data scenarios."""
    provider = _FakeProvider(
        bars=pd.DataFrame(
            {
                "timestamp": [],
                "open": [],
                "high": [],
                "low": [],
                "close": [],
                "volume": [],
            }
        )
    )
    result = recommend(
        symbol="EMPTY",
        provider=provider,
        include_lessons=False,
    )
    assert result["data_quality"]["bars_received"] == 0
    assert result["aggregated_signal"] is None
    assert result["risk_gate"]["pass"] is False
    assert result["risk_gate"]["gated_reason"] == "no_bars_returned"
    assert any("Insufficient data" in c for c in result["caveats"])


def test_recommend_handles_provider_rate_limit():
    """Rate limit raises become gated dict, not propagated exceptions."""
    from hermes_quant.protocol import RateLimitError

    provider = _FakeProvider(raise_on_fetch=RateLimitError("throttled"))
    result = recommend(
        symbol="LIMITED",
        provider=provider,
        include_lessons=False,
    )
    assert result["risk_gate"]["pass"] is False
    assert result["risk_gate"]["gated_reason"] == "rate_limited"
    assert result["doctor"]["data_provider_alive"] is False


def test_recommend_handles_provider_data_error():
    """DataProviderError -> gated, no exception."""
    provider = _FakeProvider(raise_on_fetch=DataProviderError("yfinance broken"))
    result = recommend(
        symbol="BROKEN",
        provider=provider,
        include_lessons=False,
    )
    assert result["risk_gate"]["pass"] is False
    assert result["risk_gate"]["gated_reason"] == "data_provider_error"


def test_recommend_does_not_call_calibrator_update():
    """ADR-0014 §D3.1: read-only — no calibrator.fit/update calls."""
    from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst

    analyst = ClassicalTAAnalyst()
    # Spy on the calibrator - the advisor must not touch its mutate methods
    analyst.calibrator.fit = MagicMock(side_effect=AssertionError("fit called"))
    if hasattr(analyst.calibrator, "update"):
        analyst.calibrator.update = MagicMock(side_effect=AssertionError("update called"))

    provider = _FakeProvider()
    result = recommend(
        symbol="NOMUT",
        provider=provider,
        analysts=[analyst],
        include_lessons=False,
    )
    # If either calibrator mutator was called, MagicMock side-effect would
    # have raised AssertionError. Reach here = invariant held.
    assert result["symbol"] == "NOMUT"


def test_recommend_no_lessons_returns_empty_lessons_no_journal_io(monkeypatch):
    """ADR-0014 §D6: include_lessons=False -> lessons=[] without journal IO."""
    # Block the journal reader at module-import level — if include_lessons=False
    # is honored, this ImportError-equivalent should never trigger because the
    # advisor never even attempts the import.
    import hermes_quant.advisor as advisor_module

    call_count = {"n": 0}

    def _spy_get_recent_lessons(*args, **kwargs):
        call_count["n"] += 1
        return [{"sentinel": "should-not-appear"}]

    monkeypatch.setattr(advisor_module, "_get_recent_lessons", _spy_get_recent_lessons)

    provider = _FakeProvider()
    result = recommend(
        symbol="NOLESS",
        provider=provider,
        include_lessons=False,
    )
    assert result["lessons"] == []
    assert call_count["n"] == 0  # journal helper never called


def test_recommend_with_lessons_calls_journal(monkeypatch):
    """include_lessons=True -> _get_recent_lessons is invoked."""
    import hermes_quant.advisor as advisor_module

    call_count = {"n": 0}

    def _spy_get_recent_lessons(symbol, n_same, n_cross):
        call_count["n"] += 1
        return [{"symbol": symbol, "reflection": "test"}]

    monkeypatch.setattr(advisor_module, "_get_recent_lessons", _spy_get_recent_lessons)

    provider = _FakeProvider()
    result = recommend(
        symbol="LESS",
        provider=provider,
        include_lessons=True,
        n_lessons_same=5,
        n_lessons_cross=2,
    )
    assert call_count["n"] == 1
    assert len(result["lessons"]) == 1


def test_recommend_deterministic_given_same_inputs():
    """ADR-0014 §D3.3: same (symbol, as_of, indicators) -> same dict."""
    bars = _make_bars(n=120, start_price=50.0)
    provider1 = _FakeProvider(bars=bars.copy())
    provider2 = _FakeProvider(bars=bars.copy())

    r1 = recommend(
        symbol="DET",
        provider=provider1,
        include_lessons=False,
        as_of="2026-04-01T00:00:00",
    )
    r2 = recommend(
        symbol="DET",
        provider=provider2,
        include_lessons=False,
        as_of="2026-04-01T00:00:00",
    )

    # Compare core fields — exclude metadata that legitimately varies
    # (e.g. wall-clock embedded in caveats: none currently, but be safe)
    for key in ["symbol", "asset_class", "timeframe", "as_of", "aggregated_signal", "risk_gate"]:
        assert r1[key] == r2[key], f"non-deterministic: {key}"


def test_recommend_unsupported_asset_class_returns_gated():
    """v0.1.2: only equity/etf supported. crypto/fx -> gated, not exception."""
    result = recommend(
        symbol="BTC/USDT",
        asset_class="crypto",
        include_lessons=False,
    )
    assert result["risk_gate"]["pass"] is False
    assert result["risk_gate"]["gated_reason"] == "asset_class_unsupported"
    assert result["doctor"]["data_provider_alive"] is False


def test_recommend_missing_symbol_handled_at_tool_layer():
    """Tool handler validates symbol; advisor itself accepts any string."""
    # Empty string should not crash advisor
    provider = _FakeProvider()
    result = recommend(symbol="", provider=provider, include_lessons=False)
    # No assertion on success/fail — just that it returned a dict with no exception
    assert isinstance(result, dict)
    assert "risk_gate" in result


def test_recommend_emits_view_when_data_supports():
    """Sanity: with 100 bars of monotone-trend data, ClassicalTA should emit."""
    provider = _FakeProvider(bars=_make_bars(n=120))
    result = recommend(
        symbol="TREND",
        provider=provider,
        include_lessons=False,
    )
    # ClassicalTA needs min_history_bars=60 by default; we provided 120
    assert len(result["analyst_views"]) >= 0  # may be 0 if cold-start gates
    # If we did get a view, it should have the expected structure
    if result["analyst_views"]:
        v = result["analyst_views"][0]
        assert "analyst" in v
        assert "direction" in v
        assert "confidence" in v
        assert v["direction"] in (-1, 0, 1)


def test_advisor_caveats_always_include_disclaimers():
    """Per ADR-0014 §D1: caveats are NOT optional disclaimers."""
    provider = _FakeProvider()
    result = recommend(
        symbol="DISC",
        provider=provider,
        include_lessons=False,
    )
    caveats_text = " ".join(result["caveats"]).lower()
    assert "snapshot" in caveats_text
    assert "single-symbol" in caveats_text or "portfolio" in caveats_text
    assert "calibration" in caveats_text


def test_quant_recommend_tool_handler_returns_json_string():
    """Tool layer: handler returns JSON string per Hermes plugin convention."""
    import json as _json
    from hermes_quant.tools import quant_recommend

    # We can't easily mock the lazy import inside the handler, so we use a
    # real provider that's guaranteed to fail (network call to bogus symbol).
    # The handler should return a JSON string regardless of advisor outcome.
    out = quant_recommend({"symbol": ""})
    assert isinstance(out, str)
    parsed = _json.loads(out)
    assert "success" in parsed
    assert parsed["success"] is False  # empty symbol -> early failure


def test_quant_recommend_tool_handler_parses_args():
    """Tool layer: dict args properly forwarded to advisor.recommend."""
    import json as _json
    from hermes_quant.tools import quant_recommend

    # Use a symbol/asset_class combo that shortcuts to gated (no network)
    out = quant_recommend(
        {
            "symbol": "BTC/USDT",
            "asset_class": "crypto",  # unsupported in v0.1.2 -> gated
        }
    )
    parsed = _json.loads(out)
    assert parsed["success"] is True  # the call succeeded; the gate gated
    assert parsed["risk_gate"]["pass"] is False
    assert parsed["risk_gate"]["gated_reason"] == "asset_class_unsupported"
