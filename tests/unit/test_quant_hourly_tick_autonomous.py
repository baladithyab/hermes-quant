"""Tests for the autonomous-phase shim in quant-hourly-tick.py.

Tests the gating, dry-run default, and import error handling without
ever invoking a real Alpaca call.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-hourly-tick.py"


def _load_hourly_tick_module() -> types.ModuleType:
    """Load quant-hourly-tick.py as a module without running main().

    The script uses `if __name__ == \"__main__\": sys.exit(main())` so
    importing it does NOT exec main().
    """
    # The script reads ~/.hermes/secrets/alpaca.env at module import via
    # CREDS = _load_creds(). For tests, we need that file to exist, OR
    # we monkey-patch _load_creds before exec. We'll do the latter via
    # an env-var bypass at module level.
    # Actually: looking at the script, _load_creds is called at module
    # top-level. Easiest: skip if creds file missing.
    if not (Path.home() / ".hermes" / "secrets" / "alpaca.env").exists():
        pytest.skip("Alpaca creds not present — skipping module-load test")

    spec = importlib.util.spec_from_file_location("_qht_test", str(SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_autonomous_phase_disabled_by_default(monkeypatch):
    """No env var → returns empty string, never imports playbook tick."""
    m = _load_hourly_tick_module()
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS_ARMED", raising=False)
    assert m.maybe_run_autonomous_phase() == ""


def test_autonomous_phase_enabled_dry_run(monkeypatch):
    """HERMES_QUANT_AUTONOMOUS=1, no ARMED → dry_run=True, returns string only on notable events."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS_ARMED", raising=False)

    # Replace the imported playbook tick with a stub that returns a
    # zero-fired summary — should silence-by-default.
    fake_summary = {
        "tick_id": "test", "date_et": "2026-05-26", "dry_run": True,
        "scanned": 5, "fired": 0, "silenced": 0, "gate_rejected": 5,
        "idempotent_skipped": 0, "errors": 0, "halt_aborted": False,
    }
    fake_module = types.SimpleNamespace(
        run_tick=lambda dry_run: dict(fake_summary, dry_run=dry_run),
        append_journal=lambda rec: None,
    )
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: fake_module)
    out = m.maybe_run_autonomous_phase()
    assert out == "", f"expected silent, got {out!r}"


def test_autonomous_phase_speaks_on_fire(monkeypatch):
    """A non-zero fired count surfaces a single-line summary."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS_ARMED", raising=False)

    fake_summary = {
        "tick_id": "t", "date_et": "d", "dry_run": True,
        "scanned": 5, "fired": 2, "silenced": 1, "gate_rejected": 1,
        "idempotent_skipped": 1, "errors": 0, "halt_aborted": False,
    }
    fake_module = types.SimpleNamespace(
        run_tick=lambda dry_run: dict(fake_summary, dry_run=dry_run),
        append_journal=lambda rec: None,
    )
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: fake_module)
    out = m.maybe_run_autonomous_phase()
    assert "fired=2" in out
    # Dry-run (unarmed) surfaces the 🧪 dry-run mode tag (ARIA reconcile 078b9a1
    # replaced the old " (dry-run)" suffix with a per-mode tag: 🧪 dry-run / 📦 paper).
    assert "🧪 dry-run" in out


def test_autonomous_phase_armed_loses_dry_run_suffix(monkeypatch):
    """ARMED=1 removes the dry-run suffix (and runs with dry_run=False)."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS_ARMED", "1")

    captured = {}

    def fake_run_tick(dry_run):
        captured["dry_run"] = dry_run
        return {
            "tick_id": "t", "date_et": "d", "dry_run": dry_run,
            "scanned": 5, "fired": 1, "silenced": 0, "gate_rejected": 4,
            "idempotent_skipped": 0, "errors": 0, "halt_aborted": False,
        }

    fake_module = types.SimpleNamespace(run_tick=fake_run_tick, append_journal=lambda r: None)
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: fake_module)
    out = m.maybe_run_autonomous_phase()
    assert captured["dry_run"] is False
    # ARMED surfaces the 📦 paper mode tag, NOT the 🧪 dry-run tag (post-078b9a1).
    assert "📦 paper" in out
    assert "🧪 dry-run" not in out
    assert "fired=1" in out


def test_autonomous_phase_speaks_on_halt(monkeypatch):
    """Active halts must surface a 🚨 line even with zero fires."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS_ARMED", raising=False)

    fake_module = types.SimpleNamespace(
        run_tick=lambda dry_run: {
            "tick_id": "t", "date_et": "d", "dry_run": dry_run,
            "scanned": 0, "fired": 0, "silenced": 0, "gate_rejected": 0,
            "idempotent_skipped": 0, "errors": 0, "halt_aborted": True,
        },
        append_journal=lambda rec: None,
    )
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: fake_module)
    out = m.maybe_run_autonomous_phase()
    # Halt headline is "HALT-ABORTED" (post-078b9a1 deployed formatter wording).
    assert "HALT-ABORTED" in out
    assert "🚨" in out


def test_autonomous_phase_handles_import_failure(monkeypatch):
    """If quant-playbook-tick.py is missing or unimportable, surface a warning string."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: None)
    out = m.maybe_run_autonomous_phase()
    assert "could not be imported" in out


def test_autonomous_phase_handles_run_tick_crash(monkeypatch):
    """If run_tick() throws, surface the exception summary, don't propagate."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")

    def bad_run_tick(dry_run):
        raise RuntimeError("simulated crash")

    fake_module = types.SimpleNamespace(run_tick=bad_run_tick, append_journal=lambda r: None)
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: fake_module)
    out = m.maybe_run_autonomous_phase()
    assert "crashed" in out
    assert "RuntimeError" in out
    assert "simulated crash" in out


def test_autonomous_phase_appends_audit_record(monkeypatch):
    """Even on silent ticks, an audit record must be appended to the journal."""
    m = _load_hourly_tick_module()
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS", "1")

    appended: list[dict] = []

    def fake_run_tick(dry_run):
        return {
            "tick_id": "t-id-42", "date_et": "2026-05-26", "dry_run": dry_run,
            "scanned": 3, "fired": 0, "silenced": 0, "gate_rejected": 3,
            "idempotent_skipped": 0, "errors": 0, "halt_aborted": False,
        }

    fake_module = types.SimpleNamespace(
        run_tick=fake_run_tick, append_journal=appended.append
    )
    monkeypatch.setattr(m, "_import_playbook_tick", lambda: fake_module)
    out = m.maybe_run_autonomous_phase()
    # Silent (zero fires, dry-run) — but audit record was written
    assert out == ""
    assert len(appended) == 1
    rec = appended[0]
    assert rec["event"] == "autonomous_phase_summary"
    assert rec["source"] == "hourly"
    assert rec["tick_id"] == "t-id-42"
    assert rec["scanned"] == 3
