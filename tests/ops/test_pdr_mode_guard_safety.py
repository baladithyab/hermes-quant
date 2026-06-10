"""tests/ops/test_pdr_mode_guard_safety.py — the self-heal must NOT override a
deliberate operator downgrade (Codex P1, PR #83).

The guard exists for the CLOBBER case (the `quant:` block dropped/emptied so mode
goes missing). It must re-arm `autonomous` ONLY then. A valid explicit `hitl`/
`advise` is an intentional safety pause and must be respected — re-arming would
silently re-enable paper firing within 15 minutes, defeating an operator pause.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_GUARD_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-pdr-mode-guard.py"
)


def _run_guard(home: Path) -> subprocess.CompletedProcess:
    """Run the guard as a subprocess with HOME pointed at a temp dir (the guard
    reads ~/.hermes/config.yaml via Path.home())."""
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    # Carry through the venv so `import yaml` resolves.
    import os
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(_GUARD_PATH)],
        capture_output=True, text=True, env={**os.environ, **env},
    )


def _write_config(home: Path, mode_block: str) -> Path:
    cfgdir = home / ".hermes"
    cfgdir.mkdir(parents=True, exist_ok=True)
    cfg = cfgdir / "config.yaml"
    cfg.write_text(mode_block, encoding="utf-8")
    return cfg


def _read_mode(home: Path) -> str | None:
    cfg = yaml.safe_load((home / ".hermes" / "config.yaml").read_text()) or {}
    return (cfg.get("quant") or {}).get("pdr", {}).get("mode")


def test_respects_hitl_downgrade(tmp_path):
    """Operator set hitl → guard must LEAVE IT (silent, no re-arm)."""
    _write_config(tmp_path, "quant:\n  pdr:\n    mode: hitl\n  other_key: keep_me\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 0
    assert res.stdout.strip() == "", "must be silent on intentional downgrade"
    assert _read_mode(tmp_path) == "hitl", "must NOT re-arm autonomous over a hitl pause"


def test_respects_advise_downgrade(tmp_path):
    _write_config(tmp_path, "quant:\n  pdr:\n    mode: advise\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 0
    assert res.stdout.strip() == ""
    assert _read_mode(tmp_path) == "advise"


def test_rearms_when_mode_missing(tmp_path):
    """The CLOBBER case: quant block present but pdr.mode absent → re-arm."""
    _write_config(tmp_path, "quant:\n  pdr: {}\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 0
    assert "re-asserted" in res.stdout.lower()
    assert _read_mode(tmp_path) == "autonomous"


def test_rearms_when_quant_block_dropped(tmp_path):
    """quant block entirely gone → re-arm (this is the documented failure mode)."""
    _write_config(tmp_path, "some_other_top_level: 1\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 0
    assert _read_mode(tmp_path) == "autonomous"


def test_rearms_on_invalid_mode(tmp_path):
    """Garbage value (not a valid mode) is treated as drift → re-arm."""
    _write_config(tmp_path, "quant:\n  pdr:\n    mode: bogus_value\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 0
    assert _read_mode(tmp_path) == "autonomous"


def test_silent_when_already_autonomous(tmp_path):
    _write_config(tmp_path, "quant:\n  pdr:\n    mode: autonomous\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 0
    assert res.stdout.strip() == ""
    assert _read_mode(tmp_path) == "autonomous"
