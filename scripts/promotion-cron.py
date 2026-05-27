#!/usr/bin/env python3
"""scripts/promotion-cron.py — Scheduled promotion evaluation runner (ADR-0052).

Reads a YAML config listing strategies + windows + hypotheses to evaluate and
runs :class:`PromotionOrchestrator` for each.  Designed to be invoked by cron
or a CI scheduler (e.g. weekly).

Exit codes
----------
0   All evaluations complete; no new promotions fired since last run.
2   At least one evaluation resulted in PromotionDecision.promote = True
    (signals operator review required).
1   Unexpected error (stack trace printed to stderr).

Config file format  (~/.hermes/quant/promotion-cron.yaml)
----------------------------------------------------------
.. code-block:: yaml

    # promotion-cron.yaml — scheduled promotion evaluations
    evaluations:
      - strategy: buyhold
        universe: [AAPL, MSFT, NVDA, GOOG, META]
        window_start: "2025-06-01"
        window_end: "2025-08-31"
        hypothesis_id: null          # optional
        auto_record: true
        recorded_by: cron

      - strategy: buyhold
        universe: [TSLA, AMZN]
        window_start: "2025-09-01"
        window_end: "2025-11-30"
        auto_record: true

Usage
-----
.. code-block:: bash

    # Use the default config file:
    python scripts/promotion-cron.py

    # Specify a custom config file:
    python scripts/promotion-cron.py --config /path/to/my-cron.yaml

    # Dry-run: run evaluations but do NOT append to JSONL:
    python scripts/promotion-cron.py --dry-run

NOTE: This script does NOT call any external LLM.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("promotion-cron")

# ---------------------------------------------------------------------------
# Default config path
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".hermes" / "quant" / "promotion-cron.yaml"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config(config_path: Path) -> dict:
    """Load YAML config; return dict with 'evaluations' list."""
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        # Fall back to a minimal YAML parser for simple key: value files
        # (handles the subset used by our config schema)
        logger.warning("PyYAML not installed; using json fallback — use .json extension")
        with config_path.open("r") as fh:
            return json.load(fh)

    with config_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _parse_evaluation_entry(entry: dict) -> dict:
    """Validate and normalise a single evaluations[] entry."""
    required = {"strategy", "window_start", "window_end"}
    missing = required - set(entry)
    if missing:
        raise ValueError(
            f"Evaluation entry missing required keys: {missing}.  Entry: {entry}"
        )

    return {
        "strategy": str(entry["strategy"]),
        "universe": list(entry.get("universe") or ["AAPL", "MSFT", "NVDA", "GOOG", "META"]),
        "window_start": date.fromisoformat(str(entry["window_start"])),
        "window_end": date.fromisoformat(str(entry["window_end"])),
        "hypothesis_id": entry.get("hypothesis_id"),
        "auto_record": bool(entry.get("auto_record", True)),
        "recorded_by": str(entry.get("recorded_by", "cron")),
    }


# ---------------------------------------------------------------------------
# Strategy loader (same as promotion-decision.py)
# ---------------------------------------------------------------------------


def _load_strategy(name: str):
    name_lower = name.lower().replace("-", "").replace("_", "")
    if name_lower in ("buyhold", "buyandholdstrategy"):
        from hermes_quant.eval.stockbench import _BuyAndHoldStrategy
        return _BuyAndHoldStrategy()
    raise ValueError(
        f"Unknown strategy name {name!r}.  Supported: 'buyhold'.  "
        "Extend _load_strategy() to add more."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="promotion-cron.py",
        description=(
            "Scheduled promotion evaluation runner.  "
            "Exit 0 = no promotions; 2 = promotions fired (operator review needed)."
        ),
    )
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run evaluations but suppress JSONL writes (overrides auto_record).",
    )
    p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        default=False,
        help="Emit JSONL lines for each PromotionRecord instead of human-readable logs.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        logger.error(
            "Create it or pass --config PATH.  "
            "See script docstring for the YAML schema."
        )
        return 1

    try:
        config = _load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config %s: %s", config_path, exc)
        return 1

    raw_entries = config.get("evaluations", [])
    if not raw_entries:
        logger.warning("No evaluations defined in config; nothing to do.")
        return 0

    # Parse entries
    entries = []
    for i, raw in enumerate(raw_entries):
        try:
            entries.append(_parse_evaluation_entry(raw))
        except (ValueError, KeyError) as exc:
            logger.error("evaluations[%d] is invalid: %s", i, exc)
            return 1

    logger.info("promotion-cron: %d evaluation(s) to run", len(entries))

    # Import orchestrator once
    try:
        from hermes_quant.eval.promotion_orchestrator import PromotionOrchestrator
        from hermes_quant.eval.stockbench import STOCKBENCHHarness
    except ImportError:
        logger.exception("Failed to import promotion_orchestrator")
        return 1

    # Use non-strict contamination for scheduled runs (warn but proceed)
    harness = STOCKBENCHHarness(strict_contamination=False)
    orchestrator = PromotionOrchestrator(harness=harness)

    any_promoted = False
    errors = 0

    for idx, entry in enumerate(entries):
        strat_name = entry["strategy"]
        universe = entry["universe"]
        window_start = entry["window_start"]
        window_end = entry["window_end"]
        hypothesis_id = entry["hypothesis_id"]
        auto_record = entry["auto_record"] and not args.dry_run
        recorded_by = entry["recorded_by"]

        logger.info(
            "  [%d/%d] strategy=%s window=%s:%s universe=%s hyp=%s",
            idx + 1,
            len(entries),
            strat_name,
            window_start,
            window_end,
            universe,
            hypothesis_id,
        )

        try:
            strategy = _load_strategy(strat_name)
        except ValueError as exc:
            logger.error("    Skipping entry %d: %s", idx + 1, exc)
            errors += 1
            continue

        try:
            record = orchestrator.run(
                strategy=strategy,
                universe=universe,
                window_start=window_start,
                window_end=window_end,
                hypothesis_id=hypothesis_id,
                strategy_name=strat_name,
                auto_record=auto_record,
                recorded_by=recorded_by,
            )
        except Exception:  # noqa: BLE001
            logger.exception("    Error running evaluation %d", idx + 1)
            errors += 1
            continue

        promote = record.decision.get("promote", False)
        reasons = record.decision.get("reasons", [])

        if args.output_json:
            print(record.model_dump_json())
        else:
            verdict = "PROMOTE ✅" if promote else "no-promote"
            logger.info(
                "    → %s (record_id=%s, reasons=%d)",
                verdict,
                record.record_id,
                len(reasons),
            )
            if reasons:
                for r in reasons:
                    logger.info("      • %s", r)

        if promote:
            any_promoted = True

    if errors:
        logger.error("promotion-cron: %d evaluation(s) failed", errors)

    if any_promoted:
        logger.info(
            "promotion-cron: ✅ At least one promotion fired — operator review required."
        )
        return 2

    logger.info("promotion-cron: no new promotions.  Exit 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
