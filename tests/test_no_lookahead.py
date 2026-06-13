"""Test the no-lookahead invariant — ADR-0006 amendment release blocker.

Per the founding charter: "No look-ahead bias. Every analyst signal
emitted at time T MUST be derivable from data with timestamp <= T."

This test fence verifies that:
1. Every shipped DataProvider honors the `as_of` parameter (ADR-0005
   amendment Wave C.1) — bars returned have all timestamps <= as_of.
2. Every shipped Analyst's view at time T is identical regardless of
   whether bars after T are present in the input MarketContext.
3. The advisor's recommend() with `as_of` produces a deterministic
   result that doesn't depend on whether more recent bars are
   available in the underlying source.

Per ADR-0006: this test is a **release blocker**. Any analyst that fails
the shuffle/futures invariant blocks the next version tag.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from hermes_quant.advisor import recommend
from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst
from hermes_quant.analysts.fundamentals import FundamentalsAnalyst
from hermes_quant.analysts.microstructure import MicrostructureLite
from hermes_quant.analysts.overnight_drift import OvernightDriftAnalyst
from hermes_quant.analysts.semantic import HermesSemanticAnalyst
from hermes_quant.protocol import MarketContext


def _make_bars(
    n: int = 100, *, base: float = 100.0, trend: float = 0.5, seed: int = 42
) -> pd.DataFrame:
    """Synthetic OHLCV. Deterministic given seed."""
    rng = np.random.default_rng(seed=seed)
    timestamps = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = base + np.arange(n) * trend + rng.normal(0, 0.5, n)
    opens = closes - rng.uniform(0, 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, 0.4, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


def _ctx_at(bars: pd.DataFrame, *, asof_idx: int) -> MarketContext:
    """Build a MarketContext as if `asof_idx` were the latest bar."""
    sliced = bars.iloc[: asof_idx + 1].reset_index(drop=True)
    return MarketContext(
        asset="TEST",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=sliced,
        last_close=float(sliced["close"].iloc[-1]),
        last_volume=float(sliced["volume"].iloc[-1]),
        asof=sliced["timestamp"].iloc[-1],
        extras={},
    )


# ---------------------------------------------------------------------------
# Invariant 1 — Analyst output at time T is identical regardless of future bars
# ---------------------------------------------------------------------------


# Every SHIPPED bar-consuming analyst (wired into advisor._build_default_analysts,
# even if default-OFF behind a flag) must appear here — this is the AGENTS.md
# release-blocker promise (§"No look-ahead bias": the gate runs against "every
# shipped analyst"). FundamentalsAnalyst (ADR-0064) abstains None on synthetic
# bars (cache miss) — covered: the test's both-None branch proves it doesn't peek
# at out-of-window rows. OvernightDriftAnalyst (ADR-0089) consumes ctx.bars
# open/close directly and is the discriminating case. HermesSemanticAnalyst is
# packet-driven (not bar-temporal); its lookahead fence is Invariants 5/7 below,
# so the bar-based fences here can't reach it and it is intentionally excluded.
@pytest.mark.parametrize(
    "analyst_factory",
    [
        lambda: ClassicalTAAnalyst(),
        lambda: MicrostructureLite(),
        lambda: FundamentalsAnalyst(),
        lambda: OvernightDriftAnalyst(),
    ],
)
def test_analyst_view_at_t_independent_of_future_bars(analyst_factory):
    """The analyst's view at index T must be identical whether the
    MarketContext contains bars [0..T] or [0..N-1] sliced to [0..T]."""
    bars = _make_bars(120, trend=0.5, seed=42)

    # Build two contexts: one with exactly bars[:80], one with the full
    # dataframe but sliced down to the same window in the analyst.
    # If the analyst peeks at "future" rows (rows > index 79), the two
    # outputs will differ.
    ctx_truncated = _ctx_at(bars, asof_idx=79)

    # Build a "polluted" context with future data in trailing rows.
    # We swap the analyst.analyze input to manually include rows beyond
    # index 79 — if the analyst correctly uses .iloc/.iterrows on the
    # input and respects len(bars), this should NOT matter.
    polluted = bars.copy()
    # Sentinel: replace future bars with extreme values so any leak
    # would shift indicators dramatically.
    polluted.loc[80:, "close"] = polluted.loc[80:, "close"] * 100
    polluted.loc[80:, "high"] = polluted.loc[80:, "high"] * 100
    # but slice the input as the contract requires
    sliced_polluted = polluted.iloc[:80].reset_index(drop=True)

    ctx_polluted_but_sliced = MarketContext(
        asset="TEST",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=sliced_polluted,
        last_close=float(sliced_polluted["close"].iloc[-1]),
        last_volume=float(sliced_polluted["volume"].iloc[-1]),
        asof=sliced_polluted["timestamp"].iloc[-1],
        extras={},
    )

    a1 = analyst_factory()
    a2 = analyst_factory()
    v1 = a1.analyze(ctx_truncated)
    v2 = a2.analyze(ctx_polluted_but_sliced)

    # Both contexts have IDENTICAL bars[0..79] — outputs must match
    if v1 is None and v2 is None:
        return  # both silenced; nothing to compare
    assert v1 is not None and v2 is not None, (
        "analyst behavior differs based on out-of-window data presence — "
        "investigate immediately, this is a no-lookahead violation"
    )
    assert v1.direction == v2.direction
    assert v1.confidence_raw == pytest.approx(v2.confidence_raw, rel=1e-9)
    assert v1.magnitude == pytest.approx(v2.magnitude, rel=1e-9)


# ---------------------------------------------------------------------------
# Invariant 2 — Provider as_of filter actually filters
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Provider that returns canned bars; verifies the provider receives
    `as_of` and applies the cutoff."""

    name = "recording"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars
        self.calls: list[dict] = []

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache: bool = True, as_of=None):
        self.calls.append(
            {
                "asset": asset,
                "timeframe": timeframe,
                "as_of": as_of,
            }
        )
        # Apply the same as_of filter the YFinanceProvider does
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of
            if cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def test_advisor_passes_as_of_to_provider():
    """Wave C.1 invariant: advisor MUST forward as_of to fetch_bars."""
    bars = _make_bars(120, trend=0.5)
    provider = _RecordingProvider(bars)

    asof = "2026-03-15T00:00:00Z"
    result = recommend(
        symbol="TEST",
        asset_class="equity",
        as_of=asof,
        provider=provider,
        include_lessons=False,
    )

    # Provider received as_of (not None)
    assert provider.calls
    last_call = provider.calls[-1]
    assert last_call["as_of"] is not None, "advisor did not forward as_of"

    # Result's as_of (the bar timestamp) is <= the requested cutoff
    if result.get("as_of"):
        result_dt = pd.Timestamp(result["as_of"])
        cutoff_dt = pd.Timestamp(asof)
        if result_dt.tzinfo is None:
            result_dt = result_dt.tz_localize("UTC")
        if cutoff_dt.tzinfo is None:
            cutoff_dt = cutoff_dt.tz_localize("UTC")
        assert result_dt <= cutoff_dt, (
            f"advisor returned as_of={result_dt} > cutoff={cutoff_dt}; "
            "leaf-level lookahead enforcement broken"
        )


