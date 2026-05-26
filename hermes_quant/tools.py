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
import time
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
    start_offset = max(0, size - chunk_size)
    with open(path, "rb") as f:
        f.seek(start_offset)
        chunk = f.read()
    # If we started mid-file, the first line is potentially partial — drop it.
    # If we read from offset 0, the first line is real and must be kept.
    if start_offset > 0:
        first_nl = chunk.find(b"\n")
        if first_nl < 0:
            return []
        chunk = chunk[first_nl + 1 :]
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

    return json.dumps(
        {
            "success": True,
            "daemon_running": daemon_running,
            "daemon_pid": pid,
            "quant_home": str(QUANT_HOME),
            "signal_bus_exists": SIGNAL_BUS_PATH.exists(),
            "signal_bus_size_bytes": SIGNAL_BUS_PATH.stat().st_size
            if SIGNAL_BUS_PATH.exists()
            else 0,
            "last_signal": last_signal,
            "last_heartbeat": last_heartbeat,
            "recent_signal_count": signal_count,
            "account_filter": account_filter,
            "v0.1.0_state": "scaffold — daemon not yet implemented; expect signals once `hermes quant start` is wired",
        },
        default=str,
    )


def quant_show_signals(args: dict, **_kwargs) -> str:
    n = int(args.get("n", 20))
    asset = args.get("asset")
    direction = args.get("direction", "any")

    if not SIGNAL_BUS_PATH.exists():
        return json.dumps(
            {
                "success": True,
                "signals": [],
                "note": f"Signal bus does not exist yet at {SIGNAL_BUS_PATH}. "
                "Daemon may not have started — try `hermes quant start`.",
            }
        )

    records = _read_jsonl_tail(SIGNAL_BUS_PATH, n * 4)  # over-read to allow filtering
    # Filter heartbeats out by default
    records = [r for r in records if r.get("type") != "heartbeat"]
    if asset:
        records = [r for r in records if r.get("asset") == asset]
    if direction != "any":
        target_dir = {"long": 1, "short": -1, "flat": 0}.get(direction)
        if target_dir is not None:
            records = [r for r in records if r.get("direction") == target_dir]
    return json.dumps(
        {
            "success": True,
            "signals": records[-n:],
            "count": len(records[-n:]),
        },
        default=str,
    )


def quant_show_views(args: dict, **_kwargs) -> str:
    asset = args["asset"]
    analyst = args.get("analyst")
    n = int(args.get("n", 10))

    if not SIGNAL_BUS_PATH.exists():
        return json.dumps(
            {
                "success": True,
                "views": [],
                "note": "Signal bus does not exist yet. Daemon may not be running.",
            }
        )

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

    return json.dumps(
        {
            "success": True,
            "asset": asset,
            "views": views[-n:],
            "count": len(views[-n:]),
        },
        default=str,
    )


def quant_recommend(args: dict, **_kwargs) -> str:
    """Synchronous advisor surface — runs analysts/aggregator/gate on a single
    symbol and returns a structured recommendation. Per ADR-0014.

    Read-only: does NOT mutate state.db, signals.jsonl, calibrators, or the
    journal write-side. Safe under no-data scenarios — returns a gated dict
    rather than raising.
    """
    symbol = args.get("symbol")
    if not symbol:
        return json.dumps(
            {
                "success": False,
                "error": "symbol is required",
            }
        )

    # Lazy import — advisor pulls in pandas + yfinance, which are heavy.
    # Keeping the import inside the handler means register-time cost stays
    # at ~50ms per the ADR-0007 budget.
    try:
        from hermes_quant.advisor import recommend
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_recommend: advisor import failed: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"advisor module unavailable: {exc}",
            }
        )

    try:
        result = recommend(
            symbol=symbol,
            asset_class=args.get("asset_class"),
            timeframe=args.get("timeframe"),
            lookback_bars=args.get("lookback_bars"),
            include_lessons=bool(args.get("include_lessons", True)),
            as_of=args.get("as_of"),
            recipe_id=args.get("recipe_id"),
            market_extras={
                "semantic_packets": args.get("semantic_packets", []),
                "committee_turns": args.get("committee_turns", []),
            },
        )
    except Exception as exc:  # noqa: BLE001 — advisor is best-effort
        logger.warning("quant_recommend: advisor.recommend raised: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"advisor failed: {exc}",
                "symbol": symbol,
            }
        )

    return json.dumps({"success": True, **result}, default=str)


def quant_recipes(args: dict, **_kwargs) -> str:
    """List available PDR recipes. Read-only."""
    try:
        from hermes_quant.recipes import list_recipes

        recipes = list_recipes()
        return json.dumps(
            {
                "success": True,
                "count": len(recipes),
                "recipes": [{**r.to_dict(), "config_hash": r.config_hash} for r in recipes],
            },
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"recipe listing failed: {exc}"})


def _read_pdr_mode() -> str:
    """Read quant.pdr.mode from ~/.hermes/config.yaml. Defaults to 'advise'.

    Per ADR-0015 §D7: the mode gate is read at every quant_propose call, NOT
    cached, so an operator can edit config + retry without a daemon restart.
    """
    try:
        import yaml
    except ImportError:
        return "advise"
    cfg_path = Path.home() / ".hermes" / "config.yaml"
    if not cfg_path.exists():
        return "advise"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return "advise"
    pdr = (cfg.get("quant") or {}).get("pdr") or {}
    mode = pdr.get("mode", "advise")
    return mode if mode in {"advise", "hitl", "autonomous"} else "advise"


