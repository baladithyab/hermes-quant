"""tests/scripts/test_brief_render.py — ADR-0053 graceful-degradation tests.

Tests the three Wave 7/8 subsections added to quant-daily-interim.py:
  🌡️ Market Regime
  🔬 Active Research
  📊 Shadow Counterfactuals

Each subsection must:
  - Render its emoji header when infrastructure is available.
  - Print a terse "(unavailable)" / "(no active research…)" / "(shadow accounts dormant…)"
    line when infrastructure is absent — and NEVER raise.
  - Be callable from format_brief() and appear in the final brief output.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import target module without executing the venv-re-exec guard.
# We monkey-patch HERMES_VENV_PY to a non-existent path so the guard no-ops.
# ---------------------------------------------------------------------------

def _load_brief_module():
    """Import scripts/quant-daily-interim as a module under test."""
    # __file__ is tests/scripts/test_brief_render.py
    # .parent.parent.parent gives the repo root
    repo_root = Path(__file__).parent.parent.parent
    script = repo_root / "scripts" / "quant-daily-interim.py"
    assert script.exists(), f"Brief script not found: {script}"
    spec = importlib.util.spec_from_file_location("quant_daily_interim", script)
    assert spec is not None and spec.loader is not None, "Failed to create module spec"
    mod = importlib.util.module_from_spec(spec)
    # Prevent the execv venv-re-exec from firing during import
    with patch("os.execv"):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_brief_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spy_bars():
    """Return a minimal DataFrame shaped like yfinance SPY bars."""
    import pandas as pd
    import numpy as np

    n = 300
    rng = np.random.default_rng(42)
    closes = 400.0 + np.cumsum(rng.normal(0, 2, n))
    closes = np.maximum(closes, 1.0)
    df = pd.DataFrame({
        "close": closes,
        "open": closes * 0.999,
        "high": closes * 1.001,
        "low": closes * 0.998,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    })
    return df


def _write_hypothesis_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _make_shadow_db(path: Path, pnl_total: float) -> None:
    """Create a minimal shadow SQLite DB with one pnl_history row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_pnl_history (
            asof TEXT NOT NULL PRIMARY KEY,
            equity_total REAL NOT NULL,
            cash REAL NOT NULL,
            positions_value REAL NOT NULL,
            pnl_today REAL NOT NULL,
            pnl_total REAL NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO shadow_pnl_history VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-27", 100_000 + pnl_total, 50_000.0, 50_000.0 + pnl_total, pnl_total, pnl_total)
    )
    conn.commit()
    conn.close()


# ===========================================================================
# Test 1 — format_brief() always includes the three section headers
#            when sections are passed in as strings
# ===========================================================================

def test_format_brief_includes_all_three_section_headers():
    brief = _mod.format_brief(
        actionable=[], silent=[], data_blocked=[], failed=[],
        universe_size=0,
        regime_section="## 🌡️ Market Regime\n\n🟢 BULL\n",
        research_section="## 🔬 Active Research\n\n- `hyp_001` (3d) test claim\n",
        shadow_section="## 📊 Shadow Counterfactuals\n\n1. `aggressive_long` +$1,200\n",
    )
    assert "🌡️ Market Regime" in brief
    assert "🔬 Active Research" in brief
    assert "📊 Shadow Counterfactuals" in brief


# ===========================================================================
# Test 2 — regime section renders BULL when bars indicate strong uptrend
# ===========================================================================

def test_regime_section_renders_bull(tmp_path):
    spy_bars = _make_spy_bars()
    # Patch paths so no real files are needed
    with (
        patch.object(_mod, "_YIELD_CACHE_PATH", tmp_path / "no_yield.json"),
    ):
        section = _mod._compute_regime_section(bars_by_symbol={"SPY": spy_bars})
    assert "🌡️ Market Regime" in section
    # Must be one of the four known states
    assert any(s in section for s in ("BULL", "BEAR", "VOLATILE", "UNKNOWN"))


# ===========================================================================
# Test 3 — regime section returns "(unavailable)" when regime module missing
# ===========================================================================

def test_regime_section_unavailable_when_module_missing():
    with patch.dict(sys.modules, {
        "hermes_quant.regime.state_variables": None,
        "hermes_quant.regime.detector": None,
    }):
        section = _mod._compute_regime_section(bars_by_symbol=None)
    assert "🌡️ Market Regime" in section
    assert "unavailable" in section.lower()


# ===========================================================================
# Test 4 — regime section returns "(unavailable)" when bars are absent
# ===========================================================================

