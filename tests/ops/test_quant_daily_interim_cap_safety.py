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
    _stub_live_book(monkeypatch, {})


def _stub_live_book(monkeypatch, seed: dict[str, float]):
    """Seed the cap gate's live-book read with ``seed`` (symbol -> signed qty).

    Same mechanism as :func:`_stub_live_book_empty` but pre-populates the
    in-memory positions table on ``account_id='paper-default'`` so the running
    cap state starts account-aware (a prior-day held book), which is the
    precondition for the additive-vs-REPLACE divergence."""
    import sqlite3

    real_connect = sqlite3.connect

    def fake_connect(path, *a, **k):
        if "state.db" in str(path):
            con = real_connect(":memory:")
            con.execute(
                "CREATE TABLE positions (account_id TEXT, symbol TEXT, quantity REAL)"
            )
            for sym, qty in seed.items():
                con.execute(
                    "INSERT INTO positions (account_id, symbol, quantity) "
                    "VALUES ('paper-default', ?, ?)",
                    (sym, float(qty)),
                )
            con.commit()
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


# ---------------------------------------------------------------------------
# P1-C: running-state update must REPLACE, not ADD, to match reactor semantics
# ---------------------------------------------------------------------------


def _signed_target(symbol, target_pct, pid):
    """Actionable carrying an explicit signed target weight (resolve_target_weight
    reads ``target_position_pct`` first, so this pins the per-symbol target the
    cap sees — no kelly arithmetic in the way)."""
    return {
        "symbol": symbol,
        "asset": symbol,
        "proposal_id": pid,
        "target_position_pct": target_pct,
    }


def test_held_flip_running_state_is_replace_not_additive(monkeypatch):
    """A prior-day HELD position flipped direction in-run must update the running
    cap state with REPLACE (latest-target) semantics, NOT additively.

    The reactor (portfolio/state.py + pdr_core CorePortfolioSnapshot) records each
    fire as the LATEST target for that symbol — ``positions[key] = latest`` — NOT a
    delta sum. The running cap state in quant-daily-interim.py must mirror that, or
    a flip/reduce of an already-present symbol phantom-LOWERS the running gross and
    fails the portfolio cap OPEN for the NEXT pick (the exact 2026-06-02 advisor
    leverage-runaway class this gate exists to prevent).

    Concrete RED scenario (real clip, ``PortfolioCaps.standard()`` = 200% gross /
    100% net / 20% cash):

      * seed (prior-day held): AAPL = +0.50  → gross 0.50, cash 0.50
      * pick-1: AAPL FLIP to -0.50 → clip admits -0.30 (cash headroom 0.30 binds).
        The flip is a NEW (symbol, sign) key → not deduped by open_guard.
        - ADDITIVE (buggy): running AAPL = 0.50 + (-0.30) = +0.20  (phantom gross 0.20)
        - REPLACE  (fixed): running AAPL = -0.30                   (true gross 0.30)
      * pick-2: MSFT +0.80 (new symbol)
        - against phantom +0.20 book → cash headroom 0.60 → fires MSFT +0.60
        - against true   -0.30 book → cash headroom 0.50 → fires MSFT +0.50

    The reactor applies REPLACE on every fire, so the TRUE resulting book is:
        AAPL = -0.30 (from pick-1), MSFT = <pick-2 fired size>
    Buggy: gross 0.30+0.60 = 0.90, cash 0.10 < 0.20 min_cash → OVER CAP.
    Fixed: gross 0.30+0.50 = 0.80, cash 0.20 = exactly the reserve → admissible.

    We assert on the SIZE-OVERRIDES the gate actually fired (what the reactor will
    REPLACE-record), reconstruct the true book, and require it to respect the cap.
    """
    monkeypatch.setenv("HERMES_QUANT_AUTONOMY", "paper")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    mod = _load_brief()

    calls: list[dict] = []
    _stub_quant_approve(monkeypatch, calls)
    # Prior-day held long AAPL seeded from the live book.
    _stub_live_book(monkeypatch, {"AAPL": 0.50})

    # NOTE: real clip — NOT stubbed — so this exercises the genuine headroom math.
    out = mod.auto_approve_actionables(
        [
            _signed_target("AAPL", -0.50, "p_flip"),   # flip the held long short
            _signed_target("MSFT", 0.80, "p_new"),     # then a fresh conviction
        ]
    )

    # Both picks fire (the flip clips to -0.30, MSFT clips down); collect the
    # cap-admitted sizes the reactor will REPLACE-record per symbol.
    fired_targets: dict[str, float] = {}
    for v, call in zip(out, calls, strict=True):
        assert v.get("auto_approved") is True, v.get("auto_approve_error")
        assert "size_override_pct" in call
        fired_targets[v["symbol"]] = call["size_override_pct"]

    assert fired_targets["AAPL"] == pytest.approx(-0.30), (
        "pick-1 should clip the AAPL flip to -0.30 on cash headroom"
    )

    # Reconstruct the TRUE reactor book (REPLACE per symbol) and require the cap.
    true_gross = sum(abs(t) for t in fired_targets.values())
    true_cash = 1.0 - true_gross
    min_cash_reserve = 0.20
    assert true_cash >= min_cash_reserve - 1e-9, (
        f"running-cap additive bug let the book breach min_cash_reserve: "
        f"fired={fired_targets} gross={true_gross:.3f} cash={true_cash:.3f} "
        f"< {min_cash_reserve}. MSFT must clip to +0.50 (cash 0.20), not +0.60 "
        f"(cash 0.10) — the additive update phantom-lowered running gross."
    )
    # Pin the corrected MSFT size so a future regression to additive is loud.
    assert fired_targets["MSFT"] == pytest.approx(0.50), (
        "with REPLACE running state MSFT must clip to +0.50, not the phantom +0.60"
    )
