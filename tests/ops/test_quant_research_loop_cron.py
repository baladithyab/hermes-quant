"""tests/ops/test_quant_research_loop_cron.py — W6 cron wrapper smoke tests.

The cron (quant-research-loop.py) is a thin operator-facing wrapper around
ResearchLoop. These smoke tests cover:

  - off-state silence: flag unset → exit 0, empty stdout, no real I/O.
  - dry-run makes ZERO real LLM calls (the default strategy_factory is None →
    ResearchLoop's deterministic StubLLMCommittee-backed default).
  - --json emits a single parseable JSON line with the summary keys.
  - the no_agent change-detecting _is_transition gate.
  - load_universe precedence.

The heavy per-candidate path (real ~/.hermes writes) is exercised by
tests/research/test_research_loop.py with tmp_path; here we keep the cron
hermetic by monkeypatching run_loop where a full cycle would otherwise touch
the real home dir.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_cron_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-research-loop.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_research_loop", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # neutralize the script's execv guard
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod


@pytest.fixture(scope="module")
def cron():
    return _load_cron_module()


# ---------------------------------------------------------------------------
# Off-state: flag unset → exit 0, empty stdout, no real cycle.
# ---------------------------------------------------------------------------


def test_cron_off_state_silent(cron, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_LOOP", raising=False)
    monkeypatch.setattr(sys, "argv", ["quant-research-loop.py"])
    rc = cron.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""  # silence-by-default


def test_cron_run_loop_off_state_does_no_io(cron, monkeypatch):
    """run_loop with the flag OFF returns immediately — no halt read, no
    registry construction, no writes (byte-identical off-state)."""
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_LOOP", raising=False)

    # If run_loop tried to touch the real home dir, this would fail; the
    # off-state path must not reach HypothesisRegistry()/RunCardLog().
    def _boom(*a, **k):
        raise AssertionError("off-state must not read halts")

    monkeypatch.setattr(cron, "read_active_halts", _boom)
    summary = cron.run_loop(armed=False, universe=["AAPL"], max_candidates=8)
    assert summary["flag_on"] is False
    assert summary["candidates_run"] == 0
    assert summary["halt_aborted"] is False


# ---------------------------------------------------------------------------
# Dry-run: default strategy_factory is None → ResearchLoop StubLLMCommittee
# default (zero real LLM calls). We assert the cron passes None (not a real
# LLM strategy) on the dry-run path.
# ---------------------------------------------------------------------------


def test_cron_dry_run_no_real_llm(cron, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    captured = {}

    class _FakeLoop:
        def __init__(self, *, registry, runner, strategy_factory=None, **kw):
            captured["strategy_factory"] = strategy_factory

        def run_cycle(self, *, universe, dry_run, max_candidates, halts):
            captured["dry_run"] = dry_run

            class _S:
                flag_on = True
                halt_aborted = False
                candidates_seen = 0
                candidates_run = 0
                validated = 0
                falsified = 0
                inconclusive = 0
                contaminated = 0
                promotion_records = 0
                promotions_recommended = 0
                errors = 0

            return _S()

    # Patch the symbols run_loop imports lazily inside its body.
    import hermes_quant.research.research_loop as rl_mod

    monkeypatch.setattr(rl_mod, "ResearchLoop", _FakeLoop)
    monkeypatch.setattr(cron, "read_active_halts", lambda: [])

    summary = cron.run_loop(armed=False, universe=["AAPL"], max_candidates=4)
    # Dry-run → strategy_factory is None → ResearchLoop default (Stub, LLM-free).
    assert captured["strategy_factory"] is None
    assert captured["dry_run"] is True
    assert summary["flag_on"] is True


# ---------------------------------------------------------------------------
# --json emits a single parseable JSON line with the summary keys.
# ---------------------------------------------------------------------------


def test_cron_json_summary_shape(cron, monkeypatch, capsys):
    canned = {
        "flag_on": True,
        "halt_aborted": False,
        "candidates_seen": 2,
        "candidates_run": 2,
        "validated": 1,
        "falsified": 1,
        "inconclusive": 0,
        "contaminated": 0,
        "promotion_records": 1,
        "promotions_recommended": 1,
        "errors": 0,
        "auto_promoted_to_live": False,
    }
    monkeypatch.setattr(cron, "run_loop", lambda **kw: dict(canned))
    monkeypatch.setattr(sys, "argv", ["quant-research-loop.py", "--json"])
    rc = cron.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert len(out) == 1
    parsed = json.loads(out[0])
    for key in (
        "flag_on",
        "candidates_run",
        "validated",
        "falsified",
        "promotion_records",
        "promotions_recommended",
        "auto_promoted_to_live",
    ):
        assert key in parsed
    # The cron NEVER auto-promotes — the summary asserts it.
    assert parsed["auto_promoted_to_live"] is False


# ---------------------------------------------------------------------------
# no_agent change-detecting transition gate.
# ---------------------------------------------------------------------------


def test_is_transition_silent_when_nothing_happened(cron):
    base = {"flag_on": True, "halt_aborted": False, "candidates_run": 0,
            "promotions_recommended": 0, "contaminated": 0, "errors": 0}
    assert cron._is_transition(base) is False


def test_is_transition_silent_when_flag_off(cron):
    assert cron._is_transition({"flag_on": False, "candidates_run": 5}) is False


@pytest.mark.parametrize(
    "field",
    ["candidates_run", "promotions_recommended", "contaminated", "errors"],
)
def test_is_transition_fires_on_activity(cron, field):
    summ = {"flag_on": True, "halt_aborted": False, "candidates_run": 0,
            "promotions_recommended": 0, "contaminated": 0, "errors": 0}
    summ[field] = 1
    assert cron._is_transition(summ) is True


def test_is_transition_fires_on_halt(cron):
    assert cron._is_transition({"flag_on": True, "halt_aborted": True}) is True


# ---------------------------------------------------------------------------
# load_universe precedence.
# ---------------------------------------------------------------------------


def test_load_universe_explicit_arg(cron):
    assert cron.load_universe("aapl, msft ,nvda") == ["AAPL", "MSFT", "NVDA"]


def test_load_universe_default_sleeve(cron, monkeypatch, tmp_path):
    # Point the watchlist at a non-existent path → fall to the default sleeve.
    monkeypatch.setattr(cron, "WATCHLIST_PATH", tmp_path / "nope.json")
    assert cron.load_universe(None) == cron._DEFAULT_SLEEVE