def _read_learn_from_rejections() -> bool:
    """quant.calibration.learn_from_rejections (default True per ADR-0015 §D8)."""
    try:
        import yaml
    except ImportError:
        return True
    cfg_path = Path.home() / ".hermes" / "config.yaml"
    if not cfg_path.exists():
        return True
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return True
    cal = (cfg.get("quant") or {}).get("calibration") or {}
    return bool(cal.get("learn_from_rejections", True))


# ---------------------------------------------------------------------------
# HITL React tool handlers (ADR-0015)
# ---------------------------------------------------------------------------


def quant_propose(args: dict, **_kwargs) -> str:
    """Propose a trade for human approval (ADR-0015 §D4).

    Mode gate: only fires when quant.pdr.mode=hitl. Other modes return
    a mode_mismatch error so operators don't accidentally generate
    proposals in advise-only deployments.
    """
    symbol = args.get("symbol")
    if not symbol:
        return json.dumps({"success": False, "error": "symbol is required"})

    mode = _read_pdr_mode()
    if mode != "hitl":
        return json.dumps(
            {
                "success": False,
                "error": "mode_mismatch",
                "message": f"quant_propose requires quant.pdr.mode=hitl; "
                f"current mode={mode!r}. Set in ~/.hermes/config.yaml.",
                "current_mode": mode,
            }
        )

    try:
        from hermes_quant.advisor import recommend
        from hermes_quant.proposals import get_default_store
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_propose: import failed: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant import failed: {exc}",
            }
        )

    try:
        advisor_result = recommend(
            symbol=symbol,
            asset_class=args.get("asset_class", "equity"),
            timeframe=args.get("timeframe"),
            lookback_bars=args.get("lookback_bars"),
            include_lessons=True,
            as_of=args.get("as_of"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_propose: advisor failed: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"advisor failed: {exc}",
                "symbol": symbol,
            }
        )

    # Refuse to register a proposal that the advisor itself gated.
    # An operator approving a "no_bars_returned" proposal would be a footgun.
    rg = (advisor_result or {}).get("risk_gate") or {}
    if not rg.get("pass", False):
        return json.dumps(
            {
                "success": False,
                "error": "advisor_gated",
                "message": f"Advisor gated this proposal: {rg.get('gated_reason', 'unknown')}. "
                f"Use quant_recommend to inspect; no proposal registered.",
                "advisor_result": advisor_result,
            }
        )

    try:
        store = get_default_store()
        proposal = store.propose(
            symbol=symbol,
            asset_class=args.get("asset_class", "equity"),
            timeframe=advisor_result.get("timeframe", "1d"),
            advisor_result=advisor_result,
            ttl_minutes=int(args.get("ttl_minutes", 15)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_propose: store failed: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"proposal store failed: {exc}",
            }
        )

    return json.dumps(
        {
            "success": True,
            "proposal_id": proposal.proposal_id,
            "state": proposal.state,
            "expires_at": proposal.expires_at,
            "advisor_result": advisor_result,
            "next_steps": (
                f"Review the advisor view above. Approve with "
                f"quant_approve(proposal_id='{proposal.proposal_id}') or reject "
                f"with quant_reject(proposal_id='{proposal.proposal_id}', "
                f"reason='...'). Expires at {proposal.expires_at}."
            ),
        },
        default=str,
    )


def quant_approve(args: dict, **_kwargs) -> str:
    """Approve a pending proposal -> fires React adapter (ADR-0015 §D4)."""
    proposal_id = args.get("proposal_id")
    if not proposal_id:
        return json.dumps({"success": False, "error": "proposal_id is required"})

    size_override = args.get("size_override_pct")
    if size_override is not None:
        try:
            size_override = float(size_override)
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "success": False,
                    "error": "size_override_pct must be a number",
                }
            )

    try:
        from hermes_quant.proposals import (
            ProposalExpiredError,
            ProposalStateError,
            get_default_store,
        )
        from hermes_quant.react import PaperReactor
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant import failed: {exc}",
            }
        )

    store = get_default_store()
    try:
        proposal = store.get(proposal_id)
        if proposal is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "not_found",
                    "message": f"proposal {proposal_id} not found",
                }
            )
        if proposal.state != "pending":
            return json.dumps(
                {
                    "success": False,
                    "error": "state_mismatch",
                    "message": f"proposal {proposal_id} is in state "
                    f"{proposal.state!r}; cannot approve",
                    "proposal_state": proposal.state,
                }
            )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"lookup failed: {exc}",
            }
        )

    # Determine fill size: operator override > advisor's Kelly recommendation
    rg = (proposal.advisor_result or {}).get("risk_gate") or {}
    advisor_kelly = float(rg.get("kelly_fraction", 0.0))
    fill_size_pct = float(size_override) if size_override is not None else advisor_kelly

    if fill_size_pct == 0.0:
        return json.dumps(
            {
                "success": False,
                "error": "zero_fill_size",
                "message": "Fill size resolved to 0 (advisor gated and no override). "
                "Provide size_override_pct or reject the proposal instead.",
                "advisor_kelly": advisor_kelly,
            }
        )

    # Fire the paper reactor BEFORE state advance — if React fails, the
    # proposal stays pending and the operator can retry.
    reactor = PaperReactor()
    try:
        execution = reactor.execute(
            proposal,
            fill_size_pct=fill_size_pct,
            approver_user_id=_kwargs.get("user_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_approve: PaperReactor failed: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"react failed: {exc}",
                "proposal_id": proposal_id,
            }
        )

    # Now advance state machine. If this fails, we have a paper exec on the
    # bus without a corresponding approved proposal — we surface a warning
    # and keep going. The settlement loop reconciles via signal_id.
    try:
        from hermes_quant.react.paper import _record_to_dict

        approved = store.approve(
            proposal_id,
            approver_user_id=_kwargs.get("user_id"),
            size_override_pct=size_override,
            execution=_record_to_dict(execution),
        )
    except (ProposalExpiredError, ProposalStateError) as exc:
        return json.dumps(
            {
                "success": False,
                "error": "state_mismatch",
                "message": str(exc),
            }
        )

    # Append journal entry for the approval — completes the operator audit
    # trail. ADR-0010 §Wave-A integration; degrades silently if journal
    # writer not available (e.g. older deploy without the journal/ pkg).
    try:
        from hermes_quant.journal.writer import append_human_override

        append_human_override(approved, kind="approve", reason=_kwargs.get("approval_note"))
    except ImportError:
        logger.debug("quant_approve: journal writer not available; approval not journaled")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "quant_approve: journal append failed: %s",
            exc,
            exc_info=True,
        )

    return json.dumps(
        {
            "success": True,
            "proposal_id": proposal_id,
            "state": approved.state,
            "execution": _record_to_dict(execution),
            "fill_size_pct": fill_size_pct,
        },
        default=str,
    )