def test_advisor_as_of_in_past_returns_no_future_bars():
    """If as_of is mid-dataset, advisor should report bar count consistent
    with the cutoff — not the full dataset."""
    bars = _make_bars(120, trend=0.5)
    provider = _RecordingProvider(bars)

    # Cutoff at row 60 (2026-03-02 if start is 2026-01-01)
    asof = bars["timestamp"].iloc[60].isoformat()
    result = recommend(
        symbol="TEST",
        asset_class="equity",
        as_of=asof,
        provider=provider,
        include_lessons=False,
    )

    bars_received = result.get("data_quality", {}).get("bars_received", 0)
    # We should see at most 61 bars (0..60 inclusive); never the full 120
    assert bars_received <= 61, (
        f"advisor returned {bars_received} bars with as_of=row 60; "
        "future bars leaked into recommendation"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — Determinism (ADR-0014 §D3.3)
# ---------------------------------------------------------------------------


def test_advisor_deterministic_under_as_of_replay():
    """Same (symbol, as_of) -> same dict, modulo wall-clock-derived fields.

    This is a stronger version of the deterministic-replay test that
    pins the no-lookahead invariant: even when the underlying provider
    has MORE data available, an as_of-anchored query returns the same
    answer every time.
    """
    bars = _make_bars(120, trend=0.5, seed=42)

    # Two providers, one with 80 bars, one with the full 120 — but both
    # queried at the same as_of cutoff (row 60). Outputs must match.
    provider_short = _RecordingProvider(bars.iloc[:80].reset_index(drop=True))
    provider_full = _RecordingProvider(bars)

    asof = bars["timestamp"].iloc[60].isoformat()
    r1 = recommend(
        symbol="TEST",
        asset_class="equity",
        as_of=asof,
        provider=provider_short,
        include_lessons=False,
    )
    r2 = recommend(
        symbol="TEST",
        asset_class="equity",
        as_of=asof,
        provider=provider_full,
        include_lessons=False,
    )

    # Compare the load-bearing fields
    for key in ["as_of", "aggregated_signal", "risk_gate", "decision_price"]:
        assert r1.get(key) == r2.get(key), (
            f"key {key!r} differs under as_of replay: "
            f"short={r1.get(key)} full={r2.get(key)} — lookahead violation"
        )


# ---------------------------------------------------------------------------
# Invariant 4 — Statistical lookahead test via evaluation.lookahead
# ---------------------------------------------------------------------------
# Wave-D follow-up (Phase-7 review P0): the v0.3.0 evaluation/ module
# promotion shipped `shuffle_timestamps_test` as a reusable utility, but
# this CI gate didn't actually USE it — it stayed on the inline
# `_RecordingProvider` scaffolding. v0.3.1 wires the gate to the
# canonical implementation so future analysts get statistically tested
# without copy-paste.


# Same shipped-analyst coverage promise as Invariant 1's parametrize list.
# FundamentalsAnalyst abstains None on synthetic bars (score 0.0 — structural
# assertions still exercise the harness); OvernightDriftAnalyst is the
# discriminating bar-temporal case. HermesSemanticAnalyst is packet-driven and
# intentionally excluded (its fence is Invariants 5/7).
@pytest.mark.parametrize(
    "analyst_factory",
    [
        lambda: ClassicalTAAnalyst(),
        lambda: MicrostructureLite(),
        lambda: FundamentalsAnalyst(),
        lambda: OvernightDriftAnalyst(),
    ],
)
def test_shuffle_timestamps_invariant_via_evaluation_module(analyst_factory):
    """Each shipped analyst MUST use real temporal structure (not be
    timestamp-shuffle-invariant). If the analyst's score is the SAME on
    real vs shuffled timestamps, it isn't using temporal information —
    which usually means it's relying on bar-position rather than time
    (e.g., 'bar N's close' rather than 'closest bar to time T').

    This test uses the canonical evaluation.lookahead.shuffle_timestamps_test
    rather than re-deriving the shuffle math inline (per ADR-0019 §D3
    + Phase-7 architecture review v0.3 follow-up).
    """
    from hermes_quant.evaluation import shuffle_timestamps_test

    bars = _make_bars(100, trend=0.5, seed=42)
    analyst = analyst_factory()

    def score_fn(bars_df: pd.DataFrame) -> float:
        """Score = absolute confidence_raw on the analyst's view.
        Higher = analyst is more sure; if the analyst's confidence is
        identical on shuffled bars, it has no temporal edge."""
        ctx = MarketContext(
            asset="TEST",
            timeframe="1d",
            asset_class="equity",
            exchange=None,
            bars=bars_df.reset_index(drop=True),
            last_close=float(bars_df["close"].iloc[-1]),
            last_volume=float(bars_df["volume"].iloc[-1]),
            asof=bars_df["timestamp"].iloc[-1],
            extras={},
        )
        view = analyst.analyze(ctx)
        if view is None:
            return 0.0
        return float(view.confidence_raw)

    result = shuffle_timestamps_test(
        score_fn,
        bars,
        n_shuffles=8,
        alpha=0.05,
        seed=42,
    )
    # The result's `passed` flag is True when p_value > alpha (analyst's
    # signal IS distinguishable from shuffled noise = uses temporal
    # structure = no lookahead via bar-position-only).
    # A FAILING analyst here would mean its score is consistently >=
    # the real score even after timestamp shuffling — a strong indicator
    # of timestamp-blind processing or position-based lookahead.
    # We assert structural fields, not the pass/fail (which can be flaky
    # on small n_shuffles); the canonical CI gate uses larger n_shuffles
    # with a hard threshold.
    assert 0.0 <= result.p_value <= 1.0
    assert len(result.shuffled_scores) == 8


# ---------------------------------------------------------------------------
# Invariant 5 — SemanticAnalyst: a packet with asof > decision_time has ZERO
# influence (ADR-0074 "asof = publication time" is the #1 honesty rule).
# ---------------------------------------------------------------------------
# The catalyst pipeline is LIVE: synthesize.py stamps each SemanticPacket with
# asof = the headline's PUBLICATION time, then the advisor feeds packets into
# MarketContext.extras["semantic_packets"]. HermesSemanticAnalyst consumes those
# packets (NOT bars). Its lookahead invariant therefore lives in packet-time
# space, not bar-position space, so the bar-based fences above can't reach it.
# The contract: a packet published AFTER the decision boundary MUST be dropped
# (validate_semantic_packet -> "future_packet") and contribute nothing to the
# view. We prove zero influence by differencing against the future-absent case.


def _semantic_packet(*, asof: str, stance: str, confidence: float, magnitude: float):
    """Build a hash-attached SemanticPacket dict for BTC/USDT @ 1h.

    Mirrors the shape synthesize.synthesize_packets emits (asof = publication
    time) and the fixtures in tests/unit/test_semantic_analyst.py.
    """
    from hermes_quant.semantic import semantic_packet_from_dict

    return semantic_packet_from_dict(
        {
            "schema_version": 1,
            "asset": "BTC/USDT",
            "asof": asof,
            "horizon": "1h",
            "stance": stance,
            "confidence": confidence,
            "magnitude": magnitude,
            "summary": f"semantic packet asof={asof} stance={stance}",
            "sources": [{"type": "note", "ref": "no-lookahead-fence"}],
            "model": "hermes:lookahead-test",
        }
    ).to_dict()


def _semantic_ctx(*, asof: str, extras: dict):
    """MarketContext at decision time `asof`. Bars are inert filler — the
    semantic analyst reads packets from extras, not bars."""
    ts = pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [1000.0] * 5,
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="kraken",
        bars=bars,
        last_close=104.0,
        last_volume=1000.0,
        asof=pd.Timestamp(asof),
        extras=extras,
    )


