"""M17 regression — the quant_autonomous_tick TOOL path perceives the frame.

Before PDR-1, semantic injection lived ONLY in the cron's
``_direction_screened_recommend`` monkey-patch; the TOOL path
(``quant_autonomous_tick`` -> ``quant_tools`` -> ``autonomous.tick`` calling the
bare ``advisor.recommend``) injected NOTHING, so with the flag ON it returned
``no_semantic_packets``. PDR-1 moves the producer INTO ``autonomous.tick`` (the
single producer BOTH the tool and the cron reach). This test drives ``tick`` the
way the tool handler does (default ``advisor_recommend=None``, i.e. the real
``recommend``) and asserts the semantic analyst saw the seeded packet on the tool
path — proving M17 closed structurally.

Deterministic, no network: the default provider is monkeypatched to a recording
provider; packets live in a tmp JSONL the builder reads via load_packets_for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from hermes_quant import autonomous
from hermes_quant.watchlist import WatchlistEntry


def _make_bars(n: int = 120, *, trend: float = 0.5, seed: int = 42):
    rng = np.random.default_rng(seed=seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = 100.0 + np.arange(n) * trend + rng.normal(0, 0.5, n)
    opens = closes - rng.uniform(0, 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, 0.4, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _RecordingProvider:
    name = "recording"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _seed_packet(store, *, asset, asof, stance, confidence, magnitude, horizon="1d"):
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
            "summary": f"m17 {asset} {stance} {asof}",
            "sources": [{"type": "note", "ref": "m17-fence"}],
            "model": "hermes:m17-test",
        }
    )
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pkt.to_dict(include_hash=True), default=str) + "\n")


def _set_mode_autonomous(monkeypatch, tmp_path):
    import yaml

    cfg = tmp_path / "config.yaml"
    data = {"quant": {"pdr": {"mode": "autonomous"}, "autonomous": {"max_per_tick_opens": 1}}}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr(
        "hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "autonomous_kill_switch.json"
    )


def test_tool_path_perceives_semantic_frame(monkeypatch, tmp_path):
    """With HERMES_QUANT_SEMANTIC_ENABLED=1, tick() is the PRODUCER of the frame
    (not the cron wrapper). It builds the frame and hands it to whatever
    advisor_recommend it was given. We capture the perception_frame tick passes
    and assert it carries the seeded packet — proving the producer is INSIDE tick
    (the codepath the TOOL handler reaches), i.e. M17 closed structurally. The
    real recommend is invoked too (defense-in-depth: the projected ctx admits the
    packet through the semantic analyst rather than no_semantic_packets)."""
    from hermes_quant.advisor import recommend as _real_recommend
    from hermes_quant.catalyst import synthesize

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    _set_mode_autonomous(monkeypatch, tmp_path)
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    bars = _make_bars(120, trend=0.5, seed=42)
    # Default provider resolution -> our recording provider (no network). tick's
    # frame builder resolves this; the real recommend's None-branch fallback would
    # too, so either way no network is hit.
    monkeypatch.setattr(
        "hermes_quant.advisor._get_default_provider", lambda asset_class: _RecordingProvider(bars)
    )

    # A past packet relative to wall-clock now, within the 24h freshness window
    # (load_packets_for default max_age_minutes=24*60). tick builds the frame with
    # decision_asof = wall-clock now, so seed an asof a few minutes in the past.
    recent = (datetime.now(UTC) - pd.Timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_packet(
        store, asset="AAPL", asof=recent,
        stance="bullish", confidence=0.70, magnitude=0.01,
    )

    captured: dict = {}

    def _capturing_recommend(**kw):
        captured["frame"] = kw.get("perception_frame")
        return _real_recommend(**kw)

    autonomous.tick(
        dry_run=True,
        symbols=[WatchlistEntry(symbol="AAPL", asset_class="equity", timeframe="1d")],
        advisor_recommend=_capturing_recommend,
    )

    frame = captured.get("frame")
    assert frame is not None, (
        "tick did not build a PerceptionFrame with the flag ON — M17 producer "
        "is not inside tick (tool-vs-cron decoupling not closed)"
    )
    assert frame.semantic_packets, (
        "tick built a frame but it carried NO semantic packets despite a valid "
        "seeded packet + flag ON — M17 regression"
    )
    stances = {p["stance"] for p in frame.semantic_packets}
    assert stances == {"bullish"}, f"unexpected packet content: {stances}"


def test_tool_path_flag_off_builds_no_frame(monkeypatch, tmp_path):
    """Off-switch: SEMANTIC_ENABLED=0 => tick must NOT build a frame (no redundant
    fetch / no provider resolution) — perception_frame stays None, bit-identical to
    the pre-promotion path. We assert the default provider resolver is never called
    when the flag is OFF. (Default is now ON per FLAGS.md Tier A; the inert path is
    requested explicitly.)"""
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    _set_mode_autonomous(monkeypatch, tmp_path)

    calls = {"n": 0}

    def _boom(asset_class):
        calls["n"] += 1
        raise AssertionError("default provider resolved with flag OFF — extra fetch")

    monkeypatch.setattr("hermes_quant.advisor._get_default_provider", _boom)

    captured = {}

    def _fake_advisor(**kw):
        captured.update(kw)
        return {
            "as_of": "2026-05-13T20:00:00Z",
            "decision_price": 100.0,
            "signal_id": "sig_test",
            "aggregated_signal": {"confidence": 0.85, "direction": 1, "magnitude": 0.05},
            "risk_gate": {"pass": True, "kelly_fraction": 0.05, "reason": "ok", "gated_reason": None},
            "analyst_views": [{"analyst": f"A{i}", "metadata": {"atr_relative": 0.05}} for i in range(2)],
            "lessons": [],
        }

    autonomous.tick(
        dry_run=True,
        symbols=[WatchlistEntry(symbol="AAPL", asset_class="equity", timeframe="1d")],
        advisor_recommend=_fake_advisor,
    )
    assert calls["n"] == 0, "frame build attempted with flag OFF (M17 over-reach)"
    assert captured.get("perception_frame") is None, (
        "tick passed a non-None frame with the flag OFF — not byte-identical"
    )
