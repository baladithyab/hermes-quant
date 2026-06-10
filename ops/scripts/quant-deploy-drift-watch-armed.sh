#!/bin/bash
# quant-deploy-drift-watch-armed.sh — no_agent cron wrapper for the deploy-drift watchdog.
#
# Runs the repo's drift-watch (which audits repo ops/scripts/ vs the live deployed
# ~/.hermes/scripts/). MUST run from the repo checkout — the audit resolves its
# repo-scripts dir relative to its own location. Silent when no dangerous drift;
# prints a verbatim alert (delivered by the no_agent cron) when a live script
# differs from or is missing from the repo.
set -euo pipefail
REPO=/mnt/e/CS/github/hermes-quant
VENV="$HOME/.hermes/hermes-agent/venv/bin/python3"
cd "$REPO"
exec "$VENV" ops/scripts/quant-deploy-drift-watch.py