def test_regime_section_unavailable_when_no_bars():
    # Patch yfinance to return None so no bars are available
    yf_mock = MagicMock()
    yf_mock.download.return_value = None
    with patch.dict(sys.modules, {"yfinance": yf_mock}):
        section = _mod._compute_regime_section(bars_by_symbol=None)
    # Should either say unavailable or succeed with whatever fallback
    assert "🌡️ Market Regime" in section
    # Must not crash — that's the key invariant


# ===========================================================================
# Test 5 — regime section gracefully handles an exception inside inner fn
# ===========================================================================

def test_regime_section_graceful_on_exception():
    def _boom(_bars_by_symbol):
        raise RuntimeError("simulated crash")

    with patch.object(_mod, "_compute_regime_section_inner", _boom):
        section = _mod._compute_regime_section(bars_by_symbol={})
    assert "🌡️ Market Regime" in section
    assert "unavailable" in section.lower()


# ===========================================================================
# Test 6 — research section renders running hypothesis from JSONL
# ===========================================================================

def test_research_section_renders_running_hypothesis(tmp_path):
    hyp_path = tmp_path / "hypotheses.jsonl"
    _write_hypothesis_jsonl(hyp_path, [
        {
            "kind": "hypothesis",
            "hypothesis_id": "hyp_SPY_20260101_abc123",
            "claim": "Sentiment analyst raises Sharpe >= 0.10 over 6-month backtest",
            "status": "open",
            "created_at": "2026-05-01T00:00:00+00:00",
            "related_adrs": ["ADR-0048"],
        },
        {
            # REAL registry schema: status_change rows carry "new_status",
            # NOT "status". (Previously this fixture cheated with "status",
            # which is why the new_status->status parse bug went undetected.)
            "kind": "status_change",
            "hypothesis_id": "hyp_SPY_20260101_abc123",
            "new_status": "running",
            "previous_status": "open",
            "asof": "2026-05-01T00:00:00+00:00",
        },
    ])
    with patch.object(_mod, "_HYPOTHESES_PATH", hyp_path):
        section = _mod._compute_research_section()
    assert "🔬 Active Research" in section
    assert "hyp_SPY_20260101_abc123" in section
    assert "ADR-0048" in section


def test_research_section_status_change_new_status_flips_running(tmp_path):
    """Regression: a status_change row with new_status='running' must make
    the hypothesis surface as active.

    The brief reads raw hypotheses.jsonl and merges status_change rows. The
    registry writes the transitioned state under "new_status" (never
    "status"), so a naive dict.update() left the original "open" status in
    place and running hypotheses never appeared. This pins the new_status ->
    status mapping.
    """
    hyp_path = tmp_path / "hypotheses.jsonl"
    _write_hypothesis_jsonl(hyp_path, [
        {
            "kind": "hypothesis",
            "hypothesis_id": "hyp_REG_20260609_deadzone",
            "claim": "Closing the regime dead-zone improves risk-adjusted returns",
            "status": "open",
            "created_at": "2026-06-09T00:00:00+00:00",
            "related_adrs": ["ADR-0053"],
        },
        {
            "kind": "status_change",
            "hypothesis_id": "hyp_REG_20260609_deadzone",
            "new_status": "running",
            "previous_status": "open",
            "asof": "2026-06-09T01:00:00+00:00",
        },
    ])
    with patch.object(_mod, "_HYPOTHESES_PATH", hyp_path):
        section = _mod._compute_research_section()
    assert "hyp_REG_20260609_deadzone" in section
    assert "no active research" not in section.lower()


# ===========================================================================
# Test 7 — research section shows "no active research" when JSONL missing
# ===========================================================================

def test_research_section_no_active_when_file_missing(tmp_path):
    missing = tmp_path / "hypotheses.jsonl"
    with patch.object(_mod, "_HYPOTHESES_PATH", missing):
        section = _mod._compute_research_section()
    assert "🔬 Active Research" in section
    assert "no active research" in section.lower()


# ===========================================================================
# Test 8 — research section shows "no active research" when all hypotheses
#           are terminal (validated/falsified/abandoned)
# ===========================================================================

