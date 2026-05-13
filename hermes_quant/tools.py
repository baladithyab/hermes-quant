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


def quant_recommend(args: dict, **_kwargs) -> str:
    """Synchronous advisor surface — runs analysts/aggregator/gate on a single
    symbol and returns a structured recommendation. Per ADR-0014.

    Read-only: does NOT mutate state.db, signals.jsonl, calibrators, or the
    journal write-side. Safe under no-data scenarios — returns a gated dict
    rather than raising.
    """
    symbol = args.get("symbol")
    if not symbol:
        return json.dumps({
            "success": False,
            "error": "symbol is required",
        })

    # Lazy import — advisor pulls in pandas + yfinance, which are heavy.
    # Keeping the import inside the handler means register-time cost stays
    # at ~50ms per the ADR-0007 budget.
    try:
        from hermes_quant.advisor import recommend
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_recommend: advisor import failed: %s", exc, exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"advisor module unavailable: {exc}",
        })

    try:
        result = recommend(
            symbol=symbol,
            asset_class=args.get("asset_class", "equity"),
            timeframe=args.get("timeframe"),
            lookback_bars=args.get("lookback_bars"),
            include_lessons=bool(args.get("include_lessons", True)),
            as_of=args.get("as_of"),
        )
    except Exception as exc:  # noqa: BLE001 — advisor is best-effort
        logger.warning(
            "quant_recommend: advisor.recommend raised: %s", exc, exc_info=True
        )
        return json.dumps({
            "success": False,
            "error": f"advisor failed: {exc}",
            "symbol": symbol,
        })

    return json.dumps({"success": True, **result}, default=str)


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
    pdr = ((cfg.get("quant") or {}).get("pdr") or {})
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
    cal = ((cfg.get("quant") or {}).get("calibration") or {})
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
        return json.dumps({
            "success": False,
            "error": "mode_mismatch",
            "message": f"quant_propose requires quant.pdr.mode=hitl; "
                       f"current mode={mode!r}. Set in ~/.hermes/config.yaml.",
            "current_mode": mode,
        })

    try:
        from hermes_quant.advisor import recommend
        from hermes_quant.proposals import get_default_store
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_propose: import failed: %s", exc, exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"hermes-quant import failed: {exc}",
        })

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
        return json.dumps({
            "success": False,
            "error": f"advisor failed: {exc}",
            "symbol": symbol,
        })

    # Refuse to register a proposal that the advisor itself gated.
    # An operator approving a "no_bars_returned" proposal would be a footgun.
    rg = (advisor_result or {}).get("risk_gate") or {}
    if not rg.get("pass", False):
        return json.dumps({
            "success": False,
            "error": "advisor_gated",
            "message": f"Advisor gated this proposal: {rg.get('gated_reason', 'unknown')}. "
                       f"Use quant_recommend to inspect; no proposal registered.",
            "advisor_result": advisor_result,
        })

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
        return json.dumps({
            "success": False,
            "error": f"proposal store failed: {exc}",
        })

    return json.dumps({
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
    }, default=str)


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
            return json.dumps({
                "success": False,
                "error": "size_override_pct must be a number",
            })

    try:
        from hermes_quant.proposals import (
            ProposalExpiredError,
            ProposalStateError,
            get_default_store,
        )
        from hermes_quant.react import PaperReactor
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False,
            "error": f"hermes-quant import failed: {exc}",
        })

    store = get_default_store()
    try:
        proposal = store.get(proposal_id)
        if proposal is None:
            return json.dumps({
                "success": False,
                "error": "not_found",
                "message": f"proposal {proposal_id} not found",
            })
        if proposal.state != "pending":
            return json.dumps({
                "success": False,
                "error": "state_mismatch",
                "message": f"proposal {proposal_id} is in state "
                           f"{proposal.state!r}; cannot approve",
                "proposal_state": proposal.state,
            })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False, "error": f"lookup failed: {exc}",
        })

    # Determine fill size: operator override > advisor's Kelly recommendation
    rg = (proposal.advisor_result or {}).get("risk_gate") or {}
    advisor_kelly = float(rg.get("kelly_fraction", 0.0))
    fill_size_pct = float(size_override) if size_override is not None else advisor_kelly

    if fill_size_pct == 0.0:
        return json.dumps({
            "success": False,
            "error": "zero_fill_size",
            "message": "Fill size resolved to 0 (advisor gated and no override). "
                       "Provide size_override_pct or reject the proposal instead.",
            "advisor_kelly": advisor_kelly,
        })

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
        return json.dumps({
            "success": False,
            "error": f"react failed: {exc}",
            "proposal_id": proposal_id,
        })

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
        return json.dumps({
            "success": False,
            "error": "state_mismatch",
            "message": str(exc),
        })

    # Append journal entry for the approval — completes the operator audit
    # trail. ADR-0010 §Wave-A integration; degrades silently if journal
    # writer not available (e.g. older deploy without the journal/ pkg).
    try:
        from hermes_quant.journal.writer import append_human_override
        append_human_override(approved, kind="approve",
                              reason=_kwargs.get("approval_note"))
    except ImportError:
        logger.debug("quant_approve: journal writer not available; "
                     "approval not journaled")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "quant_approve: journal append failed: %s",
            exc, exc_info=True,
        )

    return json.dumps({
        "success": True,
        "proposal_id": proposal_id,
        "state": approved.state,
        "execution": _record_to_dict(execution),
        "fill_size_pct": fill_size_pct,
    }, default=str)