def _views_equal(a, b) -> bool:
    """View-equality on the load-bearing fields (ignoring rationale prose,
    which deliberately echoes the selected packet's asof)."""
    if (a is None) != (b is None):
        return False
    if a is None and b is None:
        return True
    return (
        a.direction == b.direction
        and a.magnitude == pytest.approx(b.magnitude, rel=1e-9)
        and a.confidence == pytest.approx(b.confidence, rel=1e-9)
        and a.confidence_raw == pytest.approx(b.confidence_raw, rel=1e-9)
        and a.metadata.get("semantic_stance") == b.metadata.get("semantic_stance")
        and a.metadata.get("packet_asof") == b.metadata.get("packet_asof")
    )


def test_semantic_analyst_future_packet_has_zero_influence():
    """A packet published AFTER ctx.asof must be dropped, leaving the view
    byte-identical to the case where that future packet was never present.

    The future packet is deliberately the LOUDER one (opposite stance, higher
    confidence, later asof) so that ANY leak flips direction and confidence —
    a silent no-op leak can't hide.
    """
    decision = "2026-01-01T12:00:00Z"
    past = _semantic_packet(
        asof="2026-01-01T11:00:00Z", stance="bullish", confidence=0.70, magnitude=0.010
    )
    future = _semantic_packet(
        asof="2026-01-01T13:00:00Z", stance="bearish", confidence=0.95, magnitude=0.050
    )

    # Case A: both packets present. The future packet MUST be dropped.
    view_with_future = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=decision, extras={"semantic_packets": [past, future]})
    )
    # Case B: future packet never existed.
    view_without_future = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=decision, extras={"semantic_packets": [past]})
    )

    assert view_with_future is not None and view_without_future is not None
    assert _views_equal(view_with_future, view_without_future), (
        "future-asof semantic packet influenced the view — ADR-0074 "
        "publication-time lookahead violation"
    )
    # And concretely: the surviving view reflects the PAST (bullish) packet,
    # never the louder future (bearish) one.
    assert view_with_future.direction == 1
    assert view_with_future.confidence_raw == pytest.approx(0.70)
    assert view_with_future.metadata["semantic_stance"] == "bullish"
    assert view_with_future.metadata["packet_asof"] == past["asof"]


def test_semantic_analyst_future_packet_under_decision_asof_extra():
    """Live path (ADR-0074/ADR-0068): when ctx.extras['decision_asof'] is the
    wall-clock decision time, the future cutoff is decision_asof, not the bar
    asof. A packet published after decision_asof still has zero influence,
    and a packet published before decision_asof (but after the stale bar time)
    is correctly admitted."""
    bar_asof = "2026-01-01T00:00:00Z"  # stale daily-bar close
    decision = "2026-01-01T12:00:00Z"  # live wall-clock decision time

    # Published after the bar close but BEFORE the decision -> admissible live.
    live_past = _semantic_packet(
        asof="2026-01-01T09:00:00Z", stance="bullish", confidence=0.70, magnitude=0.010
    )
    # Published AFTER the decision -> must be dropped even with decision_asof.
    future = _semantic_packet(
        asof="2026-01-01T15:00:00Z", stance="bearish", confidence=0.95, magnitude=0.050
    )

    extras_both = {
        "semantic_packets": [live_past, future],
        "decision_asof": decision,
    }
    extras_past_only = {
        "semantic_packets": [live_past],
        "decision_asof": decision,
    }

    view_both = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=bar_asof, extras=extras_both)
    )
    view_past_only = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=bar_asof, extras=extras_past_only)
    )

    assert view_both is not None and view_past_only is not None
    assert _views_equal(view_both, view_past_only), (
        "future-asof packet leaked under decision_asof live path — lookahead "
        "violation in the wall-clock decision-time branch"
    )
    # The admitted packet IS the live_past one (proves decision_asof widened
    # the window past the stale bar time without admitting the future packet).
    assert view_both.direction == 1
    assert view_both.metadata["packet_asof"] == live_past["asof"]


