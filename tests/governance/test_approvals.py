"""Tests for hermes_quant.governance.approvals (ADR-0031 D4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.governance import approvals, audit_log
from hermes_quant.governance.approvals import HumanApprovalToken, NoApprovalError


@pytest.fixture
def gov_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    quant = tmp_path / "quant"
    gov = quant / "governance"
    monkeypatch.setattr(approvals, "TOKEN_STORE_PATH", gov / "approval_tokens.jsonl")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", gov / "audit_log.jsonl")
    return quant


def test_approvals_require_explicit_human_token(gov_paths: Path) -> None:
    with pytest.raises(NoApprovalError):
        approvals.require_human_token("promotion", "live_broker")


def test_grant_then_require_round_trip(gov_paths: Path) -> None:
    t = approvals.grant_token("promotion", "live_broker", granted_by="alice")
    fetched = approvals.require_human_token("promotion", "live_broker")
    assert fetched.token_id == t.token_id


def test_approval_token_expires_after_ttl(
    gov_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grant a 1-minute token; advance time 2 minutes; require fails."""
    t = approvals.grant_token(
        "promotion", "live_broker", granted_by="alice", ttl_minutes=1
    )

    fake_now = t.granted_at + timedelta(minutes=2)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fake_now if tz is None else fake_now.astimezone(tz)

    monkeypatch.setattr(approvals, "datetime", FakeDatetime)

    with pytest.raises(NoApprovalError):
        approvals.require_human_token("promotion", "live_broker")


def test_consume_token_one_shot(gov_paths: Path) -> None:
    t = approvals.grant_token("amendment", "amend_42", granted_by="alice")
    approvals.consume_token(t.token_id)

    # Second consume → NoApprovalError
    with pytest.raises(NoApprovalError):
        approvals.consume_token(t.token_id)

    # require_human_token also fails (consumed tokens are filtered out)
    with pytest.raises(NoApprovalError):
        approvals.require_human_token("amendment", "amend_42")


def test_grant_token_emits_audit_event(gov_paths: Path) -> None:
    approvals.grant_token("proposal", "prop_x", granted_by="alice")
    rows = list(audit_log.read())
    assert any(
        r.payload.get("row_type") == "approval_granted"
        and r.payload.get("scope") == "proposal"
        for r in rows
    )


def test_retro_code_change_target_governance_path_rejected(gov_paths: Path) -> None:
    """Defense-in-depth per ADR-0031 D7: even if a token were minted, the
    require_human_token call refuses to look one up under governance/.
    """
    with pytest.raises(NoApprovalError):
        approvals.require_human_token(
            "retro_code_change", "hermes_quant/governance/audit_log.py"
        )


def test_invalid_scope_rejected(gov_paths: Path) -> None:
    with pytest.raises(ValueError):
        approvals.grant_token("not_a_scope", "x", granted_by="alice")  # type: ignore[arg-type]