def quant_reject(args: dict, **_kwargs) -> str:
    """Reject a pending proposal with a reason (ADR-0015 §D4 + §D8)."""
    proposal_id = args.get("proposal_id")
    reason = args.get("reason")
    if not proposal_id:
        return json.dumps({"success": False, "error": "proposal_id is required"})
    if not reason or not str(reason).strip():
        return json.dumps(
            {
                "success": False,
                "error": "reason_required",
                "message": "rejection reason is required (non-empty string)",
            }
        )

    try:
        from hermes_quant.proposals import (
            ProposalExpiredError,
            ProposalStateError,
            get_default_store,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant import failed: {exc}",
            }
        )

    store = get_default_store()
    try:
        rejected = store.reject(proposal_id, reason=str(reason))
    except KeyError:
        return json.dumps(
            {
                "success": False,
                "error": "not_found",
                "message": f"proposal {proposal_id} not found",
            }
        )
    except (ProposalExpiredError, ProposalStateError) as exc:
        return json.dumps(
            {
                "success": False,
                "error": "state_mismatch",
                "message": str(exc),
            }
        )

    # Per ADR-0015 §D8, the calibrator-learn-from-rejections hook fires here.
    # v0.1.2 surfaces the config flag but the actual calibrator update path
    # depends on the settlement journal lifting (ADR-0010), shipped in same
    # release. If the journal API isn't available yet, we degrade silently.
    learn = _read_learn_from_rejections()
    if learn:
        try:
            from hermes_quant.journal.writer import (  # type: ignore[import-not-found]
                append_human_override,
            )

            append_human_override(rejected, kind="reject", reason=str(reason))
        except ImportError:
            logger.debug("quant_reject: journal writer not yet available; rejection lesson skipped")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "quant_reject: journal append failed: %s",
                exc,
                exc_info=True,
            )

    return json.dumps(
        {
            "success": True,
            "proposal_id": proposal_id,
            "state": rejected.state,
            "rejection_reason": rejected.rejection_reason,
            "calibrator_will_learn": learn,
        },
        default=str,
    )


def quant_pending(args: dict, **_kwargs) -> str:
    """List pending proposals (ADR-0015 §D4)."""
    try:
        from hermes_quant.proposals import _proposal_to_dict, get_default_store
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant import failed: {exc}",
            }
        )

    store = get_default_store()
    try:
        pending = store.list_pending(
            limit=int(args.get("limit", 20)),
            symbol=args.get("symbol"),
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"list_pending failed: {exc}",
            }
        )

    return json.dumps(
        {
            "success": True,
            "count": len(pending),
            "proposals": [_proposal_to_dict(p) for p in pending],
        },
        default=str,
    )


def quant_proposal(args: dict, **_kwargs) -> str:
    """Look up a single proposal record (ADR-0015 §D4)."""
    proposal_id = args.get("proposal_id")
    if not proposal_id:
        return json.dumps({"success": False, "error": "proposal_id is required"})

    try:
        from hermes_quant.proposals import _proposal_to_dict, get_default_store
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant import failed: {exc}",
            }
        )

    store = get_default_store()
    proposal = store.get(proposal_id)
    if proposal is None:
        return json.dumps(
            {
                "success": False,
                "error": "not_found",
                "message": f"proposal {proposal_id} not found",
            }
        )
    return json.dumps(
        {
            "success": True,
            "proposal": _proposal_to_dict(proposal),
        },
        default=str,
    )


# ---------------------------------------------------------------------------
# Autonomous-mode tool handlers (ADR-0016)
# ---------------------------------------------------------------------------