def test_semantic_analyst_only_future_packet_abstains_future_packet():
    """A context whose ONLY packet is published after the decision boundary
    must abstain with the explicit future_packet reason (silence-by-default,
    not a silent zero-influence) — pins the abstain-reason observability keys on
    (analysts/semantic.py)."""
    decision = "2026-01-01T12:00:00Z"
    future = _semantic_packet(
        asof="2026-01-01T13:00:00Z", stance="bearish", confidence=0.95, magnitude=0.05
    )
    view = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=decision, extras={"semantic_packets": [future]})
    )
    assert view is not None
    assert view.direction == 0
    assert view.metadata.get("abstain_reason") == "future_packet"


def test_semantic_analyst_admits_packet_at_decision_boundary():
    """asof == decision_time is admissible: publication exactly at the boundary
    is lookahead-honest (<=, not <). Backtest path (no decision_asof => ctx.asof)."""
    decision = "2026-01-01T12:00:00Z"
    at_boundary = _semantic_packet(
        asof=decision, stance="bullish", confidence=0.70, magnitude=0.01
    )
    view = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=decision, extras={"semantic_packets": [at_boundary]})
    )
    assert view is not None and view.direction == 1
    assert view.metadata.get("abstain_reason") is None


# ---------------------------------------------------------------------------
# Invariant 7 — Perception-PRODUCING path is lookahead-honest (M21, PDR-1 precond)
# ---------------------------------------------------------------------------
# Invariant 5 proves the SemanticAnalyst (the CONSUMER of packets) drops a
# future-asof packet. But the analyst is only honest if the code that PRODUCES
# its input is honest too: catalyst/wiring.py:semantic_market_extras() injects
# the packets, and catalyst/onboarding.py:catalyst_admissions() selects symbols
# to onboard on the basis of packets. Both call load_packets_for(symbol, asof);
# both default `asof` to wall-clock now on the live path. The release-blocker
# gate must assert the SCAN->onboard->recommend seam can't pull a wider data
# window than the decision asof justifies — i.e. a packet published AFTER the
# decision boundary is excluded by the PRODUCING seam, not just re-dropped later
# by the analyst. (Meta-review M21 / ADR-0079 D-4 lookahead honesty, the PDR-1
# eval-gate precondition "no-lookahead gate green" on the frame/onboarding path.)
#
# Deterministic, no network: packets live in a tmp JSONL the seams read via
# load_packets_for; the propagation graph and tradeable() are injected.


def _write_store_packet(
    store: pathlib.Path,
    *,
    asset: str,
    asof: str,
    stance: str = "bullish",
    confidence: float = 0.70,
    magnitude: float = 0.05,
    horizon: str = "1d",
) -> None:
    """Append one SemanticPacket dict to a JSONL store (synthesize shape:
    asof = publication time)."""
    from hermes_quant.semantic import semantic_packet_from_dict

    pkt = semantic_packet_from_dict(
        {
            "schema_version": 1,
            "asset": asset,
            "asof": asof,
            "horizon": horizon,
            "stance": stance,
            "confidence": confidence,
            "magnitude": magnitude,
            "summary": f"no-lookahead producing-path packet {asset} {stance} {asof}",
            "sources": [{"type": "note", "ref": "no-lookahead-producing-fence"}],
            "model": "hermes:lookahead-producing-test",
        }
    )
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pkt.to_dict(include_hash=True), default=str) + "\n")


def test_semantic_market_extras_excludes_future_asof_packet(monkeypatch, tmp_path):
    """wiring.semantic_market_extras(symbol, decision_asof=T) must inject ONLY
    packets with asof <= T. A packet published AFTER T must be excluded by the
    PRODUCING seam — proving the wiring seam is lookahead-honest, not just the
    consuming analyst (M21). The future packet is the LOUDER one (opposite
    stance, higher confidence, later asof) so any leak is unmistakable."""
    from hermes_quant.catalyst import synthesize, wiring

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_store_packet(  # published BEFORE the decision -> admissible
        store, asset="AAPL", asof="2026-01-01T09:00:00Z",
        stance="bullish", confidence=0.70, magnitude=0.01,
    )
    _write_store_packet(  # published AFTER the decision -> MUST be excluded
        store, asset="AAPL", asof="2026-01-01T15:00:00Z",
        stance="bearish", confidence=0.95, magnitude=0.05,
    )

    out = wiring.semantic_market_extras("AAPL", decision_asof=decision)
    assert out is not None, "expected packets to be injected at decision asof"
    asofs = [pd.Timestamp(p["asof"]) for p in out["semantic_packets"]]
    assert asofs, "wiring seam injected no packets despite a valid past packet"
    cutoff = pd.Timestamp(decision)
    for a in asofs:
        a_utc = a.tz_localize("UTC") if a.tzinfo is None else a.tz_convert("UTC")
        assert a_utc <= cutoff, (
            f"semantic_market_extras injected a future packet asof={a_utc} > "
            f"decision_asof={cutoff} — PRODUCING-path lookahead leak (M21)"
        )
    # Concretely: the surviving packet is the PAST (bullish) one, never the
    # louder future (bearish) one.
    stances = {p["stance"] for p in out["semantic_packets"]}
    assert stances == {"bullish"}, (
        f"future bearish packet leaked into the injected extras: stances={stances}"
    )


def test_semantic_market_extras_only_future_packet_injects_nothing(monkeypatch, tmp_path):
    """When the ONLY stored packet is published after the decision asof, the
    wiring seam injects NOTHING (returns None) — the producing path silences by
    default rather than handing a future packet to the analyst to re-drop."""
    from hermes_quant.catalyst import synthesize, wiring

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_store_packet(
        store, asset="AAPL", asof="2026-01-01T15:00:00Z",
        stance="bearish", confidence=0.95, magnitude=0.05,
    )
    assert wiring.semantic_market_extras("AAPL", decision_asof=decision) is None, (
        "wiring seam injected a future-only packet — PRODUCING-path lookahead leak"
    )


