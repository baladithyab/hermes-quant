"""hermes_quant.cli.halts — halt / resume / emergency-stop CLI handlers.

Per synthesis-v2 §P0-D + §P2-θ + ADR-0009 §P0-4:

- `hermes quant halt <account> [<asset_class>] [<asset>] --reason TEXT`
  Add a durable halt record. Required --reason for audit trail.

- `hermes quant resume <account> [<asset_class>] [<asset>] --reason TEXT`
  Lift an active halt. Required --reason.

- `hermes quant emergency-stop [--account ACCOUNT]`
  Critical ordering per synthesis-v2 §P0-D:
    1. Insert durable halt FIRST (so even if broker cancel races with the
       next daemon tick, the halt is committed and entries can't resume).
    2. Update halt_state.json mirror atomically.
    3. Emit halt signal record to bus (consumers flatten on next read).
    4. ONLY THEN: cancel via broker (out-of-scope for v0.1.1 — print intent;
       v0.1.2 will integrate with freqtrade's force-exit + alpaca/ccxt).

A halt at scope `(*, *, *)` halts everything. Wildcard semantics from
hermes_quant.daemon.halt_state.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import pandas as pd

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.signal_bus import emit_signal_record

logger = logging.getLogger(__name__)


def _parse_scope_arg(value: str | None) -> str | None:
    """Treat '*' as wildcard (None); pass through otherwise."""
    if value is None or value == "*" or value == "":
        return None
    return value


def cmd_halt(args: argparse.Namespace) -> int:
    """Implement `hermes quant halt <account> [<asset_class>] [<asset>] --reason TEXT`."""
    account = _parse_scope_arg(getattr(args, "account", None))
    asset_class = _parse_scope_arg(getattr(args, "asset_class", None))
    asset = _parse_scope_arg(getattr(args, "asset", None))
    reason = args.reason

    # Resolve defaults at call time so test monkeypatch works
    from hermes_quant.daemon import halt_state as _halt_module
    halt_state = HaltStateSQLite(
        db_path=_halt_module.DEFAULT_STATE_DB,
        mirror_path=_halt_module.DEFAULT_HALT_JSON_MIRROR,
    )
    try:
        rec = halt_state.add_halt(
            account_id=account,
            asset_class=asset_class,
            asset=asset,
            reason=reason,
        )
        print(f"halted: account={rec.account_id} class={rec.asset_class} "
              f"asset={rec.asset or '*'} epoch={rec.halt_epoch}")
        print(f"reason: {rec.reason}")
        print()
        print("Active halts persist across daemon restart. To lift:")
        print(f"  hermes quant resume {rec.account_id} {rec.asset_class} "
              f"{rec.asset or '*'} --reason \"why are you lifting?\"")
        return 0
    except ValueError as e:
        print(f"halt failed: {e}", file=sys.stderr)
        return 1


def cmd_resume(args: argparse.Namespace) -> int:
    """Implement `hermes quant resume <account> [<asset_class>] [<asset>] --reason TEXT`."""
    account = _parse_scope_arg(getattr(args, "account", None))
    asset_class = _parse_scope_arg(getattr(args, "asset_class", None))
    asset = _parse_scope_arg(getattr(args, "asset", None))
    reason = args.reason

    # Resolve defaults at call time so test monkeypatch works
    from hermes_quant.daemon import halt_state as _halt_module
    halt_state = HaltStateSQLite(
        db_path=_halt_module.DEFAULT_STATE_DB,
        mirror_path=_halt_module.DEFAULT_HALT_JSON_MIRROR,
    )
    try:
        cleared = halt_state.clear_halt(
            account_id=account,
            asset_class=asset_class,
            asset=asset,
            reason=reason,
        )
        if cleared:
            print(f"resumed: account={account or '*'} class={asset_class or '*'} "
                  f"asset={asset or '*'}")
            print(f"reason: {reason}")
            return 0
        else:
            print(f"no active halt at scope ({account or '*'}, "
                  f"{asset_class or '*'}, {asset or '*'})", file=sys.stderr)
            return 1
    except ValueError as e:
        print(f"resume failed: {e}", file=sys.stderr)
        return 1


def cmd_emergency_stop(args: argparse.Namespace) -> int:
    """Implement `hermes quant emergency-stop [--account ACCOUNT]`.

    Per synthesis-v2 §P0-D ordering:
      1. Halt FIRST  (durable SQLite + JSON mirror)
      2. Halt signal on bus
      3. Broker cancel  (v0.1.1: print intent only)
    """
    account = _parse_scope_arg(getattr(args, "account", None))
    reason = "operator_emergency_stop"

    # Resolve defaults at call time so test monkeypatch works
    from hermes_quant.daemon import halt_state as _halt_module
    halt_state = HaltStateSQLite(
        db_path=_halt_module.DEFAULT_STATE_DB,
        mirror_path=_halt_module.DEFAULT_HALT_JSON_MIRROR,
    )
    halt_account = account if account else "*"

    # Step 1: durable halt first
    try:
        rec = halt_state.add_halt(
            account_id=account,
            asset_class=None,  # all classes
            asset=None,        # all assets
            reason=reason,
        )
        print(f"durable halt installed: scope=({rec.account_id}, *, *) "
              f"epoch={rec.halt_epoch}")
    except ValueError as e:
        # Existing halt blocks add_halt; that's fine for emergency-stop —
        # it means a halt is already active. Continue with bus signal + broker.
        print(f"halt already active (continuing): {e}", file=sys.stderr)

    # Step 2: halt signal on bus
    halt_signal = {
        "schema_version": 1,
        "id": f"halt-{pd.Timestamp.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        "type": "halt",
        "asof": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "scope": [halt_account, "*", None],
        "reason": reason,
        "halt": True,
    }
    try:
        # Resolve path at call time (not import time) so test monkeypatch works
        from hermes_quant.daemon import signal_bus as _bus_module
        emit_signal_record(halt_signal, path=_bus_module.SIGNAL_BUS_PATH)
        print(f"halt signal emitted to bus")
    except Exception as e:  # noqa: BLE001
        print(f"halt signal emission failed (halt is still durable in SQLite): {e}",
              file=sys.stderr)

    # Step 3: broker cancel  (v0.1.1: intent only)
    print()
    print("Broker cancel: NOT YET IMPLEMENTED in v0.1.1.")
    print("Manual action required:")
    print("  1. In your freqtrade UI, force-exit all positions.")
    print("  2. (For ccxt/alpaca consumers in v0.2: this will auto-cancel.)")
    print()
    print(f"To resume after the underlying issue is resolved:")
    print(f"  hermes quant resume {halt_account} --reason \"<why are you resuming?>\"")
    return 0
