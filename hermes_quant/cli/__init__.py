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
import sys

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
    p_rec.add_argument("--json", action="store_true",
                       help="Print raw JSON instead of rich-formatted output")

    # Backtest
    p_bt = sub.add_parser("backtest", help="Run hermes-quant against historical bars")
    p_bt.add_argument("asset")
    p_bt.add_argument("--from", dest="date_from", required=True)
    p_bt.add_argument("--to", dest="date_to", required=True)
    p_bt.add_argument("--timeframe", default="1h")
    p_bt.add_argument("--analyst-set", default="default")

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
        result = json.loads(quant_recommend({
            "symbol": args.symbol,
            "asset_class": args.asset_class,
            "timeframe": args.timeframe,
            "lookback_bars": args.lookback,
            "include_lessons": not args.no_lessons,
            "as_of": args.as_of,
        }))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _pretty_print_recommend(result)
        return 0 if result.get("success") else 1

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
