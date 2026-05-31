"""tests/cron/test_deploy_audit.py — the anti-drift mechanism's own tests (meta-review M02).

The deploy-audit tool (ops/deploy/quant-deploy-audit.py) is the guard against the
repo↔deployed drift that made this whole session's fixes non-live. These tests verify the
AUDIT LOGIC is correct (so a future CI drift-check can trust it), using synthetic dirs — they
do NOT touch the real ~/.hermes/scripts/ (deterministic, no environment dependency, per
AGENTS.md testing discipline).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_AUDIT = Path(__file__).resolve().parents[2] / "ops" / "deploy" / "quant-deploy-audit.py"
_spec = importlib.util.spec_from_file_location("quant_deploy_audit", _AUDIT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_audit_classifies_all_four_drift_states(tmp_path, monkeypatch):
    """SAME / DRIFT / REPO_ONLY_NEW / DEPLOYED_ONLY are each classified correctly."""
    repo = tmp_path / "repo"
    dep = tmp_path / "deployed"
    _write(repo / "quant-same.py", "print('v1')\n")
    _write(dep / "quant-same.py", "print('v1')\n")            # SAME
    _write(repo / "quant-drift.py", "print('repo-fix')\n")
    _write(dep / "quant-drift.py", "print('live-wiring')\n")  # DRIFT (both differ)
    _write(repo / "quant-new.py", "print('new in repo')\n")   # REPO_ONLY_NEW
    _write(dep / "quant-live.py", "print('live only')\n")     # DEPLOYED_ONLY

    monkeypatch.setattr(mod, "REPO_SCRIPTS", repo)
    report = mod.audit(dep)

    verdicts = {r["script"]: r["verdict"] for r in report["files"]}
    assert verdicts["quant-same.py"] == "SAME"
    assert verdicts["quant-drift.py"] == "DRIFT"
    assert verdicts["quant-new.py"] == "REPO_ONLY_NEW"
    assert verdicts["quant-live.py"] == "DEPLOYED_ONLY"
    # The dangerous-to-clobber set is everything except SAME.
    assert set(report["drifted"]) == {"quant-drift.py", "quant-new.py", "quant-live.py"}


def test_audit_clean_when_identical(tmp_path, monkeypatch):
    """No drift → empty drifted list (the post-reconciliation steady state)."""
    repo = tmp_path / "repo"
    dep = tmp_path / "deployed"
    for d in (repo, dep):
        _write(d / "quant-a.py", "A\n")
        _write(d / "quant-b.py", "B\n")
    monkeypatch.setattr(mod, "REPO_SCRIPTS", repo)
    report = mod.audit(dep)
    assert report["drifted"] == []
    assert report["counts"].get("SAME") == 2


def test_main_exit_1_on_drift_0_when_clean(tmp_path, monkeypatch):
    """The CI contract: exit 1 when drift exists, 0 when clean."""
    repo = tmp_path / "repo"
    dep = tmp_path / "deployed"
    _write(repo / "quant-x.py", "one\n")
    _write(dep / "quant-x.py", "two\n")  # drift
    monkeypatch.setattr(mod, "REPO_SCRIPTS", repo)
    assert mod.main(["--deployed-dir", str(dep)]) == 1

    _write(dep / "quant-x.py", "one\n")  # reconciled
    assert mod.main(["--deployed-dir", str(dep)]) == 0


def test_main_no_deployed_dir_is_not_an_error(tmp_path, monkeypatch):
    """In CI there is no ~/.hermes/scripts/ — that must exit 0, not fail the build."""
    repo = tmp_path / "repo"
    _write(repo / "quant-x.py", "one\n")
    monkeypatch.setattr(mod, "REPO_SCRIPTS", repo)
    assert mod.main(["--deployed-dir", str(tmp_path / "does-not-exist")]) == 0