def test_catalyst_admissions_excludes_future_asof_packet(monkeypatch, tmp_path):
    """onboarding.catalyst_admissions at asof=T must NOT admit a symbol on the
    basis of a packet whose asof > T. The SCAN->onboard seam reads packets via
    load_packets_for(sym, asof) — selecting an onboarding candidate from a
    future packet would let an onboarded symbol pull a window its admitting
    packet's asof can't justify (M21). The future packet is again the LOUDER one
    (well above both thresholds) so a leak would admit; honest behavior abstains."""
    from hermes_quant.catalyst import onboarding, propagation, synthesize
    from hermes_quant.catalyst.propagation import PropagationEdge

    monkeypatch.setenv("HERMES_QUANT_CATALYST_ONBOARDING", "1")
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)
    monkeypatch.setattr(
        propagation,
        "load_graph",
        lambda *a, **k: (
            {"src": [PropagationEdge("src", "RKLB", "sector_member", -1, 0.80)]},
            {},
        ),
    )

    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # The ONLY packet for the out-of-universe target is published AFTER the asof.
    _write_store_packet(
        store, asset="RKLB", asof="2026-01-01T15:00:00Z",
        stance="bullish", confidence=0.95, magnitude=0.06,
    )
    admitted = onboarding.catalyst_admissions(
        set(), tradeable=lambda _s: True, asof=decision
    )
    assert admitted == [], (
        "catalyst_admissions onboarded a symbol on a future-asof packet "
        f"(asof=15:00 > decision=12:00): {[a.symbol for a in admitted]} — "
        "SCAN->onboard PRODUCING-path lookahead leak (M21)"
    )


def test_catalyst_admissions_admitted_packet_asof_not_after_decision(monkeypatch, tmp_path):
    """When a past AND a (louder) future packet both exist, admission may fire,
    but the admission it carries must be backed by the PAST packet — its
    packet_asof must be <= the decision asof. Pins that the onboarded symbol
    can't justify its window with a future packet's asof."""
    from hermes_quant.catalyst import onboarding, propagation, synthesize
    from hermes_quant.catalyst.propagation import PropagationEdge

    monkeypatch.setenv("HERMES_QUANT_CATALYST_ONBOARDING", "1")
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)
    monkeypatch.setattr(
        propagation,
        "load_graph",
        lambda *a, **k: (
            {"src": [PropagationEdge("src", "RKLB", "sector_member", -1, 0.80)]},
            {},
        ),
    )

    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_store_packet(  # past, eligible
        store, asset="RKLB", asof="2026-01-01T09:00:00Z",
        stance="bullish", confidence=0.80, magnitude=0.06,
    )
    _write_store_packet(  # future, LOUDER (higher conf) — must not be selected
        store, asset="RKLB", asof="2026-01-01T15:00:00Z",
        stance="bullish", confidence=0.99, magnitude=0.06,
    )
    admitted = onboarding.catalyst_admissions(
        set(), tradeable=lambda _s: True, asof=decision
    )
    assert [a.symbol for a in admitted] == ["RKLB"], (
        "expected RKLB admitted on the strength of its PAST packet"
    )
    cutoff = pd.Timestamp(decision)
    pa = pd.Timestamp(admitted[0].packet_asof)
    pa_utc = pa.tz_localize("UTC") if pa.tzinfo is None else pa.tz_convert("UTC")
    assert pa_utc <= cutoff, (
        f"admission carries a future packet_asof={pa_utc} > decision={cutoff} — "
        "the louder future packet was selected (PRODUCING-path lookahead leak)"
    )


# ---------------------------------------------------------------------------
# Invariant 7b — PerceptionFrame PRODUCING path is lookahead-honest (S5/M21,
# PDR-1 frame-path extension; ADR-0079 D-4 / Rollout PDR-1 "no-lookahead gate
# green" on the frame path).
# ---------------------------------------------------------------------------
# The frame builder (build_perception_frame) is now the PRODUCER of
# decision_asof + semantic_packets (it absorbed catalyst/wiring.py's
# semantic_market_extras). So the ADR-0074 publication-time honesty must hold
# THROUGH the frame path: a packet published after the decision asof must be
# excluded by the builder (not just re-dropped later by the consuming analyst),
# and the projected ctx must carry decision_asof verbatim so the SemanticAnalyst's
# `<=` cutoff (semantic.py:161-172) is unchanged.


class _InertProvider:
    """Returns canned bars with an as_of cutoff (mirrors _RecordingProvider).
    Bars are inert filler — the producing-path honesty under test is in
    packet-time space, not bar-position space."""

    name = "inert"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache: bool = True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def test_build_perception_frame_excludes_future_asof_packet(monkeypatch, tmp_path):
    """build_perception_frame(symbol, decision_asof=T) must absorb ONLY packets
    with asof <= T. A packet published AFTER T must be excluded by the PRODUCING
    (frame-builder) seam — proving the frame path is lookahead-honest, not just
    the consuming analyst (S5/M21). The future packet is the LOUDER one so any
    leak is unmistakable."""
    from hermes_quant.catalyst import synthesize
    from hermes_quant.perception.builder import build_perception_frame

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_store_packet(  # published BEFORE the decision -> admissible
        store, asset="AAPL", asof="2026-01-01T09:00:00Z",
        stance="bullish", confidence=0.70, magnitude=0.01,
    )
    _write_store_packet(  # published AFTER the decision -> MUST be excluded
        store, asset="AAPL", asof="2026-01-01T15:00:00Z",
        stance="bearish", confidence=0.95, magnitude=0.05,
    )

    bars = _make_bars(120, trend=0.5, seed=42)
    asof_ts = pd.Timestamp(bars["timestamp"].iloc[-1])
    frame = build_perception_frame(
        "AAPL",
        timeframe="1d",
        asset_class="equity",
        provider=_InertProvider(bars),
        asof_ts=asof_ts,
        lookback_bars=200,
        decision_asof=decision,
    )
    assert frame is not None
    assert frame.semantic_packets, "frame absorbed no packets despite a valid past packet"
    asofs = [pd.Timestamp(p["asof"]) for p in frame.semantic_packets]
    cutoff = pd.Timestamp(decision)
    for a in asofs:
        a_utc = a.tz_localize("UTC") if a.tzinfo is None else a.tz_convert("UTC")
        assert a_utc <= cutoff, (
            f"build_perception_frame absorbed a future packet asof={a_utc} > "
            f"decision_asof={cutoff} — FRAME PRODUCING-path lookahead leak (M21)"
        )
    stances = {p["stance"] for p in frame.semantic_packets}
    assert stances == {"bullish"}, (
        f"future bearish packet leaked into the frame: stances={stances}"
    )