def quant_reject(args: dict, **_kwargs) -> str:
    """Reject a pending proposal with a reason (ADR-0015 §D4 + §D8)."""
    proposal_id = args.get("proposal_id")
    reason = args.get("reason")
    if not proposal_id:
        return json.dumps({"success": False, "error": "proposal_id is required"})
    if not reason or not str(reason).strip():
        return json.dumps({
            "success": False,
            "error": "reason_required",
            "message": "rejection reason is required (non-empty string)",
        })

    try:
        from hermes_quant.proposals import (
            ProposalExpiredError,
            ProposalStateError,
            get_default_store,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False,
            "error": f"hermes-quant import failed: {exc}",
        })

    store = get_default_store()
    try:
        rejected = store.reject(proposal_id, reason=str(reason))
    except KeyError:
        return json.dumps({
            "success": False, "error": "not_found",
            "message": f"proposal {proposal_id} not found",
        })
    except (ProposalExpiredError, ProposalStateError) as exc:
        return json.dumps({
            "success": False, "error": "state_mismatch", "message": str(exc),
        })

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
            logger.debug("quant_reject: journal writer not yet available; "
                         "rejection lesson skipped")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "quant_reject: journal append failed: %s",
                exc, exc_info=True,
            )

    return json.dumps({
        "success": True,
        "proposal_id": proposal_id,
        "state": rejected.state,
        "rejection_reason": rejected.rejection_reason,
        "calibrator_will_learn": learn,
    }, default=str)


def quant_pending(args: dict, **_kwargs) -> str:
    """List pending proposals (ADR-0015 §D4)."""
    try:
        from hermes_quant.proposals import _proposal_to_dict, get_default_store
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False,
            "error": f"hermes-quant import failed: {exc}",
        })

    store = get_default_store()
    try:
        pending = store.list_pending(
            limit=int(args.get("limit", 20)),
            symbol=args.get("symbol"),
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False, "error": f"list_pending failed: {exc}",
        })

    return json.dumps({
        "success": True,
        "count": len(pending),
        "proposals": [_proposal_to_dict(p) for p in pending],
    }, default=str)


def quant_proposal(args: dict, **_kwargs) -> str:
    """Look up a single proposal record (ADR-0015 §D4)."""
    proposal_id = args.get("proposal_id")
    if not proposal_id:
        return json.dumps({"success": False, "error": "proposal_id is required"})

    try:
        from hermes_quant.proposals import _proposal_to_dict, get_default_store
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False,
            "error": f"hermes-quant import failed: {exc}",
        })

    store = get_default_store()
    proposal = store.get(proposal_id)
    if proposal is None:
        return json.dumps({
            "success": False, "error": "not_found",
            "message": f"proposal {proposal_id} not found",
        })
    return json.dumps({
        "success": True,
        "proposal": _proposal_to_dict(proposal),
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
    if sub in ("recommend", "rec", "advise"):
        if len(args) < 2:
            return json.dumps({
                "success": False,
                "error": "/quant recommend <SYMBOL> [asset_class] [timeframe]",
            })
        rec_args = {"symbol": args[1]}
        if len(args) > 2:
            rec_args["asset_class"] = args[2]
        if len(args) > 3:
            rec_args["timeframe"] = args[3]
        return quant_recommend(rec_args, **kwargs)
    if sub == "propose":
        if len(args) < 2:
            return json.dumps({
                "success": False,
                "error": "/quant propose <SYMBOL> [asset_class] [timeframe]",
            })
        prop_args = {"symbol": args[1]}
        if len(args) > 2:
            prop_args["asset_class"] = args[2]
        if len(args) > 3:
            prop_args["timeframe"] = args[3]
        return quant_propose(prop_args, **kwargs)
    if sub == "approve":
        if len(args) < 2:
            return json.dumps({
                "success": False,
                "error": "/quant approve <PROPOSAL_ID> [size_override_pct]",
            })
        appr_args = {"proposal_id": args[1]}
        if len(args) > 2:
            appr_args["size_override_pct"] = args[2]
        return quant_approve(appr_args, **kwargs)
    if sub == "reject":
        if len(args) < 3:
            return json.dumps({
                "success": False,
                "error": "/quant reject <PROPOSAL_ID> <reason text>",
            })
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
            return json.dumps({
                "success": False,
                "error": "/quant proposal <PROPOSAL_ID>",
            })
        return quant_proposal({"proposal_id": args[1]}, **kwargs)
    return json.dumps({
        "success": False,
        "error": f"unknown subcommand '{sub}'. Use: status | signals [N] | views <asset> | recommend <SYMBOL> | propose <SYMBOL> | approve <ID> | reject <ID> <reason> | pending | proposal <ID> | doctor",
    })
