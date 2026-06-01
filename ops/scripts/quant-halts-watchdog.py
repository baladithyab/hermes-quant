#!/usr/bin/env python3
"""quant-halts-watchdog — daily report of active halts (silence-by-default).

Runs daily as a cron. If there are NO active halts, exits silently (no message).
If there are active halts, prints a structured report to stdout and exits 0
so the cron's deliver path posts it to Discord.

This catches the class of bug seen in 2026-05-26: a `daily_loss_breaker` halt
got installed by older code with `halted_until=None` (manual-resume only),
and sat in state.db for 37 hours before being noticed. The original code path
was fixed (now sets `halted_until=_next_session_open(...)`), but for robustness
we want a daily watchdog regardless.

Output format (stdout, only when halts active):

    ⛔ Hermes-Quant Halt Watchdog — N active halt(s)

    1. account=default class=crypto asset=BTC/USDT
       reason: daily_loss_breaker
       installed: 2026-05-26 23:02 UTC (37.2 hours ago)
       auto-clear: NEVER (manual resume required)

    To resume: python -m hermes_quant.cli.halts resume <account> <class> <asset> --reason "..."

Posture:
- Silence-by-default per ADR-0031: zero output when no halts are active.
- Stale-halt threshold: any halt older than 24h triggers a louder warning.
- Read-only: never mutates state.db.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Re-exec under the hermes-agent venv if needed (where hermes_quant is installed).
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])


def main() -> int:
    try:
        from hermes_quant.daemon import halt_state as halt_module
        from hermes_quant.daemon.halt_state import HaltStateSQLite
    except Exception as exc:  # noqa: BLE001
        # Silence-by-default: if the package isn't importable in this env,
        # write a quiet stderr breadcrumb but don't block the cron.
        print(
            f"warning: hermes_quant not importable ({type(exc).__name__}: {exc}); "
            f"halt watchdog skipped",
            file=sys.stderr,
        )
        return 0

    try:
        state = HaltStateSQLite(
            db_path=halt_module.DEFAULT_STATE_DB,
            mirror_path=halt_module.DEFAULT_HALT_JSON_MIRROR,
        )
        halts = list(state.active_halts())
    except Exception as exc:  # noqa: BLE001
        print(
            f"warning: halt-state read failed ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 0

    # Silence-by-default: no output when nothing is halted.
    if not halts:
        return 0

    now = datetime.now(timezone.utc)
    lines = [f"⛔ **Hermes-Quant Halt Watchdog** — {len(halts)} active halt(s)", ""]

    stale_count = 0
    for i, halt in enumerate(halts, start=1):
        # halted_at is a pd.Timestamp; convert to py datetime for arithmetic
        try:
            halted_at = halt.halted_at.to_pydatetime()
            age_hours = (now - halted_at).total_seconds() / 3600.0
        except Exception:  # noqa: BLE001
            age_hours = -1.0

        scope_account = halt.account_id or "*"
        scope_class = halt.asset_class or "*"
        scope_asset = halt.asset or "*"

        if halt.halted_until is None:
            auto_clear = "NEVER (manual resume required)"
        else:
            try:
                until_dt = halt.halted_until.to_pydatetime()
                hrs_until_clear = (until_dt - now).total_seconds() / 3600.0
                if hrs_until_clear <= 0:
                    auto_clear = f"{until_dt.isoformat()} (overdue — should have auto-cleared)"
                else:
                    auto_clear = f"{until_dt.isoformat()} (in {hrs_until_clear:.1f} hours)"
            except Exception:  # noqa: BLE001
                auto_clear = str(halt.halted_until)

        is_stale = age_hours >= 24.0 and halt.halted_until is None
        if is_stale:
            stale_count += 1

        lines.append(f"{i}. account={scope_account} class={scope_class} asset={scope_asset}")
        lines.append(f"   reason: {halt.reason}")
        if age_hours >= 0:
            staleness_marker = "  🚨 STALE" if is_stale else ""
            lines.append(
                f"   installed: {halt.halted_at} ({age_hours:.1f} hours ago){staleness_marker}"
            )
        else:
            lines.append(f"   installed: {halt.halted_at}")
        lines.append(f"   auto-clear: {auto_clear}")
        lines.append("")

    if stale_count > 0:
        lines.append(
            f"⚠️ {stale_count} halt(s) older than 24h with no auto-clear. "
            f"These may be orphaned from older code paths. Review and resume manually:"
        )
        lines.append(
            "  python -m hermes_quant.cli.halts resume <account> <class> <asset> --reason \"why\""
        )

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
