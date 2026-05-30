"""Integration tests for the direction-vs-play-bias guard in the autonomous
tick ops script (ops/scripts/quant-autonomous-tick.py).

The script's run_tick() wraps advisor.recommend with a screen that neutralizes
a recommendation when its direction can't structurally route through any of the
symbol's eligible plays, and relabels the resulting audit decision to
gate=DIRECTION_BIAS_MISMATCH. These tests drive run_tick end-to-end against a
fake hermes_quant.autonomous module whose tick() faithfully invokes the screened
recommend wrapper and reports decisions — so we verify the propagation path AND
the audit relabel, without any network or real advisor.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-autonomous-tick.py"


def _load_script_module() -> types.ModuleType:
    """Load the hyphenated ops script as a module without running main()."""
    spec = importlib.util.spec_from_file_location("_qat_test", str(SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------- #
# Fake hermes_quant.autonomous shaped just enough for run_tick.
# --------------------------------------------------------------------------- #


@dataclass
class _FakeDecision:
    symbol: str
    asset_class: str
    timeframe: str
    gate: str
    details: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] | None = None
    execution_id: str | None = None
    error: str | None = None


@dataclass
class _FakeResult:
    decisions: list[_FakeDecision]
    errors: int = 0


def _make_fake_autonomous() -> types.ModuleType:
    """Build a fake `hermes_quant.autonomous` module.

    Its `tick()` mirrors the real orchestrator's relevant contract: it calls the
    injected `advisor_recommend` (the script's direction-screening wrapper) per
    entry, and if the advisor's risk_gate did NOT pass, it emits a
    SILENCE_GATED_BY_ADVISOR decision carrying the gated reason (exactly how the
    real silence-bias gate surfaces an advisor veto). Otherwise it FIREs,
    recording the signal direction.

    The wrapper itself calls the REAL `hermes_quant.advisor.recommend`, so tests
    stub that symbol (see `_stub_advisor`) to control per-symbol direction.
    The `_fired` list records every symbol that actually reached a FIRE/React.
    """
    fired: list[str] = []

    def tick(*, dry_run=True, symbols=None, advisor_recommend=None):
        assert advisor_recommend is not None, (
            "run_tick must inject its direction-screening wrapper"
        )
        decisions: list[_FakeDecision] = []
        for entry in symbols or []:
            res = advisor_recommend(
                symbol=entry.symbol,
                asset_class=entry.asset_class,
                timeframe=entry.timeframe,
                include_lessons=True,
            )
            rg = res.get("risk_gate") or {}
            sig = res.get("aggregated_signal") or {}
            if not rg.get("pass", False):
                # Advisor vetoed — surfaces as SILENCE_GATED_BY_ADVISOR with the
                # gated_reason in details (matches silence_bias_gate Step 0).
                decisions.append(
                    _FakeDecision(
                        symbol=entry.symbol,
                        asset_class=entry.asset_class,
                        timeframe=entry.timeframe,
                        gate="SILENCE_GATED_BY_ADVISOR",
                        details={"gated_reason": rg.get("gated_reason", "unknown")},
                    )
                )
                continue
            # FIRE path
            if not dry_run:
                fired.append(entry.symbol)
            decisions.append(
                _FakeDecision(
                    symbol=entry.symbol,
                    asset_class=entry.asset_class,
                    timeframe=entry.timeframe,
                    gate="FIRE",
                    action={
                        "target_position_pct": (
                            0.2 if int(sig.get("direction", 0)) > 0 else -0.2
                        ),
                        "direction": int(sig.get("direction", 0)),
                    },
                    execution_id=("exec-" + entry.symbol) if not dry_run else None,
                )
            )
        return _FakeResult(decisions=decisions)

    mod = types.ModuleType("hermes_quant.autonomous")
    mod.tick = tick  # type: ignore[attr-defined]
    mod._read_pdr_mode = lambda: "autonomous"  # type: ignore[attr-defined]
    mod._fired = fired  # test-only handle  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------- #
# Fixtures: redirect the script's file paths + inject the fake watchlist.
# --------------------------------------------------------------------------- #


@pytest.fixture
def script(tmp_path, monkeypatch):
    m = _load_script_module()
    # Redirect all on-disk state into tmp_path so we never touch ~/.hermes.
    monkeypatch.setattr(m, "AUDIT_LOG_PATH", tmp_path / "autonomous-tick.jsonl")
    monkeypatch.setattr(m, "HALT_MIRROR_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(m, "WATCHLIST_PATH", tmp_path / "play-fit.json")
    # The direction-vs-play-bias screen ships DEFAULT-OFF behind a flag (matches
    # the HERMES_QUANT_PORTFOLIO_CAPS pattern). These scenario tests exercise the
    # ON path; the dedicated OFF-path test below sets it to "0" explicitly.
    monkeypatch.setenv("HERMES_QUANT_DIRECTION_BIAS_GATE", "1")
    # No halts by default.
    return m


def _install_watchlist(script_mod, monkeypatch, rows):
    """rows: list of (symbol, [plays]) → patch load_active_watchlist."""
    wl = [(sym, "equity", "1d", sorted(plays)) for sym, plays in rows]
    monkeypatch.setattr(script_mod, "load_active_watchlist", lambda: wl)


def _install_fake_autonomous(monkeypatch, fake_mod):
    """Make `import hermes_quant.autonomous as auto` resolve to the fake.

    We patch BOTH the sys.modules entry AND the attribute on the parent
    `hermes_quant` package. The latter matters when the real module was already
    imported by an earlier test in the suite: `import hermes_quant.autonomous`
    binds via the parent-package attribute, which sys.modules-setitem alone does
    not override.
    """
    import hermes_quant

    monkeypatch.setitem(sys.modules, "hermes_quant.autonomous", fake_mod)
    monkeypatch.setattr(hermes_quant, "autonomous", fake_mod, raising=False)


def _stub_advisor(monkeypatch, directions: dict[str, int]):
    """Patch hermes_quant.advisor.recommend so the script's screening wrapper
    (which calls the real advisor) sees the desired per-symbol direction.

    `directions` maps symbol -> int direction the stub advisor "wants".
    risk_gate.pass is True so any unscreened signal would fire — the screen is
    the only thing that can veto it.
    """
    import hermes_quant.advisor as advisor_mod

    def _recommend(**kwargs: Any) -> dict[str, Any]:
        sym = kwargs["symbol"]
        direction = directions.get(sym, 0)
        return {
            "as_of": "2026-05-30T00:00:00Z",
            "aggregated_signal": {"direction": direction, "confidence": 0.9},
            "risk_gate": {"pass": True, "gated_reason": None, "kelly_fraction": 0.2},
            "analyst_views": [{"a": 1}, {"b": 2}],
            "lessons": [],
        }

    monkeypatch.setattr(advisor_mod, "recommend", _recommend)


def _read_decisions(audit_path: Path) -> list[dict]:
    out = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("event") == "decision":
            out.append(row)
    return out


# --------------------------------------------------------------------------- #
# The four required scenarios, end-to-end through run_tick.
# --------------------------------------------------------------------------- #


def test_short_through_csp_yields_direction_bias_mismatch_and_no_fire(script, monkeypatch):
    """SHORT-through-CSP → DIRECTION_BIAS_MISMATCH; React never fires (armed)."""
    _install_watchlist(script, monkeypatch, [("AXP", ["csp"])])
    _stub_advisor(monkeypatch, {"AXP": -1})  # advisor wants SHORT
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=True)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    axp = [d for d in decisions if d["symbol"] == "AXP"]
    assert len(axp) == 1
    assert axp[0]["gate"] == "DIRECTION_BIAS_MISMATCH"
    assert "action" not in axp[0]  # no order action propagated
    assert summary["placed"] == 0
    assert summary["direction_bias_mismatch"] == 1
    assert summary["abstained"] == 1
    assert fake._fired == []  # the React was genuinely prevented


def test_flag_off_is_a_noop_short_through_csp_fires(script, monkeypatch):
    """DEFAULT-OFF: with HERMES_QUANT_DIRECTION_BIAS_GATE=0 the screen is a no-op,
    so the SHORT-through-CSP signal fires exactly as it did before A5 (the
    reversible-rollout guarantee — the gate adds NO behavior when unset)."""
    monkeypatch.setenv("HERMES_QUANT_DIRECTION_BIAS_GATE", "0")
    _install_watchlist(script, monkeypatch, [("AXP", ["csp"])])
    _stub_advisor(monkeypatch, {"AXP": -1})  # advisor wants SHORT
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=True)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    axp = [d for d in decisions if d["symbol"] == "AXP"]
    assert len(axp) == 1
    # Flag OFF → no screening → the (incoherent) SHORT-through-CSP signal fires.
    assert axp[0]["gate"] == "FIRE"
    assert summary["direction_bias_mismatch"] == 0
    assert fake._fired == ["AXP"]


def test_short_through_swing_fires(script, monkeypatch):
    """SHORT-through-swing → allowed (swing is agnostic)."""
    _install_watchlist(script, monkeypatch, [("TSLA", ["swing"])])
    _stub_advisor(monkeypatch, {"TSLA": -1})
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=True)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    tsla = [d for d in decisions if d["symbol"] == "TSLA"]
    assert len(tsla) == 1
    assert tsla[0]["gate"] == "FIRE"
    assert tsla[0]["action"]["direction"] == -1
    assert summary["placed"] == 1
    assert summary["direction_bias_mismatch"] == 0
    assert fake._fired == ["TSLA"]


def test_long_through_csp_fires(script, monkeypatch):
    """LONG-through-CSP → allowed (csp is bullish-bias)."""
    _install_watchlist(script, monkeypatch, [("KO", ["csp"])])
    _stub_advisor(monkeypatch, {"KO": 1})
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=True)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    ko = [d for d in decisions if d["symbol"] == "KO"]
    assert len(ko) == 1
    assert ko[0]["gate"] == "FIRE"
    assert ko[0]["action"]["direction"] == 1
    assert summary["placed"] == 1
    assert summary["direction_bias_mismatch"] == 0
    assert fake._fired == ["KO"]


def test_unknown_play_never_fires(script, monkeypatch):
    """A symbol eligible only for an unknown play → never fires, regardless of
    direction (silence-by-default)."""
    _install_watchlist(script, monkeypatch, [("ZZZ", ["mystery_play"])])
    _stub_advisor(monkeypatch, {"ZZZ": 1})  # LONG, but play is unknown
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=True)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    zzz = [d for d in decisions if d["symbol"] == "ZZZ"]
    assert len(zzz) == 1
    assert zzz[0]["gate"] == "DIRECTION_BIAS_MISMATCH"
    assert summary["placed"] == 0
    assert summary["direction_bias_mismatch"] == 1
    assert fake._fired == []


def test_dry_run_short_through_csp_is_still_mismatch_not_dry_run_fire(script, monkeypatch):
    """Even in dry-run, a SHORT-through-CSP must read DIRECTION_BIAS_MISMATCH —
    never DRY_RUN_FIRE — so the operator sees the real reason."""
    _install_watchlist(script, monkeypatch, [("AXP", ["csp"])])
    _stub_advisor(monkeypatch, {"AXP": -1})
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=False)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    axp = [d for d in decisions if d["symbol"] == "AXP"]
    assert axp[0]["gate"] == "DIRECTION_BIAS_MISMATCH"
    assert summary["placed"] == 0
    assert summary["direction_bias_mismatch"] == 1


def test_short_routes_through_swing_when_eligible_for_csp_and_swing(script, monkeypatch):
    """A symbol eligible for BOTH csp (bullish) and swing (agnostic) may take a
    SHORT — at least one eligible play (swing) admits it, so it fires."""
    _install_watchlist(script, monkeypatch, [("NVDA", ["csp", "swing"])])
    _stub_advisor(monkeypatch, {"NVDA": -1})
    fake = _make_fake_autonomous()
    _install_fake_autonomous(monkeypatch, fake)

    summary = script.run_tick(armed=True)

    decisions = _read_decisions(script.AUDIT_LOG_PATH)
    nvda = [d for d in decisions if d["symbol"] == "NVDA"]
    assert nvda[0]["gate"] == "FIRE"
    assert summary["placed"] == 1
    assert summary["direction_bias_mismatch"] == 0
    assert fake._fired == ["NVDA"]
