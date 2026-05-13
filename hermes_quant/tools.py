"""hermes_quant.tools — Read-only tool handlers (per ADR-0007).

Tools surface daemon state to the agent. They do NOT spawn the daemon,
mutate state, or place trades. Long-running operations (backtests, etc)
are CLI-only.

All handlers return JSON-serializable dicts (per Hermes plugin convention).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

QUANT_HOME = Path.home() / ".hermes" / "quant"
SIGNAL_BUS_PATH = QUANT_HOME / "signals.jsonl"
EXECUTION_BUS_PATH = QUANT_HOME / "executions.jsonl"
STATE_DB_PATH = QUANT_HOME / "state.db"


def _daemon_pid() -> int | None:
    """Try to find the running daemon's PID via the lock file."""
    lock_glob = list(QUANT_HOME.glob("daemon-*.lock"))
    if not lock_glob:
        return None
    try:
        content = lock_glob[0].read_text().strip()
        pid = int(content.split()[0])
        # Check liveness
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, FileNotFoundError, IndexError):
        return None


def _read_jsonl_tail(path: Path, n: int) -> list[dict]:
    """Read last N JSONL records from a bus file. Tolerates partial trailing line."""
    if not path.exists():
        return []
    # Memory-budget cap: read up to 1 MB from the tail
    size = path.stat().st_size
    chunk_size = min(size, 1_048_576)
    with open(path, "rb") as f:
        f.seek(max(0, size - chunk_size))
        chunk = f.read()
    # Find first complete line
    first_nl = chunk.find(b"\n")
    if first_nl < 0:
        return []
    chunk = chunk[first_nl + 1:]
    records = []
    for line in chunk.split(b"\n"):
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-n:]


def quant_status(args: dict, **_kwargs) -> str:
    """JSON-string return per Hermes tool convention."""
    account_filter = args.get("account")
    pid = _daemon_pid()
    daemon_running = pid is not None

    last_signal = None
    signal_count = 0
    if SIGNAL_BUS_PATH.exists():
        recent = _read_jsonl_tail(SIGNAL_BUS_PATH, 100)
        signal_count = len(recent)
        # Filter heartbeats for "last signal"
        non_heartbeat = [r for r in recent if r.get("type") != "heartbeat"]
        if non_heartbeat:
            last_signal = non_heartbeat[-1]

    last_heartbeat = None
    if SIGNAL_BUS_PATH.exists():
        recent = _read_jsonl_tail(SIGNAL_BUS_PATH, 50)
        heartbeats = [r for r in recent if r.get("type") == "heartbeat"]
        if heartbeats:
            last_heartbeat = heartbeats[-1]

    return json.dumps({
        "success": True,
        "daemon_running": daemon_running,
        "daemon_pid": pid,
        "quant_home": str(QUANT_HOME),
        "signal_bus_exists": SIGNAL_BUS_PATH.exists(),
        "signal_bus_size_bytes": SIGNAL_BUS_PATH.stat().st_size if SIGNAL_BUS_PATH.exists() else 0,
        "last_signal": last_signal,
        "last_heartbeat": last_heartbeat,
        "recent_signal_count": signal_count,
        "account_filter": account_filter,
        "v0.1.0_state": "scaffold — daemon not yet implemented; expect signals once `hermes quant start` is wired",
    }, default=str)


def quant_show_signals(args: dict, **_kwargs) -> str:
    n = int(args.get("n", 20))
    asset = args.get("asset")
    direction = args.get("direction", "any")

    if not SIGNAL_BUS_PATH.exists():
        return json.dumps({
            "success": True,
            "signals": [],
            "note": f"Signal bus does not exist yet at {SIGNAL_BUS_PATH}. "
                    "Daemon may not have started — try `hermes quant start`.",
        })

    records = _read_jsonl_tail(SIGNAL_BUS_PATH, n * 4)   # over-read to allow filtering
    # Filter heartbeats out by default
    records = [r for r in records if r.get("type") != "heartbeat"]
    if asset:
        records = [r for r in records if r.get("asset") == asset]
    if direction != "any":
        target_dir = {"long": 1, "short": -1, "flat": 0}.get(direction)
        if target_dir is not None:
            records = [r for r in records if r.get("direction") == target_dir]
    return json.dumps({
        "success": True,
        "signals": records[-n:],
        "count": len(records[-n:]),
    }, default=str)


