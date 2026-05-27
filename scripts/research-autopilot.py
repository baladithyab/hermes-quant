#!/usr/bin/env python3
"""scripts/research-autopilot.py — Research autopilot CLI harness (ADR-0048).

Commands
--------
register   Register a new hypothesis upfront (before any backtest).
run        Execute a hypothesis through the full lifecycle.
status     Show current status of a hypothesis.
list       List all hypotheses filtered by status.

Usage examples
--------------
  # Register a hypothesis before running any backtest:
  python scripts/research-autopilot.py register \\
      --claim "Adding sentiment analyst increases Sharpe by >=0.10 over 6mo backtest" \\
      --null-hypothesis "Sentiment makes no difference (alpha <= 0)" \\
      --success-criteria "sharpe >= 0.10" "vs_buyhold_alpha > 0.0" \\
      --falsification-criteria "sharpe < 0.0" \\
      --duration-days 180 \\
      --ticker AAPL \\
      --author aria

  # Run the hypothesis (dry-run mode — zero LLM cost):
  python scripts/research-autopilot.py run \\
      --hypothesis-id hyp_AAPL_20250528_a1b2c3 \\
      --window 2025-01-01:2025-06-30 \\
      --universe AAPL MSFT GOOGL \\
      --dry-run

  # Check status:
  python scripts/research-autopilot.py status \\
      --hypothesis-id hyp_AAPL_20250528_a1b2c3

  # List all open hypotheses:
  python scripts/research-autopilot.py list --status open
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, UTC
from pathlib import Path


# ---------------------------------------------------------------------------
# Lazy imports (avoid heavy startup cost for --help)
# ---------------------------------------------------------------------------


def _get_registry():
    from hermes_quant.research.hypothesis import HypothesisRegistry
    return HypothesisRegistry()


def _get_run_card_log():
    from hermes_quant.research.run_card import RunCardLog
    return RunCardLog()


def _get_runner():
    from hermes_quant.research.orchestrator import HypothesisRunner
    return HypothesisRunner(registry=_get_registry(), run_card_log=_get_run_card_log())


# ---------------------------------------------------------------------------
# register command
# ---------------------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    from hermes_quant.research.hypothesis import Hypothesis, HypothesisRegistry

    registry = _get_registry()

    scope: dict = {}
    if args.ticker:
        scope["universe"] = [args.ticker.upper()]
    if args.universe:
        scope["universe"] = [t.upper() for t in args.universe]

    hyp = Hypothesis(
        hypothesis_id=args.hypothesis_id or "",
        author=args.author,
        claim=args.claim,
        null_hypothesis=args.null_hypothesis,
        success_criteria=args.success_criteria or [],
        falsification_criteria=args.falsification_criteria or [],
        experiment_design=args.experiment_design or "",
        duration_target_days=args.duration_days,
        scope=scope,
        related_adrs=args.related_adrs or [],
    )

    hyp_id = registry.register(hyp)
    print(f"Registered hypothesis: {hyp_id}")
    print(json.dumps(registry.read(hyp_id).model_dump(), indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from hermes_quant.research.orchestrator import HypothesisRunner, REQUIRED_METRIC_KEYS
    from hermes_quant.research.hypothesis import HypothesisRegistry
    from hermes_quant.research.run_card import RunCardLog

    registry = HypothesisRegistry()
    run_card_log = RunCardLog()
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)

    # Parse window
    try:
        window_start_str, window_end_str = args.window.split(":")
        window_start = date.fromisoformat(window_start_str)
        window_end = date.fromisoformat(window_end_str)
    except (ValueError, AttributeError):
        print(
            "ERROR: --window must be in format YYYY-MM-DD:YYYY-MM-DD",
            file=sys.stderr,
        )
        return 1

    universe = args.universe or ["SPY"]

    # Build a stub strategy for dry-run CLI usage
    def _cli_stub_strategy(
        universe: list[str],
        window_start: date,
        window_end: date,
        dry_run: bool = True,
    ) -> dict:
        """Stub strategy used when no real strategy module is provided via CLI."""
        from hermes_quant.backtest.stub_llm import StubLLMCommittee
        stub = StubLLMCommittee()
        # Return neutral-ish metrics so criteria evaluation runs
        return {
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "vs_buyhold_alpha": 0.0,
            "n_decisions": 0.0,
            "total_return": 0.0,
        }

    strategy_callable = _cli_stub_strategy
    if args.strategy_module:
        import importlib
        mod = importlib.import_module(args.strategy_module)
        strategy_callable = getattr(mod, args.strategy_fn or "run_strategy")

    card = runner.run(
        args.hypothesis_id,
        strategy=strategy_callable,
        universe=universe,
        window_start=window_start,
        window_end=window_end,
        dry_run=args.dry_run,
    )
    print(f"Run complete: {card.run_id}")
    print(f"Verdict: {card.verdict}")
    print("Metrics:")
    for k, v in card.metrics.items():
        print(f"  {k}: {v:.4f}")
    print("Reasons:")
    for r in card.verdict_reasons:
        print(f"  {r}")
    return 0


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    registry = _get_registry()
    hyp = registry.read(args.hypothesis_id)
    if hyp is None:
        print(f"ERROR: hypothesis {args.hypothesis_id!r} not found.", file=sys.stderr)
        return 1

    print(json.dumps(hyp.model_dump(), indent=2, default=str))

    # Also show linked run cards
    run_card_log = _get_run_card_log()
    cards = run_card_log.read_for_hypothesis(args.hypothesis_id)
    if cards:
        print(f"\nRun cards ({len(cards)}):")
        for card in cards:
            print(f"  {card.run_id}  verdict={card.verdict}  sharpe={card.metrics.get('sharpe', float('nan')):.4f}")
    return 0


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    registry = _get_registry()
    status_filter = args.status or "open"

    if status_filter == "open":
        hypotheses = list(registry.read_all_open())
    elif status_filter == "running":
        hypotheses = list(registry.read_all_running())
    elif status_filter in ("validated", "falsified", "abandoned"):
        hypotheses = [
            h for h in registry.read_all_resolved()
            if h.status == status_filter
        ]
    else:
        hypotheses = list(registry.read_all_resolved())

    if not hypotheses:
        print(f"No hypotheses with status={status_filter!r}.")
        return 0

    print(f"Hypotheses (status={status_filter!r}, count={len(hypotheses)}):")
    for h in hypotheses:
        print(f"  {h.hypothesis_id}  status={h.status}  author={h.author}")
        print(f"    claim: {h.claim[:80]}{'...' if len(h.claim) > 80 else ''}")
    return 0


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-autopilot",
        description="Hypothesis Registry + Run Card harness (ADR-0048).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- register ---
    p_reg = sub.add_parser("register", help="Register a new hypothesis.")
    p_reg.add_argument("--claim", required=True, help="Falsifiable claim (max 512 chars).")
    p_reg.add_argument(
        "--null-hypothesis", required=True, dest="null_hypothesis",
        help="Null hypothesis (max 512 chars)."
    )
    p_reg.add_argument(
        "--success-criteria", nargs="*", dest="success_criteria",
        help="One or more success criterion expressions, e.g. 'sharpe >= 0.5'."
    )
    p_reg.add_argument(
        "--falsification-criteria", nargs="*", dest="falsification_criteria",
        help="One or more falsification criterion expressions."
    )
    p_reg.add_argument(
        "--experiment-design", dest="experiment_design",
        default="",
        help="Walk-forward backtest description (max 2048 chars)."
    )
    p_reg.add_argument("--duration-days", type=int, default=90, dest="duration_days")
    p_reg.add_argument("--ticker", help="Primary ticker (used for ID generation).")
    p_reg.add_argument(
        "--universe", nargs="*",
        help="Ticker universe list (overrides --ticker for scope)."
    )
    p_reg.add_argument("--author", default="operator", help="Author name.")
    p_reg.add_argument("--hypothesis-id", dest="hypothesis_id", default="")
    p_reg.add_argument(
        "--related-adrs", nargs="*", dest="related_adrs",
        help="Related ADR identifiers, e.g. ADR-0044."
    )

    # --- run ---
    p_run = sub.add_parser("run", help="Execute a hypothesis through the full lifecycle.")
    p_run.add_argument("--hypothesis-id", dest="hypothesis_id", required=True)
    p_run.add_argument(
        "--window", required=True,
        help="Holdout window: YYYY-MM-DD:YYYY-MM-DD"
    )
    p_run.add_argument("--universe", nargs="*", help="Ticker list.")
    p_run.add_argument(
        "--dry-run", action="store_true", dest="dry_run", default=True,
        help="Use StubLLMCommittee (no LLM API calls). Default: True."
    )
    p_run.add_argument(
        "--no-dry-run", action="store_false", dest="dry_run",
        help="Use real strategy (requires LLM config)."
    )
    p_run.add_argument(
        "--strategy-module", dest="strategy_module", default="",
        help="Python module path for real strategy, e.g. myproject.strategies.sentiment."
    )
    p_run.add_argument(
        "--strategy-fn", dest="strategy_fn", default="run_strategy",
        help="Function name in strategy module. Default: run_strategy."
    )

    # --- status ---
    p_status = sub.add_parser("status", help="Show current status of a hypothesis.")
    p_status.add_argument("--hypothesis-id", dest="hypothesis_id", required=True)

    # --- list ---
    p_list = sub.add_parser("list", help="List hypotheses.")
    p_list.add_argument(
        "--status",
        choices=["open", "running", "validated", "falsified", "abandoned", "all"],
        default="open",
        help="Filter by status. Default: open."
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    command_map = {
        "register": cmd_register,
        "run": cmd_run,
        "status": cmd_status,
        "list": cmd_list,
    }
    fn = command_map.get(args.command)
    if fn is None:
        parser.print_help()
        return 1

    try:
        return fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
