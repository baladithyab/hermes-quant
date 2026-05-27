#!/usr/bin/env python3
"""Daily Alpaca universe scanner CLI.

Drop-in replacement for the hardcoded ~/.hermes/scripts/quant-universe-interim.txt.
Writes a JSON file at ~/.hermes/quant/universe/alpaca-daily.json containing
the top-N most-liquid US equities that are tradable + fractionable on Alpaca.

USAGE
-----
    ~/.hermes/scripts/quant-universe-scan.py

ENV
---
    ALPACA_API_KEY / ALPACA_API_SECRET   (preferred)
    or ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (legacy, sourced from
    ~/.hermes/secrets/alpaca.env)

CRON SUGGESTION
---------------
Run weekdays at 06:30 America/New_York (10:30 UTC during EDT, 11:30 UTC
during EST) so the universe is fresh well before the 09:30 ET open. Cron
in UTC:

    30 11 * * 1-5  source $HOME/.hermes/secrets/alpaca.env && \
        $HOME/.hermes/hermes-agent/venv/bin/python3 \
        $HOME/.hermes/scripts/quant-universe-scan.py \
        >> $HOME/.hermes/quant/universe/scan.log 2>&1

(Adjust the hour by -1 during EDT if you want a fixed wall-clock 06:30 ET.)

POSTURE
-------
READ-ONLY against the Alpaca paper API. No order flow. Silence-by-default:
on any failure, the existing universe file is left untouched (atomic write
via tempfile + os.replace).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Re-exec under the hermes-agent venv if needed (where hermes_quant is installed).
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

# Best-effort: if the secrets file exists and the env vars aren't set, source it.
SECRETS = Path.home() / ".hermes" / "secrets" / "alpaca.env"
if SECRETS.exists() and not (
    os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
):
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")  # bash strips shell quotes; Python doesn't
            os.environ.setdefault(k.strip(), v)

from hermes_quant.universe import scan_universe  # noqa: E402


def main() -> int:
    payload = scan_universe()
    print(payload["count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