def quant_show_views(args: dict, **_kwargs) -> str:
    asset = args["asset"]
    analyst = args.get("analyst")
    n = int(args.get("n", 10))

    if not SIGNAL_BUS_PATH.exists():
        return json.dumps({
            "success": True,
            "views": [],
            "note": "Signal bus does not exist yet. Daemon may not be running.",
        })

    # Views are nested in signals.components — extract them
    records = _read_jsonl_tail(SIGNAL_BUS_PATH, 200)
    views = []
    for rec in records:
        if rec.get("type") == "heartbeat":
            continue
        if rec.get("asset") != asset:
            continue
        for comp in rec.get("components", []):
            if analyst and comp.get("analyst") != analyst:
                continue
            views.append({**comp, "asof": rec.get("asof")})

    return json.dumps({
        "success": True,
        "asset": asset,
        "views": views[-n:],
        "count": len(views[-n:]),
    }, default=str)


def quant_doctor(args: dict, **_kwargs) -> str:
    """Comprehensive health check. Read-only."""
    include_calibration = args.get("calibration", False)
    pid = _daemon_pid()

    checks = {
        "quant_home_exists": QUANT_HOME.exists(),
        "signal_bus_exists": SIGNAL_BUS_PATH.exists(),
        "execution_bus_exists": EXECUTION_BUS_PATH.exists(),
        "state_db_exists": STATE_DB_PATH.exists(),
        "daemon_running": pid is not None,
        "daemon_pid": pid,
    }

    # Optional providers
    optional_libs = {}
    for lib in ["yfinance", "ccxt", "alpaca", "torch", "transformers",
                "huggingface_hub", "sklearn", "mlflow"]:
        try:
            __import__(lib if lib != "alpaca" else "alpaca.trading.client",
                       globals(), locals(), [], 0)
            optional_libs[lib] = "available"
        except ImportError:
            optional_libs[lib] = "missing (install via: pip install hermes-quant[<extra>])"

    # Torch + CUDA detail
    try:
        import torch
        optional_libs["torch_version"] = torch.__version__
        optional_libs["torch_cuda_available"] = torch.cuda.is_available()
    except ImportError:
        pass

    return json.dumps({
        "success": True,
        "v0.1.0_state": "scaffold — protocol locked, daemon not yet implemented",
        "checks": checks,
        "optional_libs": optional_libs,
        "include_calibration": include_calibration,
        "next_step": (
            "1. `hermes quant setup` to write config\n"
            "2. `hermes quant start` to launch daemon (NOT YET IMPLEMENTED in v0.1.0 scaffold)\n"
            "3. Track GitHub for v0.1.1 implementation drop"
        ),
    }, default=str)


def handle_quant_slash(args: list, **kwargs) -> str:
    """Slash-command multiplexer for /quant <subcommand>.

    Subcommands: status | signals [N] | views <asset> | doctor
    """
    sub = args[0] if args else "status"
    if sub == "status":
        return quant_status({}, **kwargs)
    if sub == "signals":
        n = int(args[1]) if len(args) > 1 else 20
        return quant_show_signals({"n": n}, **kwargs)
    if sub == "views":
        if len(args) < 2:
            return json.dumps({"success": False, "error": "/quant views <asset>"})
        return quant_show_views({"asset": args[1]}, **kwargs)
    if sub == "doctor":
        return quant_doctor({}, **kwargs)
    return json.dumps({
        "success": False,
        "error": f"unknown subcommand '{sub}'. Use: status | signals [N] | views <asset> | doctor",
    })
