"""hermes_quant.tools — Read-only tool handlers (per ADR-0007).

Tools surface daemon state to the agent. They do NOT spawn the daemon,
mutate state, or place trades. Long-running operations (backtests, etc)
are CLI-only.

All handlers return JSON-serializable dicts (per Hermes plugin convention).
"""

from __future__ import annotations

import json
import logging
import math
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
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue  # valid JSON but not an object (corrupt/partial append) — skip
        records.append(rec)
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

    # ADR-0085 reporting rule: the AUTHORITATIVE halt state is the live halt
    # registry (halt_state.json), NOT the last entry on the deprecated signals.jsonl
    # bus. A stale halt SIGNAL (e.g. the 2026-05-13 operator_emergency_stop) on the
    # old bus is historical and must never read as a current halt. Surface the real
    # halt state explicitly and demote last_signal to clearly-historical.
    active_halts: list[dict[str, Any]] = []
    halt_read_ok = True
    try:
        from hermes_quant.daemon.halt_state import read_halt_mirror

        active_halts = read_halt_mirror()  # reads halt_state.json mirror; [] = not halted
    except Exception as exc:  # noqa: BLE001 — read-only diagnostic; never crash status
        halt_read_ok = False
        logger.debug("quant_status: authoritative halt read failed: %s", exc)

    return json.dumps(
        {
            "success": True,
            "daemon_running": daemon_running,
            "daemon_pid": pid,
            "quant_home": str(QUANT_HOME),
            # AUTHORITATIVE halt state (ADR-0085): empty list => NOT halted.
            "halted": bool(active_halts),
            "active_halts": active_halts,
            "halt_state_read_ok": halt_read_ok,
            "signal_bus_exists": SIGNAL_BUS_PATH.exists(),
            "signal_bus_size_bytes": SIGNAL_BUS_PATH.stat().st_size
            if SIGNAL_BUS_PATH.exists()
            else 0,
            # HISTORICAL only — the last entry on the deprecated signals.jsonl bus.
            # NOT a current-state indicator; a stale halt signal here does NOT mean
            # the system is halted (see "halted"/"active_halts" above, ADR-0085).
            "last_signal_historical": last_signal,
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
    """Read quant.pdr.mode from the active (profile-aware) Hermes config.

    Per ADR-0015 §D7: the mode gate is read at every quant_propose call, NOT
    cached, so an operator can edit config + retry without a daemon restart.

    Per ADR-0013 §D4: resolves the SAME profile-aware path the autonomous
    engine reads (`~/.hermes/profiles/<name>/config.yaml` when HERMES_PROFILE
    is set, else the global `~/.hermes/config.yaml`). Reading the global file
    unconditionally would let a stale pre-migration global config override the
    active profile's mode — a fail-OPEN at the HITL mode gate. Defaults to
    'advise'.
    """
    try:
        import yaml
    except ImportError:
        return "advise"
    from hermes_quant.watchlist import get_config_path

    cfg_path = get_config_path()
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
    """quant.calibration.learn_from_rejections (default True per ADR-0015 §D8).

    Profile-aware per ADR-0013 §D4 — same active-config path as _read_pdr_mode.
    """
    try:
        import yaml
    except ImportError:
        return True
    from hermes_quant.watchlist import get_config_path

    cfg_path = get_config_path()
    if not cfg_path.exists():
        return True
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return True
    cal = (cfg.get("quant") or {}).get("calibration") or {}
    return bool(cal.get("learn_from_rejections", True))


def _hitl_mode_mismatch_response(tool_name: str, mode: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": "mode_mismatch",
            "message": f"{tool_name} requires quant.pdr.mode=hitl; "
            f"current mode={mode!r}. Set in the active Hermes config "
            f"(~/.hermes/profiles/<HERMES_PROFILE>/config.yaml when a profile "
            f"is active, else ~/.hermes/config.yaml).",
            "current_mode": mode,
        }
    )


def _json_safe_float(value: Any) -> float | str:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return repr(value)
    return parsed if math.isfinite(parsed) else repr(value)


# ---------------------------------------------------------------------------
# HITL React tool handlers (ADR-0015)
# ---------------------------------------------------------------------------


_MULTI_LEG_STRATEGIES = {"covered_call", "cash_secured_put", "wheel"}


def _maybe_propose_multi_leg(symbol: str, args: dict) -> str | None:
    """B01 multi-leg producer branch for ``quant_propose``.

    Returns a JSON result string when ``args['strategy_kind']`` is a multi-leg
    strategy (covered_call / cash_secured_put / wheel); returns ``None`` otherwise so
    the equity path runs unchanged (byte-identical). The whole path is inert unless
    HERMES_QUANT_OPTIONS_GATE=1 — the builder runs the deterministic options_gate,
    which raises ``OptionsGateDisabled`` without the flag; we surface that as a clear
    ``options_gate_disabled`` error rather than building anything.

    Inputs (paper / offline / deterministic): ``asof`` (ISO; required for the replay
    chain), ``nav`` and ``options_buying_power`` (account context), optional
    ``held_shares`` (CC cover), ``chains_dir`` (test/replay override). The chain is
    read via ``ChainSnapshotReader.replay_chain`` — NEVER live.
    """
    strategy_kind = args.get("strategy_kind")
    if strategy_kind not in _MULTI_LEG_STRATEGIES:
        return None  # equity path; caller continues byte-identically.

    # Fail-closed flag check (mirror the builder/gate rail; clearer operator error).
    if os.environ.get("HERMES_QUANT_OPTIONS_GATE", "0") != "1":
        return json.dumps(
            {
                "success": False,
                "error": "options_gate_disabled",
                "message": "multi-leg proposals require HERMES_QUANT_OPTIONS_GATE=1 "
                "(the options gate is default-OFF). No proposal registered.",
                "strategy_kind": strategy_kind,
            }
        )

    try:
        from hermes_quant.options.data import ChainSnapshotReader
        from hermes_quant.options.recipes import (
            RecipeBuildError,
            build_and_persist_multi_leg,
        )
        from hermes_quant.proposals import get_default_store
        from hermes_quant.risk.options_gate import OptionsGateDisabled
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_propose[mleg]: import failed: %s", exc, exc_info=True)
        return json.dumps(
            {"success": False, "error": f"hermes-quant import failed: {exc}"}
        )

    asof_raw = args.get("asof")
    if not asof_raw:
        return json.dumps(
            {
                "success": False,
                "error": "asof_required",
                "message": "multi-leg proposals require an `asof` (ISO UTC) for the "
                "deterministic replay chain.",
            }
        )
    try:
        from datetime import UTC, datetime

        asof = datetime.fromisoformat(str(asof_raw).replace("Z", "+00:00"))
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=UTC)
    except ValueError:
        return json.dumps(
            {"success": False, "error": "bad_asof", "message": f"invalid asof: {asof_raw!r}"}
        )

    try:
        nav = float(args.get("nav", 0.0))
        options_buying_power = float(args.get("options_buying_power", 0.0))
        held_shares = int(args.get("held_shares", 0))
    except (TypeError, ValueError):
        return json.dumps(
            {"success": False, "error": "bad_account_context",
             "message": "nav / options_buying_power / held_shares must be numeric."}
        )
    if nav <= 0 or options_buying_power <= 0:
        return json.dumps(
            {
                "success": False,
                "error": "account_context_required",
                "message": "multi-leg proposals require positive nav and "
                "options_buying_power (paper account context).",
            }
        )

    chains_dir = args.get("chains_dir")
    reader = (
        ChainSnapshotReader(chains_dir=Path(chains_dir)) if chains_dir else None
    )

    store = get_default_store()
    try:
        result, record = build_and_persist_multi_leg(
            store=store,
            symbol=symbol,
            asof=asof,
            strategy_kind=strategy_kind,  # type: ignore[arg-type]
            nav=nav,
            options_buying_power=options_buying_power,
            held_shares=held_shares,
            reader=reader,
            ttl_minutes=int(args.get("ttl_minutes", 15)),
        )
    except OptionsGateDisabled:
        return json.dumps(
            {
                "success": False,
                "error": "options_gate_disabled",
                "message": "the options gate is default-OFF; set "
                "HERMES_QUANT_OPTIONS_GATE=1 to enable.",
            }
        )
    except RecipeBuildError as exc:
        return json.dumps(
            {"success": False, "error": "recipe_build_failed", "message": str(exc)}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_propose[mleg]: build/persist failed: %s", exc, exc_info=True)
        return json.dumps(
            {"success": False, "error": f"multi-leg proposal failed: {exc}"}
        )

    if not result.admitted or record is None:
        # Gate REJECT: no passing proposal persisted (rail). Surface the verdict.
        return json.dumps(
            {
                "success": False,
                "error": "gate_rejected",
                "message": f"options gate rejected this {strategy_kind}: "
                f"{result.reason}. No proposal registered.",
                "bucket": result.bucket.value,
                "reason": result.reason,
            }
        )

    return json.dumps(
        {
            "success": True,
            "proposal_id": record.proposal_id,
            "proposal_kind": "multi_leg",
            "state": record.state,
            "strategy_kind": strategy_kind,
            "bucket": result.bucket.value,
            "contracts": result.contracts,
            "expires_at": record.expires_at,
            "next_steps": (
                f"Multi-leg {strategy_kind} gated + registered. Approve with "
                f"quant_approve(proposal_id='{record.proposal_id}') (fires the "
                f"MultiLegPaperReactor — default-OFF behind "
                f"HERMES_QUANT_MULTILEG_REACTOR=1) or reject with "
                f"quant_reject(proposal_id='{record.proposal_id}', reason='...'). "
                f"Expires at {record.expires_at}."
            ),
        },
        default=str,
    )


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
        return _hitl_mode_mismatch_response("quant_propose", mode)

    # ── B01 multi-leg producer branch (ADR-0029). ───────────────────────────────
    # When the caller passes a multi-leg strategy_kind, route to the multi-leg
    # builder/persist seam instead of the equity advisor path. The branch is INERT
    # unless HERMES_QUANT_OPTIONS_GATE=1 (the builder runs options_gate, which raises
    # OptionsGateDisabled without the flag). When no strategy_kind (or an equity one)
    # is passed, this returns None and the equity path below runs byte-identically.
    _ml = _maybe_propose_multi_leg(symbol, args)
    if _ml is not None:
        return _ml

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

    mode = _read_pdr_mode()
    if mode != "hitl":
        return _hitl_mode_mismatch_response("quant_approve", mode)

    size_override = args.get("size_override_pct")
    if size_override is not None:
        try:
            size_override = float(size_override)
        except (TypeError, ValueError, OverflowError):
            return json.dumps(
                {
                    "success": False,
                    "error": "size_override_pct must be a number",
                }
            )
        if not math.isfinite(size_override):
            return json.dumps(
                {
                    "success": False,
                    "error": "fill_size_invariant",
                    "message": "size_override_pct must be finite",
                    "proposal_id": proposal_id,
                    "requested_fill_size_pct": repr(size_override),
                }
            )

    # B13: source/play_tag of this fire so the retro/settlement loop can tell
    # advisor (the default HITL approve) from a playbook-driven approve apart.
    # Additive + default-OFF: omitted => "advisor" (bit-for-bit prior behavior);
    # an unrecognized tag falls back to "advisor" so a bad caller can't poison
    # the audit field. Allowed: advisor | playbook | autonomous.
    play_tag = str(args.get("play_tag") or "advisor")
    if play_tag not in ("advisor", "playbook", "autonomous"):
        play_tag = "advisor"

    try:
        from hermes_quant.proposals import (
            ProposalExpiredError,
            ProposalStateError,
            get_default_store,
        )
        from hermes_quant.react.dispatch import select_reactor
        from hermes_quant.react.paper import FillSizeInvariantError
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

    # ar16 — ATOMICALLY claim the proposal out of `pending` BEFORE the fire.
    # This closes the TOCTOU double-fire window: the old flow read state ==
    # 'pending' here, fired the reactor, THEN advanced state — so two concurrent
    # approves of the same proposal_id both passed the read-state gate and both
    # fired (the reactor stamps a fresh asof_execution per call -> distinct
    # idempotency keys -> two recorded fills -> capital moved twice). The claim
    # is a single BEGIN IMMEDIATE compare-and-set (UPDATE ... WHERE state =
    # 'pending'); exactly ONE caller wins and reaches the fire. A loser raises
    # ProposalStateError and NEVER fires. Safe-money polarity: the proposal is
    # left CLAIMED (approved) before the fire — if React then fails, a claimed-
    # but-unfired proposal that needs operator re-approval is strictly safer
    # than a double-fire; the execution is attached afterward via
    # store.record_execution.
    try:
        store.claim_for_approval(
            proposal_id,
            approver_user_id=_kwargs.get("user_id"),
            size_override_pct=size_override,
        )
    except ProposalExpiredError as exc:
        return json.dumps(
            {
                "success": False,
                "error": "state_mismatch",
                "message": str(exc),
                "proposal_id": proposal_id,
            }
        )
    except ProposalStateError as exc:
        # Lost the atomic claim (a concurrent approve already won, or the
        # proposal is no longer pending). Do NOT fire.
        return json.dumps(
            {
                "success": False,
                "error": "state_mismatch",
                "message": str(exc),
                "proposal_id": proposal_id,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error": f"claim failed: {exc}",
                "proposal_id": proposal_id,
            }
        )

    # Fire the reactor AFTER the atomic claim. The proposal is already advanced
    # out of `pending`, so a re-entrant/concurrent approve cannot fire it again.
    # select_reactor() dispatches on proposal kind: equity -> PaperReactor,
    # multi-leg -> MultiLegPaperReactor (default-OFF; a MultiLegReactorDisabled
    # raise surfaces the error — never a silent equity fill). HITL/CLI-only
    # money seam.
    reactor = select_reactor(proposal)
    try:
        execution = reactor.execute(
            proposal,
            fill_size_pct=fill_size_pct,
            approver_user_id=_kwargs.get("user_id"),
            play_tag=play_tag,  # B13: advisor (default) | playbook
        )
    except FillSizeInvariantError as exc:
        logger.warning("quant_approve: fill-size invariant rejected %s: %s", proposal_id, exc)
        # PROVEN no-capital refusal: the reactor raised before any fill landed
        # on the bus. Roll the ar16 claim back to `pending` so the operator can
        # revise + retry, exactly as the pre-ar16 flow did. No money moved.
        store.release_claim(proposal_id)
        return json.dumps(
            {
                "success": False,
                "error": "fill_size_invariant",
                "message": str(exc),
                "proposal_id": proposal_id,
                "state": "pending",
                "requested_fill_size_pct": _json_safe_float(fill_size_pct),
                "advisor_kelly": _json_safe_float(advisor_kelly),
            },
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant_approve: PaperReactor failed: %s", exc, exc_info=True)
        # The reactor raised. We cannot prove a fill did NOT land, so we keep
        # the safe-money polarity from the ar16 brief: leave the proposal
        # CLAIMED (approved) rather than re-pend it — re-pending could let a
        # second approve re-fire on top of a partial/ambiguous first fire
        # (double-fire). A claimed-but-maybe-unfired proposal that needs
        # operator attention is the safe direction. The settlement loop
        # reconciles via signal_id.
        return json.dumps(
            {
                "success": False,
                "error": f"react failed: {exc}",
                "proposal_id": proposal_id,
                "state": "approved",
            }
        )

    # ADR-0077/0079 admissibility: if the reactor refused the short pre-trade
    # (a 0-fill no-bus record flagged in reactor_metadata), do NOT report
    # success — that would mark a broker-refused order as a successful approval
    # (operator-facing dishonesty, found in the Wave-S review). This is a PROVEN
    # no-capital refusal (0-fill, no bus record), so roll the ar16 claim back to
    # `pending` and surface the rejection — the operator may revise or reject.
    _rmeta = getattr(execution, "reactor_metadata", None) or {}
    if _rmeta.get("admissibility_rejected"):
        store.release_claim(proposal_id)
        return json.dumps(
            {
                "success": False,
                "error": "admissibility_rejected",
                "proposal_id": proposal_id,
                "state": "pending",  # rolled back — operator may revise or reject
                "admissibility_state": _rmeta.get("admissibility_state"),
                "admissibility_reason": _rmeta.get("admissibility_reason"),
                "requested_fill_size_pct": fill_size_pct,
                "message": (
                    "Pre-trade admissibility REJECTED this short (e.g. not "
                    "easy-to-borrow / missing account context); no paper fill was "
                    "placed and the proposal remains pending."
                ),
            },
            default=str,
        )

    # cs02 cap-silence parity: when the reactor's portfolio-cap clip SILENCED the
    # fill (0-fill, no bus write, reactor_metadata.silenced=True), mirror the
    # admissibility branch above — do NOT advance the proposal to `approved` and do
    # NOT report success with the ORIGINAL requested size (that marks a cap-refused
    # order as a successful approval and consumes the proposal so it cannot be
    # re-approved when headroom frees). Keep it PENDING and report realized 0.0.
    # ar16: this is a PROVEN no-capital outcome (0-fill, no bus write), so roll the
    # atomic claim back to `pending` — the store state must match the reported
    # state="pending" (the claim advanced it to `approved` BEFORE the fire).
    if _rmeta.get("silenced"):
        store.release_claim(proposal_id)
        return json.dumps(
            {
                "success": False,
                "error": "portfolio_cap_silenced",
                "proposal_id": proposal_id,
                "state": "pending",  # NOT advanced — re-approvable when headroom frees
                "silence_reason": _rmeta.get("silence_reason"),
                "requested_fill_size_pct": _json_safe_float(fill_size_pct),
                "realized_fill_size_pct": _json_safe_float(
                    getattr(execution, "fill_size_pct", 0.0)
                ),
                "message": (
                    "Portfolio-cap clip SILENCED this fill (no headroom / over a "
                    "portfolio cap); no capital moved, nothing was written to the "
                    "bus, and the proposal remains pending so it can be re-approved "
                    "when headroom frees."
                ),
            },
            default=str,
        )

    # ar27: broker / backend NO-FILL parity (cs02/ar16 family). The DEFAULT
    # PaperReactor stamps silenced=True on a no-fill (caught above), but the
    # flag-gated reactors signal a no-fill DIFFERENTLY and carry NO `silenced`:
    #   * DeterministicEquityReactor -> reactor_metadata.no_fill=True (+bp_rejected /
    #     backend_unavailable) when buying power refuses the order (deterministic_equity.py:496)
    #   * MultiLegPaperReactor._write_nofill_parent -> no_fill=True on a broker
    #     non-fill terminal (multileg.py:924)
    #   * AlpacaPaperReactor -> reactor_metadata.unfilled_timeout=True when the live
    #     paper order does not fill within the poll window (alpaca_paper.py:215)
    # None match the two guards above, so pre-ar27 they fell through to
    # record_execution -> state=approved -> success:True echoing the REQUESTED size
    # for a fill that NEVER happened (operator-facing dishonesty, Wave-S class), AND
    # the pending proposal was irrecoverably consumed (could not be re-approved when
    # BP freed). A no-fill is a PROVEN no-capital outcome (fill_size_pct=0, nothing on
    # the bus), so roll the ar16 claim back to pending and report the rejection.
    _nofill = (
        _rmeta.get("no_fill") is True
        or _rmeta.get("unfilled_timeout") is True
        or _rmeta.get("bp_rejected") is True
    )
    if _nofill:
        store.release_claim(proposal_id)
        _reason = (
            _rmeta.get("no_fill_reason")
            or _rmeta.get("broker_status")
            or ("unfilled_timeout" if _rmeta.get("unfilled_timeout") else "no_fill")
        )
        return json.dumps(
            {
                "success": False,
                "error": "no_fill",
                "proposal_id": proposal_id,
                "state": "pending",  # NOT advanced — re-approvable when conditions change
                "no_fill_reason": _reason,
                "requested_fill_size_pct": _json_safe_float(fill_size_pct),
                "realized_fill_size_pct": _json_safe_float(
                    getattr(execution, "fill_size_pct", 0.0)
                ),
                "message": (
                    "The reactor did NOT fill this order (e.g. buying-power refusal, "
                    "broker non-fill, or unfilled-timeout); no capital moved and the "
                    "proposal remains pending so it can be re-approved when conditions "
                    "change. This is NOT a successful approval."
                ),
            },
            default=str,
        )

    # P1-B (shadow wiring): when HERMES_QUANT_ALPACA_SHADOW=1 and the fill we
    # just made went through the SYNTHETIC PaperReactor (reactor_name=="paper"),
    # ALSO submit the same proposal to Alpaca paper and log the divergence. This
    # is the validation path the shadow-first cutover plan depends on — without
    # this call the flag is a silent no-op. Gated to reactor_name=="paper" so we
    # NEVER double-submit when HERMES_QUANT_ALPACA_PAPER already routed the real
    # fill through Alpaca. Strictly non-blocking / fail-closed: run_shadow swallows
    # every error so the (already-committed) synthetic fill is never disturbed.
    try:
        from hermes_quant.react.alpaca_shadow import run_shadow, shadow_enabled

        if shadow_enabled() and getattr(execution, "reactor_name", None) == "paper":
            run_shadow(proposal, execution, fill_size_pct=fill_size_pct)
    except Exception as _shadow_exc:  # noqa: BLE001 — shadow must never break approve
        logger.warning("quant_approve: shadow hook failed (non-blocking): %s", _shadow_exc)

    # ar16: state already advanced to `approved` by the atomic claim BEFORE the
    # fire. Now attach the execution record onto the claimed proposal so the
    # audit trail carries the fill. The state transition is done; this is a
    # field-only update (no re-gate on state). If it fails, the fill is already
    # on the bus and the proposal is already approved; the settlement loop
    # reconciles via signal_id — surface a warning and keep going.
    try:
        from hermes_quant.react.paper import _record_to_dict

        approved = store.record_execution(
            proposal_id,
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

    # cs02 reporting-honesty: report the REALIZED fill, not the operator's
    # REQUESTED size. Two live paths reach this success return with a nonzero
    # PARTIAL fill (realized < requested) that is NOT a full silence and NOT a
    # no-fill:
    #   (1) PaperReactor + HERMES_QUANT_PORTFOLIO_CAPS=1 "partial scale" —
    #       _portfolio_cap_clip clips fill_size_pct down and the reactor books a
    #       REAL fill at record.fill_size_pct = clipped (paper.py partial-scale
    #       branch), with cap_metadata carrying cap_scaled_from/to/factor.
    #   (2) AlpacaPaperReactor on a done_for_day/canceled order with a realized
    #       partial — record.fill_size_pct = realized_fill_pct (< requested) with
    #       reactor_metadata alpaca_status/filled_qty/requested_target_pct
    #       (alpaca_paper.py partial path).
    # The local `fill_size_pct` here is the operator's REQUESTED size; echoing it
    # as the prominent `fill_size_pct` OVERSTATES the realized size to the operator
    # (operator-facing dishonesty, cs02 family). Surface both: keep the back-compat
    # `fill_size_pct` key but set it to the REALIZED value so the prominent field
    # is truthful, and add explicit requested/realized fields. A partial IS a
    # successful approval — no state-machine change (state stays approved).
    realized_fill_size_pct = getattr(execution, "fill_size_pct", fill_size_pct)
    partial_fill = (
        isinstance(realized_fill_size_pct, (int, float))
        and abs(float(realized_fill_size_pct)) < abs(float(fill_size_pct)) - 1e-9
    )
    return json.dumps(
        {
            "success": True,
            "proposal_id": proposal_id,
            "state": approved.state,
            "execution": _record_to_dict(execution),
            # Back-compat key, now truthful: REALIZED (clipped/partial) fill.
            "fill_size_pct": _json_safe_float(realized_fill_size_pct),
            "requested_fill_size_pct": _json_safe_float(fill_size_pct),
            "realized_fill_size_pct": _json_safe_float(realized_fill_size_pct),
            "partial_fill": partial_fill,
        },
        default=str,
    )


def quant_reject(args: dict, **_kwargs) -> str:
    """Reject a pending proposal with a reason (ADR-0015 §D4 + §D8)."""
    proposal_id = args.get("proposal_id")
    reason = args.get("reason")
    if not proposal_id:
        return json.dumps({"success": False, "error": "proposal_id is required"})

    mode = _read_pdr_mode()
    if mode != "hitl":
        return _hitl_mode_mismatch_response("quant_reject", mode)

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

    # ADR-0085 reporting rule: report the watchlist the ENGINE actually scans, not just
    # the (often-empty) config.yaml operator watchlist. The deployed autonomous tick scans
    # the evolving PDR play-fit watchlist (~/.hermes/quant/watchlist/play-fit.json); reporting
    # only list_watchlist() showed size:0 while the tick scanned 117 — a confusing drift.
    # Surface BOTH, clearly labeled, so neither is mistaken for the other.
    engine_watchlist_size = 0
    engine_watchlist_asof = None
    try:
        play_fit = QUANT_HOME / "watchlist" / "play-fit.json"
        if play_fit.exists():
            pf = json.loads(play_fit.read_text())
            plays = pf.get("plays") if isinstance(pf, dict) else None
            engine_watchlist_size = len(plays) if isinstance(plays, (list, dict)) else 0
            engine_watchlist_asof = pf.get("as_of") if isinstance(pf, dict) else None
    except Exception as exc:  # noqa: BLE001 — read-only diagnostic; never crash status
        logger.debug("quant_autonomous_status: play-fit read failed: %s", exc)

    return json.dumps(
        {
            "success": True,
            "mode": mode,
            # config.yaml operator watchlist (ADR-0016) — hand-curated, may be empty.
            "operator_watchlist": [e.to_dict() for e in watchlist],
            "operator_watchlist_size": len(watchlist),
            # the watchlist the autonomous tick ACTUALLY scans (PDR play-fit, evolving).
            "engine_watchlist_size": engine_watchlist_size,
            "engine_watchlist_asof": engine_watchlist_asof,
            # back-compat aliases (the legacy keys pointed at the operator watchlist).
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

    # Cold-start guard: when the signal bus hasn't been created yet,
    # skip per-symbol/heartbeat probes (they need bus rows) but still
    # run the halt-registry and proposal-pending probes. A missing bus
    # is NOT a "no halts" signal — operator emergency stops live in
    # state.db and may exist before the daemon ever writes a row.
    # Suppressing them here is exactly the false-clean failure mode
    # that hid the May 13 phantom-halt scare.
    bus_present = signal_bus_path.exists()

    # Read a generous tail — we'll filter to last per_symbol_n per symbol.
    # 200 rows total is enough for typical configs (1-20 symbols × 10 each).
    raw_rows: list[dict[str, Any]] = (
        _read_jsonl_tail(signal_bus_path, max(200, per_symbol_n * 20))
        if bus_present
        else []
    )

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

    # Journal pending count — read-only file probe, no ProposalStore
    # construction (which would touch ~/.hermes/quant/proposals.{jsonl,db}
    # and trigger the expiration sweep on `list_pending`). We tail the
    # JSONL log directly and count rows whose status is "pending".
    journal_pending_count = 0
    try:
        from hermes_quant.proposals import PROPOSAL_BUS_PATH

        if PROPOSAL_BUS_PATH.exists():
            # Single-pass tail: read last 5000 rows, count status=="pending".
            # Bounded read; pending proposals are short-lived (TTL-bounded).
            recent = _read_jsonl_tail(PROPOSAL_BUS_PATH, 5000)
            journal_pending_count = sum(
                1 for r in recent if r.get("status") == "pending"
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("daemon_state: pending-proposal probe failed: %s", exc)

    out: dict[str, Any] = {
        "per_symbol": per_symbol_out,
        "halts": halts_out,
        "last_heartbeat_age_s": last_heartbeat_age_s,
        "journal_pending_count": journal_pending_count,
        "n_dedup_events": len(_seen_event_ids),
        "_walked_at": time.time(),
    }
    if not bus_present:
        # Surface the cold-start state so callers know per_symbol/heartbeat
        # are empty because the bus is absent, not because no signals
        # have been emitted. Halts + pending counts above DID run.
        out["note"] = "signal bus does not exist yet"
    return out


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


def quant_insider(args: dict, **_kwargs) -> str:
    """Read-only SEC EDGAR Form-4 insider-transactions surface (B20). Read-only.

    DEFAULT-OFF: gated behind ``HERMES_QUANT_INSIDER_ENABLED`` read at call time.
    When the flag is OFF (the default) this returns
    ``{"success": True, "enabled": False, "filings": []}`` and touches NO network
    (silence-by-default; ADR-0007 read-only tool surface). When ON it fetches the
    issuer's recent Form-4 filings from data.sec.gov and surfaces them with their
    asof-honest ``filed_at`` anchor (the EDGAR acceptance/filing moment — NEVER
    the transaction date). The documented SEC 403 from cloud egress degrades to an
    empty filing list, never an exception.

    This tool only READS/surfaces filings and (optionally) writes append-only
    EvidenceRecords; it never trades or mutates daemon state (ADR-0007).

    Args:
        cik: 10-digit (or shorter, zero-padded internally) SEC CIK of the issuer.
        since: optional ISO timestamp; keep only filings with filed_at >= since.
        limit: max filings to return (most recent kept; default 20).
        store: when True, append each filing as a FilingEvidence to the evidence
            store (idempotent). Default False (pure read).
    """
    # Lazy import — the adapter pulls urllib/network; keep register() fast.
    from hermes_quant.evidence.adapters import form4 as _form4

    if not _form4.insider_enabled():
        # Flag OFF -> silence. No network, no error: this is the safe default.
        return json.dumps(
            {
                "success": True,
                "enabled": False,
                "filings": [],
                "note": (
                    "insider adapter is default-OFF; set HERMES_QUANT_INSIDER_ENABLED=1 "
                    "(after a connectivity smoke check) to enable."
                ),
            }
        )

    cik = args.get("cik")
    if cik is None or str(cik).strip() == "":
        return json.dumps({"success": False, "error": "cik is required"})
    limit = int(args.get("limit", 20))
    since = None
    since_raw = args.get("since")
    if since_raw:
        try:
            from datetime import UTC as _UTC
            from datetime import datetime as _dt

            since = _dt.fromisoformat(str(since_raw))
        except ValueError:
            return json.dumps(
                {"success": False, "error": f"unparseable since timestamp: {since_raw!r}"}
            )
        # A bare ISO date (e.g. "2025-01-01") or a no-offset datetime yields a
        # NAIVE datetime. filed_at in parse_submissions is tz-aware UTC, so a
        # naive `since` would raise TypeError on the `filed_at < since` compare
        # and drop EVERY filing behind an opaque error. Anchor naive input to UTC.
        if since.tzinfo is None:
            since = since.replace(tzinfo=_UTC)
    do_store = bool(args.get("store", False))

    try:
        filings, latency = _form4.fetch_form4_filings(str(cik), since=since)
    except Exception as e:  # noqa: BLE001 - defensive: adapter is fail-closed, but never crash the tool
        logger.warning("quant_insider: fetch error for CIK %s: %s", cik, e)
        return json.dumps({"success": False, "error": str(e)})

    # Most-recent-first, capped.
    filings = sorted(filings, key=lambda f: f.filed_at, reverse=True)[:limit]

    n_stored = 0
    store_error = None
    if do_store and filings:
        try:
            from hermes_quant.evidence.store import EvidenceStore

            est = EvidenceStore()
            for f in filings:
                est.append(_form4.to_filing_evidence(f))
                n_stored += 1
        except Exception as e:  # noqa: BLE001 - storing must not break the read surface
            store_error = str(e)
            logger.warning("quant_insider: store error: %s", e)

    out_filings = [
        {
            "accession_number": f.accession_number,
            "form_type": f.form_type,
            "issuer_symbol": f.issuer_symbol,
            "issuer_cik": f.issuer_cik,
            # filed_at is the asof anchor (acceptance/filing moment), NOT the trade date.
            "filed_at": f.filed_at.isoformat(),
            "period_of_report": f.period_of_report.isoformat()
            if f.period_of_report is not None
            else None,
            "url": f.archive_url(),
        }
        for f in filings
    ]
    return json.dumps(
        {
            "success": True,
            "enabled": True,
            "cik": str(cik),
            "count": len(out_filings),
            "latency_seconds": round(latency, 4),
            "stored": n_stored,
            "store_error": store_error,
            "filings": out_filings,
        },
        default=str,
    )


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
