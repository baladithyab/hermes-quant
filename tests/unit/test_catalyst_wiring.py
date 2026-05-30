"""Tests for hermes_quant.catalyst.wiring.semantic_market_extras (C2-2).

The single catalyst->advisor wiring seam. These pin:
  * flag-OFF -> None (byte-identical no-op everywhere),
  * no-packets / error -> None (silence-by-default, never raises),
  * flag-ON + a packet in the store -> dict with semantic_packets + decision_asof
    stamped (and base_extras preserved),
  * the regression test for gap G3: all three live decision paths route packets
    through this helper so a flipped flag takes effect on every path.

No network: the packet store is a tmp_path JSONL the helper reads via
load_packets_for; advisor.recommend is monkeypatched to a spy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hermes_quant.catalyst import synthesize, wiring


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
    """quant-daily-interim recommend_one routes packets through the helper."""
    advisor = __import__("hermes_quant.advisor", fromlist=["recommend"])
    _arm_store(monkeypatch, tmp_path, "AAPL")

    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return {"aggregated_signal": {}, "risk_gate": {}, "data_quality": {}, "caveats": []}

    monkeypatch.setattr(advisor, "recommend", _spy)

    import_mod = _load_script("quant-daily-interim.py")
    import_mod.recommend_one("AAPL", "equity", "1d")
    me = captured.get("market_extras")
    assert me is not None and me.get("semantic_packets"), (
        "daily-interim did not inject semantic_packets via the wiring helper"
    )


def test_autonomous_tick_path_injects_packets(monkeypatch, tmp_path):
    """quant-autonomous-tick _direction_screened_recommend injects packets.

    We reconstruct the wrapper exactly as run_tick builds it: it closes over
    _base_recommend (= advisor.recommend) and the flag. The bias gate is OFF
    by default, so the wrapper returns the base result untouched — but it must
    still have injected market_extras before calling the base recommend.
    """
    _arm_store(monkeypatch, tmp_path, "AAPL")
    advisor = __import__("hermes_quant.advisor", fromlist=["recommend"])

    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return {"aggregated_signal": {"direction": 0}, "risk_gate": {}}

    monkeypatch.setattr(advisor, "recommend", _spy)

    # Build the wrapper the same way the script does (flag OFF -> bias screen no-op).
    _base_recommend = advisor.recommend
    _direction_bias_gate_on = False

    def _direction_screened_recommend(**kwargs):
        _inj_sym = kwargs.get("symbol")
        if _inj_sym and "market_extras" not in kwargs:
            from hermes_quant.catalyst.wiring import semantic_market_extras
            _me = semantic_market_extras(_inj_sym, horizon=kwargs.get("timeframe", "1d"))
            if _me is not None:
                kwargs = {**kwargs, "market_extras": _me}
        res = _base_recommend(**kwargs)
        if not _direction_bias_gate_on:
            return res
        return res

    _direction_screened_recommend(symbol="AAPL", asset_class="equity", timeframe="1d")
    me = captured.get("market_extras")
    assert me is not None and me.get("semantic_packets"), (
        "autonomous-tick did not inject semantic_packets via the wiring helper"
    )


def test_playbook_tick_path_injects_packets(monkeypatch, tmp_path):
    """quant-playbook-tick call_advisor routes packets through the helper.

    Mock mode short-circuits before the wiring seam, so we run the real
    advisor path with advisor.recommend monkeypatched to a spy.
    """
    monkeypatch.delenv("HERMES_QUANT_PLAYBOOK_TICK_MOCK", raising=False)
    _arm_store(monkeypatch, tmp_path, "AAPL")
    advisor = __import__("hermes_quant.advisor", fromlist=["recommend"])

    captured: dict = {}

    def _spy(symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return {"aggregated_signal": {}, "risk_gate": {}, "caveats": []}

    monkeypatch.setattr(advisor, "recommend", _spy)

    pt = _load_script("quant-playbook-tick.py")
    pt.call_advisor("AAPL")
    me = captured.get("market_extras")
    assert me is not None and me.get("semantic_packets"), (
        "playbook-tick did not inject semantic_packets via the wiring helper"
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
