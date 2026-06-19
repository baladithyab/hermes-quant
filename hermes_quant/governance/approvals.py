"""hermes_quant.governance.approvals — single HITL contract (ADR-0031 D4).

Token store at `~/.hermes/quant/governance/approval_tokens.jsonl`. The store
is append-only — expired or consumed tokens are filtered at read time, never
deleted in place. NEVER auto-grants. Including from `--yes`. Including from
the daemon. Including from a retro proposal.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from hermes_quant.governance import audit_log
from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)


QUANT_HOME = _resolve_quant_home()
GOVERNANCE_HOME = QUANT_HOME / "governance"
TOKEN_STORE_PATH = GOVERNANCE_HOME / "approval_tokens.jsonl"

DEFAULT_TTL_MINUTES = 15

ApprovalScope = Literal[
    "proposal",
    "amendment",
    "promotion",
    "retro_code_change",
    "kill_switch_clear",
]

VALID_SCOPES: tuple[str, ...] = (
    "proposal",
    "amendment",
    "promotion",
    "retro_code_change",
    "kill_switch_clear",
)


class NoApprovalError(Exception):
    """Raised when a governance-gated path is invoked without a valid token."""


class HumanApprovalToken(BaseModel):
    token_id: str = Field(default_factory=lambda: "ap_" + secrets.token_hex(8))
    granted_by: str
    granted_at: datetime
    scope: ApprovalScope
    target_ref: str
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        if now is None:
            now = datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return now >= exp


# ---------------------------------------------------------------------------
# Path resolution + persistence helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _store_path() -> Path:
    return TOKEN_STORE_PATH


@contextmanager
def _flocked() -> Iterator[None]:
    """Hold an OS-level exclusive lock on the token store's `.lock` sidecar.

    ADR-0031 D3 one-shot single-owner: the token store is a money-gate
    authority store (tokens authorize kill_switch_clear, promotion, proposal
    fires). The process-local `_write_lock` only serializes a single append
    within one process — it does NOT prevent two SEPARATE processes (operator
    CLI + daemon, or two CLI invocations) from interleaving a read+check+append
    and double-spending the same one-shot token. This flock makes the whole
    check-then-act cross-process atomic. Released (and fd closed) on exit even
    on error. POSIX flock; the daemon + CLI are both POSIX, which is sufficient.
    """
    path = _store_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _append_row(row: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(path, "a", buffering=1) as f:
            f.write(json.dumps(row, default=str, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _iter_rows() -> Iterator[dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return
    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "approvals: skipping corrupt (non-JSON) line in token store %s",
                    path,
                )
                continue
            if not isinstance(obj, dict):
                logger.warning(
                    "approvals: skipping non-dict line (type=%s) in token store %s",
                    type(obj).__name__,
                    path,
                )
                continue
            yield obj


def _consumed_ids() -> set[str]:
    return {row["token_id"] for row in _iter_rows() if row.get("row_type") == "consumed"}


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def grant_token(
    scope: ApprovalScope,
    target_ref: str,
    granted_by: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> HumanApprovalToken:
    """Mint a new approval token and persist it. Caller is the human; this
    function does not validate the human's identity — it merely records
    that a grant occurred. Call sites that bypass HITL UX are bugs.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"invalid scope: {scope!r}")

    now = datetime.now(UTC)
    token = HumanApprovalToken(
        granted_by=granted_by,
        granted_at=now,
        scope=scope,
        target_ref=target_ref,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )

    row = token.model_dump()
    row["row_type"] = "grant"
    _append_row(row)

    audit_log.append(
        audit_log.GovernanceEvent(
            kind="proposal_emitted" if scope == "proposal" else "promotion_event",
            asof=now,
            source="governance.approvals.grant_token",
            payload={
                "row_type": "approval_granted",
                "token_id": token.token_id,
                "scope": scope,
                "target_ref": target_ref,
                "granted_by": granted_by,
            },
        )
    )

    return token


def require_human_token(
    scope: ApprovalScope,
    target_ref: str,
) -> HumanApprovalToken:
    """Return the unexpired, unconsumed token matching (scope, target_ref);
    raise `NoApprovalError` otherwise. NEVER auto-grants.

    Refuses tokens whose `target_ref` resolves under
    `hermes_quant/governance/**` when scope is `retro_code_change` —
    defense-in-depth per ADR-0031 D7.
    """
    if scope == "retro_code_change" and target_ref.startswith("hermes_quant/governance/"):
        raise NoApprovalError("retro_code_change tokens cannot target hermes_quant/governance/**")

    consumed = _consumed_ids()
    now = datetime.now(UTC)

    candidate: HumanApprovalToken | None = None
    for row in _iter_rows():
        if row.get("row_type") != "grant":
            continue
        if row.get("scope") != scope:
            continue
        if row.get("target_ref") != target_ref:
            continue
        if row.get("token_id") in consumed:
            continue

        granted_at = _parse_dt(row["granted_at"])
        expires_at = _parse_dt(row["expires_at"])
        token = HumanApprovalToken(
            token_id=row["token_id"],
            granted_by=row["granted_by"],
            granted_at=granted_at,
            scope=row["scope"],
            target_ref=row["target_ref"],
            expires_at=expires_at,
        )
        if token.is_expired(now):
            continue
        candidate = token
        break

    if candidate is None:
        raise NoApprovalError(
            f"no unexpired human approval token for scope={scope!r} target_ref={target_ref!r}"
        )
    return candidate


def consume_token(token_id: str) -> None:
    """Mark a token as consumed (one-shot). Subsequent require_human_token
    calls referencing the same target return NoApprovalError.
    """
    # ADR-0031 D3 one-shot single-owner: the read+check+append below is a
    # cross-process-atomic critical section. Holding the flock for the WHOLE
    # check-then-act (not just the append) is what prevents two processes from
    # both reading an unconsumed snapshot and both appending a `consumed` row
    # — i.e. double-spending a one-shot kill_switch_clear / promotion token.
    # `_consumed_ids()` is recomputed INSIDE the lock so a snapshot captured by
    # a racing process before it acquired the lock cannot pass the guard.
    with _flocked():
        consumed = _consumed_ids()
        if token_id in consumed:
            raise NoApprovalError(f"token {token_id!r} already consumed")

        # Verify the token exists somewhere as a grant
        found = False
        for row in _iter_rows():
            if row.get("row_type") == "grant" and row.get("token_id") == token_id:
                found = True
                break
        if not found:
            raise NoApprovalError(f"unknown token: {token_id!r}")

        now = datetime.now(UTC)
        _append_row(
            {
                "row_type": "consumed",
                "token_id": token_id,
                "consumed_at": now.isoformat(),
            }
        )

    audit_log.append(
        audit_log.GovernanceEvent(
            kind="promotion_event",
            asof=now,
            source="governance.approvals.consume_token",
            payload={"row_type": "approval_consumed", "token_id": token_id},
        )
    )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        return _utc(dt)
    raise ValueError(f"cannot parse datetime: {value!r}")
