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
import logging
import sqlite3
import sys

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
        print(
            f"halted: account={rec.account_id} class={rec.asset_class} "
            f"asset={rec.asset or '*'} epoch={rec.halt_epoch}"
        )
        print(f"reason: {rec.reason}")
        print()
        print("Active halts persist across daemon restart. To lift:")
        print(
            f"  hermes quant resume {rec.account_id} {rec.asset_class} "
            f'{rec.asset or "*"} --reason "why are you lifting?"'
        )
        return 0
    except ValueError as e:
        print(f"halt failed: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        # ar04: a concurrent writer won the add_halt BEGIN IMMEDIATE race — the
        # raw sqlite error is not a ValueError. For an explicit `halt` command
        # (not emergency-stop) this is a genuine failure to install THIS halt, so
        # report it non-zero — but with a clear contention message, not a crash.
        print(f"halt failed (write contended by a concurrent halt): {e}", file=sys.stderr)
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
            print(
                f"resumed: account={account or '*'} class={asset_class or '*'} asset={asset or '*'}"
            )
            print(f"reason: {reason}")
            return 0
        else:
            print(
                f"no active halt at scope ({account or '*'}, {asset_class or '*'}, {asset or '*'})",
                file=sys.stderr,
            )
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
            asset=None,  # all assets
            reason=reason,
        )
        print(f"durable halt installed: scope=({rec.account_id}, *, *) epoch={rec.halt_epoch}")
    except ValueError as e:
        # Existing halt blocks add_halt; that's fine for emergency-stop —
        # it means a halt is already active. Continue with bus signal + broker.
        print(f"halt already active (continuing): {e}", file=sys.stderr)
    except sqlite3.Error as e:
        # ar04: a concurrent emergency-stop (or any writer) may win the BEGIN
        # IMMEDIATE race and leave this process a sqlite contention loser
        # (IntegrityError on the UNIQUE PK, or OperationalError on busy-timeout).
        # That is NOT a ValueError, so it would otherwise crash us BEFORE the
        # Step-2 bus signal and defeat the HALT-FIRST ordering. Treat it as
        # "a halt is being installed by a concurrent operator" and CONTINUE —
        # the winner's durable halt is committed; we still emit our bus signal.
        print(f"halt write contended (continuing — a concurrent halt won): {e}", file=sys.stderr)

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
        print("halt signal emitted to bus")
    except Exception as e:  # noqa: BLE001
        print(
            f"halt signal emission failed (halt is still durable in SQLite): {e}", file=sys.stderr
        )

    # Step 3: broker cancel  (v0.1.1: intent only)
    print()
    print("Broker cancel: NOT YET IMPLEMENTED in v0.1.1.")
    print("Manual action required:")
    print("  1. In your freqtrade UI, force-exit all positions.")
    print("  2. (For ccxt/alpaca consumers in v0.2: this will auto-cancel.)")
    print()
    print("To resume after the underlying issue is resolved:")
    print(f'  hermes quant resume {halt_account} --reason "<why are you resuming?>"')
    return 0


# ---------------------------------------------------------------------------
# argparse driver — `python -m hermes_quant.cli.halts {halt|resume|emergency-stop}`
#
# Fixes ROLLOUT.md §5 kill-switch invocation per v0.4 MoA F4 finding (GPT C1).
# Without this driver, `python -m hermes_quant.cli.halts halt '*' --reason "..."`
# was a silent no-op, leaving the operator believing a halt was installed when
# it was not.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes_quant.cli.halts",
        description=(
            "Halt-management CLI. Subcommands: halt, resume, emergency-stop. "
            "Halts are durable in SQLite and consulted by gate.py before any "
            "approval (silence-by-default kill-switch)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # halt <account> [<asset_class>] [<asset>] --reason TEXT
    p_halt = sub.add_parser("halt", help="Install a durable halt for the given scope.")
    p_halt.add_argument("account", help="Account scope ('*' for all accounts).")
    p_halt.add_argument("asset_class", nargs="?", default=None, help="Optional asset class scope.")
    p_halt.add_argument("asset", nargs="?", default=None, help="Optional asset (ticker) scope.")
    p_halt.add_argument("--reason", required=True, help="Operator-supplied reason for the halt.")
    p_halt.set_defaults(_handler=cmd_halt)

    # resume <account> [<asset_class>] [<asset>] --reason TEXT
    p_resume = sub.add_parser("resume", help="Lift an active halt for the given scope.")
    p_resume.add_argument("account", help="Account scope.")
    p_resume.add_argument("asset_class", nargs="?", default=None)
    p_resume.add_argument("asset", nargs="?", default=None)
    p_resume.add_argument("--reason", required=True, help="Operator-supplied reason for the resume.")
    p_resume.set_defaults(_handler=cmd_resume)

    # emergency-stop [--account ACCOUNT]
    p_em = sub.add_parser(
        "emergency-stop",
        help="Halt EVERYTHING for the account (or all accounts if --account omitted).",
    )
    p_em.add_argument("--account", default=None, help="Optional account scope; default '*'.")
    p_em.set_defaults(_handler=cmd_emergency_stop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
