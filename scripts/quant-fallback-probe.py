#!/usr/bin/env python3
"""quant-fallback-probe — verify silence-by-default holds under LLM failure (ADR-0060).

Usage:
    quant-fallback-probe [--surface SURFACE] [--failure-mode MODE] [--format human|json]

Exit code:
    0 — all probes returned valid output (silence-by-default holds)
    1 — at least one probe produced invalid output (DO NOT activate v0.2 LLM in prod)

This script makes NO real network calls. All LLMCaller stubs are in-process Python.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from hermes_quant.observability.fallback_probe import (
    FAILURE_MODES,
    SURFACES,
    format_results_human,
    format_results_json,
    run_fallback_probe,
)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quant-fallback-probe",
        description="Verify silence-by-default fallback holds for v0.2 LLM-wired surfaces.",
    )
    parser.add_argument(
        "--surface",
        choices=list(SURFACES) + ["all"],
        default="all",
        help="Which surface to probe (default: all).",
    )
    parser.add_argument(
        "--failure-mode",
        choices=list(FAILURE_MODES) + ["all"],
        default="all",
        help="Which failure mode to inject (default: all).",
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format (default: human).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    surfaces = None if args.surface == "all" else [args.surface]
    failure_modes = None if args.failure_mode == "all" else [args.failure_mode]

    results = run_fallback_probe(surfaces=surfaces, failure_modes=failure_modes)

    if args.format == "json":
        print(format_results_json(results))
    else:
        print(format_results_human(results))

    # Exit 1 if any probe produced invalid output (silence-by-default broken)
    if any(not r.output_valid for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
