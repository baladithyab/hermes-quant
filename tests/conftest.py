"""Top-level pytest fixtures for hermes-quant.

The single autouse fixture here protects the user's real `~/.hermes/quant/`
from test pollution. Without it, any test that exercises a code path which
emits a governance audit event (risk gate, halt creation, proposal create,
etc.) writes into the live user home directory.

Discovered 2026-05-24: 9,104 test-fixture rows had accumulated in the live
audit log because integration tests of `advisor.recommend()` were running
the gate without redirecting `governance.audit_log.AUDIT_LOG_PATH`.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_governance_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect governance audit_log writes to a per-test tmp_path.

    Autouse so tests that don't explicitly opt in still get isolation.
    Tests that need to verify audit_log content can read from
    `~/.hermes/quant/governance/audit_log.jsonl` via `audit_log.AUDIT_LOG_PATH`
    (post-monkeypatch the symbol points into tmp_path).

    Yields the tmp path so individual tests can inspect it if needed.
    """
    # Defer the import so this fixture works even if governance is not yet
    # loaded (e.g., a unit test that only imports protocol.py).
    try:
        from hermes_quant.governance import audit_log
    except ImportError:
        return tmp_path

    isolated = tmp_path / "governance"
    isolated.mkdir(parents=True, exist_ok=True)
    audit_path = isolated / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", audit_path, raising=True)
    monkeypatch.setattr(audit_log, "GOVERNANCE_HOME", isolated, raising=True)
    return audit_path


@pytest.fixture(autouse=True)
def _isolate_evidence_store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect EvidenceStore default root to a per-test tmp_path.

    Tests that construct EvidenceStore() without an explicit `root` argument
    will land in tmp_path, not the user's real ~/.hermes/quant/evidence_store.
    Tests that pass `root=...` explicitly are unaffected.
    """
    evidence_dir = tmp_path / "evidence_store"
    monkeypatch.setenv("HERMES_QUANT_EVIDENCE_DIR", str(evidence_dir))
    return evidence_dir


@pytest.fixture(autouse=True)
def _isolate_kill_switch_state_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect governance.kill_switch.STATE_JSON_PATH to a per-test tmp_path.

    Without this, a test that calls kill_switch.fire() pollutes the user's
    real ~/.hermes/quant/state.json.
    """
    try:
        from hermes_quant.governance import kill_switch
    except ImportError:
        return tmp_path

    state_path = tmp_path / "state.json"
    monkeypatch.setattr(kill_switch, "STATE_JSON_PATH", state_path, raising=True)
    return state_path
