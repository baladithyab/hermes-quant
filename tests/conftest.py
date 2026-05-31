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

import os
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


@pytest.fixture(autouse=True)
def _autouse_dummy_third_party_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject placeholder env vars for every third-party SDK so CI never
    blocks on missing creds. Real tests that need real creds opt out by
    overriding via their own monkeypatch.setenv() calls.

    BACKFILL ONLY: if the env var is already set (e.g. live-integration
    runs with `HERMES_QUANT_LIVE_LLM=1` and a real OPENROUTER_API_KEY
    in the environment), do NOT overwrite it. This preserves existing
    credentials so live tests can authenticate while still defaulting
    every absent key to the placeholder for offline-CI reliability.

    ADR-0038 §D.4 (P8) — TradingAgents pattern backfill, Wave D Track A.
    """
    placeholders = {
        # LLM providers
        "OPENROUTER_API_KEY": "test-placeholder",
        "ANTHROPIC_API_KEY": "test-placeholder",
        "OPENAI_API_KEY": "test-placeholder",
        "AWS_BEARER_TOKEN_BEDROCK": "test-placeholder",
        # Data providers
        "ALPACA_API_KEY": "test-placeholder",
        "ALPACA_SECRET_KEY": "test-placeholder",
        "ALPHAVANTAGE_API_KEY": "test-placeholder",
        # Exchanges (ccxt)
        "BINANCE_API_KEY": "test-placeholder",
        "BINANCE_SECRET": "test-placeholder",
        "COINBASE_API_KEY": "test-placeholder",
        "COINBASE_SECRET": "test-placeholder",
    }
    for key, val in placeholders.items():
        if key not in os.environ:
            monkeypatch.setenv(key, val)


@pytest.fixture(autouse=True)
def _isolate_hermes_quant_flags():
    """Snapshot every ``HERMES_QUANT_*`` feature flag before a test and restore
    it after — so a test that flips a flag via raw ``os.environ[...] = "1"``
    (which ``monkeypatch.setenv`` would auto-undo, but a direct assignment does
    NOT) cannot leak that flag into a later test.

    This closes the order-dependent test-pollution class the 2026-05-30 meta-review
    (#12) flagged: the catalyst onboarding/wiring tests read
    ``HERMES_QUANT_SEMANTIC_ENABLED`` / ``HERMES_QUANT_CATALYST_ONBOARDING`` and so
    are sensitive to any earlier test that sets one without cleanup. Snapshot/restore
    makes those tests order-independent regardless of who the upstream leaker is.
    Defensive and cheap — it neither sets nor unsets any flag a test didn't touch.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("HERMES_QUANT_")}
    try:
        yield
    finally:
        # Remove any HERMES_QUANT_* the test ADDED, and restore any it CHANGED/removed.
        current = {k for k in os.environ if k.startswith("HERMES_QUANT_")}
        for k in current - saved.keys():
            del os.environ[k]
        for k, v in saved.items():
            os.environ[k] = v
