"""tests/ops/test_quant_daily_interim_cap_safety.py — P1 cap-gate safety regressions.

Covers the two P1s Codex flagged on PR #82 against the backported advisor cap gate
in ops/scripts/quant-daily-interim.py::auto_approve_actionables, PLUS the
2026-06-10 phantom-gross correctness fix:

  P1-A  size_override_pct pass-through — the cap-admitted size MUST be passed into
        quant_approve so routes that skip the home-grown cap (e.g. AlpacaPaperReactor)
        still fire the CLIPPED size, not the stored Kelly size.
  P1-B  fail-CLOSED on cap init / clip failure under HERMES_QUANT_PORTFOLIO_CAPS=1 —
        an unreadable live book or a clip exception must BLOCK the actionable, never
        fall back to firing uncapped (the exact 2026-06-02 runaway behavior).
  P0-C  (2026-06-10) phantom-gross seed source — the cap MUST seed its running book
        from the canonical paper-book projection
        (reconstruct_portfolio_state(reactor_filter="paper")), NOT from
        state.db.positions.quantity. The state.db table stores ADDITIVE raw share
        counts (a corrupt AAPL row at qty=399.93 from the 2026-06-08
        reconstruct_from-after-flatten incident) which, read as target_position_pct,
        made the cap see a phantom gross ~402% and silence EVERY advisor auto-fire
        on fake over-leverage while the real paper book was 20% gross.

The brief is a standalone script (not a package module), so we load it via
spec_from_file_location and stub the quant_approve tool (`_qa`) + the cap primitive.
"""
from __future__ import annotations

import importlib.util
import json
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


def _write_paper_bus(path: Path, positions: dict[str, float]) -> None:
    """Write a minimal executions.jsonl whose reconstruct_portfolio_state(
    reactor_filter="paper") projection yields the given signed target weights.

    One paper record per symbol; target_position_pct == fill_size_pct == weight.
    """
    lines = []
    for i, (sym, wt) in enumerate(positions.items()):
        lines.append(
            json.dumps(
                {
                    "asset": sym,
                    "asset_class": "equity",
                    "timeframe": "1d",
                    "reactor_name": "paper",
                    "target_position_pct": wt,
                    "fill_size_pct": wt,
                    "asof_execution": f"2026-06-09T1{i}:00:00Z",
                    "proposal_id": f"seed_{sym}",
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_paper_book(monkeypatch, tmp_path: Path, positions: dict[str, float]) -> Path:
    """Point the cap gate's seed source at a tmp paper bus with `positions`.

    The fix seeds `_running` from reconstruct_portfolio_state(reactor_filter="paper"),
    which reads hermes_quant.portfolio.state._DEFAULT_EXECUTIONS_PATH when called with
    no path arg. Redirect that module constant at a tmp executions.jsonl so the test
    is hermetic (the skill's documented test-isolation gotcha — the real helper reads
    the live ~/.hermes/quant book otherwise). Returns the bus path.
    """
    bus = tmp_path / "executions.jsonl"
    _write_paper_bus(bus, positions)
    monkeypatch.setattr(
        "hermes_quant.portfolio.state._DEFAULT_EXECUTIONS_PATH", bus
    )
    return bus


def _poison_state_db(monkeypatch, tmp_path: Path) -> dict:
    """Install a sqlite3.connect that, if the cap gate STILL reads state.db, hands
    back the corrupt additive book that caused the phantom gross (AAPL qty=399.93).

    Records every state.db SELECT so the phantom-gross test can assert the fixed
    cap NEVER touches this table. Returns a mutable dict with a `reads` counter.
    """
    import sqlite3

    real_connect = sqlite3.connect
    tracker = {"reads": 0}

    def fake_connect(path, *a, **k):
        if "state.db" in str(path):
            tracker["reads"] += 1
            con = real_connect(":memory:")
            con.execute(
                "CREATE TABLE positions (account_id TEXT, symbol TEXT, quantity REAL)"
            )
            # The 2026-06-08 corruption: AAPL accumulated as a raw SHARE count.
            con.executemany(
                "INSERT INTO positions VALUES (?, ?, ?)",
                [
                    ("paper-default", "AAPL", 399.93314914080986),
                    ("paper-default", "BA", -0.8),
                ],
            )
            con.commit()
            return con
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    return tracker


# ---------------------------------------------------------------------------
# P1-A: size_override_pct pass-through
# ---------------------------------------------------------------------------


def test_clipped_size_passed_as_size_override(monkeypatch, tmp_path):
    """When the cap scales a pick down, quant_approve must receive the clipped
    size via size_override_pct — otherwise it re-derives the uncapped Kelly size
    and the clip is cosmetic on routes that skip the home-grown cap."""
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)
    _seed_paper_book(monkeypatch, tmp_path, {})  # empty paper book → init succeeds

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


# ---------------------------------------------------------------------------
# P0-C: phantom-gross seed source (2026-06-10)
# ---------------------------------------------------------------------------


def test_cap_seeds_from_paper_book_not_corrupt_state_db(monkeypatch, tmp_path):
    """REGRESSION: the cap must seed from the paper-book projection, NOT state.db.

    Reproduces the 2026-06-10 incident: state.db.positions holds a corrupt
    additive share-count row (AAPL qty=399.93), while the real paper book is a
    single BA short at 20% gross. The OLD code seeded `_running` from
    state.db.quantity, read 399.93 as a 39993% target weight → phantom gross
    ~402% → silenced EVERY actionable as portfolio_cap_silenced. The fix seeds
    from reconstruct_portfolio_state(reactor_filter="paper") (20% gross, 80%
    headroom), so a fresh 20% pick has ample room and FIRES.

    We use the REAL clip primitive (not a stub) so the assertion exercises the
    actual headroom math against the actual seeded book — the only thing that
    differs between pass and fail is which book the seed reads.
    """
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)

    # Real paper book: BA short 20% gross, 80% headroom.
    _seed_paper_book(monkeypatch, tmp_path, {"BA": -0.20})
    # Corrupt state.db is present and would poison the seed IF the cap read it.
    db_tracker = _poison_state_db(monkeypatch, tmp_path)

    # A fresh 20% short into an 80%-headroom book: admissible under the 200%
    # gross / 100% net / 20% cash standard caps.
    out = mod.auto_approve_actionables([_actionable(symbol="CRSP", kelly=-0.20)])

    # 1. The pick FIRED — was NOT silenced on the phantom gross.
    assert out[0].get("auto_approved") is True, (
        "phantom-gross regression: actionable was silenced — "
        f"err={out[0].get('auto_approve_error')!r}; the cap likely seeded from "
        "the corrupt state.db (402% gross) instead of the 20% paper book"
    )
    assert "portfolio_cap_silenced" not in (out[0].get("auto_approve_error") or "")
    assert len(calls) == 1, "the admitted pick should have called quant_approve once"

    # 2. The corrupt state.db must NEVER have been consulted for seeding.
    assert db_tracker["reads"] == 0, (
        "the fixed cap read state.db.positions — it must seed ONLY from the "
        "canonical paper-book projection (reconstruct_portfolio_state)"
    )


def test_phantom_gross_book_still_silences_a_real_breach(monkeypatch, tmp_path):
    """Counterpart: the fix did NOT remove the cap — a genuinely full paper book
    (read from executions.jsonl) still silences a new fire.

    Seeds a real 200%-gross paper book (two 100% legs) via the paper projection;
    a new 20% pick has no headroom under the 20% cash floor and must be silenced.
    This proves the seed swap preserved the cap's protective behavior — it only
    changed the (now-correct) source the book is read from.
    """
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)
    # Real 200% gross book (cash fully consumed) — from the paper projection.
    _seed_paper_book(monkeypatch, tmp_path, {"AAA": 1.0, "BBB": 1.0})

    out = mod.auto_approve_actionables([_actionable(symbol="CCC", kelly=0.20)])

    assert out[0].get("auto_approved") is not True, (
        "a new fire into a real 200%-gross book must be silenced by the cap"
    )
    assert "portfolio_cap_silenced" in (out[0].get("auto_approve_error") or "")
    assert len(calls) == 0, "a cap-silenced pick must NOT call quant_approve"


