#!/usr/bin/env python3
"""scripts/quant-status.py — unified `quant status` CLI (ADR-0059).

Read-only entry point that surfaces all six append-only event stores plus
state.db in a single view. See ``hermes_quant.cli.status`` for the
implementation. This script always exits 0 (read-only, never fails).
"""

from __future__ import annotations

import sys

from hermes_quant.cli.status import run_cli


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
