#!/usr/bin/env python3
"""quant-proposals-ttl-watchdog.py — surface proposals approaching TTL expiry.

Per architecture critique 2026-05-27 Risk 3: proposals are created with a 24h
TTL; if the user misses the Discord notification, they expire silently. This
watchdog runs daily at 06:30 PT and reports any proposal with elapsed > 18h
and no decision (approved/rejected). Silence-by-default when nothing is wrong.

Outputs to stdout (cron delivers to Discord via cron deliver field):
- 0 active proposals approaching TTL: silent (no output)
- N proposals approaching TTL: markdown alert with table

Exit codes:
- 0: silent or successful alert
- 1: database read error
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Configuration --------------------------------------------------------

PROPOSALS_DB = Path.home() / ".hermes" / "quant" / "proposals.db"
WARN_THRESHOLD_HOURS = 18  # alert when >18h elapsed (75% of 24h TTL)


def _parse_iso(ts: str) -> datetime:
    """Parse ISO timestamp; tolerant of trailing 'Z' and offsets."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def find_aging_proposals(db_path: Path = PROPOSALS_DB) -> list[dict]:
    """Return proposals in 'pending' state with elapsed > WARN_THRESHOLD_HOURS.

    Each entry: {proposal_id, symbol, asset_class, age_hours, expires_in_hours}.
    Returns [] if DB missing (silent — proposals system not yet provisioned).
    """
    if not db_path.exists():
        return []

    aging: list[dict] = []
    now = datetime.now(timezone.utc)

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT proposal_id, symbol, asset_class, created_at, expires_at "
            "FROM proposals WHERE state = 'pending'"
        )
        rows = cur.fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"ERROR: cannot read {db_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    for proposal_id, symbol, asset_class, created_at_str, expires_at_str in rows:
        try:
            created_at = _parse_iso(created_at_str)
            expires_at = _parse_iso(expires_at_str)
        except (ValueError, TypeError):
            # Skip un-parseable rows (data quality concern, not watchdog's job)
            continue

        age_hours = (now - created_at).total_seconds() / 3600.0
        expires_in_hours = (expires_at - now).total_seconds() / 3600.0

        if age_hours > WARN_THRESHOLD_HOURS and expires_in_hours > 0:
            aging.append({
                "proposal_id": proposal_id,
                "symbol": symbol,
                "asset_class": asset_class,
                "age_hours": round(age_hours, 1),
                "expires_in_hours": round(expires_in_hours, 1),
            })

    # Sort by most-urgent-first (smallest expires_in_hours)
    aging.sort(key=lambda x: x["expires_in_hours"])
    return aging


def render_alert(aging: list[dict]) -> str:
    """Render markdown alert. Empty input → empty output (silent)."""
    if not aging:
        return ""

    lines = [
        f"⏰ **Proposals TTL Watchdog** — {len(aging)} proposal(s) approaching expiry",
        "",
        "| Proposal ID | Symbol | Asset | Age (hrs) | Expires in (hrs) |",
        "|---|---|---|---|---|",
    ]
    for p in aging:
        lines.append(
            f"| `{p['proposal_id']}` | {p['symbol']} | {p['asset_class']} | "
            f"{p['age_hours']:.1f} | **{p['expires_in_hours']:.1f}** |"
        )
    lines.append("")
    lines.append("Approve via: `approve <PROPOSAL_ID>` (NOT ticker — id only).")
    lines.append("Or let them expire if rejected.")

    return "\n".join(lines)


def main() -> int:
    aging = find_aging_proposals()
    if aging:
        print(render_alert(aging))
    # else: silent (silence-by-default per ADR-0031)
    return 0


if __name__ == "__main__":
    sys.exit(main())
