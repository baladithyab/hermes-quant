"""hermes_quant.cli — `hermes quant <subcommand>` control plane.

Per ADR-0007 + ADR-0009 §P1-11 (canonical CLI surface):

  hermes quant setup [PROFILE]                  # interactive
  hermes quant setup --use-profile PROFILE      # alias avoiding global --profile collision

  hermes quant start [--account ACCOUNT]
  hermes quant stop [--account ACCOUNT]
  hermes quant restart [--account ACCOUNT]
  hermes quant uninstall [--account ACCOUNT]
  hermes quant status [--account ACCOUNT] [--all]

  hermes quant resume <account> [<asset_class>] [<asset>] --reason TEXT
  hermes quant halt <account> [<asset_class>] [<asset>] --reason TEXT
  hermes quant emergency-stop [--account ACCOUNT]

  hermes quant signals [-n N] [--asset ASSET] [--follow]
  hermes quant show-views [-n N] [--asset ASSET] [--analyst NAME]
  hermes quant doctor [--fix] [--calibration]
  hermes quant logs [--follow] [-n N]

  hermes quant backtest <asset> --from DATE --to DATE [--timeframe TF] [--analyst-set NAME]
  hermes quant backtest-replay <run_id>
  hermes quant freqtrade-setup [--freqtrade-dir DIR]
  hermes quant freqtrade-backtest <signal_log> [--freqtrade-config PATH]

  hermes quant config edit
  hermes quant config show
  hermes quant config validate

v0.1.0 SCAFFOLD: lifecycle / backtest / freqtrade subcommands print
"NOT YET IMPLEMENTED — track v0.1.1 milestone"; status / signals / doctor /
config-show ARE wired to read-only state.
"""
from __future__ import annotations

import argparse
import json

