#!/usr/bin/env python3
"""scripts/shadow-replay-daily.py — Shadow Account daily replay harness.

Wave 8b / ADR-0049.

Usage
-----
    python scripts/shadow-replay-daily.py --from 2025-06-01 --to 2025-06-15

Reads governance audit_log.jsonl events in the specified date range, feeds
them through all 5 shadow accounts, and emits a ShadowComparisonReport as
JSON + a formatted summary table to stdout.

Environment variables
---------------------
HERMES_QUANT_SHADOW_PRICES_PATH
    Path to a JSON file containing cached prices.
    Format: {"AAPL": {"2025-06-01": 182.5, "2025-06-02": 183.0, ...}, ...}
    If not set, prices are resolved from the snapshot cache
    (~/.hermes/quant/prices_snapshot.json) if it exists, otherwise fills are
    skipped for tickers with missing prices.

HERMES_QUANT_SHADOW_REAL_PNL_PATH
    Path to a JSON file containing real portfolio P&L per date.
    Format: {"2025-06-01": 123.45, "2025-06-02": -50.0, ...}
    If not set, real_pnl defaults to 0.0.

HERMES_QUANT_SHADOW_DB_DIR
    Directory for shadow SQLite databases.
    Defaults to ~/.hermes/quant/shadow/.

EXIT CODES
----------
0   success
1   no events found in the date range
2   configuration error
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("shadow_replay")

# ---------------------------------------------------------------------------
# Path constants (mirrors governance/audit_log.py)
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
AUDIT_LOG_PATH = QUANT_HOME / "governance" / "audit_log.jsonl"
DEFAULT_PRICES_SNAPSHOT = QUANT_HOME / "prices_snapshot.json"
DEFAULT_SHADOW_DB_DIR = QUANT_HOME / "shadow"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Shadow Account daily replay — counterfactual backtest harness"
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        required=True,
        metavar="YYYY-MM-DD",
        help="Start date (inclusive)",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        required=True,
        metavar="YYYY-MM-DD",
        help="End date (inclusive)",
    )
    parser.add_argument(
        "--audit-log",
        dest="audit_log",
        metavar="PATH",
        help=f"Path to audit_log.jsonl (default: {AUDIT_LOG_PATH})",
    )
    parser.add_argument(
        "--prices",
        dest="prices_path",
        metavar="PATH",
        help="Path to prices JSON cache (overrides HERMES_QUANT_SHADOW_PRICES_PATH)",
    )
    parser.add_argument(
        "--real-pnl",
        dest="real_pnl_path",
        metavar="PATH",
        help="Path to real P&L JSON (overrides HERMES_QUANT_SHADOW_REAL_PNL_PATH)",
    )
    parser.add_argument(
        "--db-dir",
        dest="db_dir",
        metavar="PATH",
        help=f"Shadow DB directory (default: {DEFAULT_SHADOW_DB_DIR})",
    )
    parser.add_argument(
        "--json-out",
        dest="json_out",
        metavar="PATH",
        help="Write ShadowComparisonReport JSON to this path",
    )
    parser.add_argument(
        "--no-persist",
        dest="no_persist",
        action="store_true",
        help="Use in-memory temp DBs (do not persist shadow state)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------


def _load_prices(prices_path: Path | None) -> dict[str, dict[date, float]]:
    """Load price snapshot: {ticker: {date: price}}."""
    candidate = prices_path or Path(
        os.environ.get("HERMES_QUANT_SHADOW_PRICES_PATH", str(DEFAULT_PRICES_SNAPSHOT))
    )
    if not candidate.exists():
        logger.warning("No prices file found at %s — fills will be skipped for missing tickers", candidate)
        return {}
    try:
        raw = json.loads(candidate.read_text())
    except Exception as exc:
        logger.warning("Could not load prices from %s: %s", candidate, exc)
        return {}

    result: dict[str, dict[date, float]] = {}
    for ticker, date_prices in raw.items():
        result[ticker] = {}
        for date_str, price in date_prices.items():
            try:
                result[ticker][date.fromisoformat(date_str)] = float(price)
            except (ValueError, TypeError):
                pass
    logger.info("Loaded prices for %d tickers", len(result))
    return result


def _load_real_pnl(real_pnl_path: Path | None) -> dict[date, float]:
    """Load real P&L per date: {date: pnl}."""
    candidate = real_pnl_path or Path(
        os.environ.get("HERMES_QUANT_SHADOW_REAL_PNL_PATH", "")
    )
    if not candidate or not candidate.exists():
        logger.info("No real P&L file; defaulting all dates to 0.0")
        return {}
    try:
        raw = json.loads(candidate.read_text())
        return {date.fromisoformat(k): float(v) for k, v in raw.items()}
    except Exception as exc:
        logger.warning("Could not load real P&L: %s", exc)
        return {}


def _load_audit_events(
    audit_log_path: Path,
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Read audit_log.jsonl and filter to gate_approval events in [date_from, date_to]."""
    if not audit_log_path.exists():
        logger.error("Audit log not found: %s", audit_log_path)
        return []

    events: list[dict] = []
    with open(audit_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug("Skipping malformed line: %s", exc)
                continue

            if row.get("kind") != "gate_approval":
                continue

            asof_raw = row.get("asof", "")
            try:
                asof_dt = datetime.fromisoformat(asof_raw.replace("Z", "+00:00"))
                asof_d = asof_dt.date()
            except (ValueError, AttributeError):
                continue

            if date_from <= asof_d <= date_to:
                events.append(row)

    logger.info("Loaded %d gate_approval events in [%s, %s]", len(events), date_from, date_to)
    return events


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_summary_table(report) -> None:
    """Print a compact human-readable summary table to stdout."""
    print()
    print("=" * 72)
    print(f"  SHADOW ACCOUNT COMPARISON REPORT — {report.asof.isoformat()}")
    print("=" * 72)
    print(f"  Real P&L: ${report.real_pnl:+,.2f}")
    print()
    print(f"  {'RULE':<32}  {'SHADOW P&L':>12}  {'ALPHA':>12}  {'STATUS'}")
    print(f"  {'-' * 32}  {'-' * 12}  {'-' * 12}  {'-' * 8}")
    for rule_name, pnl in sorted(
        report.shadow_pnls.items(), key=lambda x: x[1], reverse=True
    ):
        alpha = pnl - report.real_pnl
        status = "✓ WINNER" if pnl > report.real_pnl else "✗ LOSER"
        print(f"  {rule_name:<32}  ${pnl:>+11,.2f}  ${alpha:>+11,.2f}  {status}")

    print()
    best_rule, best_alpha = report.biggest_alpha
    print(f"  Biggest alpha: {best_rule} (${best_alpha:+,.2f} vs real)")
    print(f"  Winners: {len(report.counterfactual_winners)}  "
          f"Losers: {len(report.counterfactual_losers)}")
    print()
    print("  POST-HOC RATIONALIZATION DEFENSE:")
    print("  Winners → production rule left alpha on the table.")
    print("  Losers  → production rule was not systematically wrong.")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        date_from = date.fromisoformat(args.date_from)
        date_to = date.fromisoformat(args.date_to)
    except ValueError as exc:
        logger.error("Invalid date format: %s", exc)
        return 2

    if date_from > date_to:
        logger.error("--from must be <= --to")
        return 2

    # Paths
    audit_log_path = Path(args.audit_log) if args.audit_log else AUDIT_LOG_PATH
    prices_path = Path(args.prices_path) if args.prices_path else None
    real_pnl_path = Path(args.real_pnl_path) if args.real_pnl_path else None
    db_dir = Path(args.db_dir) if args.db_dir else None

    # Load data
    events = _load_audit_events(audit_log_path, date_from, date_to)
    if not events:
        logger.warning("No gate_approval events found in [%s, %s]", date_from, date_to)
        return 1

    prices_by_ticker = _load_prices(prices_path)
    real_pnl_by_date = _load_real_pnl(real_pnl_path)

    # Build runner
    import tempfile
    from hermes_quant.shadow.rules import default_rules
    from hermes_quant.shadow.runner import ShadowAccountRunner

    if args.no_persist:
        _tmp = tempfile.mkdtemp(prefix="shadow_replay_")
        effective_db_dir = Path(_tmp)
        logger.info("--no-persist: using temp dir %s", effective_db_dir)
    else:
        effective_db_dir = db_dir or DEFAULT_SHADOW_DB_DIR

    runner = ShadowAccountRunner(
        rules=default_rules(),
        db_dir=effective_db_dir,
        initial_cash=100_000.0,
        cost_model_bps=10.0,
    )

    # Replay
    logger.info("Replaying %d events across %d shadow accounts…", len(events), len(runner.accounts))
    runner.replay_session(events, prices_by_ticker)

    # Mark to market at end date prices
    end_prices: dict[str, float] = {}
    for ticker, date_prices in prices_by_ticker.items():
        p = date_prices.get(date_to)
        if p is not None:
            end_prices[ticker] = p
    for acct in runner.accounts.values():
        acct.mark_to_market(end_prices)

    # Real P&L for the range (sum over all dates)
    total_real_pnl = sum(
        v for d, v in real_pnl_by_date.items() if date_from <= d <= date_to
    )

    # Compare
    report = runner.compare_to_real(real_pnl=total_real_pnl, asof=date_to)

    # Output
    _print_summary_table(report)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_dict(), indent=2))
        logger.info("Report written to %s", out_path)
    else:
        # Print JSON to stdout as well
        print(json.dumps(report.to_dict(), indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
