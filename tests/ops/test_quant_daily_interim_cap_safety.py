"""tests/ops/test_quant_daily_interim_cap_safety.py — P1 cap-gate safety regressions.

Covers the two P1s Codex flagged on PR #82 against the backported advisor cap gate
in ops/scripts/quant-daily-interim.py::auto_approve_actionables:

  P1-A  size_override_pct pass-through — the cap-admitted size MUST be passed into
        quant_approve so routes that skip the home-grown cap (e.g. AlpacaPaperReactor)
        still fire the CLIPPED size, not the stored Kelly size.
  P1-B  fail-CLOSED on cap init / clip failure under HERMES_QUANT_PORTFOLIO_CAPS=1 —
        an unreadable live book or a clip exception must BLOCK the actionable, never
        fall back to firing uncapped (the exact 2026-06-02 runaway behavior).

The brief is a standalone script (not a package module), so we load it via
spec_from_file_location and stub the quant_approve tool (`_qa`) + the cap primitive.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_BRIEF_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-daily-interim.py"
)


def _load_brief():
    spec = importlib.util.spec_from_file_location("ops_brief_capsafety", _BRIEF_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _actionable(symbol="NVDA", kelly=0.20, pid="prop_1"):
    return {
        "symbol": symbol,
        "asset": symbol,
        "proposal_id": pid,
        "kelly_fraction": kelly,
    }


def _stub_quant_approve(monkeypatch, sink):
    """Patch hermes_quant.tools.quant_approve (the brief imports it LOCALLY as
    `from hermes_quant.tools import quant_approve as _qa`, so we must patch the
    source attribute, not a module-level name on the brief)."""
    import hermes_quant.tools as qt

    def fake_qa(args):
        sink.append(args)
        return '{"success": true}'

    monkeypatch.setattr(qt, "quant_approve", fake_qa)


def _stub_live_book_empty(monkeypatch):
    """Make the cap gate's live-book seeding succeed with an EMPTY book.

    The brief does `import sqlite3 as _sq; _sq.connect("~/.hermes/quant/state.db")`
    then `SELECT symbol, quantity FROM positions ...`. CI has no state.db (no
    positions table) → the real read raises OperationalError, which the
    fail-closed guard (correctly) treats as cap-init failure. For the
    cap-SUCCESS tests we stub sqlite3.connect to hand back an in-memory DB with
    an empty positions table so init succeeds and we exercise the clip path."""
    import sqlite3

    real_connect = sqlite3.connect

    def fake_connect(path, *a, **k):
        if "state.db" in str(path):
            con = real_connect(":memory:")
            con.execute(
                "CREATE TABLE positions (account_id TEXT, symbol TEXT, quantity REAL)"
            )
            return con
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)


# ---------------------------------------------------------------------------
# P1-A: size_override_pct pass-through
# ---------------------------------------------------------------------------


def test_clipped_size_passed_as_size_override(monkeypatch):
    """When the cap scales a pick down, quant_approve must receive the clipped
    size via size_override_pct — otherwise it re-derives the uncapped Kelly size
    and the clip is cosmetic on routes that skip the home-grown cap."""
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)
    _stub_live_book_empty(monkeypatch)

    # Cap admits a DOWN-SCALED 0.05 (from kelly 0.20).
    clipped = SimpleNamespace(
        fired=True, portfolio_target_pct=0.05, scale_factor=0.25, silence_reason=None
    )
    import hermes_quant.risk.portfolio_normalize as pn
    monkeypatch.setattr(pn, "clip_one_to_remaining_headroom", lambda *a, **k: clipped)

    out = mod.auto_approve_actionables([_actionable()])
    assert out[0].get("auto_approved") is True, out[0].get("auto_approve_error")
    assert len(calls) == 1, "quant_approve should have been called exactly once"
    assert "size_override_pct" in calls[0], "clipped size was NOT passed to quant_approve"
    assert calls[0]["size_override_pct"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# P1-B: fail CLOSED on cap init failure
# ---------------------------------------------------------------------------


def test_cap_init_failure_blocks_not_fires(monkeypatch):
    """If caps_enabled but the cap primitive import / live-book read fails, every
    actionable must be BLOCKED (auto_approve_error set, not fired) — never the
    uncapped runaway fallback."""
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)

    # Make cap init fail hard (PortfolioCaps.standard() raises).
    import hermes_quant.risk.portfolio_normalize as pn
    monkeypatch.setattr(
        pn, "PortfolioCaps",
        SimpleNamespace(standard=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
    )

    out = mod.auto_approve_actionables([_actionable(pid="p1"), _actionable(pid="p2")])
    assert len(calls) == 0, "BLOCKED actionables must NOT fire when cap init fails"
    for v in out:
        assert v.get("auto_approved") is not True
        assert "cap_gate_init_failed" in (v.get("auto_approve_error") or "")
        assert "BLOCKED" in (v.get("auto_approve_error") or "")


def test_cap_disabled_fires_normally(monkeypatch):
    """Default-OFF: with the flag unset, the cap gate is a no-op and fires proceed
    (proving the fail-closed change does not break the default path)."""
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)

    out = mod.auto_approve_actionables([_actionable()])
    assert out[0].get("auto_approved") is True
    assert len(calls) == 1
    # No cap applied → no size override forced.
    assert "size_override_pct" not in calls[0]
