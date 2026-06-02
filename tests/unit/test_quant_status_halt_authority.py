"""ADR-0085 regression: quant_status reports the AUTHORITATIVE halt state, not a stale signal.

The 2026-06-01 phantom-halt scare: quant_status surfaced `last_signal` from the deprecated
signals.jsonl bus, whose last entry was a 2026-05-13 operator_emergency_stop — so status read
as halted while the live halt registry (halt_state.json) was empty. ADR-0085 reporting rule:
the authoritative halt state comes from the halt registry; a stale halt SIGNAL is historical
and must never read as a current halt.
"""
from __future__ import annotations

import json

from hermes_quant import tools


def _seed_stale_halt_signal(signal_bus_path, monkeypatch):
    """Write a stale halt entry to a tmp signals.jsonl and point tools at it."""
    rows = [
        {"type": "heartbeat", "asof": "2026-05-13T18:00:00Z"},
        {"type": "halt", "asof": "2026-05-13T18:15:12Z", "reason": "operator_emergency_stop"},
    ]
    signal_bus_path.parent.mkdir(parents=True, exist_ok=True)
    with open(signal_bus_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setattr(tools, "SIGNAL_BUS_PATH", signal_bus_path, raising=True)


def test_stale_halt_signal_does_not_read_as_halted(tmp_path, monkeypatch):
    """A 2026-05-13 halt SIGNAL on the deprecated bus must NOT make status report halted,
    when the authoritative halt registry is empty."""
    _seed_stale_halt_signal(tmp_path / "signals.jsonl", monkeypatch)
    # authoritative registry: empty (the state.db isolation fixture already gives a tmp,
    # empty halt registry, so read_halt_mirror() returns []).
    result = json.loads(tools.quant_status({}))

    # The authoritative fields say NOT halted.
    assert result["halted"] is False, "stale halt signal must not read as a current halt"
    assert result["active_halts"] == []
    assert result["halt_state_read_ok"] is True

    # The stale halt is preserved but explicitly HISTORICAL — never the legacy `last_signal` key.
    assert "last_signal" not in result, "the misleading last_signal key must be renamed"
    hist = result["last_signal_historical"]
    assert hist is not None and hist.get("type") == "halt"  # still visible, just labeled historical


def test_active_registry_halt_does_read_as_halted(tmp_path, monkeypatch):
    """When the AUTHORITATIVE halt registry has an active halt, status reports halted=True."""
    _seed_stale_halt_signal(tmp_path / "signals.jsonl", monkeypatch)

    fake_halt = {
        "account_id": "paper-default",
        "asset_class": "equity",
        "asset": None,
        "reason": "test active halt",
        "halted_at": "2026-06-01T00:00:00Z",
        "halted_until": None,
    }
    # Patch the authoritative read to return an active halt.
    import hermes_quant.daemon.halt_state as hs

    monkeypatch.setattr(hs, "read_halt_mirror", lambda *a, **k: [fake_halt], raising=True)

    result = json.loads(tools.quant_status({}))
    assert result["halted"] is True
    assert result["active_halts"] == [fake_halt]


def test_halt_read_failure_is_fail_safe(tmp_path, monkeypatch):
    """If the authoritative halt read raises, status must not crash — it reports the read
    failure and does not silently claim 'not halted' as if verified."""
    _seed_stale_halt_signal(tmp_path / "signals.jsonl", monkeypatch)
    import hermes_quant.daemon.halt_state as hs

    def _boom(*a, **k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(hs, "read_halt_mirror", _boom, raising=True)

    result = json.loads(tools.quant_status({}))
    assert result["success"] is True  # never crashes the status read
    assert result["halt_state_read_ok"] is False  # the failure is visible, not hidden