PROFILES = ["conservative", "moderate", "aggressive"]


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    """Hermes calls this with a sub-parser. We add our own subcommands here."""
    sub = parser.add_subparsers(dest="quant_cmd", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Interactive setup wizard")
    p_setup.add_argument("profile_pos", nargs="?", choices=PROFILES, default=None,
                          metavar="PROFILE", help="Risk profile to apply")
    p_setup.add_argument("--use-profile", dest="use_profile", choices=PROFILES,
                          default=None, help="Risk profile (alias for positional)")

    # Daemon lifecycle
    for verb, helptext in [
        ("start",     "Start the hermes-quant daemon (systemd/launchd/tmux fallback)"),
        ("stop",      "Stop the daemon"),
        ("restart",   "Restart the daemon"),
        ("uninstall", "Remove the systemd/launchd unit"),
    ]:
        p = sub.add_parser(verb, help=helptext)
        p.add_argument("--account", default=None,
                       help="Account identifier (default: configured primary account)")

    # Status
    p_status = sub.add_parser("status", help="Show daemon status")
    p_status.add_argument("--account", default=None)
    p_status.add_argument("--all", action="store_true", help="Show all accounts")

    # Halt / resume
    p_resume = sub.add_parser("resume", help="Lift a halt (requires --reason)")
    p_resume.add_argument("account")
    p_resume.add_argument("asset_class", nargs="?", default="*")
    p_resume.add_argument("asset", nargs="?", default=None)
    p_resume.add_argument("--reason", required=True,
                           help="Why are you lifting this halt? (audit log)")

    p_halt = sub.add_parser("halt", help="Manually halt a scope")
    p_halt.add_argument("account")
    p_halt.add_argument("asset_class", nargs="?", default="*")
    p_halt.add_argument("asset", nargs="?", default=None)
    p_halt.add_argument("--reason", required=True)

    p_estop = sub.add_parser("emergency-stop",
                              help="Cancel all orders + create durable halt across all scopes")
    p_estop.add_argument("--account", default=None)

    # Information
    p_signals = sub.add_parser("signals", help="Show recent signals from the bus")
    p_signals.add_argument("-n", type=int, default=20)
    p_signals.add_argument("--asset", default=None)
    p_signals.add_argument("--follow", action="store_true",
                           help="Tail-follow the signal bus")

    p_views = sub.add_parser("show-views", help="Show analyst views for an asset")
    p_views.add_argument("--asset", required=True)
    p_views.add_argument("--analyst", default=None)
    p_views.add_argument("-n", type=int, default=10)

    p_doctor = sub.add_parser("doctor", help="Comprehensive health check")
    p_doctor.add_argument("--fix", action="store_true",
                           help="Attempt to fix issues found")
    p_doctor.add_argument("--calibration", action="store_true",
                           help="Include per-analyst calibration table")

    p_logs = sub.add_parser("logs", help="Show daemon logs")
    p_logs.add_argument("--follow", action="store_true")
    p_logs.add_argument("-n", type=int, default=100)

    # Advisor (chat-mode synchronous recommend, ADR-0014). No daemon required.
    p_rec = sub.add_parser(
        "recommend",
        help="Get a snapshot recommendation for a symbol (no daemon, ADR-0014)",
    )
    p_rec.add_argument("symbol")
    p_rec.add_argument("--asset-class", default="equity",
                       choices=["equity", "etf", "crypto", "fx"])
    p_rec.add_argument("--timeframe", default=None,
                       choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    p_rec.add_argument("--lookback", type=int, default=None,
                       help="Bars of history to fetch (default per timeframe)")
    p_rec.add_argument("--no-lessons", action="store_true",
                       help="Skip recent journal lesson retrieval (saves tokens)")
    p_rec.add_argument("--as-of", default=None,
                       help="ISO timestamp anchor for replay-mode (default: now)")
    p_rec.add_argument("--recipe-id", default=None,
                       help="PDR recipe id (e.g. btc-usdt-deliberative)")
    p_rec.add_argument("--semantic-packet-file", action="append", default=[],
                       help="Semantic packet artifact JSON to inject (repeatable)")
    p_rec.add_argument("--committee-turns-file", action="append", default=[],
                       help="Committee-turn artifact JSON to inject (repeatable)")
    p_rec.add_argument("--json", action="store_true",
                       help="Print raw JSON instead of rich-formatted output")

    # Recipes
    p_recipes = sub.add_parser("recipes", help="List/validate/example PDR recipes")
    recipes_sub = p_recipes.add_subparsers(dest="recipes_cmd", required=True)
    recipes_list = recipes_sub.add_parser("list", help="List built-in and user recipes")
    recipes_list.add_argument("--json", action="store_true")
    recipes_validate = recipes_sub.add_parser("validate", help="Validate one user recipe YAML")
    recipes_validate.add_argument("path")
    recipes_validate.add_argument("--json", action="store_true")
    recipes_example = recipes_sub.add_parser("example", help="Print an example user recipe YAML")
    recipes_example.add_argument("--output", default=None, help="Optional path to write YAML template")

    # Semantic perception artifacts
    p_sem = sub.add_parser("semantic-packet", help="Write/validate/list semantic perception artifacts")
    sem_sub = p_sem.add_subparsers(dest="semantic_cmd", required=True)
    sem_write = sem_sub.add_parser("write", help="Write a hashed semantic packet artifact")
    sem_write.add_argument("--asset", required=True)
    sem_write.add_argument("--horizon", default="1h")
    sem_write.add_argument("--stance", required=True, choices=["bullish", "bearish", "neutral"])
    sem_write.add_argument("--confidence", required=True, type=float)
    sem_write.add_argument("--magnitude", required=True, type=float)
    sem_write.add_argument("--summary", required=True)
    sem_write.add_argument("--source", action="append", default=[],
                           help="Source ref as type:ref or type:ref|title, repeatable")
    sem_write.add_argument("--model", default="hermes:manual")
    sem_write.add_argument("--as-of", default=None)
    sem_write.add_argument("--output-root", default=None)
    sem_write.add_argument("--json", action="store_true")
    sem_validate = sem_sub.add_parser("validate", help="Validate one semantic packet artifact")
    sem_validate.add_argument("path")
    sem_validate.add_argument("--asset", default=None)
    sem_validate.add_argument("--as-of", default=None)
    sem_validate.add_argument("--horizon", default=None)
    sem_validate.add_argument("--max-age-minutes", type=float, default=24 * 60)
    sem_validate.add_argument("--json", action="store_true")
    sem_list = sem_sub.add_parser("list", help="List semantic packet artifacts")
    sem_list.add_argument("--asset", default=None)
    sem_list.add_argument("--limit", type=int, default=20)
    sem_list.add_argument("--json", action="store_true")

    # Committee-turn artifacts
    p_committee = sub.add_parser("committee", help="Build/list deliberative committee-turn artifacts")
    committee_sub = p_committee.add_subparsers(dest="committee_cmd", required=True)
    committee_run = committee_sub.add_parser("run", help="Build committee_turns from semantic packet artifacts")
    committee_run.add_argument("--asset", required=True)
    committee_run.add_argument("--semantic-packet-file", action="append", required=True)
    committee_run.add_argument("--model", default="deterministic:semantic_packets")
    committee_run.add_argument("--as-of", default=None)
    committee_run.add_argument("--output-root", default=None)
    committee_run.add_argument("--json", action="store_true")
    committee_list = committee_sub.add_parser("list", help="List committee-turn artifacts")
    committee_list.add_argument("--asset", default=None)
    committee_list.add_argument("--limit", type=int, default=20)
    committee_list.add_argument("--json", action="store_true")
    committee_prompt = committee_sub.add_parser("prompt", help="Print a safe Hermes prompt for model-mixture committee turns")
    committee_prompt.add_argument("--asset", required=True)
    committee_prompt.add_argument("--semantic-packet-file", action="append", required=True)
    committee_prompt.add_argument("--models", default="", help="Comma-separated provider/model ids to use or simulate")
    committee_prompt.add_argument("--json", action="store_true")

    # Perception cron helper
    p_perception = sub.add_parser("perception", help="Autonomous semantic perception setup")
    perception_sub = p_perception.add_subparsers(dest="perception_cmd", required=True)
    perception_start = perception_sub.add_parser("start", help="Create a Hermes cron job that generates semantic packets")
    perception_start.add_argument("--asset", required=True)
    perception_start.add_argument("--horizon", default="1h")
    perception_start.add_argument("--cadence", default="1h")
    perception_start.add_argument("--sources", default="operator notes, major market news, exchange status, macro regime")
    perception_start.add_argument("--recipe-id", default="btc-usdt-deliberative")
    perception_start.add_argument("--dry-run", action="store_true")
    perception_start.add_argument("--json", action="store_true")
    perception_status = perception_sub.add_parser("status", help="Show semantic packet freshness for a recipe")
    perception_status.add_argument("--recipe-id", default="btc-usdt-deliberative")
    perception_status.add_argument("--packet-root", default=None)
    perception_status.add_argument("--json", action="store_true")

    # HITL React subcommands (ADR-0015)
    p_propose = sub.add_parser(
        "propose",
        help="Propose a trade for human approval (HITL mode, ADR-0015)",
    )
    p_propose.add_argument("symbol")
    p_propose.add_argument("--asset-class", default="equity",
                           choices=["equity", "etf", "crypto", "fx"])
    p_propose.add_argument("--timeframe", default=None,
                           choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    p_propose.add_argument("--ttl-minutes", type=int, default=15)
    p_propose.add_argument("--lookback", type=int, default=None)
    p_propose.add_argument("--as-of", default=None)
    p_propose.add_argument("--json", action="store_true")

    p_approve = sub.add_parser("approve",
                               help="Approve a pending proposal (paper React)")
    p_approve.add_argument("proposal_id")
    p_approve.add_argument("--size-override", type=float, default=None,
                           dest="size_override_pct",
                           help="Override the advisor's Kelly fraction "
                                "(signed; e.g. -0.03 = 3% short)")
    p_approve.add_argument("--json", action="store_true")

    p_reject = sub.add_parser("reject", help="Reject a pending proposal")
    p_reject.add_argument("proposal_id")
    p_reject.add_argument("--reason", required=True,
                          help="Why are you rejecting? Becomes a journal entry.")
    p_reject.add_argument("--json", action="store_true")

    p_pend = sub.add_parser("pending", help="List pending proposals")
    p_pend.add_argument("-n", type=int, default=20)
    p_pend.add_argument("--symbol", default=None)
    p_pend.add_argument("--json", action="store_true")

    p_lookup = sub.add_parser("proposal", help="Look up a proposal by id")
    p_lookup.add_argument("proposal_id")
    p_lookup.add_argument("--json", action="store_true")

    # Autonomous-mode subcommands (ADR-0016)
    p_auto = sub.add_parser(
        "autonomous",
        help="Autonomous mode: silence-bias-gated paper trading on a watchlist (ADR-0016)",
    )
    auto_sub = p_auto.add_subparsers(dest="autonomous_cmd", required=True)

    p_auto_tick = auto_sub.add_parser(
        "tick", help="Run a single autonomous tick (default: dry-run)",
    )
    p_auto_tick.add_argument("--no-dry-run", action="store_true",
                              help="ACTUALLY fire paper trades on FIRE. "
                                   "Default is dry-run for safety; this flag "
                                   "is what cron uses.")
    p_auto_tick.add_argument("--json", action="store_true")

    p_auto_status = auto_sub.add_parser(
        "status", help="Show autonomous-mode state",
    )
    p_auto_status.add_argument("--json", action="store_true")

    p_auto_start = auto_sub.add_parser(
        "start",
        help="Enable autonomous mode + create a Hermes cron job for the tick cadence",
    )
    p_auto_start.add_argument("--cadence", default="15m",
                              help="Cron schedule expression (default: 15m)")
    p_auto_start.add_argument("--watchlist", default=None,
                              help="Comma-separated SYMBOL[:asset_class[:timeframe]] "
                                   "entries; sets the watchlist and overrides existing")
    p_auto_start.add_argument(
        "--no-cron", action="store_true",
        help="Skip creating the Hermes cron job; just enable autonomous mode "
             "in config (the operator can run ticks manually or wire cron later)",
    )

    auto_sub.add_parser(
        "stop",
        help="Disable autonomous mode (set quant.pdr.mode=advise)",
    )

    p_auto_reset = auto_sub.add_parser(
        "reset",
        help="Reset the kill switch (re-enable autonomous mode after a trip)",
    )
    p_auto_reset.add_argument("--confirm", action="store_true", required=False,
                               help="Required to actually reset")

    p_auto_wl = auto_sub.add_parser(
        "watchlist", help="Manage the autonomous-mode watchlist",
    )
    wl_sub = p_auto_wl.add_subparsers(dest="watchlist_cmd", required=True)

    wl_add = wl_sub.add_parser("add", help="Add or update a watchlist entry")
    wl_add.add_argument("symbol")
    wl_add.add_argument("--asset-class", default="equity",
                        choices=["equity", "etf", "crypto", "fx"])
    wl_add.add_argument("--timeframe", default=None,
                        choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])

    wl_rm = wl_sub.add_parser("remove", help="Remove a watchlist entry")
    wl_rm.add_argument("symbol")
    wl_rm.add_argument("--asset-class", default=None,
                       choices=["equity", "etf", "crypto", "fx"])

    wl_list = wl_sub.add_parser("list", help="List watchlist entries")
    wl_list.add_argument("--json", action="store_true")

    # Backtest
    p_bt = sub.add_parser("backtest", help="Run hermes-quant against historical bars (ADR-0020)")
    p_bt.add_argument("--symbol", required=True,
                      help="Trading symbol (e.g. AAPL, BTC/USDT)")
    p_bt.add_argument("--asset-class", default="equity",
                      choices=["equity", "etf", "crypto", "fx"])
    p_bt.add_argument("--timeframe", default="1h",
                      choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    p_bt.add_argument("--bars-file",
                      help="Path to CSV/parquet with OHLCV bars (timestamp, open, high, low, close, volume)")
    p_bt.add_argument("--recipe-id", default=None,
                      help="PDR recipe id to select analyst/aggregator/risk-gate composition")
    p_bt.add_argument("--semantic-packet-file", action="append", default=[],
                      help="Semantic packet artifact JSON to inject into replay (repeatable)")
    p_bt.add_argument("--committee-turns-file", action="append", default=[],
                      help="Committee-turn artifact JSON to inject into replay (repeatable)")
    p_bt.add_argument("--provider", default=None,
                      help="Fetch provider when --bars-file omitted (e.g. ccxt:kraken, ccxt:coinbase, yfinance)")
    p_bt.add_argument("--no-cache", action="store_true",
                      help="Disable OHLCV file cache for provider fetches")
    p_bt.add_argument("--cache-root", default=None,
                      help="OHLCV cache root (default ~/.hermes/quant/cache)")
    p_bt.add_argument("--start", default=None,
                      help="Start date (ISO 8601); used when fetching via configured provider")
    p_bt.add_argument("--end", default=None,
                      help="End date (ISO 8601); used when fetching via configured provider")
    p_bt.add_argument("--initial-equity", type=float, default=10_000.0)
    p_bt.add_argument("--warmup-bars", type=int, default=60,
                      help="Bars consumed for analyst warmup before any decisions")
    p_bt.add_argument("--commission", type=float, default=0.001,
                      help="Per-trade commission as fraction (default 10bps)")
    p_bt.add_argument("--slippage", type=float, default=0.0005,
                      help="Per-trade slippage as fraction (default 5bps)")
    p_bt.add_argument("--settlement-horizon-bars", type=int, default=1,
                      help="Bars forward used to settle decisions into aggregator posteriors")
    p_bt.add_argument("--no-learn-from-fills", action="store_true",
                      help="Disable in-replay calibrator updates")
    p_bt.add_argument("--walk-forward", action="store_true",
                      help="Run purged walk-forward replay folds instead of one contiguous backtest")
    p_bt.add_argument("--n-splits", type=int, default=5,
                      help="Number of walk-forward folds (default 5)")
    p_bt.add_argument("--embargo-pct", type=float, default=0.01,
                      help="Train/validation embargo fraction for walk-forward")
    p_bt.add_argument("--train-pct", type=float, default=0.6,
                      help="Train fraction inside each walk-forward fold")
    p_bt.add_argument("--val-pct", type=float, default=0.2,
                      help="Validation fraction inside each walk-forward fold")
    p_bt.add_argument("--output-dir", default=None,
                      help="Directory to write report.md + result.json (default: ~/.hermes/quant/backtests/<run-id>/)")
    p_bt.add_argument("--json", action="store_true", help="Print JSON to stdout instead of report")

    p_btr = sub.add_parser("backtest-replay",
                            help="Replay a signal log through freqtrade backtester")
    p_btr.add_argument("run_id")

    # Freqtrade integration
    p_fts = sub.add_parser("freqtrade-setup",
                            help="Wire hermes-quant into a local freqtrade install")
    p_fts.add_argument("--freqtrade-dir", default=None,
                        help="Path to freqtrade install (default: auto-detect)")

    p_ftb = sub.add_parser("freqtrade-backtest",
                            help="Run freqtrade backtest against a signal log")
    p_ftb.add_argument("signal_log")
    p_ftb.add_argument("--freqtrade-config", default=None)

    # Config
    p_cfg = sub.add_parser("config", help="Configuration management")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)
    cfg_sub.add_parser("edit", help="Open config in $EDITOR")
    cfg_sub.add_parser("show", help="Print current config")
    cfg_sub.add_parser("validate", help="Validate config schema")


def dispatch(args: argparse.Namespace) -> int:
    """Hermes calls this after argparse. Return shell exit code."""
    cmd = getattr(args, "quant_cmd", None)
    if cmd is None:
        print("hermes quant: missing subcommand. Try `hermes quant --help`.")
        return 2

    # v0.1.0 SCAFFOLD — three commands work; rest are stubs
    if cmd == "status":
        from hermes_quant.tools import quant_status
        result = json.loads(quant_status({"account": args.account}))
        _pretty_print_status(result)
        return 0

    if cmd == "signals":
        from hermes_quant.tools import quant_show_signals
        result = json.loads(quant_show_signals({
            "n": args.n, "asset": args.asset,
        }))
        _pretty_print_signals(result)
        return 0

    if cmd == "show-views":
        from hermes_quant.tools import quant_show_views
        result = json.loads(quant_show_views({
            "asset": args.asset, "analyst": args.analyst, "n": args.n,
        }))
        print(json.dumps(result, indent=2, default=str))
        return 0

    if cmd == "doctor":
        from hermes_quant.tools import quant_doctor
        result = json.loads(quant_doctor({"calibration": args.calibration}))
        _pretty_print_doctor(result)
        return 0

    if cmd == "recommend":
        from hermes_quant.tools import quant_recommend
        semantic_packets, committee_turns = _load_perception_artifacts(
            getattr(args, "semantic_packet_file", []),
            getattr(args, "committee_turns_file", []),
        )
        result = json.loads(quant_recommend({
            "symbol": args.symbol,
            "asset_class": args.asset_class,
            "timeframe": args.timeframe,
            "lookback_bars": args.lookback,
            "include_lessons": not args.no_lessons,
            "as_of": args.as_of,
            "recipe_id": args.recipe_id,
            "semantic_packets": semantic_packets,
            "committee_turns": committee_turns,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_recommend(result)
        return 0 if result.get("success") else 1

    if cmd == "recipes":
        return _dispatch_recipes(args)

    if cmd == "semantic-packet":
        return _dispatch_semantic_packet(args)

    if cmd == "committee":
        return _dispatch_committee(args)

    if cmd == "perception":
        return _dispatch_perception(args)

    if cmd == "propose":
        from hermes_quant.tools import quant_propose
        result = json.loads(quant_propose({
            "symbol": args.symbol,
            "asset_class": args.asset_class,
            "timeframe": args.timeframe,
            "lookback_bars": args.lookback,
            "ttl_minutes": args.ttl_minutes,
            "as_of": args.as_of,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_propose(result)
        return 0 if result.get("success") else 1

    if cmd == "approve":
        from hermes_quant.tools import quant_approve
        result = json.loads(quant_approve({
            "proposal_id": args.proposal_id,
            "size_override_pct": args.size_override_pct,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_approve(result)
        return 0 if result.get("success") else 1

    if cmd == "reject":
        from hermes_quant.tools import quant_reject
        result = json.loads(quant_reject({
            "proposal_id": args.proposal_id,
            "reason": args.reason,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_reject(result)
        return 0 if result.get("success") else 1

    if cmd == "pending":
        from hermes_quant.tools import quant_pending
        result = json.loads(quant_pending({
            "limit": args.n, "symbol": args.symbol,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_pending(result)
        return 0 if result.get("success") else 1

    if cmd == "proposal":
        from hermes_quant.tools import quant_proposal
        result = json.loads(quant_proposal({
            "proposal_id": args.proposal_id,
        }))
        # always pretty-print as JSON for now (rich form deferred)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("success") else 1

    if cmd == "autonomous":
        return _dispatch_autonomous(args)

    if cmd == "backtest":
        return _dispatch_backtest(args)

    if cmd == "config" and args.config_action == "show":
        _show_config()
        return 0

    # v0.1.1: halt / resume / emergency-stop are wired
    if cmd == "halt":
        from hermes_quant.cli.halts import cmd_halt
        return cmd_halt(args)
    if cmd == "resume":
        from hermes_quant.cli.halts import cmd_resume
        return cmd_resume(args)
    if cmd == "emergency-stop":
        from hermes_quant.cli.halts import cmd_emergency_stop
        return cmd_emergency_stop(args)

    # Everything else: scaffold notice
    print(f"hermes quant {cmd}: NOT YET IMPLEMENTED in v0.1.0 scaffold.")
    print()
    print("v0.1.0 ships the architecture (8 ADRs + amendments + cross-family review),")
    print("the plugin scaffold (tools, slash command, CLI subcommand surface), and")
    print("the protocol contracts (MarketContext, AnalystView, AggregatedSignal, ...).")
    print()
    print("v0.1.1 (in progress) will add:")
    print("  - The systemd/launchd-managed daemon")
    print("  - Three baseline analysts (classical-TA, microstructure-lite, kronos-small)")
    print("  - BMA aggregator with isotonic calibration")
    print("  - The risk gate with v1+v2 review fixes baked in")
    print("  - yfinance + ccxt + alpaca data providers")
    print("  - The freqtrade consumer strategy")
    print()
    print("Track GitHub: https://github.com/baladithyab/hermes-quant")
    return 0


def _pretty_print_status(result: dict) -> None:
    if not result.get("success"):
        print(json.dumps(result, indent=2))
        return
    print(f"hermes-quant — daemon: {'running' if result['daemon_running'] else 'STOPPED'}")
    if result.get("daemon_pid"):
        print(f"  PID: {result['daemon_pid']}")
    print(f"  Quant home: {result['quant_home']}")
    print(f"  Signal bus: {'exists' if result['signal_bus_exists'] else 'NOT CREATED'}")
    if result.get("last_signal"):
        sig = result["last_signal"]
        print(f"  Last signal: {sig.get('asof', '?')} {sig.get('asset')} "
              f"dir={sig.get('direction')} conf={sig.get('confidence')}")
    if result.get("last_heartbeat"):
        hb = result["last_heartbeat"]
        print(f"  Last heartbeat: {hb.get('asof', '?')}")
    if "v0.1.0_state" in result:
        print(f"\n  STATE: {result['v0.1.0_state']}")


def _pretty_print_signals(result: dict) -> None:
    if not result.get("success"):
        print(json.dumps(result, indent=2))
        return
    sigs = result.get("signals", [])
    if not sigs:
        print("(no signals)")
        if "note" in result:
            print(f"\n{result['note']}")
        return
    for s in sigs:
        print(f"  {s.get('asof', '?')} {s.get('asset', '?'):20} "
              f"dir={s.get('direction', '?'):>2} "
              f"size={s.get('target_position_pct', 0):>+6.2%} "
              f"conf={s.get('confidence', 0):.2f}")


def _pretty_print_doctor(result: dict) -> None:
    if not result.get("success"):
        print(json.dumps(result, indent=2))
        return
    print("hermes-quant doctor")
    print("=" * 60)
    print()
    if "v0.1.0_state" in result:
        print(f"State: {result['v0.1.0_state']}")
        print()
    print("Core checks:")
    for k, v in result.get("checks", {}).items():
        marker = "✓" if v else "✗"
        print(f"  {marker} {k}: {v}")
    print()
    print("Optional libraries:")
    for lib, status in result.get("optional_libs", {}).items():
        marker = "✓" if "available" in str(status) else "✗"
        print(f"  {marker} {lib}: {status}")
    print()
    if "next_step" in result:
        print("Next steps:")
        for line in result["next_step"].splitlines():
            print(f"  {line}")


def _pretty_print_propose(result: dict) -> None:
    if not result.get("success"):
        print(f"hermes-quant propose: {result.get('error', 'unknown error')}")
        if result.get("message"):
            print(f"  {result['message']}")
        if result.get("error") == "mode_mismatch":
            print()
            print("To enable HITL mode, add to ~/.hermes/config.yaml:")
            print("  quant:")
            print("    pdr:")
            print("      mode: hitl")
        return
    pid = result.get("proposal_id")
    print(f"hermes-quant propose — proposal_id: {pid}")
    print(f"  state:       {result.get('state')}")
    print(f"  expires_at:  {result.get('expires_at')}")
    print()
    advisor = result.get("advisor_result") or {}
    if advisor:
        _pretty_print_recommend({"success": True, **advisor})
    print(f"\nNext: hermes quant approve {pid}")
    print(f"      hermes quant reject  {pid} --reason '...'")


def _pretty_print_approve(result: dict) -> None:
    if not result.get("success"):
        print(f"hermes-quant approve: {result.get('error')}")
        if result.get("message"):
            print(f"  {result['message']}")
        return
    print("hermes-quant approve — APPROVED")
    print(f"  proposal_id: {result.get('proposal_id')}")
    print(f"  state:       {result.get('state')}")
    print(f"  fill_size:   {result.get('fill_size_pct'):+.4f}")
    exe = result.get("execution") or {}
    print(f"  decision_price: {exe.get('decision_price', 0):.4f}")
    print(f"  fill_price:     {exe.get('fill_price', 0):.4f}  (paper)")
    print(f"  reactor:        {exe.get('reactor_name')}")


def _pretty_print_reject(result: dict) -> None:
    if not result.get("success"):
        print(f"hermes-quant reject: {result.get('error')}")
        if result.get("message"):
            print(f"  {result['message']}")
        return
    print("hermes-quant reject — REJECTED")
    print(f"  proposal_id: {result.get('proposal_id')}")
    print(f"  reason:      {result.get('rejection_reason')}")
    if result.get("calibrator_will_learn"):
        print("  → calibrator update queued (learn_from_rejections=true)")


def _pretty_print_pending(result: dict) -> None:
    if not result.get("success"):
        print(f"hermes-quant pending: {result.get('error')}")
        return
    proposals = result.get("proposals", [])
    if not proposals:
        print("(no pending proposals)")
        return
    print(f"hermes-quant pending — {result.get('count')} proposal(s)")
    print()
    for p in proposals:
        print(f"  {p['proposal_id']}")
        print(f"    {p['symbol']:12} {p['asset_class']:8} {p['timeframe']:5}  "
              f"expires {p['expires_at']}")
        rg = (p.get("advisor_result") or {}).get("risk_gate") or {}
        if rg.get("pass"):
            print(f"    {rg.get('recommended_action', '?')}, "
                  f"kelly={rg.get('kelly_fraction', 0):+.4f}")
        else:
            print(f"    GATED — {rg.get('gated_reason', 'unknown')}")
        print()


def _dispatch_recipes(args) -> int:
    from pathlib import Path

    from hermes_quant.recipes import example_user_recipe, list_recipes, recipe_from_mapping

    if args.recipes_cmd == "list":
        recipes = list_recipes()
        result = {
            "success": True,
            "count": len(recipes),
            "recipes": [{**r.to_dict(), "config_hash": r.config_hash} for r in recipes],
        }
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            for r in recipes:
                print(f"{r.id:24} {r.asset_class:7} {r.timeframe:4} {r.aggregator:24} {r.config_hash}")
        return 0

    if args.recipes_cmd == "validate":
        try:
            import yaml
            data = yaml.safe_load(Path(args.path).expanduser().read_text(encoding="utf-8")) or {}
            recipe = recipe_from_mapping(data)
            result = {"success": True, "recipe": recipe.to_dict(), "config_hash": recipe.config_hash}
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error": str(exc)}
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if result["success"]:
                print(f"✓ recipe valid: {result['recipe']['id']} ({result['config_hash']})")
            else:
                print(f"✗ recipe invalid: {result['error']}")
        return 0 if result["success"] else 1

    if args.recipes_cmd == "example":
        try:
            import yaml
        except ImportError:
            print("pyyaml is required")
            return 2
        text = yaml.safe_dump(example_user_recipe(), sort_keys=False)
        if args.output:
            out = Path(args.output).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"example recipe written: {out}")
        else:
            print(text)
        return 0

    return 2


def _parse_source_arg(raw: str) -> dict:
    """Parse source strings as type:ref or type:ref|title.

    The `|title` separator avoids breaking URLs, which naturally contain
    additional colons.
    """
    if ":" not in raw:
        ref, _, title = raw.partition("|")
        out = {"type": "note", "ref": ref}
    else:
        source_type, rest = raw.split(":", 1)
        ref, _, title = rest.partition("|")
        out = {"type": source_type, "ref": ref}
    if title:
        out["title"] = title
    return out


def _load_perception_artifacts(
    semantic_packet_files: list[str] | None,
    committee_turns_files: list[str] | None,
) -> tuple[list[dict], list[dict]]:
    semantic_packets = []
    committee_turns = []
    if semantic_packet_files:
        from hermes_quant.artifacts import load_semantic_packet
        for path in semantic_packet_files:
            semantic_packets.append(load_semantic_packet(path).to_dict())
    if committee_turns_files:
        from hermes_quant.artifacts import load_committee_turns
        for path in committee_turns_files:
            payload = load_committee_turns(path)
            committee_turns.extend(payload.get("turns") or [])
    return semantic_packets, committee_turns


def _dispatch_semantic_packet(args) -> int:
    from pathlib import Path

    import pandas as _pd

    from hermes_quant.artifacts import (
        list_semantic_packets,
        validate_semantic_packet_file,
        write_semantic_packet,
    )

    if args.semantic_cmd == "write":
        asof = args.as_of or _pd.Timestamp.now(tz="UTC").isoformat()
        payload = {
            "schema_version": 1,
            "asset": args.asset,
            "asof": asof,
            "horizon": args.horizon,
            "stance": args.stance,
            "confidence": args.confidence,
            "magnitude": args.magnitude,
            "summary": args.summary,
            "sources": [_parse_source_arg(src) for src in args.source],
            "model": args.model,
        }
        root = Path(args.output_root).expanduser() if args.output_root else None
        path, packet = write_semantic_packet(payload, root=root)
        result = {"success": True, "path": str(path), "packet": packet}
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"semantic packet written: {path}")
            print(f"  hash: {packet.get('packet_hash')}")
            print(f"  {packet.get('asset')} {packet.get('horizon')} {packet.get('stance')} conf={packet.get('confidence')}")
        return 0

    if args.semantic_cmd == "validate":
        result = validate_semantic_packet_file(
            args.path,
            asset=args.asset,
            asof=args.as_of,
            horizon=args.horizon,
            max_age_minutes=args.max_age_minutes,
        )
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            marker = "✓" if result.get("success") else "✗"
            print(f"{marker} {args.path}: {result.get('reason')}")
            print(f"  hash: {result.get('packet_hash')}")
        return 0 if result.get("success") else 1

    if args.semantic_cmd == "list":
        packets = list_semantic_packets(asset=args.asset, limit=args.limit)
        result = {"success": True, "packets": packets, "count": len(packets)}
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            for pkt in packets:
                print(f"{pkt['asof']} {pkt['asset']:14} {pkt['stance']:7} conf={pkt['confidence']:.2f} {pkt['path']}")
        return 0

    return 2


def _dispatch_committee(args) -> int:
    from pathlib import Path

    from hermes_quant.artifacts import (
        list_committee_turn_artifacts,
        load_semantic_packet,
    )
    from hermes_quant.committee_runner import run_committee_from_packets

    if args.committee_cmd == "run":
        packets = [load_semantic_packet(path).to_dict() for path in args.semantic_packet_file]
        root = Path(args.output_root).expanduser() if args.output_root else None
        path, payload = run_committee_from_packets(
            packets,
            asset=args.asset,
            asof=args.as_of,
            model=args.model,
            root=root,
        )
        result = {"success": True, "path": str(path), "artifact": payload}
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"committee turns written: {path}")
            print(f"  hash: {payload.get('turns_hash')}")
            print(f"  turns: {len(payload.get('turns', []))}")
        return 0

    if args.committee_cmd == "list":
        artifacts = list_committee_turn_artifacts(asset=args.asset, limit=args.limit)
        result = {"success": True, "artifacts": artifacts, "count": len(artifacts)}
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            for artifact in artifacts:
                print(f"{artifact['asof']} {artifact['asset']:14} turns={artifact['n_turns']} {artifact['path']}")
        return 0

    if args.committee_cmd == "prompt":
        from hermes_quant.committee_runner import build_model_mixture_prompt
        packets = [load_semantic_packet(path).to_dict() for path in args.semantic_packet_file]
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        prompt = build_model_mixture_prompt(packets, asset=args.asset, models=models)
        result = {"success": True, "prompt": prompt, "models": models}
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(prompt)
        return 0

    return 2


def _dispatch_perception(args) -> int:
    if args.perception_cmd == "start":
        return _perception_start(args)
    if args.perception_cmd == "status":
        return _perception_status(args)
    return 2


def _perception_status(args) -> int:
    from pathlib import Path

    from hermes_quant.artifacts import semantic_status_for_recipe
    from hermes_quant.recipes import get_recipe

    root = Path(args.packet_root).expanduser() if args.packet_root else None
    recipe = get_recipe(args.recipe_id)
    status = semantic_status_for_recipe(recipe, root=root)
    result = {"success": True, "status": status}
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"perception status — recipe={status['recipe_id']} hash={status['recipe_hash']}")
        for row in status["symbols"]:
            age = row["age_minutes"]
            age_s = "n/a" if age is None else f"{age:.1f}m"
            print(f"  {row['symbol']:14} {row['status']:7} age={age_s} max={row['max_age_minutes']:.0f}m")
            latest = row.get("latest_packet") or {}
            if latest:
                print(f"    {latest.get('stance')} conf={latest.get('confidence')} hash={latest.get('packet_hash')}")
    return 0


def _perception_start(args) -> int:
    import shutil
    import subprocess

    hermes_bin = shutil.which("hermes") or "hermes"
    prompt = (
        "You are running as an autonomous semantic-perception cron job for the "
        "Hermes Agent plugin hermes-quant. Research the requested market context, "
        "then write exactly one semantic packet using the CLI. Do not place trades. "
        "Use only public/non-secret sources. If evidence is mixed, choose neutral "
        "with low confidence.\n\n"
        f"Asset: {args.asset}\n"
        f"Horizon: {args.horizon}\n"
        f"Recipe: {args.recipe_id}\n"
        f"Sources to consult/summarize: {args.sources}\n\n"
        "After research, run a command like:\n"
        f"{hermes_bin} quant semantic-packet write --asset {args.asset!r} --horizon {args.horizon!r} "
        "--stance neutral --confidence 0.35 --magnitude 0.0 --summary '...' "
        "--source 'url:https://example.com|source title' --model hermes:cron\n"
        "Return the artifact path and hash."
    )
    if args.dry_run:
        result = {"success": True, "dry_run": True, "cadence": args.cadence, "prompt": prompt}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(prompt)
        return 0

    cmd = [
        hermes_bin,
        "cron",
        "create",
        args.cadence,
        prompt,
        "--name",
        f"hermes-quant-perception-{args.asset.replace('/', '_')}-{args.horizon}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {"success": False, "error": str(exc), "prompt": prompt}
        print(json.dumps(result, indent=2) if args.json else f"perception cron failed: {exc}")
        return 1
    result = {
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "prompt": prompt,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(proc.stdout.strip() or proc.stderr.strip())
    return 0 if proc.returncode == 0 else 1


def _dispatch_backtest(args) -> int:
    """Dispatch `hermes quant backtest --symbol X ...` (ADR-0020)."""
    import json as _json
    import uuid as _uuid
    from pathlib import Path as _Path

    import pandas as _pd

    from hermes_quant.backtest import replay, walk_forward_replay

    # Load bars
    if args.bars_file:
        bars = _load_bars_file(args.bars_file)
    else:
        bars = _fetch_bars_via_provider(
            symbol=args.symbol,
            asset_class=args.asset_class,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
            provider_spec=args.provider,
            use_cache=not args.no_cache,
            cache_root=args.cache_root,
        )
        if bars is None:
            print("backtest: --bars-file is required when no provider available")
            return 2

    # Run replay (single contiguous run or purged walk-forward folds)
    try:
        semantic_packets, committee_turns = _load_perception_artifacts(
            getattr(args, "semantic_packet_file", []),
            getattr(args, "committee_turns_file", []),
        )
        common_kwargs = dict(
            symbol=args.symbol,
            asset_class=args.asset_class,
            timeframe=args.timeframe,
            initial_equity=args.initial_equity,
            warmup_bars=args.warmup_bars,
            commission=args.commission,
            slippage=args.slippage,
            settlement_horizon_bars=args.settlement_horizon_bars,
            learn_from_fills=not args.no_learn_from_fills,
            recipe_id=args.recipe_id,
            semantic_packets=semantic_packets,
            committee_turns=committee_turns,
        )
        if args.walk_forward:
            result = walk_forward_replay(
                bars,
                n_splits=args.n_splits,
                embargo_pct=args.embargo_pct,
                train_pct=args.train_pct,
                val_pct=args.val_pct,
                **common_kwargs,
            )
        else:
            result = replay(bars, **common_kwargs)
    except ValueError as exc:
        print(f"backtest: {exc}")
        return 2

    # Determine output dir
    out_dir = (
        _Path(args.output_dir).expanduser()
        if args.output_dir else
        _Path.home() / ".hermes" / "quant" / "backtests"
        / f"{args.symbol.replace('/', '_')}-{args.timeframe}-{_uuid.uuid4().hex[:8]}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write artifacts
    (out_dir / "result.json").write_text(
        _json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8",
    )
    (out_dir / "report.md").write_text(
        result.to_markdown_report(), encoding="utf-8",
    )

    if args.walk_forward:
        # One decisions file per fold; fold summaries are already in result.json.
        for fold in result.folds:
            with open(out_dir / f"fold-{fold.fold}-decisions.jsonl", "w", encoding="utf-8") as f:
                for d in fold.result.decisions_summary:
                    f.write(_json.dumps(d, default=str) + "\n")
            eq_df = _pd.DataFrame({
                "equity": fold.result.equity_curve.values,
                "buy_hold_equity": fold.result.bh_equity_curve.values,
                "position": fold.result.positions.values,
            }, index=fold.result.equity_curve.index)
            eq_df.to_csv(out_dir / f"fold-{fold.fold}-equity_curve.csv")
    else:
        # Equity curve as CSV
        eq_df = _pd.DataFrame({
            "equity": result.equity_curve.values,
            "buy_hold_equity": result.bh_equity_curve.values,
            "position": result.positions.values,
        }, index=result.equity_curve.index)
        eq_df.to_csv(out_dir / "equity_curve.csv")

        # Decisions JSONL
        with open(out_dir / "decisions.jsonl", "w", encoding="utf-8") as f:
            for d in result.decisions_summary:
                f.write(_json.dumps(d, default=str) + "\n")

    # Output
    if args.json:
        print(_json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(result.to_markdown_report())
        print()
        print(f"Artifacts written to: {out_dir}")
        print("  result.json      — machine-readable BacktestResult/WalkForwardBacktestResult")
        print("  report.md        — operator-readable summary")
        if args.walk_forward:
            print("  fold-*-equity_curve.csv — per-fold equity / buy-hold / position")
            print("  fold-*-decisions.jsonl  — per-fold advisor result snapshots")
        else:
            print("  equity_curve.csv — per-bar equity / buy-hold / position")
            print("  decisions.jsonl  — per-bar advisor result snapshots")

    return 0


def _load_bars_file(path: str):
    """Load bars from CSV or parquet."""
    from pathlib import Path as _Path

    import pandas as _pd

    p = _Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"bars file not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        bars = _pd.read_parquet(p)
    elif suffix == ".csv":
        bars = _pd.read_csv(p)
    else:
        raise ValueError(f"unsupported bars file extension: {suffix} (use .csv or .parquet)")

    # Validate required columns
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(
            f"bars file missing required columns: {missing}; "
            f"required: {required}"
        )
    return bars


def _fetch_bars_via_provider(
    *,
    symbol,
    asset_class,
    timeframe,
    start,
    end,
    provider_spec=None,
    use_cache=True,
    cache_root=None,
):
    """Fetch bars via yfinance/ccxt with optional read-through OHLCV cache.

    provider_spec examples:
      - None: default provider for asset class (ccxt default exchange for crypto)
      - "ccxt:kraken" / "ccxt:coinbase"
      - "yfinance"
    """
    from pathlib import Path as _Path

    import pandas as _pd

    # Use as_of=end if specified, else None (= now)
    as_of = _pd.Timestamp(end) if end else None
    if as_of is not None and as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")

    # Compute lookback_bars from start/end if provided
    lookback = 1000   # default
    if start and end:
        s = _pd.Timestamp(start)
        e = _pd.Timestamp(end)
        delta_seconds = (e - s).total_seconds()
        tf_seconds = {
            "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 14400, "1d": 86400,
        }.get(timeframe, 3600)
        # Small buffer only: provider implementations add their own pagination
        # / closed-bar slack. A large CLI buffer makes caches miss forever when
        # venues return slightly fewer bars than requested.
        lookback = max(int(delta_seconds / tf_seconds) + 5, 100)

    provider_spec = provider_spec or ("ccxt" if asset_class == "crypto" else "yfinance")
    provider_name = provider_spec

    if provider_spec.startswith("ccxt"):
        if asset_class != "crypto":
            return None
        try:
            from hermes_quant.data.ccxt_provider import CcxtProvider
        except Exception:   # noqa: BLE001
            return None
        parts = provider_spec.split(":", 1)
        exchange_id = parts[1] if len(parts) == 2 and parts[1] else "binance"
        provider = CcxtProvider(exchange_id=exchange_id)
        provider_name = f"ccxt:{exchange_id}"

        def fetch():
            return provider.fetch_bars(
                symbol=symbol,
                asset_class=asset_class,
                timeframe=timeframe,
                lookback_bars=lookback,
                as_of=as_of,
            )
    elif provider_spec == "yfinance":
        if asset_class not in {"equity", "etf"}:
            return None
        try:
            from hermes_quant.data.yfinance_provider import YFinanceProvider
        except Exception:   # noqa: BLE001
            return None
        provider = YFinanceProvider()
        provider_name = "yfinance"

        def fetch():
            return provider.fetch_bars(
                symbol=symbol,
                asset_class=asset_class,
                timeframe=timeframe,
                lookback_bars=lookback,
                as_of=as_of,
            )
    else:
        raise ValueError(
            f"unsupported --provider {provider_spec!r}; use ccxt:<exchange> or yfinance"
        )

    if not use_cache:
        return fetch()

    from hermes_quant.data.cache import cached_fetch
    root = _Path(cache_root).expanduser() if cache_root else None
    bars, meta = cached_fetch(
        fetch,
        provider=provider_name,
        symbol=symbol,
        timeframe=timeframe,
        lookback_bars=lookback,
        cache_root=root if root is not None else _Path.home() / ".hermes" / "quant" / "cache",
    )
    print(
        "backtest: OHLCV cache "
        f"{'hit' if meta.get('cache_hit') else 'miss'} "
        f"({meta.get('cache', {}).get('n_bars', len(bars))} cached bars)",
    )
    return bars


def _dispatch_autonomous(args) -> int:
    """Dispatch `hermes quant autonomous <subcommand>` (ADR-0016)."""
    sub = getattr(args, "autonomous_cmd", None)
    if sub is None:
        print("hermes quant autonomous: missing subcommand. "
              "Try `hermes quant autonomous --help`.")
        return 2

    if sub == "tick":
        from hermes_quant.tools import quant_autonomous_tick
        result = json.loads(quant_autonomous_tick({
            "dry_run": not args.no_dry_run,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_autonomous_tick(result)
        return 0 if result.get("success") else 1

    if sub == "status":
        from hermes_quant.tools import quant_autonomous_status
        result = json.loads(quant_autonomous_status({}))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_autonomous_status(result)
        return 0 if result.get("success") else 1

    if sub == "start":
        return _autonomous_start(
            cadence=args.cadence, watchlist_str=args.watchlist,
            no_cron=args.no_cron,
        )

    if sub == "stop":
        return _autonomous_stop()

    if sub == "reset":
        if not args.confirm:
            print("hermes quant autonomous reset: --confirm is required.")
            print("This re-enables autonomous mode after a kill-switch trip.")
            return 2
        from hermes_quant.autonomous import reset_kill_switch
        cleared = reset_kill_switch()
        print(f"kill switch reset: {cleared}")
        print("Run `hermes quant autonomous status` to verify.")
        return 0

    if sub == "watchlist":
        return _dispatch_watchlist(args)

    print(f"hermes quant autonomous: unknown subcommand {sub!r}")
    return 2


def _dispatch_watchlist(args) -> int:
    sub = getattr(args, "watchlist_cmd", None)
    if sub == "add":
        from hermes_quant.tools import quant_watchlist_add
        result = json.loads(quant_watchlist_add({
            "symbol": args.symbol,
            "asset_class": args.asset_class,
            "timeframe": args.timeframe,
        }))
        if result.get("success"):
            entry = result["added"]
            print(f"added: {entry['symbol']} ({entry['asset_class']}, "
                  f"{entry['timeframe']})")
            return 0
        print(f"watchlist add failed: {result.get('error')}: "
              f"{result.get('message', '')}")
        return 1

    if sub == "remove":
        from hermes_quant.tools import quant_watchlist_remove
        result = json.loads(quant_watchlist_remove({
            "symbol": args.symbol,
            "asset_class": args.asset_class,
        }))
        if result.get("success"):
            if result["removed"]:
                print(f"removed: {args.symbol}")
            else:
                print(f"no entries matched: {args.symbol}")
            return 0
        print(f"watchlist remove failed: {result.get('error')}")
        return 1

    if sub == "list":
        from hermes_quant.tools import quant_watchlist_list
        result = json.loads(quant_watchlist_list({}))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return 0
        if not result.get("success"):
            print(f"watchlist list failed: {result.get('error')}")
            return 1
        entries = result.get("watchlist", [])
        if not entries:
            print("(watchlist empty)")
            return 0
        for e in entries:
            print(f"  {e['symbol']:14}  {e['asset_class']:8}  {e['timeframe']}")
        return 0

    print(f"watchlist: unknown subcommand {sub!r}")
    return 2


def _autonomous_start(
    *, cadence: str, watchlist_str: str | None, no_cron: bool = False,
) -> int:
    """Set quant.pdr.mode=autonomous + (optionally) write the watchlist
    + (optionally) create a Hermes cron job for the tick cadence (per
    ADR-0016 §D4 + V03-4).

    The cron job runs `hermes quant autonomous tick --no-dry-run --json`
    on the operator's chosen cadence. The tick stdout becomes the cron
    job's output, delivered via the operator's configured cron destination.
    """
    import os as _os
    from pathlib import Path as _Path
    try:
        import yaml as _yaml
    except ImportError:
        print("hermes quant autonomous start: pyyaml is required")
        return 2

    cfg_path = _Path.home() / ".hermes" / "config.yaml"
    if cfg_path.exists():
        cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    else:
        cfg = {}
    quant = cfg.setdefault("quant", {})
    pdr = quant.setdefault("pdr", {})
    pdr["mode"] = "autonomous"
    auto = quant.setdefault("autonomous", {})
    auto["cadence"] = cadence

    if watchlist_str:
        # Format: SYMBOL[:asset_class[:timeframe]],SYMBOL[:asset_class[:timeframe]],...
        entries: list[dict] = []
        for raw in watchlist_str.split(","):
            parts = raw.strip().split(":")
            if not parts or not parts[0]:
                continue
            entry = {
                "symbol": parts[0],
                "asset_class": parts[1] if len(parts) > 1 else "equity",
            }
            if len(parts) > 2:
                entry["timeframe"] = parts[2]
            entries.append(entry)
        if entries:
            auto["watchlist"] = entries

    # Atomic-rename write
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
        f.flush()
        _os.fsync(f.fileno())
    _os.replace(tmp, cfg_path)

    print(f"autonomous mode ENABLED (cadence={cadence})")
    if watchlist_str:
        print(f"  watchlist set: {watchlist_str}")
    else:
        print("  watchlist preserved from previous config")

    if no_cron:
        print()
        print("(skipped cron job creation per --no-cron)")
        print()
        print("To create the cron tick job manually, run:")
        print(f'  hermes cron create "{cadence}" \\\n'
              '    --script "$(which hermes) quant autonomous tick --no-dry-run --json" \\\n'
              '    --no-agent')
        print()
        print("Verify with: hermes quant autonomous status")
        return 0

    # Actually create the cron job
    cron_result = _create_autonomous_cron_job(cadence=cadence)
    print()
    if cron_result["created"]:
        print(f"✓ cron job created: {cron_result['summary']}")
        if cron_result.get("job_id"):
            print(f"  job_id: {cron_result['job_id']}")
    else:
        print(f"⚠ cron job creation skipped: {cron_result['reason']}")
        print()
        print("To create the cron tick job manually, run:")
        print(f'  hermes cron create "{cadence}" \\\n'
              '    --script "$(which hermes) quant autonomous tick --no-dry-run --json" \\\n'
              '    --no-agent')

    print()
    print("Verify with: hermes quant autonomous status")
    return 0


def _create_autonomous_cron_job(*, cadence: str) -> dict:
    """Shell out to `hermes cron create` to wire the autonomous tick job.

    Returns a dict with:
      created: bool
      reason: str (when created=False)
      summary: str (when created=True; one-line description)
      job_id: str | None (cron-system-assigned ID, when extractable)
    """
    import shutil
    import subprocess

    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        return {
            "created": False,
            "reason": "`hermes` CLI not found on PATH",
        }

    script = (f"{hermes_bin} quant autonomous tick "
              f"--no-dry-run --json")

    cmd = [
        hermes_bin, "cron", "create", cadence,
        "--script", script,
        "--no-agent",
        "--name", f"hermes-quant-autonomous-{cadence}",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "created": False,
            "reason": f"hermes cron create failed: {exc}",
        }

    if result.returncode != 0:
        return {
            "created": False,
            "reason": (
                f"`hermes cron create` exited {result.returncode}. "
                f"stderr: {result.stderr.strip()[:200]}"
            ),
        }

    # Parse "Created cron job <id>" from stdout if present
    job_id = None
    for line in result.stdout.splitlines():
        if "job_id" in line.lower() or "created" in line.lower():
            # Extract anything that looks like an ID
            import re
            m = re.search(r"\b([a-f0-9]{6,}|[A-Z0-9_-]{4,})\b", line)
            if m:
                job_id = m.group(1)
                break

    return {
        "created": True,
        "summary": f"every {cadence} -> hermes quant autonomous tick",
        "job_id": job_id,
    }


def _autonomous_stop() -> int:
    """Set quant.pdr.mode=advise — autonomous tick will refuse to fire."""
    import os as _os
    from pathlib import Path as _Path
    try:
        import yaml as _yaml
    except ImportError:
        print("hermes quant autonomous stop: pyyaml is required")
        return 2

    cfg_path = _Path.home() / ".hermes" / "config.yaml"
    if not cfg_path.exists():
        print("(no config.yaml — nothing to stop)")
        return 0
    cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("quant", {}).setdefault("pdr", {})["mode"] = "advise"

    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
        f.flush()
        _os.fsync(f.fileno())
    _os.replace(tmp, cfg_path)

    print("autonomous mode DISABLED (mode set to 'advise')")
    print("Existing cron tick job (if any) is NOT deleted automatically; ")
    print("the tick will return mode_mismatch and no-op until you re-enable.")
    print("To remove the cron job: hermes cron list, then hermes cron remove <id>")
    return 0


def _pretty_print_autonomous_tick(result: dict) -> None:
    if not result.get("success"):
        print(f"autonomous tick: {result.get('error')}")
        if result.get("message"):
            print(f"  {result['message']}")
        if result.get("error") == "mode_mismatch":
            print()
            print("Enable autonomous mode with:")
            print("  hermes quant autonomous start --cadence 15m \\")
            print("    --watchlist AAPL:equity,BTC/USDT:crypto:1h")
        return
    if result.get("kill_switch_tripped"):
        print(f"⚠ KILL SWITCH TRIPPED — {result.get('message')}")
        ks = result.get("kill_switch", {})
        print(f"  pnl: {ks.get('cumulative_pnl_pct'):+.2%}")
        print(f"  threshold: -{ks.get('threshold_pct'):.0%}")
        print(f"  tripped_at: {ks.get('tripped_at')}")
        return
    asof = result.get("asof", "?")
    fires = result.get("fires", 0)
    silences = result.get("silences", 0)
    errors = result.get("errors", 0)
    n = result.get("watchlist_size", 0)
    dry = " (DRY RUN)" if result.get("dry_run") else ""
    print(f"autonomous tick @ {asof}{dry}")
    print(f"  watchlist: {n} symbols  →  fires: {fires}, silences: {silences}, errors: {errors}")
    print()
    for d in result.get("decisions", []):
        gate = d.get("gate", "?")
        sym = d.get("symbol", "?")
        if gate == "FIRE":
            action = d.get("action", {})
            tgt = action.get("target_position_pct", 0.0)
            dir_word = {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(
                action.get("direction"), "?")
            exec_id = d.get("execution_id", "(dry-run)")
            print(f"  ✓ {sym:14}  FIRE  {dir_word:5}  size={tgt:+.4f}  exec={exec_id}")
        elif gate == "ERROR":
            print(f"  ✗ {sym:14}  ERROR  {d.get('error', '')}")
        else:
            details = d.get("details", {})
            tag = gate.replace("SILENCE_", "")
            extra = ""
            if "confidence" in details and "min_required" in details:
                extra = f" (conf={details['confidence']:.2f} < {details['min_required']:.2f})"
            elif "urgency" in details:
                extra = f" (urgency={details['urgency']:.2f})"
            elif "emitted" in details:
                extra = f" ({details['emitted']}/{details['min_required']} voices)"
            print(f"  · {sym:14}  silence  {tag}{extra}")


def _pretty_print_autonomous_status(result: dict) -> None:
    if not result.get("success"):
        print(f"autonomous status: {result.get('error')}")
        return
    print(f"autonomous mode: {result.get('mode')}")
    print()
    wl = result.get("watchlist", [])
    print(f"watchlist ({len(wl)} symbols):")
    for e in wl:
        print(f"  {e['symbol']:14}  {e['asset_class']:8}  {e['timeframe']}")
    print()
    cfg = result.get("silence_bias_config", {})
    print("silence-bias gate config:")
    print(f"  min_confidence:        {cfg.get('min_confidence', 0):.2f}")
    print(f"  min_urgency:           {cfg.get('min_urgency', 0):.2f}")
    print(f"  min_analysts_emitted:  {cfg.get('min_analysts_emitted', 0)}")
    print(f"  max_recent_rejections: {cfg.get('max_recent_rejections', 0)}")
    print(f"  salience_window:       {cfg.get('salience_window_hours', 0)}h")
    print()
    rails = result.get("safety_rails", {})
    print("safety rails:")
    print(f"  max_per_tick_opens:        {rails.get('max_per_tick_opens')}")
    print(f"  max_concurrent_positions:  {rails.get('max_concurrent_positions')}")
    print(f"  kill_switch_pct:           {rails.get('kill_switch_pct')}")
    print(f"  log_silences:              {rails.get('log_silences')}")
    print(f"  allow_live:                {rails.get('allow_live')}")
    print()
    ks = result.get("kill_switch", {})
    if ks.get("tripped"):
        print(f"⚠ KILL SWITCH TRIPPED at {ks.get('tripped_at')}")
        print(f"  pnl: {ks.get('cumulative_pnl_pct'):+.2%}")
        print(f"  reason: {ks.get('reason', '?')}")
        print("  reset with: hermes quant autonomous reset --confirm")
    else:
        print("kill switch: armed (not tripped)")


def _pretty_print_recommend(result: dict) -> None:
    """Rich-formatted output for `hermes quant recommend SYMBOL`."""
    if not result.get("success"):
        print(json.dumps(result, indent=2))
        return
    print(f"hermes-quant recommend — {result.get('symbol', '?')} "
          f"({result.get('asset_class', '?')}, {result.get('timeframe', '?')})")
    print("=" * 60)
    dq = result.get("data_quality", {})
    print(f"  as_of:       {result.get('as_of', '?')}")
    print(f"  bars:        {dq.get('bars_received', 0)}")
    age = dq.get("last_bar_age_minutes")
    if age is not None:
        print(f"  bar age:     {age:.1f} min")
    print()

    views = result.get("analyst_views") or []
    if views:
        print("Analyst views:")
        for v in views:
            d = {-1: "SHORT", 0: "FLAT", 1: "LONG"}.get(v.get("direction"), "?")
            print(f"  {v.get('analyst', '?'):20} {d:5}  "
                  f"conf={v.get('confidence', 0):.2f}  "
                  f"mag={v.get('magnitude', 0):+.4f}  "
                  f"horizon={v.get('horizon', '?')}")
        print()

    sig = result.get("aggregated_signal")
    if sig:
        d = {-1: "SHORT", 0: "FLAT", 1: "LONG"}.get(sig.get("direction"), "?")
        print(f"Aggregated:    {d:5}  "
              f"conf={sig.get('confidence', 0):.2f}  "
              f"mag={sig.get('magnitude', 0):+.4f}  "
              f"({sig.get('aggregator', '?')}, "
              f"{sig.get('n_components', 0)} components)")
    else:
        print("Aggregated:    (none — no analyst views)")
    print()

    gate = result.get("risk_gate") or {}
    if gate.get("pass"):
        print(f"Risk gate:     PASS  →  {gate.get('recommended_action', '?')}  "
              f"(kelly={gate.get('kelly_fraction', 0):+.4f})")
        if gate.get("reason"):
            print(f"  reason:      {gate['reason']}")
    else:
        print(f"Risk gate:     GATED — {gate.get('gated_reason', 'unknown')}")
    print()

    lessons = result.get("lessons") or []
    if lessons:
        print(f"Recent lessons ({len(lessons)}):")
        for lesson in lessons[:5]:
            when = lesson.get("when", "?")
            sym = lesson.get("symbol", "?")
            ref = (lesson.get("reflection") or "")[:80]
            print(f"  {when}  {sym}  {ref}")
        print()

    caveats = result.get("caveats") or []
    if caveats:
        print("Caveats:")
        for c in caveats:
            print(f"  • {c}")
        print()

    doctor = result.get("doctor") or {}
    if not doctor.get("data_provider_alive", True):
        print("⚠ data provider unavailable")
    errors = doctor.get("analyst_errors") or []
    if errors:
        print("Analyst errors:")
        for e in errors:
            print(f"  ! {e}")


def _show_config() -> None:
    from pathlib import Path

    import yaml
    cfg_path = Path.home() / ".hermes" / "config.yaml"
    if not cfg_path.exists():
        print(f"No config at {cfg_path}. Run `hermes quant setup` first.")
        return
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    quant_cfg = cfg.get("quant", {})
    print(yaml.safe_dump(quant_cfg, default_flow_style=False, sort_keys=True))
