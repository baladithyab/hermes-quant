"""hermes_quant.governance.kill_switch — single-owner halt flag (ADR-0031 D3).

Idempotent `fire()`. Atomic-rename writes to `~/.hermes/quant/state.json`.
`clear()` requires a HumanApprovalToken with scope='kill_switch_clear'.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.governance import approvals, audit_log

logger = logging.getLogger(__name__)


QUANT_HOME = Path.home() / ".hermes" / "quant"
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
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, default=str))
    tmp.replace(path)  # atomic on POSIX


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

    if token.scope != "kill_switch_clear":
        from hermes_quant.governance.approvals import NoApprovalError

        raise NoApprovalError(
            f"token scope is {token.scope!r}; kill_switch_clear required"
        )

    # Validate via the canonical check (raises if invalid)
    approvals.require_human_token("kill_switch_clear", token.target_ref)

    with _fire_lock:
        prior = _read_state()
        if prior.get("halt") is not True:
            logger.warning("kill_switch.clear() called but halt is not set")
            return
        new_state = dict(prior)
        new_state["halt"] = False
        new_state["halt_cleared_at"] = datetime.now(UTC).isoformat()
        _write_state_atomic(new_state)

        approvals.consume_token(token.token_id)
