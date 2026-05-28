#!/usr/bin/env python3
"""quant-daily-report — Markdown brief generator (ADR-0061).

Synthesizes today's hermes-quant events into a publishable report:
gate decisions, open positions, P&L proxy, last-24h reflections,
hypothesis status changes, factor verdicts by tier, pending proposals.

Read-only: never mutates state. Caller (cron, slash command, future
Telegram bot) handles delivery — this script just writes the report
and optionally prints it.

Usage
-----
    quant-daily-report                         # today, write to default path
    quant-daily-report --asof 2026-05-26       # historical
    quant-daily-report --format telegram       # MD-V2 truncated brief
    quant-daily-report --out -                 # print to stdout
    quant-daily-report --also-print            # write file AND print
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

# Re-exec under the hermes-agent venv if available — same convention as
# scripts/quant-daily-interim.py so cron jobs pick up the right interpreter.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--asof must be YYYY-MM-DD, got {s!r}"
        ) from exc


def _default_out_path(asof: date, quant_home: Path) -> Path:
    return quant_home / "reports" / f"{asof.isoformat()}.md"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="quant-daily-report",
        description="Generate the hermes-quant daily Markdown brief.",
    )
    p.add_argument(
        "--asof",
        type=_parse_date,
        default=None,
        help="Calendar date (UTC) to report on. Default: today.",
    )
    p.add_argument(
        "--quant-home",
        type=Path,
        default=None,
        help="Override ~/.hermes/quant root (mainly for tests).",
    )
    p.add_argument(
        "--format",
        choices=("markdown", "telegram", "json"),
        default="markdown",
        help="Output format. Default: markdown.",
    )
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help=(
            "Output path. '-' prints to stdout. Default: "
            "~/.hermes/quant/reports/{asof}.md (Markdown only; JSON/Telegram "
            "default to stdout)."
        ),
    )
    p.add_argument(
        "--also-print",
        action="store_true",
        help="When --out is a file, also echo the report to stdout.",
    )
    p.add_argument(
        "--account",
        type=str,
        default="paper-default",
        help="account_id to read positions/cash for. Default: paper-default.",
    )
    args = p.parse_args(argv)

    # Lazy import so --help works without the full quant tree.
    from hermes_quant.reporting.daily_report import (  # noqa: WPS433
        format_markdown,
        format_telegram,
        generate_daily_report,
    )

    asof = args.asof or datetime.now(UTC).date()
    quant_home = args.quant_home or (Path.home() / ".hermes" / "quant")

    report = generate_daily_report(
        asof=asof, quant_home=quant_home, account_id=args.account
    )

    if args.format == "markdown":
        rendered = format_markdown(report)
    elif args.format == "telegram":
        rendered = format_telegram(report, quant_home=quant_home)
    elif args.format == "json":
        d = asdict(report)
        d["date"] = report.date.isoformat()
        # gate_table asof is datetime — coerce.
        for row in d.get("gate_table", []):
            if isinstance(row.get("asof"), datetime):
                row["asof"] = row["asof"].isoformat()
        rendered = json.dumps(d, indent=2, default=str)
    else:
        raise AssertionError(f"unreachable format {args.format}")

    # Resolve output target
    if args.out is None:
        if args.format == "markdown":
            out_path: Path | None = _default_out_path(asof, quant_home)
        else:
            # JSON / Telegram default to stdout.
            out_path = None
    elif args.out == "-":
        out_path = None
    else:
        out_path = Path(args.out)

    if out_path is None:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        sys.stderr.write(f"daily report written to {out_path}\n")
        if args.also_print:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