def test_missing_bus_fails_closed_not_empty_book(monkeypatch, tmp_path):
    """REGRESSION (Codex P1, 2026-06-10): a MISSING/unreadable execution bus must
    FAIL CLOSED, not be silently accepted as an empty (100% cash) book.

    reconstruct_portfolio_state is fail-soft — a nonexistent executions.jsonl
    returns an empty PortfolioState rather than raising. If the cap seed accepted
    that, an operator with real paper exposure but a bad/mis-mounted bus would
    size against a fabricated flat book and auto-fire UNCAPPED — the exact
    fail-OPEN posture PR #82 closed. The seed now probes the bus explicitly and
    raises on missing/unreadable, so the fail-closed guard blocks every fire.
    """
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)

    # Point the seed at a bus path that does NOT exist.
    missing = tmp_path / "nonexistent" / "executions.jsonl"
    monkeypatch.setattr(
        "hermes_quant.portfolio.state._DEFAULT_EXECUTIONS_PATH", missing
    )

    out = mod.auto_approve_actionables([_actionable(symbol="NVDA", kelly=0.20)])

    assert len(calls) == 0, "a missing bus must BLOCK all fires (fail closed)"
    assert out[0].get("auto_approved") is not True
    assert "cap_gate_init_failed" in (out[0].get("auto_approve_error") or "")
    assert "BLOCKED" in (out[0].get("auto_approve_error") or "")


def test_empty_but_present_bus_fires_flat_book(monkeypatch, tmp_path):
    """Counterpart to the missing-bus test: a bus that EXISTS and reads but parses
    to an empty book (a system that has genuinely never recorded a paper fill) is
    a legitimately flat book — the cap accepts it and a fresh pick FIRES.

    This is the boundary that distinguishes 'headroom unknown' (missing/unreadable
    → block) from 'headroom = 100% free' (present + empty → fire).
    """
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)
    _seed_paper_book(monkeypatch, tmp_path, {})  # writes an existing, empty bus

    out = mod.auto_approve_actionables([_actionable(symbol="NVDA", kelly=0.20)])

    assert out[0].get("auto_approved") is True, (
        "a present-but-empty bus is a flat book — the pick should fire; "
        f"err={out[0].get('auto_approve_error')!r}"
    )
    assert len(calls) == 1