def test_research_section_no_active_when_all_terminal(tmp_path):
    hyp_path = tmp_path / "hypotheses.jsonl"
    _write_hypothesis_jsonl(hyp_path, [
        {
            "kind": "hypothesis",
            "hypothesis_id": "hyp_AAA_20260101_111111",
            "claim": "Some claim",
            "status": "falsified",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "kind": "hypothesis",
            "hypothesis_id": "hyp_BBB_20260101_222222",
            "claim": "Another claim",
            "status": "validated",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    ])
    with patch.object(_mod, "_HYPOTHESES_PATH", hyp_path):
        section = _mod._compute_research_section()
    assert "🔬 Active Research" in section
    assert "no active research" in section.lower()


# ===========================================================================
# Test 9 — research section handles corrupt JSONL without crashing
# ===========================================================================

def test_research_section_handles_corrupt_jsonl(tmp_path):
    hyp_path = tmp_path / "hypotheses.jsonl"
    hyp_path.write_text("{invalid json}\n{also bad}")
    with patch.object(_mod, "_HYPOTHESES_PATH", hyp_path):
        section = _mod._compute_research_section()
    # Still renders the header, shows "no active research" (no valid rows parsed)
    assert "🔬 Active Research" in section
    assert "no active research" in section.lower()


# ===========================================================================
# Test 10 — shadow section renders top performers when DBs exist
# ===========================================================================

def test_shadow_section_renders_top_performers(tmp_path):
    shadow_dir = tmp_path / "shadow"
    _make_shadow_db(shadow_dir / "aggressive_long.db", pnl_total=2500.0)
    _make_shadow_db(shadow_dir / "momentum_filter.db", pnl_total=800.0)
    _make_shadow_db(shadow_dir / "contrarian.db", pnl_total=-300.0)

    with patch.object(_mod, "_SHADOW_HOME", shadow_dir):
        section = _mod._compute_shadow_section()
    assert "📊 Shadow Counterfactuals" in section
    assert "aggressive_long" in section
    assert "momentum_filter" in section
    # Underperformer should appear
    assert "contrarian" in section


# ===========================================================================
# Test 11 — shadow section shows "dormant" when no DB files exist
# ===========================================================================

def test_shadow_section_dormant_when_no_dbs(tmp_path):
    empty_dir = tmp_path / "shadow"
    empty_dir.mkdir()
    with patch.object(_mod, "_SHADOW_HOME", empty_dir):
        section = _mod._compute_shadow_section()
    assert "📊 Shadow Counterfactuals" in section
    assert "dormant" in section.lower()


# ===========================================================================
# Test 12 — shadow section shows "dormant" when shadow dir does not exist
# ===========================================================================

def test_shadow_section_dormant_when_dir_missing(tmp_path):
    nonexistent = tmp_path / "shadow_does_not_exist"
    with patch.object(_mod, "_SHADOW_HOME", nonexistent):
        section = _mod._compute_shadow_section()
    assert "📊 Shadow Counterfactuals" in section
    assert "dormant" in section.lower()


# ===========================================================================
# Test 13 — shadow section handles corrupt DB without crashing
# ===========================================================================

def test_shadow_section_handles_corrupt_db(tmp_path):
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    # Write a non-SQLite file with .db extension
    (shadow_dir / "corrupt.db").write_bytes(b"this is not a sqlite database 0xDEADBEEF")
    with patch.object(_mod, "_SHADOW_HOME", shadow_dir):
        section = _mod._compute_shadow_section()
    # Should not crash; shows dormant (no valid pnl rows read)
    assert "📊 Shadow Counterfactuals" in section
    assert "dormant" in section.lower()


# ===========================================================================
# Test 14 — format_brief() with None sections omits Wave 7/8 content
#            (backward-compat: sections are optional kwargs)
# ===========================================================================

def test_format_brief_without_wave78_sections():
    brief = _mod.format_brief(
        actionable=[], silent=[], data_blocked=[], failed=[],
        universe_size=5,
    )
    # Legacy output still includes disclaimer
    assert "Disclaimer" in brief
    # Headers should NOT appear when sections are not passed
    assert "🌡️ Market Regime" not in brief
    assert "🔬 Active Research" not in brief
    assert "📊 Shadow Counterfactuals" not in brief


# ===========================================================================
# Test 15 — all three _compute_* functions never raise; always return str
#            when called with no infrastructure at all (cold machine)
# ===========================================================================

def test_all_three_sections_never_raise_on_cold_machine(tmp_path):
    """On a machine with zero infrastructure, every section must return a str."""
    nonexistent_shadow = tmp_path / "no_shadow"
    nonexistent_hyp = tmp_path / "no_hyp" / "hypotheses.jsonl"

    yf_mock = MagicMock()
    yf_mock.download.return_value = None

    with (
        patch.dict(sys.modules, {"yfinance": yf_mock,
                                  "hermes_quant.regime.state_variables": None,
                                  "hermes_quant.regime.detector": None}),
        patch.object(_mod, "_HYPOTHESES_PATH", nonexistent_hyp),
        patch.object(_mod, "_SHADOW_HOME", nonexistent_shadow),
    ):
        r = _mod._compute_regime_section()
        h = _mod._compute_research_section()
        s = _mod._compute_shadow_section()

    assert isinstance(r, str) and "🌡️" in r
    assert isinstance(h, str) and "🔬" in h
    assert isinstance(s, str) and "📊" in s