def quant_autonomous_tick(args: dict, **_kwargs) -> str:
    """Run an autonomous-mode tick (ADR-0016 §D11). Defaults to dry-run."""
    dry_run = bool(args.get("dry_run", True))  # ADR-0016 §D11 safe default

    try:
        from hermes_quant.autonomous import tick
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant autonomous import failed: {exc}",
            }
        )

    try:
        result = tick(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_autonomous_tick: tick failed: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "error": f"tick failed: {exc}",
            }
        )

    out = result.to_dict()
    # Surface mode-mismatch + kill-switch as errors so agent sees them
    if out["mode"] != "autonomous":
        return json.dumps(
            {
                "success": False,
                "error": "mode_mismatch",
                "message": f"autonomous tick requires quant.pdr.mode=autonomous; "
                f"current mode={out['mode']!r}. Set in ~/.hermes/config.yaml.",
                "current_mode": out["mode"],
            }
        )

    ks = out.get("kill_switch")
    if ks and ks.get("tripped"):
        return json.dumps(
            {
                "success": True,
                "kill_switch_tripped": True,
                "message": (
                    "autonomous mode is DISABLED — kill switch tripped. "
                    "Run `hermes quant autonomous reset --confirm` to re-enable."
                ),
                **out,
            },
            default=str,
        )

    return json.dumps({"success": True, **out}, default=str)


def quant_autonomous_status(args: dict, **_kwargs) -> str:
    """Show autonomous-mode status: mode, watchlist, gate config, kill-switch."""
    try:
        from hermes_quant.autonomous import (
            _read_kill_switch,
            _read_pdr_mode,
            _read_safety_rails,
            _read_silence_bias_config,
        )
        from hermes_quant.watchlist import list_watchlist
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"hermes-quant import failed: {exc}",
            }
        )

    mode = _read_pdr_mode()
    watchlist = list_watchlist()
    config = _read_silence_bias_config()
    rails = _read_safety_rails()
    ks = _read_kill_switch()

    return json.dumps(
        {
            "success": True,
            "mode": mode,
            "watchlist": [e.to_dict() for e in watchlist],
            "watchlist_size": len(watchlist),
            "silence_bias_config": {
                "min_confidence": config.min_confidence,
                "min_urgency": config.min_urgency,
                "min_analysts_emitted": config.min_analysts_emitted,
                "max_recent_rejections": config.max_recent_rejections,
                "salience_window_hours": config.salience_window_hours,
            },
            "safety_rails": rails,
            "kill_switch": {
                "tripped": ks.tripped,
                "tripped_at": ks.tripped_at,
                "cumulative_pnl_pct": ks.cumulative_pnl_pct,
                "threshold_pct": ks.threshold_pct,
                "reason": ks.reason,
            },
        },
        default=str,
    )


def quant_watchlist_add(args: dict, **_kwargs) -> str:
    """Add or update a watchlist entry (ADR-0016 §D11)."""
    symbol = args.get("symbol")
    asset_class = args.get("asset_class")
    timeframe = args.get("timeframe")
    if not symbol or not asset_class:
        return json.dumps(
            {
                "success": False,
                "error": "symbol and asset_class are required",
            }
        )

    try:
        from hermes_quant.watchlist import add_to_watchlist

        entry = add_to_watchlist(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
        )
    except ValueError as exc:
        return json.dumps(
            {
                "success": False,
                "error": "validation",
                "message": str(exc),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"add failed: {exc}",
            }
        )

    return json.dumps(
        {
            "success": True,
            "added": entry.to_dict(),
        },
        default=str,
    )


def quant_watchlist_remove(args: dict, **_kwargs) -> str:
    """Remove a watchlist entry."""
    symbol = args.get("symbol")
    asset_class = args.get("asset_class")
    if not symbol:
        return json.dumps({"success": False, "error": "symbol is required"})

    try:
        from hermes_quant.watchlist import remove_from_watchlist

        removed = remove_from_watchlist(symbol=symbol, asset_class=asset_class)
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"remove failed: {exc}",
            }
        )

    return json.dumps(
        {
            "success": True,
            "removed": removed,
            "symbol": symbol,
        },
        default=str,
    )


def quant_watchlist_list(args: dict, **_kwargs) -> str:
    """List watchlist entries."""
    try:
        from hermes_quant.watchlist import list_watchlist

        entries = list_watchlist()
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"list failed: {exc}",
            }
        )

    return json.dumps(
        {
            "success": True,
            "count": len(entries),
            "watchlist": [e.to_dict() for e in entries],
        },
        default=str,
    )


