"""PDR-3 is COMPLEMENTARY to BMA cross-ANALYST require_ensemble (plan §5.5).

ADR-0079 D79.3 / design §3.2 "clean distinction": cross-SOURCE@perception (PDR-3,
"is the trend real?") and cross-ANALYST@decision (BMA require_ensemble, "do my
models concur?") are TWO INDEPENDENT gates. A social-arb signal must clear BOTH.

Made executable: a multi-source packet that CLEARS PDR-3 (>=2 source families)
but finds NO numerical corroborator is STILL silenced by BMA require_ensemble
(n_distinct_analysts <= 1). Turning PDR-3 ON does NOT relax bma.py:498-519.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import load_graph
from hermes_quant.catalyst.synthesize import synthesize_packets
from hermes_quant.protocol import AnalystView, MarketContext

GRAPH, ALIASES = load_graph()
_ASOF = dt.datetime(2021, 3, 1, tzinfo=dt.UTC)


def _ctx(asset: str = "CELH") -> MarketContext:
    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2021-02-01", periods=2, freq="1d"),
            "open": [50.0, 51.0],
            "high": [51.0, 52.0],
            "low": [49.0, 50.0],
            "close": [50.5, 51.5],
            "volume": [1000.0, 1000.0],
        }
    )
    return MarketContext(
        asset=asset, timeframe="1d", asset_class="equity", exchange=None,
        bars=bars, last_close=51.5, last_volume=1000.0,
        asof=pd.Timestamp("2021-03-01T20:00:00"),
    )


def _multi_source_celh_items() -> list[CatalystItem]:
    """Three independent source families on CELH -> clears PDR-3 (validated)."""
    return [
        CatalystItem(
            title="Celsius energy drink goes viral on TikTok as sales surge among Gen Z",
            published_at=_ASOF, source="reddit/r/stocks (score=812 c=143)", link="n/a",
        ),
        CatalystItem(
            title="Celsius energy drink search interest surges as sales soar",
            published_at=_ASOF, source="google_trends/US", link="n/a",
        ),
        CatalystItem(
            title="Celsius sales surge as energy drink demand soars among young consumers",
            published_at=_ASOF, source="Reuters", link="n/a",
        ),
    ]


def _semantic_view_from_packet(pkt) -> AnalystView:
    """A lone semantic view built from a surviving packet (mirrors semantic.py:132)."""
    direction = {"bullish": 1, "bearish": -1, "neutral": 0}[pkt.stance]
    return AnalystView(
        analyst="hermes-semantic",
        direction=direction,  # type: ignore[arg-type]
        magnitude=float(pkt.magnitude),
        confidence=float(pkt.confidence),
        confidence_raw=float(pkt.confidence),
        horizon=pkt.horizon,
        rationale=pkt.summary[:256],
    )


def test_social_arb_must_clear_BOTH_layers(monkeypatch):  # noqa: N802 — plan §5.5 name
    """A multi-source packet clears PDR-3, but a LONE semantic view (no numerical
    corroborator) is STILL silenced by BMA require_ensemble (n_distinct_analysts<=1)."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    pkts = synthesize_packets(_multi_source_celh_items(), graph=GRAPH, aliases=ALIASES)
    celh = [p for p in pkts if p.asset == "CELH"]
    assert celh, "PDR-3 should NOT have dropped the multi-source CELH packet"
    # it cleared PDR-3 (validated)
    assert all(p.metadata.get("convergence", {}).get("validated") for p in celh)

    # Feed ONLY the lone semantic view into BMA (TA/Kronos abstained) -> silenced.
    view = _semantic_view_from_packet(max(celh, key=lambda p: p.confidence))
    agg = BMAAggregator()  # require_ensemble=True is the default
    sig = agg.aggregate([view], _ctx("CELH"))
    assert sig.direction == 0, (
        "BMA must STILL silence a lone semantic view even after it cleared PDR-3 "
        f"(two-layer ensemble), got direction={sig.direction}, conf={sig.confidence}"
    )
    assert sig.confidence == 0.0


def test_convergence_is_complementary_not_replacement(monkeypatch):
    """Turning PDR-3 ON does not relax BMA's cross-ANALYST guard. The SAME lone view
    is silenced whether convergence is OFF or ON — PDR-3 and BMA are independent."""
    agg = BMAAggregator()
    ctx = _ctx("CELH")
    lone = AnalystView(
        analyst="hermes-semantic", direction=1, magnitude=0.04,
        confidence=0.45, confidence_raw=0.45, horizon="1d",
    )

    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    sig_off = agg.aggregate([lone], ctx)

    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    sig_on = agg.aggregate([lone], ctx)

    assert sig_off.direction == 0 and sig_on.direction == 0, (
        "BMA require_ensemble must silence the lone view regardless of the "
        "convergence flag — PDR-3 is complementary, never a relaxation of BMA."
    )


def test_pdr3_validated_plus_numerical_corroborator_can_fire(monkeypatch):
    """The positive control: a PDR-3-validated semantic view PLUS a distinct
    numerical analyst agreeing IS a genuine ensemble (clears BOTH layers)."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    pkts = synthesize_packets(_multi_source_celh_items(), graph=GRAPH, aliases=ALIASES)
    celh = max((p for p in pkts if p.asset == "CELH"), key=lambda p: p.confidence)
    semantic_view = _semantic_view_from_packet(celh)
    # a distinct numerical analyst agreeing on direction (the missing corroborator)
    kronos_view = AnalystView(
        analyst="kronos", direction=semantic_view.direction, magnitude=0.03,
        confidence=0.6, confidence_raw=0.6, horizon="1d",
    )
    agg = BMAAggregator()
    sig = agg.aggregate([semantic_view, kronos_view], _ctx("CELH"))
    # two DISTINCT analysts -> not silenced by require_ensemble
    assert sig.metadata.get("n_views") == 2
    assert sig.direction == semantic_view.direction