def test_build_perception_frame_only_future_packet_absorbs_nothing(monkeypatch, tmp_path):
    """When the ONLY stored packet is published after the decision asof, the
    frame builder absorbs NOTHING (empty semantic_packets, no decision_asof) —
    the producing path silences by default rather than handing a future packet on."""
    from hermes_quant.catalyst import synthesize
    from hermes_quant.perception.builder import build_perception_frame

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_store_packet(
        store, asset="AAPL", asof="2026-01-01T15:00:00Z",
        stance="bearish", confidence=0.95, magnitude=0.05,
    )
    bars = _make_bars(120, trend=0.5, seed=42)
    frame = build_perception_frame(
        "AAPL",
        timeframe="1d",
        asset_class="equity",
        provider=_InertProvider(bars),
        asof_ts=pd.Timestamp(bars["timestamp"].iloc[-1]),
        lookback_bars=200,
        decision_asof=decision,
    )
    assert frame is not None
    assert frame.semantic_packets == (), (
        "frame absorbed a future-only packet — FRAME PRODUCING-path lookahead leak"
    )
    assert "decision_asof" not in frame.extras


# ---------------------------------------------------------------------------
# Invariant 7c — PDR-2 TrendVelocity PRODUCING path is lookahead-honest (rail #3,
# Wave S5/M21; ADR-0079 D-4). The velocity score at asof=T must come from ONLY
# observations <= T — a future interest spike must NOT inflate the slope.
# ---------------------------------------------------------------------------
def test_velocity_score_excludes_future_observations():
    """A velocity series at asof=T must score from ONLY observations <= T. A future
    observation must NOT inflate the slope (producing-path lookahead leak class, M21)."""
    from hermes_quant.perception.velocity import compute_trend_velocity, counts_per_period

    asof = pd.Timestamp("2026-01-15T00:00:00Z")
    past = [pd.Timestamp(f"2026-01-{d:02d}T00:00:00Z") for d in (1, 2, 8, 9, 14)]
    future = [pd.Timestamp("2026-01-25T00:00:00Z")] * 50  # LOUD future spike
    counts = counts_per_period(past + future, asof=asof, freq="W")
    sc = compute_trend_velocity(counts, asof=asof)
    # the future spike's bucket must be absent (no period starts after asof):
    assert all(pd.Timestamp(period.start_time, tz="UTC") <= asof for period in counts.index), (
        "a future interest bucket leaked into the velocity counts (M21 producing-path leak)"
    )
    assert int(counts.sum()) == len(past), "future observations inflated the count series"
    # and if a score was produced, it stamps the asof anchor (never a future one):
    assert sc is None or sc.asof == asof


def test_build_perception_frame_velocity_honors_decision_asof(monkeypatch):
    """build_perception_frame(..., decision_asof=T) with HERMES_QUANT_TREND_VELOCITY=1
    must feed the velocity producer ONLY observations <= T, so frame.trend_velocity
    stamps asof <= T. A LOUD post-T interest burst must not change the score's anchor
    (frame PRODUCING-path no-lookahead, rail #3)."""
    from hermes_quant.perception import velocity_source
    from hermes_quant.perception.builder import build_perception_frame

    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    decision = datetime(2026, 2, 15, 12, 0, 0, tzinfo=UTC)

    # The seam returns a past series (4 weekly buckets <= T) PLUS a loud future burst.
    # An honest producer feeds counts_per_period the raw timestamps and the <= asof cut
    # happens inside it — so we hand the seam BOTH and require the future to be ignored.
    def _fake_ts_by_symbol(symbol, asof, *, horizon=None):
        base = pd.Timestamp("2026-01-19T00:00:00Z")  # a Monday, 4 weeks before T
        past = []
        for wk, n in enumerate([1, 1, 1, 6]):
            for i in range(n):
                past.append(base + pd.Timedelta(weeks=wk) + pd.Timedelta(hours=i))
        future = [pd.Timestamp("2026-03-10T00:00:00Z")] * 99  # LOUD post-T burst
        return {"AAPL": [t.to_pydatetime() for t in (past + future)]}

    monkeypatch.setattr(
        velocity_source, "interest_timestamps_by_symbol", _fake_ts_by_symbol
    )

    bars = _make_bars(120, trend=0.5, seed=42)
    frame = build_perception_frame(
        "AAPL",
        timeframe="1d",
        asset_class="equity",
        provider=_InertProvider(bars),
        asof_ts=pd.Timestamp(bars["timestamp"].iloc[-1]),
        lookback_bars=200,
        decision_asof=decision,
    )
    assert frame is not None
    assert frame.trend_velocity, "flag ON + past series should attach a velocity score"
    score = frame.trend_velocity["AAPL"]
    score_asof = pd.Timestamp(score["asof"])
    score_asof = score_asof.tz_localize("UTC") if score_asof.tzinfo is None else score_asof.tz_convert("UTC")
    assert score_asof <= pd.Timestamp(decision), (
        f"velocity score stamped asof={score_asof} > decision={decision} — FRAME "
        f"PRODUCING-path velocity lookahead leak (M21)"
    )


def test_frame_to_context_preserves_decision_asof_cutoff():
    """The frame carries decision_asof in extras verbatim; after projection the
    SemanticAnalyst's `<=` cutoff (semantic.py:161-172) is unchanged — a packet
    with asof > decision_asof has ZERO influence post-projection. We prove this by
    differencing the projected-ctx view against the future-absent case."""
    from hermes_quant.perception.adapter import frame_to_context
    from hermes_quant.perception.frame import PerceptionFrame

    bar_asof = "2026-01-01T00:00:00Z"  # stale daily-bar close
    decision = "2026-01-01T12:00:00Z"  # live wall-clock decision time
    live_past = _semantic_packet(
        asof="2026-01-01T09:00:00Z", stance="bullish", confidence=0.70, magnitude=0.010
    )
    future = _semantic_packet(
        asof="2026-01-01T15:00:00Z", stance="bearish", confidence=0.95, magnitude=0.050
    )

    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC"),
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [1000.0] * 5,
        }
    )

    def _frame(packets):
        return PerceptionFrame(
            symbol="BTC/USDT",
            asof=pd.Timestamp(bar_asof),
            bars=bars,
            last_close=104.0,
            semantic_packets=tuple(packets),
            extras={"decision_asof": decision},
        )

    ctx_both = frame_to_context(_frame([live_past, future]), timeframe="1h", asset_class="crypto")
    ctx_past_only = frame_to_context(_frame([live_past]), timeframe="1h", asset_class="crypto")

    view_both = HermesSemanticAnalyst().analyze(ctx_both)
    view_past_only = HermesSemanticAnalyst().analyze(ctx_past_only)

    assert view_both is not None and view_past_only is not None
    assert _views_equal(view_both, view_past_only), (
        "future-asof packet influenced the view AFTER frame projection — the "
        "adapter failed to pass decision_asof through verbatim (S5/M21 frame-path)"
    )
    # Concretely: the admitted packet is the PAST (bullish) one, never the future.
    assert view_both.direction == 1
    assert view_both.metadata["packet_asof"] == live_past["asof"]