def quant_doctor(args: dict, **_kwargs) -> str:
    """Comprehensive health check. Read-only.

    Args:
        calibration: include legacy calibration block (default False).
        drift: include analyst confidence-drift surface (V03-6, default True).
        drift_recent_n: how many recent signals to use as the "recent"
            window for drift comparison (default 50).
        drift_threshold: absolute confidence delta to flag (default 0.15).
        daemon_state: include content-presence DaemonState mirror per
            ADR-0038 §D.3 / P6 (default True).
        daemon_state_per_symbol_n: how many recent rows per symbol to
            walk when inferring stage status (default 10).
    """
    # Re-read module attrs from the LIVE module dict via sys.modules — pytest's
    # import system can produce duplicate module-dict instances under certain
    # collection orderings, so the function's __globals__ may be stale relative
    # to monkeypatched module attrs. Going through sys.modules guarantees
    # the live attribute.
    import sys

    _t = sys.modules.get("hermes_quant.tools")
    _SIGNAL_BUS_PATH = getattr(_t, "SIGNAL_BUS_PATH", SIGNAL_BUS_PATH)
    _EXECUTION_BUS_PATH = getattr(_t, "EXECUTION_BUS_PATH", EXECUTION_BUS_PATH)
    _STATE_DB_PATH = getattr(_t, "STATE_DB_PATH", STATE_DB_PATH)
    _QUANT_HOME = getattr(_t, "QUANT_HOME", QUANT_HOME)

    include_calibration = args.get("calibration", False)
    include_drift = args.get("drift", True)
    drift_recent_n = int(args.get("drift_recent_n", 50))
    drift_threshold = float(args.get("drift_threshold", 0.15))
    include_daemon_state = args.get("daemon_state", True)
    daemon_state_per_symbol_n = int(args.get("daemon_state_per_symbol_n", 10))
    pid = _daemon_pid()

    checks = {
        "quant_home_exists": _QUANT_HOME.exists(),
        "signal_bus_exists": _SIGNAL_BUS_PATH.exists(),
        "execution_bus_exists": _EXECUTION_BUS_PATH.exists(),
        "state_db_exists": _STATE_DB_PATH.exists(),
        "daemon_running": pid is not None,
        "daemon_pid": pid,
    }

    # Optional providers
    optional_libs = {}
    for lib in [
        "yfinance",
        "ccxt",
        "alpaca",
        "torch",
        "transformers",
        "huggingface_hub",
        "sklearn",
        "mlflow",
    ]:
        try:
            __import__(
                lib if lib != "alpaca" else "alpaca.trading.client", globals(), locals(), [], 0
            )
            optional_libs[lib] = "available"
        except ImportError:
            optional_libs[lib] = "missing (install via: pip install hermes-quant[<extra>])"

    # Torch + CUDA detail
    try:
        import torch

        optional_libs["torch_version"] = getattr(torch, "__version__", "unknown")
        optional_libs["torch_cuda_available"] = (
            torch.cuda.is_available() if hasattr(torch, "cuda") else False
        )
    except (ImportError, AttributeError):
        # AttributeError catches the case where a stub or partially-mocked
        # torch module is on sys.modules without the standard surface.
        pass

    # V03-6: analyst confidence-drift surface
    drift_block = None
    if include_drift and _SIGNAL_BUS_PATH.exists():
        drift_block = _compute_drift_surface(
            recent_n=drift_recent_n,
            threshold=drift_threshold,
            signal_bus_path=_SIGNAL_BUS_PATH,
        )

    # ADR-0038 §D.3 (P6): content-presence DaemonState mirror
    daemon_state_block = None
    if include_daemon_state:
        # Pass through the resolved configured paths so the halt mirror
        # reads the same state.db that other quant_doctor checks consult
        # (rather than the import-time DEFAULT_STATE_DB).
        _halt_mirror = _QUANT_HOME / "halt_state.json"
        daemon_state_block = _compute_daemon_state_mirror(
            signal_bus_path=_SIGNAL_BUS_PATH,
            state_db_path=_STATE_DB_PATH,
            halt_mirror_path=_halt_mirror,
            per_symbol_n=daemon_state_per_symbol_n,
        )

    return json.dumps(
        {
            "success": True,
            "v0.1.0_state": "scaffold — protocol locked, daemon not yet implemented",
            "checks": checks,
            "optional_libs": optional_libs,
            "include_calibration": include_calibration,
            "drift": drift_block,
            "daemon_state": daemon_state_block,
            "next_step": (
                "1. `hermes quant setup` to write config\n"
                "2. `hermes quant start` to launch daemon (NOT YET IMPLEMENTED in v0.1.0 scaffold)\n"
                "3. Track GitHub for v0.1.1 implementation drop"
            ),
        },
        default=str,
    )


# ---------------------------------------------------------------------------
# ADR-0038 §D.3 (P6) — DaemonState content-presence mirror
# ---------------------------------------------------------------------------

# Stage discriminator: BarSnapshot slot key (V2 rows) OR legacy-row indicator key.
# The order is the canonical pipeline order — useful for renderers.
_STAGE_ORDER: tuple[str, ...] = (
    "ohlcv",
    "indicators",
    "analysts",
    "aggregated",
    "risk",
    "final",
)


def _infer_stages_for_row(row: dict[str, Any]) -> set[str]:
    """Return the set of pipeline stages "seen" in a single JSONL row.

    Works for both:
      * V2 typed rows — checks BarSnapshot top-level slot keys.
      * Legacy rows (`tick_loop._build_signal_record` shape) — infers
        from key presence (components, aggregator, target_position_pct, ...).
    """
    seen: set[str] = set()
    # V2-typed row detection: presence of the BarSnapshot top-level keys.
    if "meta" in row and isinstance(row.get("meta"), dict):
        if row.get("ohlcv") is not None:
            seen.add("ohlcv")
        if row.get("indicators") is not None:
            seen.add("indicators")
        if row.get("analyst_views"):
            seen.add("analysts")
        if row.get("aggregated_signal") is not None:
            seen.add("aggregated")
        if row.get("risk_check") is not None:
            seen.add("risk")
        if row.get("final_decision") is not None:
            seen.add("final")
        return seen

    # Legacy row: every emitted record went through ohlcv → analysts →
    # aggregated → risk → final, so presence of these keys implies the stage ran.
    if row.get("type") == "heartbeat":
        return seen
    if row.get("decision_price") is not None:
        seen.add("ohlcv")
    if row.get("components"):
        seen.add("analysts")
    if row.get("aggregator"):
        seen.add("aggregated")
    if "target_position_pct" in row:
        seen.add("risk")
        seen.add("final")
    return seen


