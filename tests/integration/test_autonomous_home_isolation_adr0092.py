"""ADR-0092 ph3 acceptance: the autonomous path honors an INJECTED home.

This is the integration test the operator's smoke-test could NOT make pass this
session: setting HERMES_QUANT_HOME did not reach the autonomous tick because
``autonomous.QUANT_HOME`` was bound to ``~/.hermes`` at import time. Phase 3
routes the constant through ``hermes_quant.home.quant_home()`` (call-time,
env-honoring), so an isolated home set BEFORE import now reaches the tick.

The test runs in a SUBPROCESS with the env var exported before the interpreter
starts (the real cron / standalone-daemon shape). A subprocess is required: an
in-process ``monkeypatch.setenv`` after this test session already imported
``autonomous`` would NOT re-bind the module global, exactly masking the bug —
the same reason the contract-purity test shells out.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "hermes_quant" / "autonomous.py").is_file():
            return parent
    raise AssertionError("repo root (containing hermes_quant/autonomous.py) not found")


def test_autonomous_home_is_isolated_via_env_before_import(tmp_path: Path) -> None:
    """HERMES_QUANT_HOME exported before import isolates EVERY autonomous home
    path — the quant root, the kill-switch, the loss-cooldown sidecar, the
    cum-pnl sidecar — to the tmp dir, never touching the live ~/.hermes/quant."""
    iso_home = tmp_path / "iso_quant_home"
    code = r"""
import os, sys
from pathlib import Path
iso = Path(os.environ["HERMES_QUANT_HOME"]).resolve()
import hermes_quant.autonomous as a

checks = {
    "QUANT_HOME": a.QUANT_HOME,
    "KILL_SWITCH_PATH": a.KILL_SWITCH_PATH,
    "_LOSS_COOLDOWN_SIDECAR_PATH": a._LOSS_COOLDOWN_SIDECAR_PATH,
    "_LAST_KNOWN_CUM_PNL_PATH": a._LAST_KNOWN_CUM_PNL_PATH,
}
live = (Path.home() / ".hermes" / "quant").resolve()
bad = []
for name, val in checks.items():
    p = Path(val).resolve()
    # every path must live UNDER the isolated home, and NONE under live ~/.hermes/quant
    if iso not in p.parents and p != iso:
        bad.append(f"{name}={p} not under iso {iso}")
    if live == p or live in p.parents:
        bad.append(f"{name}={p} LEAKED to live {live}")
if bad:
    print("FAIL:" + "; ".join(bad)); sys.exit(2)
print("ISOLATED_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_repo_root()),
        env={
            **_clean_env(),
            "HERMES_QUANT_HOME": str(iso_home),
        },
    )
    assert proc.returncode == 0, (
        "autonomous home did not isolate to the injected HERMES_QUANT_HOME.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "ISOLATED_OK" in proc.stdout


def test_autonomous_home_hermes_home_env_also_isolates(tmp_path: Path) -> None:
    """The upstream HERMES_HOME var (the one the operator's smoke-test used)
    also isolates: quant root resolves to $HERMES_HOME/quant."""
    hhome = tmp_path / "hermes_home"
    code = r"""
import os, sys
from pathlib import Path
hhome = Path(os.environ["HERMES_HOME"]).resolve()
import hermes_quant.autonomous as a
expected = hhome / "quant"
if Path(a.QUANT_HOME).resolve() != expected.resolve():
    print(f"FAIL: QUANT_HOME={a.QUANT_HOME} != {expected}"); sys.exit(2)
print("HERMES_HOME_ISOLATED_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_repo_root()),
        env={**_clean_env(), "HERMES_HOME": str(hhome)},
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "HERMES_HOME_ISOLATED_OK" in proc.stdout


def _clean_env() -> dict[str, str]:
    """A minimal env without either home override, but with PATH/PYTHONPATH so
    the subprocess can import hermes_quant from the repo (run with cwd=repo)."""
    import os

    keep = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HERMES_QUANT_HOME", "HERMES_HOME")
    }
    return keep