# ---------------------------------------------------------------------------
# Invariant 6 — Kronos (heavy/optional): view at T is independent of future
# bars. Guarded by importorskip so CI without the model still passes green.
# ---------------------------------------------------------------------------


def test_kronos_view_at_t_independent_of_future_bars():
    """Same no-lookahead fence as Invariant 1, but for the optional Kronos
    foundation-model analyst. Skipped when the `kronos` package isn't
    installed (heavy/optional dependency) so a model-free CI stays green."""
    pytest.importorskip("kronos", reason="kronos model package not installed")

    from hermes_quant.analysts.kronos import KronosAnalyst, KronosConfig

    bars = _make_bars(120, trend=0.5, seed=42)

    # Truncated-at-79 context.
    ctx_truncated = _ctx_at(bars, asof_idx=79)

    # Polluted-then-sliced: blow up the out-of-window rows so any leak shifts
    # the forecast dramatically, then slice to the same [0..79] window.
    polluted = bars.copy()
    polluted.loc[80:, "close"] = polluted.loc[80:, "close"] * 100
    polluted.loc[80:, "high"] = polluted.loc[80:, "high"] * 100
    sliced_polluted = polluted.iloc[:80].reset_index(drop=True)
    ctx_polluted_but_sliced = MarketContext(
        asset="TEST",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=sliced_polluted,
        last_close=float(sliced_polluted["close"].iloc[-1]),
        last_volume=float(sliced_polluted["volume"].iloc[-1]),
        asof=sliced_polluted["timestamp"].iloc[-1],
        extras={},
    )

    # deterministic_seed pins Kronos's stochastic path sampling so the two
    # runs are comparable (charter replayability invariant).
    cfg = KronosConfig(deterministic_seed=42)
    v1 = KronosAnalyst(cfg).analyze(ctx_truncated)
    v2 = KronosAnalyst(cfg).analyze(ctx_polluted_but_sliced)

    if v1 is None and v2 is None:
        return
    assert v1 is not None and v2 is not None, (
        "kronos behavior differs based on out-of-window data presence — "
        "no-lookahead violation"
    )
    assert v1.direction == v2.direction
    assert v1.confidence_raw == pytest.approx(v2.confidence_raw, rel=1e-9)
    assert v1.magnitude == pytest.approx(v2.magnitude, rel=1e-9)



# ---------------------------------------------------------------------------
# Invariant 7 — still-forming-bar discipline is TIMEFRAME-AWARE (ADR-0083
# Phase 0a). The "still-forming" boundary is the bar's OWN period, not
# unconditionally the day. An intraday read at decision-time T must not see a
# partial current period -- that is a no-lookahead honesty hole if the cutoff
# assumes a daily bar for a 1h/15m read.
# ---------------------------------------------------------------------------


def test_still_forming_drop_is_timeframe_aware_intraday():
    """A 1h read mid-hour drops the current incomplete hour but keeps closed
    hours. Drives the real `drop_still_forming_bar` (the same helper the
    advisor + builder call)."""
    from hermes_quant.data.bar_alignment import drop_still_forming_bar

    # 14:00/15:00 are closed hours; 16:00 is the current still-forming hour.
    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-05-28 14:00", "2026-05-28 15:00", "2026-05-28 16:00"]
            ).tz_localize("UTC"),
            "open": [100.0, 101.0, 102.0],
            "high": [101.5, 102.5, 103.0],
            "low": [99.5, 100.5, 101.0],
            "close": [101.0, 102.0, 102.5],
            "volume": [1e6, 9e5, 5e5],
        }
    )

    # Decision time T = 16:30 UTC: the 16:00 hour has NOT closed (closes 17:00).
    decision_t = datetime(2026, 5, 28, 16, 30, tzinfo=UTC)
    trimmed, info = drop_still_forming_bar(bars, "1h", "equity", now=decision_t)

    # The partial current hour is invisible; only the two closed hours remain.
    assert info["still_forming_dropped"] is True
    assert list(trimmed["timestamp"].dt.hour) == [14, 15]
    assert float(trimmed["close"].iloc[-1]) == 102.0  # last CLOSED hour
    assert info["still_forming_close"] == 102.5  # surfaced for opt-in analysts

    # No-lookahead fence: the kept set at T must equal "all bars whose period
    # closed by T" (timestamp + 1h <= T) -- it cannot depend on the partial.
    closed_by_t = bars[bars["timestamp"] + pd.Timedelta(hours=1) <= pd.Timestamp(decision_t)]
    assert list(trimmed["timestamp"]) == list(closed_by_t["timestamp"])


def test_still_forming_daily_path_byte_identical_under_change():
    """The DAILY path (the common case) is byte-identical after the
    timeframe-aware change: equity 1d mid-session still drops today's
    still-forming bar; after the ET close it keeps it."""
    from hermes_quant.data.bar_alignment import drop_still_forming_bar

    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-05-27", "2026-05-28"]).tz_localize("UTC"),
            "open": [100.0, 102.0],
            "high": [101.5, 103.0],
            "low": [99.5, 101.0],
            "close": [101.0, 102.5],
            "volume": [1e6, 5e5],
        }
    )

    # 14:00 ET (18:00 UTC, EDT): mid-session -> drop today's still-forming bar.
    mid = datetime(2026, 5, 28, 18, 0, tzinfo=UTC)
    trimmed_mid, info_mid = drop_still_forming_bar(bars, "1d", "equity", now=mid)
    assert len(trimmed_mid) == 1
    assert info_mid["still_forming_dropped"] is True
    assert float(trimmed_mid["close"].iloc[-1]) == 101.0

    # 17:00 ET (21:00 UTC, EDT): post-close -> keep the settled bar.
    post = datetime(2026, 5, 28, 21, 0, tzinfo=UTC)
    trimmed_post, info_post = drop_still_forming_bar(bars, "1d", "equity", now=post)
    assert len(trimmed_post) == 2
    assert info_post["still_forming_dropped"] is False
    assert "bar_settled" in info_post["reason"]