def _bar_ts_from_row(row: dict[str, Any]) -> str | None:
    """Extract the canonical bar_ts string from a V2 or legacy row."""
    # V2 rows put bar_ts at top-level
    if "bar_ts" in row:
        return row.get("bar_ts")
    # Legacy rows use 'asof' as the decision timestamp; for dedup that's fine.
    return row.get("asof")


def _symbol_from_row(row: dict[str, Any]) -> str | None:
    """Extract symbol from a V2 or legacy row."""
    if "symbol" in row:
        return row.get("symbol")
    return row.get("asset")


def _compute_daemon_state_mirror(
    *,
    signal_bus_path: Path,
    state_db_path: Path | None = None,
    halt_mirror_path: Path | None = None,
    per_symbol_n: int = 10,
) -> dict[str, Any]:
    """Build the DaemonState content-presence mirror (ADR-0038 §D.3 / P6).

    Reads the last N rows of signals.jsonl per symbol and infers stage
    status from BarSnapshot slot presence (or legacy key presence).
    Dedup via `_seen_event_ids: set` keyed on `(symbol, bar_ts)`
    (matches TradingAgents `_processed_message_ids` pattern).

    Returns a dict shaped like
    `hermes_quant.schemas.bar_snapshot.SymbolStatus` /
    `HaltSummary` (model_dump form), so callers don't need pydantic.

    Read-only: never raises on bus parse errors — returns empty mirror.
    """
    # Lazy imports — keep the doctor handler import-cheap.
    try:
        # We read these for typed validation but the public output is dicts.
        from hermes_quant.schemas import HaltSummary, SymbolStatus  # noqa: F401
    except ImportError:  # pragma: no cover — schemas package always present in v0.4
        HaltSummary = None  # type: ignore[assignment]  # noqa: N806, F841
        SymbolStatus = None  # type: ignore[assignment]  # noqa: N806, F841

    if not signal_bus_path.exists():
        return {
            "per_symbol": {},
            "halts": [],
            "last_heartbeat_age_s": None,
            "journal_pending_count": 0,
            "note": "signal bus does not exist yet",
        }

    # Read a generous tail — we'll filter to last per_symbol_n per symbol.
    # 200 rows total is enough for typical configs (1-20 symbols × 10 each).
    raw_rows = _read_jsonl_tail(signal_bus_path, max(200, per_symbol_n * 20))

    # Dedup events by (symbol, bar_ts) — TradingAgents _processed_message_ids
    _seen_event_ids: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in raw_rows:
        sym = _symbol_from_row(row)
        ts = _bar_ts_from_row(row)
        if sym is None or ts is None:
            # Heartbeats / malformed — skip silently
            continue
        key = (sym, ts)
        if key in _seen_event_ids:
            continue
        _seen_event_ids.add(key)
        deduped.append(row)

    # Group by symbol, keeping last per_symbol_n rows per symbol
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in deduped:
        sym = _symbol_from_row(row)
        if sym is None:
            continue
        by_symbol.setdefault(sym, []).append(row)
    for sym in by_symbol:
        by_symbol[sym] = by_symbol[sym][-per_symbol_n:]

    # Per-symbol status
    per_symbol_out: dict[str, dict[str, Any]] = {}
    for sym, rows in by_symbol.items():
        stages_seen: set[str] = set()
        last_bar_ts: str | None = None
        last_action_dir: int | None = None
        last_action_conf: float | None = None
        for row in rows:
            stages_seen |= _infer_stages_for_row(row)
            ts = _bar_ts_from_row(row)
            if ts is not None:
                last_bar_ts = ts
            # Action dir/conf: prefer typed slot, fall back to legacy keys
            fd = row.get("final_decision")
            if isinstance(fd, dict):
                # V2: target_position_pct sign → direction; we surface direction
                # from aggregated_signal slot for explicit dir
                ag = row.get("aggregated_signal")
                if isinstance(ag, dict):
                    last_action_dir = ag.get("direction")
                    last_action_conf = ag.get("confidence")
            elif "direction" in row:
                last_action_dir = row.get("direction")
                last_action_conf = row.get("confidence")

        # Stable ordered list of stages
        ordered = [s for s in _STAGE_ORDER if s in stages_seen]
        per_symbol_out[sym] = {
            "last_bar_ts": last_bar_ts,
            "stages_seen": ordered,
            "last_action_dir": last_action_dir,
            "last_action_conf": last_action_conf,
        }

    # Halt summary (read-only mirror) — honor configured state-DB path so
    # `quant_doctor` against a redirected QUANT_HOME (e.g. tests, profiles)
    # surfaces halts from THAT db, not the user's default ~/.hermes/quant/.
    halts_out: list[dict[str, Any]] = []
    try:
        from hermes_quant.daemon.halt_state import (
            DEFAULT_HALT_JSON_MIRROR,
            DEFAULT_STATE_DB,
            HaltStateSQLite,
        )

        db = state_db_path if state_db_path is not None else DEFAULT_STATE_DB
        mirror = halt_mirror_path if halt_mirror_path is not None else DEFAULT_HALT_JSON_MIRROR
        hs = HaltStateSQLite(db_path=db, mirror_path=mirror)
        for h in hs.active_halts():
            halts_out.append(
                {
                    "account_id": h.account_id,
                    "asset_class": h.asset_class,
                    "asset": h.asset,
                    "reason": h.reason,
                    "halted_at": str(h.halted_at),
                    "halted_until": (str(h.halted_until) if h.halted_until else None),
                }
            )
    except Exception as exc:  # noqa: BLE001 — read-only diagnostic; never crash
        logger.debug("daemon_state: halt registry read failed: %s", exc)

    # Last heartbeat age (seconds)
    last_heartbeat_age_s: float | None = None
    try:
        recent = _read_jsonl_tail(signal_bus_path, 50)
        heartbeats = [r for r in recent if r.get("type") == "heartbeat"]
        if heartbeats:
            last_hb = heartbeats[-1]
            ts_str = last_hb.get("asof") or last_hb.get("ts")
            if ts_str:
                import pandas as pd

                last_heartbeat_age_s = float(
                    (pd.Timestamp.utcnow().tz_localize(None) - pd.Timestamp(ts_str).tz_localize(None)).total_seconds()
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("daemon_state: heartbeat parse failed: %s", exc)
        last_heartbeat_age_s = None

    # Journal pending count
    journal_pending_count = 0
    try:
        from hermes_quant.proposals import get_default_store

        store = get_default_store()
        journal_pending_count = len(store.list_pending(limit=1000))
    except Exception as exc:  # noqa: BLE001
        logger.debug("daemon_state: pending-proposal read failed: %s", exc)

    return {
        "per_symbol": per_symbol_out,
        "halts": halts_out,
        "last_heartbeat_age_s": last_heartbeat_age_s,
        "journal_pending_count": journal_pending_count,
        "n_dedup_events": len(_seen_event_ids),
        "_walked_at": time.time(),
    }


def _compute_drift_surface(
    *,
    recent_n: int = 50,
    threshold: float = 0.15,
    signal_bus_path: Path | None = None,
) -> dict:
    """V03-6 drift detection: per-analyst confidence-distribution shift.

    Args:
        recent_n: how many of the most recent signal-bus records define the
            "recent" window.
        threshold: absolute confidence delta to flag as drift.
        signal_bus_path: explicit path to the signal bus. Defaults to the
            module-level SIGNAL_BUS_PATH; passing explicitly enables tests
            to use temp paths even if pytest's import-mode creates duplicate
            module dicts (which would shadow monkeypatch'd module-globals).

    For each analyst with views in the signal bus:
      lifetime_mean_confidence = mean over ALL its emitted views
      recent_mean_confidence   = mean over the last `recent_n` signals
      delta                    = recent - lifetime
      flagged                  = abs(delta) >= threshold

    A flagged analyst means its confidence distribution has shifted recently
    — possibly a regime change, a calibrator break, or a feature-pipeline
    issue. The operator should investigate before trusting current views.

    Returns:
      {
        "n_signals_total": int,
        "n_signals_recent": int,
        "threshold": float,
        "per_analyst": {
          "<analyst>": {
            "n_lifetime": int,
            "n_recent": int,
            "lifetime_mean_confidence": float,
            "recent_mean_confidence": float,
            "delta": float,
            "flagged": bool,
            "reason": str | None,
          },
          ...
        },
        "any_flagged": bool,
      }
    """
    # Read the full bus (capped to a reasonable max for safety)
    MAX_LIFETIME = 5000  # noqa: N806 — module-level constant referenced inside a function-level helper
    bus_path = signal_bus_path if signal_bus_path is not None else SIGNAL_BUS_PATH
    all_records = _read_jsonl_tail(bus_path, MAX_LIFETIME)
    if not all_records:
        return {
            "n_signals_total": 0,
            "n_signals_recent": 0,
            "threshold": threshold,
            "per_analyst": {},
            "any_flagged": False,
            "note": "signal bus is empty — no drift surface available yet",
        }

    recent_records = all_records[-recent_n:] if len(all_records) > recent_n else all_records

    def _accumulate(records: list[dict]) -> dict[str, list[float]]:
        """Returns {analyst_name: [confidence, confidence, ...]}."""
        per_analyst: dict[str, list[float]] = {}
        for rec in records:
            views = rec.get("analyst_views") or []
            for v in views:
                name = v.get("analyst")
                conf = v.get("confidence")
                if name is None or conf is None:
                    continue
                try:
                    per_analyst.setdefault(name, []).append(float(conf))
                except (TypeError, ValueError):
                    continue
        return per_analyst

    lifetime = _accumulate(all_records)
    recent = _accumulate(recent_records)

    per_analyst: dict[str, dict] = {}
    any_flagged = False
    for name, lifetime_confs in lifetime.items():
        if not lifetime_confs:
            continue
        n_life = len(lifetime_confs)
        life_mean = sum(lifetime_confs) / n_life
        recent_confs = recent.get(name, [])
        n_rec = len(recent_confs)
        rec_mean = (sum(recent_confs) / n_rec) if n_rec else float("nan")

        if n_rec == 0:
            entry = {
                "n_lifetime": n_life,
                "n_recent": 0,
                "lifetime_mean_confidence": round(life_mean, 4),
                "recent_mean_confidence": None,
                "delta": None,
                "flagged": True,
                "reason": "no_recent_views",
            }
            any_flagged = True
        else:
            delta = rec_mean - life_mean
            flagged = abs(delta) >= threshold
            reason = None
            if flagged:
                reason = f"recent confidence shifted by {delta:+.3f} (threshold ±{threshold})"
            entry = {
                "n_lifetime": n_life,
                "n_recent": n_rec,
                "lifetime_mean_confidence": round(life_mean, 4),
                "recent_mean_confidence": round(rec_mean, 4),
                "delta": round(delta, 4),
                "flagged": flagged,
                "reason": reason,
            }
            if flagged:
                any_flagged = True
        per_analyst[name] = entry

    return {
        "n_signals_total": len(all_records),
        "n_signals_recent": len(recent_records),
        "threshold": threshold,
        "per_analyst": per_analyst,
        "any_flagged": any_flagged,
    }


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
    if sub == "recipes":
        return quant_recipes({}, **kwargs)
    if sub in ("recommend", "rec", "advise"):
        if len(args) < 2:
            return json.dumps(
                {
                    "success": False,
                    "error": "/quant recommend <SYMBOL> [asset_class] [timeframe]",
                }
            )
        rec_args = {"symbol": args[1]}
        if len(args) > 2:
            rec_args["asset_class"] = args[2]
        if len(args) > 3:
            rec_args["timeframe"] = args[3]
        return quant_recommend(rec_args, **kwargs)
    if sub == "propose":
        if len(args) < 2:
            return json.dumps(
                {
                    "success": False,
                    "error": "/quant propose <SYMBOL> [asset_class] [timeframe]",
                }
            )
        prop_args = {"symbol": args[1]}
        if len(args) > 2:
            prop_args["asset_class"] = args[2]
        if len(args) > 3:
            prop_args["timeframe"] = args[3]
        return quant_propose(prop_args, **kwargs)
    if sub == "approve":
        if len(args) < 2:
            return json.dumps(
                {
                    "success": False,
                    "error": "/quant approve <PROPOSAL_ID> [size_override_pct]",
                }
            )
        appr_args = {"proposal_id": args[1]}
        if len(args) > 2:
            appr_args["size_override_pct"] = args[2]
        return quant_approve(appr_args, **kwargs)
    if sub == "reject":
        if len(args) < 3:
            return json.dumps(
                {
                    "success": False,
                    "error": "/quant reject <PROPOSAL_ID> <reason text>",
                }
            )
        rej_args = {
            "proposal_id": args[1],
            "reason": " ".join(args[2:]),
        }
        return quant_reject(rej_args, **kwargs)
    if sub == "pending":
        n = int(args[1]) if len(args) > 1 else 20
        return quant_pending({"limit": n}, **kwargs)
    if sub == "proposal":
        if len(args) < 2:
            return json.dumps(
                {
                    "success": False,
                    "error": "/quant proposal <PROPOSAL_ID>",
                }
            )
        return quant_proposal({"proposal_id": args[1]}, **kwargs)
    if sub == "auto" or sub == "autonomous":
        # /quant auto <subcommand>
        if len(args) < 2:
            return quant_autonomous_status({}, **kwargs)
        sub2 = args[1]
        if sub2 == "tick":
            # Default dry-run from slash to avoid surprise; use CLI for real fires
            return quant_autonomous_tick({"dry_run": True}, **kwargs)
        if sub2 == "status":
            return quant_autonomous_status({}, **kwargs)
        return json.dumps(
            {
                "success": False,
                "error": f"unknown /quant auto subcommand {sub2!r}. Use: tick | status",
            }
        )
    if sub == "watchlist" or sub == "wl":
        if len(args) < 2:
            return quant_watchlist_list({}, **kwargs)
        sub2 = args[1]
        if sub2 == "list":
            return quant_watchlist_list({}, **kwargs)
        if sub2 == "add":
            if len(args) < 3:
                return json.dumps(
                    {
                        "success": False,
                        "error": "/quant watchlist add <SYMBOL> [asset_class] [timeframe]",
                    }
                )
            wl_args = {"symbol": args[2], "asset_class": args[3] if len(args) > 3 else "equity"}
            if len(args) > 4:
                wl_args["timeframe"] = args[4]
            return quant_watchlist_add(wl_args, **kwargs)
        if sub2 == "remove":
            if len(args) < 3:
                return json.dumps(
                    {
                        "success": False,
                        "error": "/quant watchlist remove <SYMBOL>",
                    }
                )
            return quant_watchlist_remove({"symbol": args[2]}, **kwargs)
        return json.dumps(
            {
                "success": False,
                "error": f"unknown /quant watchlist subcommand {sub2!r}. Use: list | add | remove",
            }
        )
    return json.dumps(
        {
            "success": False,
            "error": f"unknown subcommand '{sub}'. Use: status | signals [N] | views <asset> | recommend <SYMBOL> | propose <SYMBOL> | approve <ID> | reject <ID> <reason> | pending | proposal <ID> | auto tick|status | watchlist list|add|remove | doctor",
        }
    )
