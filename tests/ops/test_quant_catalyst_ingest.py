"""OpenBB live-source wiring for quant-catalyst-ingest."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-catalyst-ingest.py"


def _load():
    spec = importlib.util.spec_from_file_location("quant_catalyst_ingest", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quant_catalyst_ingest"] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_pipeline(monkeypatch, mod, *, packets=None):
    monkeypatch.setattr(mod, "load_graph", lambda: ({}, {}))
    monkeypatch.setattr(mod, "ingest_queries", lambda queries: [])
    monkeypatch.setattr(mod, "synthesize_packets", lambda items, **kwargs: packets or [])
    monkeypatch.setattr(mod, "log_propagations", lambda rows: 0)
    monkeypatch.setattr(mod, "write_packets", lambda ps: len(ps))


def test_openbb_news_default_off_not_called(monkeypatch, capsys):
    mod = _load()
    _stub_pipeline(monkeypatch, mod)
    monkeypatch.delenv("HERMES_QUANT_OPENBB", raising=False)
    monkeypatch.delenv("HERMES_QUANT_SOCIAL_INGEST", raising=False)
    monkeypatch.setattr(
        mod,
        "ingest_openbb_news",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OpenBB called while off")),
    )
    monkeypatch.setattr(sys, "argv", ["quant-catalyst-ingest.py", "--json"])

    assert mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == 0
    assert payload["openbb_items"] == 0


def test_openbb_news_flag_on_feeds_ingest_with_asof(monkeypatch, capsys):
    mod = _load()
    seen: dict[str, object] = {}

    def fake_openbb(query, *, as_of):
        seen["query"] = query
        seen["as_of"] = as_of
        return ([object()], 0.01)

    _stub_pipeline(monkeypatch, mod)
    monkeypatch.setenv("HERMES_QUANT_OPENBB", "1")
    monkeypatch.delenv("HERMES_QUANT_SOCIAL_INGEST", raising=False)
    monkeypatch.setattr(mod, "ingest_openbb_news", fake_openbb)
    monkeypatch.setattr(sys, "argv", ["quant-catalyst-ingest.py", "--json"])

    assert mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == 1
    assert payload["openbb_items"] == 1
    assert seen["query"] == "openbb_world"
    assert isinstance(seen["as_of"], datetime)
    assert seen["as_of"].tzinfo is not None


def test_openbb_news_failure_is_nonfatal(monkeypatch, capsys):
    mod = _load()
    _stub_pipeline(monkeypatch, mod)
    monkeypatch.setenv("HERMES_QUANT_OPENBB", "1")
    monkeypatch.delenv("HERMES_QUANT_SOCIAL_INGEST", raising=False)
    monkeypatch.setattr(
        mod,
        "ingest_openbb_news",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(sys, "argv", ["quant-catalyst-ingest.py", "--json"])

    assert mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == 0
    assert payload["openbb_items"] == 0
    assert "RuntimeError: boom" in payload["openbb_error"]