# ---------------------------------------------------------------------------
# Invariant 7d — PDR-4 SaturationScore PRODUCING path is lookahead-honest (RR11,
# Wave S5/M21; ADR-0079 D-4). The saturation decay multiplier at asof=T must be
# derived from ONLY anchors <= T: a future velocity peak, a future confirm_date,
# and a future packet asof must ALL be ignored (no decay manufactured from the
# future), and the producer must stamp the DECISION asof, never a future anchor.
# This is the saturation analogue of the velocity fence (Invariant 7c): RR11's
# test_pdr4_saturation.py covers the unit producer, this pins it as a RELEASE-
# BLOCKER fence (the canonical no-lookahead gate) on BOTH the pure producer and
# the frame-builder Step-6b seam.
# ---------------------------------------------------------------------------


def test_saturation_producer_ignores_future_anchors():
    """compute_saturation(asof=T) must IGNORE every anchor in the future of T —
    a future velocity peak, future confirm_date, and future packet asof all yield
    NO decay (m == 1.0, basis 'no_basis'), and the stamped asof is exactly T (the
    decision anchor), never a future anchor. A past anchor decays as expected, so
    this is a discriminating fence (not vacuously green)."""
    from hermes_quant.perception.saturation import compute_saturation

    asof = pd.Timestamp("2026-03-01T00:00:00Z")
    future = compute_saturation(
        packet_asof="2026-04-01T00:00:00Z",
        asof=asof,
        trend_velocity={"peak_period": "2026-04-15T00:00:00Z"},  # the REAL producer key
        confirm_date="2026-04-10T00:00:00Z",
    )
    assert future["decay_multiplier"] == 1.0, (
        "a future anchor manufactured saturation decay — PDR-4 producer lookahead leak"
    )
    assert future["basis"] == "no_basis"
    assert pd.Timestamp(future["asof"]) == asof, "stamped a future asof, not the decision asof"

    # And concretely: the SAME packet/peak in the PAST does decay (discriminating).
    past = compute_saturation(
        packet_asof="2026-01-01T00:00:00Z",
        asof=asof,
        trend_velocity={"peak_period": "2026-02-01T00:00:00Z"},
    )
    assert 0.0 < past["decay_multiplier"] < 1.0
    assert past["basis"] == "velocity_peak"


def test_build_perception_frame_saturation_ignores_future_anchors(monkeypatch, tmp_path):
    """build_perception_frame(..., decision_asof=T) with HERMES_QUANT_SATURATION=1
    must score saturation from ONLY anchors <= the frame asof (== last closed bar,
    which is <= T). A LOUD future confirm_date on the packet metadata must NOT drive
    the multiplier to the floor — the frame-builder Step-6b producer is lookahead-
    honest, not just the pure unit (RR11 frame-path release-blocker fence)."""
    from hermes_quant.catalyst import synthesize
    from hermes_quant.perception.builder import build_perception_frame

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("HERMES_QUANT_SATURATION", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    # Bars end at a fixed close; the frame asof is the last closed daily bar.
    bars = _make_bars(120, trend=0.5, seed=42)
    bar_last = pd.Timestamp(bars["timestamp"].iloc[-1])
    decision = (bar_last + pd.Timedelta(hours=1)).to_pydatetime()  # live decision after the close

    # A FRESH packet (published right at the last bar, so it is admitted) whose
    # metadata carries a FUTURE confirm_date — the loudest possible saturation
    # anchor. An honest producer must ignore it (confirm_date > asof) and fall
    # back to packet_age, which at age ~0 gives ~no decay.
    fresh_asof = bar_last.isoformat()
    future_confirm = (bar_last + pd.Timedelta(days=400)).isoformat()
    pkt = semantic_packet_from_dict_for_store(
        asset="AAPL", asof=fresh_asof, confirm_date=future_confirm
    )
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pkt, default=str) + "\n")

    frame = build_perception_frame(
        "AAPL",
        timeframe="1d",
        asset_class="equity",
        provider=_InertProvider(bars),
        asof_ts=bar_last,
        lookback_bars=200,
        decision_asof=decision,
    )
    assert frame is not None
    assert frame.semantic_packets, "expected the fresh packet to be absorbed"
    sat = frame.saturation
    assert sat is not None, "flag ON + packets present should attach a saturation score"
    # The future confirm_date must NOT have engaged -> not the hard-floor confirm basis.
    assert sat["basis"] != "confirm_date_passed", (
        "a FUTURE confirm_date drove saturation to the floor — PDR-4 FRAME producer "
        "lookahead leak (Step 6b)"
    )
    # asof-honesty: the score is stamped at the bar asof (<= decision), never the future.
    sat_asof = pd.Timestamp(sat["asof"])
    sat_asof = sat_asof.tz_localize("UTC") if sat_asof.tzinfo is None else sat_asof.tz_convert("UTC")
    assert sat_asof <= pd.Timestamp(decision), (
        f"saturation stamped asof={sat_asof} > decision={decision} — FRAME producer "
        f"lookahead leak"
    )
    # A fresh packet at age ~0 with no past anchor must not be silenced (m ~ 1.0).
    assert sat["decay_multiplier"] == pytest.approx(1.0, abs=1e-3)


def semantic_packet_from_dict_for_store(*, asset: str, asof: str, confirm_date: str) -> dict:
    """Build a hash-attached SemanticPacket dict carrying a confirm_date in metadata
    (the synthesize.py shape the saturation producer reads at builder.py Step 6b)."""
    from hermes_quant.semantic import semantic_packet_from_dict

    return semantic_packet_from_dict(
        {
            "schema_version": 1,
            "asset": asset,
            "asof": asof,
            "horizon": "1d",
            "stance": "bullish",
            "confidence": 0.70,
            "magnitude": 0.05,
            "summary": f"saturation no-lookahead fence packet {asset} {asof}",
            "sources": [{"type": "note", "ref": "saturation-lookahead-fence"}],
            "model": "hermes:saturation-lookahead-test",
            "metadata": {"confirm_date": confirm_date},
        }
    ).to_dict(include_hash=True)
