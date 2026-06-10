"""tests/ops/test_deploy_audit_exclusions.py — repo-only tooling is excluded from drift.

The drift audit globs quant-*.py in ops/scripts/. Three of those are repo-only
tooling (doc generators + the watchdog itself) that must NEVER be deployed. They
were showing as perpetual REPO_ONLY_NEW false positives — the cry-wolf failure
that trains an operator to ignore the drift alert. This pins the exclusion so a
future script add can't silently reintroduce the noise.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_AUDIT_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "deploy" / "quant-deploy-audit.py"
)


def _load_audit():
    spec = importlib.util.spec_from_file_location("quant_deploy_audit_x", _AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_repo_only_tooling_set_contents():
    """The exclusion set must contain the doc generators + the watchdog itself."""
    mod = _load_audit()
    assert "quant-deploy-drift-watch.py" in mod.REPO_ONLY_TOOLING
    assert "quant-adr-index.py" in mod.REPO_ONLY_TOOLING
    assert "quant-flag-inventory.py" in mod.REPO_ONLY_TOOLING


def test_excluded_tooling_never_appears_in_audit(tmp_path):
    """Even if a repo-only tool exists ONLY in repo (the classic REPO_ONLY_NEW
    trigger), it must not appear in the audit results at all."""
    mod = _load_audit()
    # Empty deployed dir → every repo script would be REPO_ONLY_NEW if not excluded.
    report = mod.audit(tmp_path)
    audited = {r["script"] for r in report["files"]}
    assert audited.isdisjoint(mod.REPO_ONLY_TOOLING), (
        f"repo-only tooling leaked into audit: {audited & mod.REPO_ONLY_TOOLING}"
    )


def test_excluded_tooling_not_counted_as_deployed_only(tmp_path):
    """A repo-only tool that somehow got deployed must also not be flagged
    (exclusion applies to BOTH sides of the comparison)."""
    mod = _load_audit()
    # Simulate a deployed dir containing ONLY an excluded tool.
    (tmp_path / "quant-deploy-drift-watch.py").write_text("# deployed copy\n")
    report = mod.audit(tmp_path)
    audited = {r["script"] for r in report["files"]}
    assert "quant-deploy-drift-watch.py" not in audited
