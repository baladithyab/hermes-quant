"""Tests for hermes_quant.catalyst.wiring.semantic_market_extras (C2-2).

The single catalyst->advisor wiring seam. These pin:
  * flag-OFF -> None (byte-identical no-op everywhere),
  * no-packets / error -> None (silence-by-default, never raises),
  * flag-ON + a packet in the store -> dict with semantic_packets + decision_asof
    stamped (and base_extras preserved),
  * the regression test for gap G3 / M17: all three live decision paths route
    packets to the advisor so a flipped flag takes effect on every path. Post
    ADR-0079 PDR-1 the helper is now an INTERNAL of build_perception_frame, and
    the 3 crons hand the advisor a PerceptionFrame carrying semantic_packets
    (the single producer) instead of three bespoke market_extras dicts.

No network: the packet store is a tmp_path JSONL the helper reads via
load_packets_for; the default provider + advisor.recommend are monkeypatched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hermes_quant.catalyst import synthesize, wiring


class _RecordingProvider:
    """Canned-bars provider so build_perception_frame_live never hits network."""

    name = "recording"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, n: int = 120):
        rng = np.random.default_rng(seed=42)
        ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
        closes = 100.0 + np.arange(n) * 0.5 + rng.normal(0, 0.5, n)
        self._bars = pd.DataFrame(
            {
                "timestamp": ts,
                "open": closes - 0.1,
                "high": closes + 0.4,
                "low": closes - 0.4,
                "close": closes,
                "volume": rng.uniform(1e6, 5e6, n),
            }
        )

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _patch_default_provider(monkeypatch):
    monkeypatch.setattr(
        "hermes_quant.advisor._get_default_provider", lambda asset_class: _RecordingProvider()
    )


def _write_packet(
    store: Path,
    *,
    asset: str = "AAPL",
    asof: str,
    stance: str = "bullish",
    confidence: float = 0.7,
    magnitude: float = 0.02,
    horizon: str = "1d",
) -> None:
    """Append one SemanticPacket dict to a JSONL store, matching synthesize shape."""
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
            "summary": f"wiring test packet {asset} {stance}",
            "sources": [{"type": "note", "ref": "wiring-test"}],
            "model": "hermes:wiring-test",
        }
    )
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pkt.to_dict(include_hash=True), default=str) + "\n")


def test_semantic_market_extras_off_returns_none(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_SEMANTIC_ENABLED", raising=False)
    assert wiring.semantic_market_extras("AAPL") is None


def test_semantic_market_extras_no_packets_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    empty_store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", empty_store)
    assert wiring.semantic_market_extras("AAPL") is None


def test_semantic_market_extras_loads_and_stamps_decision_asof(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)
    # Packet published in the past relative to the decision asof.
    _write_packet(store, asset="AAPL", asof="2026-01-01T09:00:00Z")
    decision = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    out = wiring.semantic_market_extras(
        "AAPL", decision_asof=decision, base_extras={"keep_me": 1}
    )
    assert out is not None
    assert out["semantic_packets"], "expected a non-empty packet list"
    assert out["decision_asof"] == decision.isoformat()
    # base_extras keys preserved.
    assert out["keep_me"] == 1


def test_semantic_market_extras_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")

    def _boom(*a, **k):
        raise RuntimeError("packet load exploded")

    monkeypatch.setattr(synthesize, "load_packets_for", _boom)
    # Must swallow the exception and return None (silence-by-default).
    assert wiring.semantic_market_extras("AAPL") is None


# ---------------------------------------------------------------------------
# Gap-G3 regression: all three live decision paths inject packets via the helper.
# ---------------------------------------------------------------------------


def _arm_store(monkeypatch, tmp_path, symbol: str) -> Path:
    """Flag-on + a fresh past packet for `symbol` in a tmp store."""
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)
    # Use a recent asof so freshness (24h default) holds against wall-clock now.
    now = datetime.now(UTC)
    _write_packet(
        store,
        asset=symbol,
        asof=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    return store


def test_daily_interim_path_injects_packets(monkeypatch, tmp_path):
    """quant-daily-interim recommend_one hands the advisor a PerceptionFrame
    carrying semantic_packets (PDR-1: the single producer, not market_extras)."""
    advisor = __import__("hermes_quant.advisor", fromlist=["recommend"])
    _arm_store(monkeypatch, tmp_path, "AAPL")
    _patch_default_provider(monkeypatch)

    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return {"aggregated_signal": {}, "risk_gate": {}, "data_quality": {}, "caveats": []}

    monkeypatch.setattr(advisor, "recommend", _spy)

    import_mod = _load_script("quant-daily-interim.py")
    import_mod.recommend_one("AAPL", "equity", "1d")
    frame = captured.get("perception_frame")
    assert frame is not None and frame.semantic_packets, (
        "daily-interim did not hand the advisor a frame carrying semantic_packets"
    )


def test_autonomous_tick_path_injects_packets(monkeypatch, tmp_path):
    """The autonomous path hands the advisor a PerceptionFrame carrying packets.

    Post ADR-0079 PDR-1 / M17, the producer moved OUT of the cron's
    _direction_screened_recommend wrapper and INTO autonomous.tick itself (so the
    quant_autonomous_tick TOOL path perceives the same frame). This drives the
    REAL autonomous.tick with the flag ON; the frame it builds must reach the
    spied advisor as perception_frame carrying semantic_packets.
    """
    _arm_store(monkeypatch, tmp_path, "AAPL")
    _patch_default_provider(monkeypatch)
    advisor = __import__("hermes_quant.advisor", fromlist=["recommend"])

    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return {
            "as_of": "2026-01-01T00:00:00Z",
            "decision_price": 100.0,
            "signal_id": "sig",
            "aggregated_signal": {"direction": 0, "confidence": 0.5, "magnitude": 0.01},
            "risk_gate": {"pass": False, "kelly_fraction": 0.0, "gated_reason": "flat"},
            "analyst_views": [],
            "lessons": [],
        }

    monkeypatch.setattr(advisor, "recommend", _spy)

    import yaml

    import hermes_quant.autonomous as auto
    from hermes_quant.watchlist import WatchlistEntry

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"quant": {"pdr": {"mode": "autonomous"}}}), encoding="utf-8"
    )
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr(
        "hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "autonomous_kill_switch.json"
    )

    auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry(symbol="AAPL", asset_class="equity", timeframe="1d")],
        advisor_recommend=_spy,
    )

    frame = captured.get("perception_frame")
    assert frame is not None and frame.semantic_packets, (
        "autonomous.tick did not hand the advisor a frame carrying semantic_packets "
        "(M17: producer must live inside tick so the tool path perceives it too)"
    )


def test_playbook_tick_path_injects_packets(monkeypatch, tmp_path):
    """quant-playbook-tick call_advisor hands the advisor a PerceptionFrame
    carrying semantic_packets (PDR-1: the single producer, not market_extras).

    Mock mode short-circuits before the seam, so we run the real advisor path
    with advisor.recommend monkeypatched to a spy.
    """
    monkeypatch.delenv("HERMES_QUANT_PLAYBOOK_TICK_MOCK", raising=False)
    _arm_store(monkeypatch, tmp_path, "AAPL")
    _patch_default_provider(monkeypatch)
    advisor = __import__("hermes_quant.advisor", fromlist=["recommend"])

    captured: dict = {}

    def _spy(symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return {"aggregated_signal": {}, "risk_gate": {}, "caveats": []}

    monkeypatch.setattr(advisor, "recommend", _spy)

    pt = _load_script("quant-playbook-tick.py")
    pt.call_advisor("AAPL")
    frame = captured.get("perception_frame")
    assert frame is not None and frame.semantic_packets, (
        "playbook-tick did not hand the advisor a frame carrying semantic_packets"
    )


def _load_script(name: str):
    """Import an ops/scripts/*.py module by path (they aren't a package).

    The scripts re-exec under the hermes-agent venv at import via os.execv when
    ``sys.executable`` differs from that venv. Under pytest that would replace the
    test process. We neutralize the guard by pinning ``sys.executable`` to the venv
    path (so the guard's inequality is false) for the duration of the import.
    """
    import importlib.util
    import sys

    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / name
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # make the script's execv guard a no-op
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod
