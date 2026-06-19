"""hermes_quant.governance.kill_switch — single-owner halt flag (ADR-0031 D3).

Idempotent `fire()`. Atomic-rename writes to `~/.hermes/quant/state.json`.
`clear()` requires a HumanApprovalToken with scope='kill_switch_clear'.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.governance import approvals, audit_log
from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)


QUANT_HOME = _resolve_quant_home()
STATE_JSON_PATH = QUANT_HOME / "state.json"


_fire_lock = threading.Lock()


def _state_path() -> Path:
    return STATE_JSON_PATH


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    with open(path) as f:
        text = f.read().strip()
    if not text:
        return {}
    return json.loads(text)


def _write_state_atomic(state: dict[str, Any]) -> None:
    """Durably write state via tmp -> fsync -> rename (ADR-0031 D3).

    The halt flag is an always-on money-safety rail (ADR-0016): it MUST be
    durable before this returns. Without fsync, a power-loss/crash in the window
    between the rename and the OS flushing the page cache loses the halt, and
    is_halted() reads False on the next process start (fail-OPEN). Mirrors the
    sibling durable writers (journal._atomic_write, autonomous.trip_kill_switch,
    audit_log.append).
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, sort_keys=True, default=str))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX
    # Parent-dir fsync so the rename itself survives a crash.
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def fire(reason: str, source: str) -> None:
    """Set the halt flag. Idempotent: a second call is a no-op + WARN.

    On the first call:
      1. Acquires the process-local mutex.
      2. Re-reads state.json. If `halt is True`, logs WARNING + returns.
      3. Otherwise writes the new state via tmp+rename, then appends one
         `kill_switch_fired` event to the governance audit log.
    """
    with _fire_lock:
        prior = _read_state()
        if prior.get("halt") is True:
            prior_source = prior.get("halt_source", "<unknown>")
            logger.warning(
                "kill switch already fired by %r; ignoring fire(reason=%r, source=%r)",
                prior_source,
                reason,
                source,
            )
            return

        now = datetime.now(UTC)
        new_state = dict(prior)
        new_state["halt"] = True
        new_state["halt_reason"] = reason
        new_state["halt_source"] = source
        new_state["halt_fired_at"] = now.isoformat()

        _write_state_atomic(new_state)

        audit_log.append(
            audit_log.GovernanceEvent(
                kind="kill_switch_fired",
                asof=now,
                source=source,
                payload={
                    "reason": reason,
                    "prior_state": prior,
                },
            )
        )


def is_halted() -> bool:
    """Return whether the halt flag is set in state.json."""
    return bool(_read_state().get("halt") is True)


def clear(token: approvals.HumanApprovalToken | None = None) -> None:
    """Clear the halt flag. Admin-only — requires a HumanApprovalToken with
    scope='kill_switch_clear' and target_ref='state.json'.
    """
    if token is None:
        # Even attempting clear() without a token is forbidden.
        approvals.require_human_token("kill_switch_clear", "state.json")
        return  # unreachable; require_human_token raises

    from hermes_quant.governance.approvals import NoApprovalError

    if token.scope != "kill_switch_clear":
        raise NoApprovalError(f"token scope is {token.scope!r}; kill_switch_clear required")

    # ar36: BIND the PASSED token. require_human_token only proves SOME valid grant
    # exists for (kill_switch_clear, target_ref); its return value was previously
    # DISCARDED, so an EXPIRED or WRONG token object would clear the rail while the
    # genuinely-valid one-shot grant survived unspent (re-spendable) — breaking the
    # one-shot single-owner invariant (ADR-0031 D3). Verify the validated live grant is
    # THIS token by token_id; also reject a self-expired token object defensively.
    valid = approvals.require_human_token("kill_switch_clear", token.target_ref)
    if token.token_id != valid.token_id:
        raise NoApprovalError(
            f"passed token {token.token_id!r} is not the live kill_switch_clear grant "
            f"for {token.target_ref!r} (expected {valid.token_id!r}); refusing to clear"
        )
    if token.is_expired():
        raise NoApprovalError(f"passed token {token.token_id!r} is expired; refusing to clear")

    with _fire_lock:
        prior = _read_state()
        if prior.get("halt") is not True:
            logger.warning("kill_switch.clear() called but halt is not set")
            return
        # ar37: CONSUME the token BEFORE writing the cleared state. Previously the disk
        # was flipped halt=False FIRST and consume_token() ran AFTER — so a forged /
        # already-consumed token_id raised NoApprovalError only AFTER the rail was
        # already disarmed on disk (caller sees an exception but the halt is silently
        # lifted: fail-OPEN). Consume first (raises on unknown/already-consumed, leaving
        # state untouched), THEN write the cleared state — mirroring fire()'s
        # write-only-after-the-guard ordering.
        approvals.consume_token(token.token_id)
        new_state = dict(prior)
        new_state["halt"] = False
        new_state["halt_cleared_at"] = datetime.now(UTC).isoformat()
        _write_state_atomic(new_state)
