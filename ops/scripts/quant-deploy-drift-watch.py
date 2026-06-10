#!/usr/bin/env python3
"""quant-deploy-drift-watch.py — no_agent watchdog: alert on dangerous repo↔deployed drift.

THE recurrence guard for the 2026-06-09 incident: the advisor portfolio-cap gate
(ADR-0071, the fix for the 41.6x runaway) lived ONLY in the deployed
~/.hermes/scripts/quant-daily-interim.py for a week — never committed. A redeploy
from repo would have silently re-armed the runaway. The drift-audit tool
(ops/deploy/quant-deploy-audit.py) already existed but NOTHING ran it, so the gap
sat undetected.

This wraps that audit as a classic silence-by-default watchdog so the drift can
never again go unnoticed:
  - Runs the audit against the live deployed tree (which only exists on the box —
    CI can't see it, which is why this is a cron, not a CI check).
  - SILENT (empty stdout, exit 0) when there is no DANGEROUS drift.
  - Alerts (non-empty stdout) ONLY on the dangerous verdicts:
      DRIFT          — repo and deployed both exist but DIFFER (a live hotfix or a
                       repo change that never crossed over — the cap-gate case).
      DEPLOYED_ONLY  — a live script with NO repo copy at all (e.g. a safety guard
                       that exists only on the box — the same hazard class).
    REPO_ONLY_NEW is NOT alerted: a committed-but-not-yet-deployed script risks no
    live behavior; deploying it is a deliberate operator step, not a drift alarm.
  - Non-zero exit / exception → the cron's error path alerts (a broken watchdog
    must never fail silently).

Deploy: wire as a no_agent Hermes cron (stdout delivered verbatim; empty == silent).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the existing audit module by path (ops/deploy/quant-deploy-audit.py).
#
# IMPORTANT: this watchdog MUST be run from the repo checkout, NOT from a flat
# deployed copy. The audit tool resolves its repo-scripts dir relative to its own
# location (parents[2]/ops/scripts); run from anywhere else it sees zero repo files
# and false-flags every deployed script as DEPLOYED_ONLY. The cron therefore cds
# into the repo and runs THIS file in place (see the cron command in the PR).
_AUDIT_PATH = Path(__file__).resolve().parent.parent / "deploy" / "quant-deploy-audit.py"


def _load_audit():
    if not _AUDIT_PATH.is_file():
        raise RuntimeError(
            f"audit tool not found at {_AUDIT_PATH} — this watchdog must run from "
            f"the repo checkout (cd into the repo first), not a deployed copy."
        )
    spec = importlib.util.spec_from_file_location("quant_deploy_audit", _AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load audit module at {_AUDIT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Verdicts that mean live behavior may diverge from source control — alert on these.
DANGEROUS = ("DRIFT", "DEPLOYED_ONLY")


def main() -> int:
    deployed = Path.home() / ".hermes" / "scripts"
    if not deployed.is_dir():
        # No deployed tree (e.g. CI / fresh box) — nothing to compare, stay silent.
        return 0

    audit = _load_audit()
    report = audit.audit(deployed)
    dangerous = [
        r for r in report["files"] if r["verdict"] in DANGEROUS
    ]
    if not dangerous:
        return 0  # SILENT — no dangerous drift.

    # Build the verbatim alert (delivered by the no_agent cron).
    lines = [
        "⚠️ **Quant deploy drift detected** — live scripts diverge from repo source.",
        "",
        "Dangerous drift means a live trading/guard script differs from (or is",
        "missing from) `ops/scripts/`. This is the class that hid the ADR-0071",
        "cap-gate for a week. Reconcile **deployed → repo** (deployed is the",
        "behavioral source of truth); never blind-copy repo → deployed.",
        "",
    ]
    for r in dangerous:
        if r["verdict"] == "DRIFT":
            lines.append(
                f"  • DRIFT          `{r['script']}` "
                f"(repo={r['repo_lines']}L vs deployed={r['deployed_lines']}L)"
            )
        else:  # DEPLOYED_ONLY
            lines.append(
                f"  • DEPLOYED_ONLY  `{r['script']}` "
                f"(deployed={r['deployed_lines']}L, NO repo copy)"
            )
    lines.append("")
    lines.append("Audit: `python ops/deploy/quant-deploy-audit.py` (full per-file verdict).")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — a broken watchdog must alert, not die silently
        print(f"quant-deploy-drift-watch FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
