"""hermes_quant.autonomous — Autonomous-mode tick orchestrator (ADR-0016).

Wires together:
  Perceive (advisor.recommend per symbol)
  Decide   (BMA aggregator + DefaultRiskGate, inside advisor)
  Gate     (silence_bias_gate after the advisor's risk_gate)
  React    (PaperReactor.execute on FIRE; v0.2 = paper only)

Per ADR-0016, the tick is invoked via:
  - tool surface: quant_autonomous_tick(dry_run=true) — agent-callable,
    DRY-RUN by default for safety
  - cron-script: hermes quant autonomous tick — via Hermes cron, fires
    real (paper) trades because dry_run is False there
  - CLI: hermes quant autonomous tick [--dry-run] — operator-driven

The orchestrator is a SYNC function. No async, no daemons, no
long-running coroutines. Each tick stands alone; state lives in the
journal + executions.jsonl + proposal store, all of which are durable
across restarts.

Mode gating per ADR-0015 §D7: tick refuses to fire (returns
mode_mismatch) unless quant.pdr.mode=autonomous.

Kill-switch per ADR-0016 §D9: if cumulative paper P&L since autonomous
start drops below `quant.autonomous.kill_switch_pct`, the orchestrator
returns disabled-with-alert and refuses further fires until the
operator runs `hermes quant autonomous reset --confirm`.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.gates.silence_bias import (
    GateConfig,
    silence_bias_gate,
)
from hermes_quant.react.paper import FillSizeInvariantError
from hermes_quant.watchlist import WatchlistEntry, list_watchlist

logger = logging.getLogger(__name__)


QUANT_HOME = Path.home() / ".hermes" / "quant"
KILL_SWITCH_PATH = QUANT_HOME / "autonomous_kill_switch.json"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _read_config() -> dict:
    """Read ~/.hermes/config.yaml (profile-aware via watchlist module)."""
    from hermes_quant.watchlist import get_config_path

    path = get_config_path()
    if not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("autonomous: config read failed: %s", exc)
        return {}


def _read_pdr_mode() -> str:
    cfg = _read_config()
    pdr = (cfg.get("quant") or {}).get("pdr") or {}
    mode = pdr.get("mode", "advise")
    return mode if mode in {"advise", "hitl", "autonomous"} else "advise"


def _finite_threshold(raw: Any, default: float, name: str) -> float:
    """ar09 (ar08 family): coerce an operator-YAML money threshold to a finite POSITIVE float,
    falling back to the documented default + a warning on a NaN/inf/<=0/non-numeric value.

    A non-finite threshold silently NEUTERS a `<`/`<=`/`>` money gate (every comparison against NaN
    is False), so it must fail CLOSED to the conservative default rather than propagate. Byte-identical
    for any finite positive configured value (the only legal shape)."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = float("nan")
    if not math.isfinite(val) or val <= 0:
        logger.warning(
            "autonomous: invalid %s=%r (non-finite or <=0) — falling back to default %r; "
            "the gate stays ARMED",
            name,
            raw,
            default,
        )
        return default
    return val


def _read_silence_bias_config() -> GateConfig:
    cfg = _read_config()
    raw = ((cfg.get("quant") or {}).get("autonomous") or {}).get("silence_bias") or {}
    return GateConfig(
        # ar09: finite-guard the operator-YAML thresholds (a NaN min_confidence/min_urgency
        # would make `metric < threshold` False -> a should-be-SILENCED signal FIRES).
        min_confidence=_finite_threshold(raw.get("min_confidence", 0.65), 0.65, "min_confidence"),
        min_urgency=_finite_threshold(raw.get("min_urgency", 0.5), 0.5, "min_urgency"),
        min_analysts_emitted=int(raw.get("min_analysts_emitted", 2)),
        max_recent_rejections=int(raw.get("max_recent_rejections", 3)),
        salience_window_hours=int(raw.get("salience_window_hours", 168)),
    )


def _read_safety_rails() -> dict:
    cfg = _read_config()
    auto = (cfg.get("quant") or {}).get("autonomous") or {}
    risk = (cfg.get("quant") or {}).get("risk") or {}
    return {
        "max_per_tick_opens": int(auto.get("max_per_tick_opens", 1)),
        "max_concurrent_positions": int(auto.get("max_concurrent_positions", 5)),
        "kill_switch_pct": float(auto.get("kill_switch_pct", 0.10)),
        "log_silences": bool(auto.get("log_silences", False)),
        "allow_live": bool(auto.get("allow_live", False)),
        # Paper-mode-only cost-gate override. Default False (conservative).
        # Consumed by the autonomous tick when constructing RiskConfig and
        # enforced fail-closed in _react() against the active reactor name.
        "paper_zero_costs": bool(risk.get("paper_zero_costs", False)),
        # Stop-loss backstop (deep-review 2026-06-07, defense-in-depth for the
        # June-4 ASTS stopless-loss). The trader now derives a stop even without
        # ATR (root-cause fix), but this is the LAST line: if a FIRE still
        # arrives with stop_loss=None and a size above the threshold, act.
        #   require_stop_loss: master enable (default False = byte-identical
        #     legacy behavior; opt-in so it can be validated before defaulting).
        #   stopless_max_size_pct: a stopless position is allowed up to this NAV
        #     fraction; above it, the backstop engages.
        #   stopless_mode: "size_down" (cap to the threshold, keep trading) or
        #     "silence" (refuse the trade entirely).
        "require_stop_loss": bool(auto.get("require_stop_loss", False)),
        # ar09: finite-guard the backstop limit (a NaN/<=0 would make `abs(kelly) > limit`
        # False -> a full-size stopless position passes UNCAPPED, silently disabling the backstop).
        "stopless_max_size_pct": _finite_threshold(
            auto.get("stopless_max_size_pct", 0.05), 0.05, "stopless_max_size_pct"
        ),
        "stopless_mode": str(auto.get("stopless_mode", "size_down")),
    }


# ---------------------------------------------------------------------------
# Admissibility sizing inputs (ADR-0077 unit-bridge)
# ---------------------------------------------------------------------------


def _decision_price_from_advisor(advisor_result: dict | None) -> float | None:
    """Pull the decision-time price out of an advisor_result.

    Mirrors PaperReactor._extract_decision_price so the admissibility share
    conversion uses the SAME price the reactor would fill at. Returns None
    (not 0.0) when no usable price is present so the caller can fail-closed
    instead of dividing NAV by a zero/garbage price.
    """
    ar = advisor_result or {}
    top_dp = ar.get("decision_price")
    if top_dp is not None:
        try:
            dp = float(top_dp)
            if dp > 0:
                return dp
        except (TypeError, ValueError):
            pass
    for view in ar.get("analyst_views") or []:
        md = view.get("metadata") or {}
        if "last_close" in md:
            try:
                lc = float(md["last_close"])
                if lc > 0:
                    return lc
            except (TypeError, ValueError):
                pass
    return None


def _account_nav_usd() -> float | None:
    """Best-available NAV (account equity in USD) for the paper account.

    Source priority:
      1. state.db cash.equity_total (materialized NAV after fills) — the truth.
      2. HERMES_QUANT_PAPER_INITIAL_CASH / paper bootstrap (no fills yet).
    Returns None on any failure so the caller fails-closed (no fabricated NAV).
    """
    try:
        from hermes_quant.state.portfolio_state import (
            _default_initial_cash,
            get_portfolio_state,
        )

        cash = get_portfolio_state().get_cash("paper-default")
        if cash is not None and cash.equity_total > 0:
            return float(cash.equity_total)
        # No fills yet -> the account is still the bootstrap cash balance.
        boot = _default_initial_cash()
        return float(boot) if boot > 0 else None
    except Exception as exc:  # noqa: BLE001 — fail-closed: unknown NAV => None.
        logger.warning("autonomous: NAV lookup failed (admissibility fail-closed): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Kill-switch state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KillSwitchState:
    tripped: bool
    tripped_at: str | None
    cumulative_pnl_pct: float
    threshold_pct: float
    reason: str | None


def _read_kill_switch() -> KillSwitchState:
    """Read ~/.hermes/quant/autonomous_kill_switch.json. If missing,
    return not-tripped state."""
    if not KILL_SWITCH_PATH.exists():
        return KillSwitchState(
            tripped=False,
            tripped_at=None,
            cumulative_pnl_pct=0.0,
            threshold_pct=0.10,
            reason=None,
        )
    try:
        d = json.loads(KILL_SWITCH_PATH.read_text(encoding="utf-8"))
        return KillSwitchState(
            tripped=bool(d.get("tripped", False)),
            tripped_at=d.get("tripped_at"),
            cumulative_pnl_pct=float(d.get("cumulative_pnl_pct", 0.0)),
            threshold_pct=float(d.get("threshold_pct", 0.10)),
            reason=d.get("reason"),
        )
    except Exception as exc:
        # ar21: we only reach here when the file EXISTS (the absent-file early
        # return above already handled a legitimate not-tripped cold start) but is
        # UNREADABLE / corrupt / torn (e.g. a half-written '{"tripped": tru'). An
        # existing-but-unparseable kill-switch file must FAIL CLOSED — read it as
        # TRIPPED — because the most dangerous interpretation is that the rail was
        # tripped and the flag got corrupted; returning tripped=False here would
        # silently RE-ARM a previously-tripped ADR-0016 rail. This mirrors the
        # never-re-arm-on-degraded-read posture the sibling realized-P&L rail (ar02)
        # already enforces. The ABSENT-file path stays tripped=False (unchanged), so
        # this is byte-identical on the healthy and cold-start paths.
        logger.warning(
            "autonomous: kill-switch file present but UNREADABLE (%s); failing "
            "CLOSED — treating the rail as TRIPPED so a corrupted flag cannot "
            "silently re-arm trading. Operator must inspect/clear %s.",
            exc,
            KILL_SWITCH_PATH,
        )
        return KillSwitchState(
            tripped=True,
            tripped_at=None,
            cumulative_pnl_pct=0.0,
            threshold_pct=0.10,
            reason="kill_switch_file_unreadable_fail_closed",
        )


def trip_kill_switch(*, cumulative_pnl_pct: float, threshold_pct: float, reason: str) -> None:
    KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tripped": True,
        "tripped_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cumulative_pnl_pct": cumulative_pnl_pct,
        "threshold_pct": threshold_pct,
        "reason": reason,
    }
    tmp = KILL_SWITCH_PATH.with_suffix(KILL_SWITCH_PATH.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, KILL_SWITCH_PATH)


def reset_kill_switch() -> bool:
    """Operator-driven reset (ADR-0016 §D9). Returns True if the file
    existed and was cleared."""
    if not KILL_SWITCH_PATH.exists():
        return False
    try:
        KILL_SWITCH_PATH.unlink()
        return True
    except OSError:
        return False


def compute_cumulative_realized_pnl_pct(
    executions_path: Path | None = None,
) -> float:
    """Cumulative realized P&L as a signed NAV fraction, from the executions bus.

    The kill-switch basis (ADR-0016 §D9). Uses the canonical FIFO matcher
    (`settlement_loop.join_exit_fills`) so this rail agrees with the daemon's
    settlement accounting rather than reimplementing lot-matching.

    ar25 — UNITS FIX (was a P1 fail-OPEN). ``SettledRoundTrip.qty`` is the
    NAV-FRACTION magnitude the fill moved the position by (``abs(fill_size_pct)``,
    e.g. 0.05 = 5% of NAV — settlement_loop._normalize_exec_record), NOT a share
    count. So each round-trip's P&L as a fraction of NAV is simply
    ``realized_return × qty`` and the cumulative basis is the SUM of those — NAV
    cancels and is never read. The prior code computed
    ``realized_return × (qty × entry_price)`` and then divided the sum by current
    NAV, which double-discounted by ≈nav/entry_price (~1000× for a $100 stock):
    a genuine -10%-of-NAV realized drawdown read as ~-0.01%, so the trip check
    ``_cum_pnl <= -kill_switch_pct`` was effectively dead on every NAV-fraction
    lane (synthetic-paper/autonomous AND alpaca-paper both derive qty from
    fill_size_pct). RED-verified empirically (50%-NAV position down 20% -> -0.0001
    vs correct -0.10).

    Returns a SIGNED fraction: negative = net loss (e.g. -0.12 = down 12% of NAV
    on realized round-trips). Unrealized (still-open) positions are NOT counted —
    the kill-switch reacts to LOCKED-IN losses ("closed-position" semantics).

    ar02 — degraded-rail handling (was a fail-OPEN). On a SUCCESSFUL compute we
    persist the value to a last-known sidecar. On a compute error (e.g. the FIFO
    matcher faulting mid-book, or a corrupt bus) we do NOT return 0.0 ("no breach")
    — that silently disarmed this SECONDARY rail when a catastrophic book also
    happened to fail to parse. Instead we return the LAST-KNOWN value (conservative:
    a losing book that briefly can't parse stays tripped) and emit a
    state_reconstruction_failed audit event so an operator sees the rail went blind.
    Cold start (no last-known) still returns 0.0 — we cannot fabricate a loss, and
    the deterministic gate (ADR-0004, independent + fail-CLOSED) remains the final
    authority. An empty/absent book legitimately returns 0.0 (no realized P&L yet).
    The basis no longer reads NAV, so the old ar20 NAV-None degraded branch is moot
    (structurally subsumed); the except-branch carry-forward below still guards a
    FIFO-matcher / bus-parse fault.
    """
    try:
        from hermes_quant.daemon.settlement_loop import join_exit_fills

        path = executions_path or (QUANT_HOME / "executions.jsonl")
        if not path.exists():
            return 0.0
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        if not records:
            return 0.0
        round_trips, _open = join_exit_fills(records)
        # ar34: this rail is the AUTONOMOUS lane's realized-drawdown as a fraction of the
        # paper-default NAV. The shared executions.jsonl ALSO carries other accounts whose
        # qty is in a DIFFERENT unit system — notably the freqtrade crypto consumer writes
        # account_id="freqtrade" with qty = RAW COIN COUNT (e.g. 0.5 ETH), not a NAV
        # fraction. Pooling a raw-coin qty into `Σ realized_return × qty` corrupts the
        # paper-NAV fraction (0.5 coins reads as 50% of NAV) and can spuriously trip OR mask
        # the kill-switch. Restrict to the autonomous lane's own account (paper-default —
        # the sentinel _normalize_exec_record assigns when no explicit account_id is set);
        # other accounts (freqtrade, and any future named/true-unit lane) have their own
        # rails and must not pollute this NAV-fraction basis.
        # ar25: within paper-default, a SINGLE-LEG fill's qty is ALREADY a NAV-fraction, so
        # its NAV-fraction P&L is realized_return × qty. Sum directly — NAV cancels (do NOT
        # multiply by entry_price, do NOT divide by NAV). A non-finite term is skipped.
        #
        # ar57: a MULTI-LEG per-leg child is the EXCEPTION. MultiLegPaperReactor._build_records
        # writes EVERY leg's fill_size_pct == the WHOLE family's NAV fraction F (a proxy), so
        # qty == F for every leg — NOT a true per-leg weight. Summing realized_return × F per
        # leg (a) over-counts F once per leg (Σ = F×leg_count, not F) and (b) weights legs of
        # vastly different true notionals EQUALLY, so a small offsetting option leg (+98% on
        # premium kept) masks a large stock-leg loss → the basis biases POSITIVE and the
        # kill-switch fails to trip on a genuine realized loss (fail-OPEN on the ADR-0016 rail).
        # For a multi-leg leg the authoritative size is reactor_metadata.quantity (signed TRUE
        # units, carried as rt.true_units); its real NAV-fraction P&L is
        #   realized_return × (|true_units| × entry_price × contract_multiplier) / NAV
        # which makes Σ over a family's legs equal the family's true net realized NAV fraction.
        # We read NAV ONLY for these legs; a multi-leg leg with NAV unreadable or missing
        # true_units fails CLOSED to the equal-F proxy (never silently drops a realized loss).
        nav_usd: float | None = None
        nav_resolved = False
        frac = 0.0
        for rt in round_trips:
            if getattr(rt, "account_id", "paper-default") != "paper-default":
                continue
            mleg_id = getattr(rt, "multi_leg_id", None)
            true_units = getattr(rt, "true_units", None)
            if mleg_id is not None and true_units is not None:
                if not nav_resolved:
                    nav_usd = _account_nav_usd()
                    nav_resolved = True
                mult = getattr(rt, "notional_multiplier", 1.0) or 1.0
                if (
                    nav_usd is not None
                    and math.isfinite(nav_usd)
                    and nav_usd > 0
                    and math.isfinite(true_units)
                    and math.isfinite(rt.entry_price)
                ):
                    leg_notional = abs(true_units) * abs(rt.entry_price) * abs(mult)
                    term = rt.realized_return * (leg_notional / nav_usd)
                else:
                    # Fail CLOSED: NAV unreadable / non-finite inputs → fall back to the
                    # equal-F proxy so a realized LOSS is never silently dropped to 0.
                    term = rt.realized_return * rt.qty
            else:
                term = rt.realized_return * rt.qty
            if not math.isfinite(term):
                continue
            frac += term
        if not math.isfinite(frac):
            return 0.0
        _persist_last_known_cum_pnl(frac)
        return frac
    except Exception as exc:  # noqa: BLE001 - degraded rail: carry last-known forward, never silently re-arm
        logger.warning("autonomous: cumulative-PnL computation failed: %s", exc)
        last_known = _read_last_known_cum_pnl()
        _emit_killswitch_degraded_audit(exc, last_known)
        return last_known if last_known is not None else 0.0


_LAST_KNOWN_CUM_PNL_PATH = QUANT_HOME / "autonomous_cum_pnl_last_known.json"


def _persist_last_known_cum_pnl(frac: float) -> None:
    """Persist the most recent SUCCESSFUL cumulative realized-P&L fraction so a later
    compute error can carry it forward instead of fail-opening to 0.0 (ar02). Atomic;
    best-effort (a persist failure must never break the healthy compute path)."""
    path = QUANT_HOME / _LAST_KNOWN_CUM_PNL_PATH.name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cum_pnl_pct": float(frac),
            "asof": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort, never break the rail
        logger.warning("autonomous: failed to persist last-known cum-PnL: %s", exc)


def _read_last_known_cum_pnl() -> float | None:
    """Return the last successfully-computed cumulative realized-P&L fraction, or None
    if no sidecar exists / it is unreadable (cold start)."""
    path = QUANT_HOME / _LAST_KNOWN_CUM_PNL_PATH.name
    if not path.exists():
        return None
    try:
        val = float(json.loads(path.read_text(encoding="utf-8")).get("cum_pnl_pct"))
        return val if math.isfinite(val) else None
    except Exception:  # noqa: BLE001 - a corrupt sidecar is treated as cold start
        return None


def _emit_killswitch_fired_audit(
    *, cumulative_pnl_pct: float, threshold_pct: float, reason: str
) -> None:
    """Emit a ``kill_switch_fired`` governance audit event when the autonomous LIVE
    realized-P&L kill-switch trips (ar28). Best-effort — an audit-append failure must
    NEVER break the trip (the sidecar JSON already halted trading; the audit is the
    observability + promotion-gate side-effect, not the rail itself).

    Why this is required (NOT cosmetic):
      * The canonical operator observability surface is the governance audit log
        (cli/status.py); without this event a real-money autonomous trip is
        indistinguishable from "never tripped" on that surface.
      * governance/promotion.py sets ``killswitch_in_14d`` ONLY from
        ``kind=='kill_switch_fired'`` events and BLOCKS paper->live promotion when it is
        True. A trip that emits no such event lets a strategy that just lost
        >=kill_switch_pct of NAV slip through the trailing-14d promotion block.
      * The sibling rails already do this (governance/kill_switch.fire(),
        daemon/halt_state.register_halt()); this mirrors them + the best-effort
        idiom of _emit_killswitch_degraded_audit.
    """
    try:
        from hermes_quant.governance import audit_log
        from hermes_quant.governance.audit_log import GovernanceEvent

        audit_log.append(
            GovernanceEvent(
                kind="kill_switch_fired",
                asof=datetime.now(tz=UTC),
                source="autonomous_kill_switch",
                payload={
                    "rail": "cumulative_realized_pnl_pct",
                    "cumulative_pnl_pct": cumulative_pnl_pct,
                    "threshold_pct": threshold_pct,
                    "reason": reason,
                },
            )
        )
    except Exception as audit_exc:  # noqa: BLE001 - audit is best-effort
        logger.warning("autonomous: failed to emit kill_switch_fired audit: %s", audit_exc)


def _emit_killswitch_degraded_audit(exc: Exception, last_known: float | None) -> None:
    """Emit an operator-visible audit event when the secondary kill-switch rail goes blind
    (ar02). Best-effort — an audit-append failure must never break the compute path."""
    try:
        from hermes_quant.governance import audit_log
        from hermes_quant.governance.audit_log import GovernanceEvent

        audit_log.append(
            GovernanceEvent(
                kind="state_reconstruction_failed",
                asof=datetime.now(tz=UTC),
                source="autonomous_kill_switch",
                payload={
                    "rail": "cumulative_realized_pnl_pct",
                    "error": str(exc)[:300],
                    "carried_last_known_cum_pnl_pct": last_known,
                    "degraded": True,
                },
            )
        )
    except Exception as audit_exc:  # noqa: BLE001 - audit is best-effort
        logger.warning("autonomous: failed to emit kill-switch degraded audit: %s", audit_exc)


# ---------------------------------------------------------------------------
# Tick orchestration
# ---------------------------------------------------------------------------


@dataclass
class SymbolDecision:
    symbol: str
    asset_class: str
    timeframe: str
    gate: str
    details: dict[str, Any] = field(default_factory=dict)
    advisor_result: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    execution_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        out = {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "timeframe": self.timeframe,
            "gate": self.gate,
            "details": self.details,
        }
        if self.action is not None:
            out["action"] = self.action
        if self.execution_id is not None:
            out["execution_id"] = self.execution_id
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class TickResult:
    asof: str
    mode: str
    dry_run: bool
    watchlist_size: int
    decisions: list[SymbolDecision] = field(default_factory=list)
    fires: int = 0
    silences: int = 0
    errors: int = 0
    kill_switch_state: KillSwitchState | None = None
    next_run_at: str | None = None

    def to_dict(self) -> dict:
        out = {
            "asof": self.asof,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "watchlist_size": self.watchlist_size,
            "decisions": [d.to_dict() for d in self.decisions],
            "fires": self.fires,
            "silences": self.silences,
            "errors": self.errors,
        }
        if self.kill_switch_state is not None:
            out["kill_switch"] = {
                "tripped": self.kill_switch_state.tripped,
                "tripped_at": self.kill_switch_state.tripped_at,
                "cumulative_pnl_pct": self.kill_switch_state.cumulative_pnl_pct,
                "threshold_pct": self.kill_switch_state.threshold_pct,
                "reason": self.kill_switch_state.reason,
            }
        if self.next_run_at:
            out["next_run_at"] = self.next_run_at
        return out


def tick(
    *,
    dry_run: bool = True,
    symbols: list[WatchlistEntry] | None = None,
    advisor_recommend=None,
) -> TickResult:
    """Single autonomous tick across the watchlist (ADR-0016 §D8).

    Args:
        dry_run: When True (default), do NOT React even on FIRE — just
            report what would happen. Tool-surface default per ADR-0016
            §D11 "tool surface defaults to dry-run." The cron-script
            sets dry_run=False.
        symbols: Optional override of the watchlist (for tests). Default
            reads from quant.autonomous.watchlist.
        advisor_recommend: Optional override of advisor.recommend (for tests).

    Returns:
        TickResult with structured per-symbol decisions + fires/silences/
        errors counters + kill-switch state.

    Raises:
        Nothing externally-visible. All errors are caught and surfaced
        via SymbolDecision.error.
    """
    asof = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = _read_pdr_mode()

    # Mode gate
    if mode != "autonomous":
        return TickResult(
            asof=asof,
            mode=mode,
            dry_run=dry_run,
            watchlist_size=0,
            decisions=[],
            errors=1,
            kill_switch_state=_read_kill_switch(),
        )

    # Kill-switch check — STORED state first (operator-tripped or a prior
    # live-trip). A tripped file always wins and halts the tick.
    ks = _read_kill_switch()
    if ks.tripped:
        return TickResult(
            asof=asof,
            mode=mode,
            dry_run=dry_run,
            watchlist_size=0,
            decisions=[],
            errors=0,
            kill_switch_state=ks,
        )

    # LIVE kill-switch trip (ADR-0016 §D9). Compute cumulative realized P&L as a
    # signed NAV fraction; if the loss breaches -kill_switch_pct, trip the switch
    # and halt. This is the rail that was previously DEAD CODE — the tick only
    # honored an already-tripped file and never computed live P&L (deep-review
    # 2026-06-07). On a DRY RUN we DETECT-but-do-not-WRITE (report would_trip in
    # the returned state; never persist the trip file on the dry path).
    # ar08: kill_switch_pct comes from operator-editable ~/.hermes/config.yaml. A
    # malformed value (NaN/inf/<=0) must NOT silently DISABLE the ADR-0016 §D9
    # always-on rail — `nan > 0` is False, so the pre-fix `_ks_threshold > 0` guard
    # short-circuited and a catastrophic realized loss would not trip, with no trace.
    # Fail CLOSED: fall back to the documented 0.10 floor AND warn, so an operator can
    # tell a neutered config from a healthy account. Byte-identical for any finite
    # positive threshold (the only legal shape). Mirrors the finite-guard posture the
    # SAME function applies to its P&L basis (autonomous.py:306/310/313) + gate.py.
    try:
        _ks_raw = float(_read_safety_rails().get("kill_switch_pct", 0.10))
    except (TypeError, ValueError):
        _ks_raw = float("nan")
    if not math.isfinite(_ks_raw) or _ks_raw <= 0:
        logger.warning(
            "autonomous: invalid kill_switch_pct=%r (non-finite or <=0) — falling back "
            "to the ADR-0016 0.10 floor; the kill-switch rail stays ARMED",
            _ks_raw,
        )
        _ks_threshold = 0.10
    else:
        _ks_threshold = _ks_raw
    _cum_pnl = compute_cumulative_realized_pnl_pct()
    if _ks_threshold > 0 and _cum_pnl <= -abs(_ks_threshold):
        reason = (
            f"cumulative realized P&L {_cum_pnl:+.4f} breached "
            f"kill_switch_pct=-{abs(_ks_threshold):.4f}"
        )
        if not dry_run:
            trip_kill_switch(
                cumulative_pnl_pct=_cum_pnl,
                threshold_pct=abs(_ks_threshold),
                reason=reason,
            )
            # ar28: emit the kill_switch_fired governance event so the trip is visible on
            # the canonical audit log AND so promotion.py's killswitch_in_14d block fires.
            # Best-effort: the sidecar above already halted; the audit must never break it.
            _emit_killswitch_fired_audit(
                cumulative_pnl_pct=_cum_pnl,
                threshold_pct=abs(_ks_threshold),
                reason=reason,
            )
            logger.warning("autonomous: LIVE kill-switch TRIPPED — %s", reason)
        tripped_state = KillSwitchState(
            tripped=True,
            tripped_at=asof,
            cumulative_pnl_pct=_cum_pnl,
            threshold_pct=abs(_ks_threshold),
            reason=reason + (" [dry-run: not persisted]" if dry_run else ""),
        )
        return TickResult(
            asof=asof,
            mode=mode,
            dry_run=dry_run,
            watchlist_size=0,
            decisions=[],
            errors=0,
            kill_switch_state=tripped_state,
        )

    # Lazy advisor import (avoid heavy deps if dry-run + no symbols)
    if advisor_recommend is None:
        from hermes_quant.advisor import recommend as advisor_recommend

    watchlist = symbols if symbols is not None else list_watchlist()
    config = _read_silence_bias_config()
    rails = _read_safety_rails()

    result = TickResult(
        asof=asof,
        mode=mode,
        dry_run=dry_run,
        watchlist_size=len(watchlist),
        kill_switch_state=ks,
    )

    if not watchlist:
        return result

    fires_this_tick = 0
    journal_lessons_cache: dict[str, list[dict]] = {}

    # ADR-0016 §D9 safety rail: max_concurrent_positions. Count the CURRENT open
    # book once at tick start (independent of the portfolio-caps opt-in flag —
    # this is a hard safety rail, not a sizing refinement, so it is always on).
    # reconstruct_portfolio_state() returns {symbol: target_pct} for non-zero
    # (open) positions only, so len() == current concurrent position count.
    # cs16/ADR-0016: pass reactor_filter=None so this rail counts the WHOLE open
    # equity book across EVERY reactor_name the live router can emit — paper,
    # deterministic-equity (HERMES_QUANT_DETERMINISTIC_EQUITY=1), and alpaca_paper
    # (HERMES_QUANT_ALPACA_PAPER=1). reconstruct_portfolio_state DEFAULTS to the
    # paper-only slice (portfolio/state.py:40), which would UNDER-count the book
    # (and let the rail open MORE than max_concurrent_positions) now that equity
    # fills route to non-paper reactors. A safety rail must see the whole book.
    # The per-symbol keying inside reconstruct_portfolio_state collapses a symbol
    # written under two reactor names to ONE row at its latest target, so this
    # cannot over-count a single logical position.
    # cs19/ADR-0016 §D9: fail-CLOSED on a read EXCEPTION. reconstruct_portfolio_state
    # is ALREADY internally fail-soft — a missing bus (portfolio/state.py:108-109) and
    # an OSError (:117-118) return an EMPTY PortfolioState WITHOUT raising, which is a
    # legitimate "no open book" the SUCCESS path below admits against. So the bare
    # `except` here catches only GENUINELY-UNEXPECTED faults (a corrupt reconstruct, a
    # programming error, a non-OSError filesystem fault). In that population we are BLIND
    # to the real exposure, and assuming the book is EMPTY (full headroom) is the most
    # dangerous possible assumption for a HARD safety rail. The conservative direction is
    # to treat the unreadable book as AT-CAP and SILENCE new-symbol opens this tick
    # (recoverable next tick once the read succeeds), via the `rail_read_failed` sentinel
    # wired into the D9 check below. NOTE: a SUCCESSFUL empty read keeps the old admit
    # behavior — only the EXCEPTION path fails closed.
    open_positions_at_tick_start = 0
    open_symbols_at_tick_start: set[str] = set()
    rail_read_failed = False
    try:
        from hermes_quant.portfolio.state import reconstruct_portfolio_state as _recon

        # Read from QUANT_HOME's bus explicitly (not the helper's hard-coded
        # default) so the rail honors the same home the rest of this module uses
        # — keeps it test-isolatable via the QUANT_HOME monkeypatch and correct
        # when the home is reconfigured.
        _open = _recon(QUANT_HOME / "executions.jsonl", reactor_filter=None).positions
        open_symbols_at_tick_start = set(_open)
        open_positions_at_tick_start = len(_open)
    except Exception as _exc:  # noqa: BLE001 - never block tick on a read error
        # cs19: fail-CLOSED for the HARD rail (was fail-open to count=0/empty-set).
        rail_read_failed = True
        logger.warning(
            "autonomous: could not count open positions for concurrent-cap rail "
            "(failing CLOSED — silencing NEW-symbol opens this tick): %s",
            _exc,
        )

    # ADR-0071: portfolio-aware Stage-2 sizing. When the operator opts in
    # via HERMES_QUANT_PORTFOLIO_CAPS=1, each fire is clipped greedily
    # against running portfolio headroom (gross / net / cash caps). The
    # default is OFF so this PR is observe-only on its first day; flip the
    # env var on once the operator has reviewed a tick log post-merge.
    portfolio_caps_enabled = os.environ.get("HERMES_QUANT_PORTFOLIO_CAPS") == "1"
    portfolio_state = None
    portfolio_caps = None
    if portfolio_caps_enabled:
        from hermes_quant.portfolio.state import reconstruct_portfolio_state
        from hermes_quant.risk.portfolio_normalize import (
            PortfolioCaps,
            clip_one_to_remaining_headroom,
            headroom_summary,
        )

        # cs16/ADR-0016: count the WHOLE open equity book (all reactor_names),
        # not just the paper-only default slice — same rationale as the D9 rail
        # above, so headroom is computed against the true book.
        portfolio_state = reconstruct_portfolio_state(reactor_filter=None)
        portfolio_caps = PortfolioCaps()
        logger.info(
            "autonomous: portfolio-caps gate ENABLED. initial state: %s",
            headroom_summary(portfolio_state, portfolio_caps),
        )

    # ADR-0079 PDR-1 / M17: build the ONE PerceptionFrame here (inside tick), the
    # producer BOTH the cron and the quant_autonomous_tick TOOL path reach — so
    # the tool path perceives the same frame the cron does (closes the GAP-D /
    # M17 tool-vs-cron semantic decoupling structurally, not via a second
    # monkey-patch). Semantic is default ON (FLAGS.md Tier A); set
    # HERMES_QUANT_SEMANTIC_ENABLED=0 to opt out — with the flag explicitly OFF
    # there are no packets to carry, perception_frame=None is byte-identical to
    # the pre-promotion path, and we skip the redundant fetch.
    # build_perception_frame_live never raises (returns None on any error) and a
    # None frame is identical to not passing one.
    _inject_frame = os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "1") == "1"

    # cs86 (DEFAULT-OFF): when the operator opts in via
    # HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE=1, resolve the REAL paper-account NAV
    # ONCE per tick and thread it into recommend() so the advisor's gate measures
    # drawdown / daily-loss against the DURABLE HWM peak + session-open (cs01)
    # rather than its synthetic flat 100k portfolio (which fails-OPEN). Flag OFF
    # (production default) => `durable_equity` stays None => recommend() receives
    # durable_equity_account=None => byte-identical call shape (no NAV resolved,
    # no store, no state.db write). `_account_nav_usd()` is fail-closed (returns
    # None on any failure); a None NAV flows through to recommend()'s flag-ON
    # fail-CLOSED branch (durable_baseline_nav_unavailable), never a fall-open.
    _durable_baseline = (
        os.environ.get("HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE", "0") == "1"
    )
    _durable_nav = _account_nav_usd() if _durable_baseline else None

    for entry in watchlist:
        try:
            _frame = None
            if _inject_frame:
                from hermes_quant.perception import build_perception_frame_live

                _frame = build_perception_frame_live(
                    entry.symbol,
                    asset_class=entry.asset_class,
                    timeframe=entry.timeframe,
                )
            _durable_equity_account = (
                ("paper-default", entry.asset_class, _durable_nav)
                if _durable_baseline
                else None
            )
            advisor_result = advisor_recommend(
                symbol=entry.symbol,
                asset_class=entry.asset_class,
                timeframe=entry.timeframe,
                include_lessons=True,
                perception_frame=_frame,
                durable_equity_account=_durable_equity_account,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "autonomous: advisor failed for %s: %s",
                entry.symbol,
                exc,
                exc_info=True,
            )
            result.decisions.append(
                SymbolDecision(
                    symbol=entry.symbol,
                    asset_class=entry.asset_class,
                    timeframe=entry.timeframe,
                    gate="ERROR",
                    error=f"advisor failed: {exc}",
                )
            )
            result.errors += 1
            continue

        # Pull lessons for salience check (already in advisor_result, but
        # surface separately for the gate)
        lessons = advisor_result.get("lessons") or []
        journal_lessons_cache[entry.symbol] = lessons

        # Run silence-bias gate
        gate_result = silence_bias_gate(
            advisor_result,
            config=config,
            journal_lessons=lessons,
        )

        decision = SymbolDecision(
            symbol=entry.symbol,
            asset_class=entry.asset_class,
            timeframe=entry.timeframe,
            gate=gate_result.decision.value,
            details=gate_result.details,
            advisor_result=advisor_result if rails.get("log_silences") else None,
        )

        if gate_result.fired:
            # Per-tick safety rail (D9)
            if fires_this_tick >= rails["max_per_tick_opens"]:
                decision.gate = "SILENCE_PER_TICK_CAP"
                decision.details = {
                    "reason": (
                        f"max_per_tick_opens={rails['max_per_tick_opens']} "
                        "reached; this signal would have fired but tick is at cap"
                    ),
                    "would_have_fired": True,
                    "original_gate": gate_result.decision.value,
                }
                result.silences += 1
                result.decisions.append(decision)
                continue

            # Concurrent-positions safety rail (ADR-0016 §D9). The book already
            # at the cap must not grow. Count = open positions at tick start +
            # fires already executed this tick. A FIRE on a symbol we ALREADY
            # hold is an adjustment (not a new slot), so it is exempt from the
            # cap — only genuinely-new symbols consume a concurrency slot.
            max_concurrent = rails["max_concurrent_positions"]
            is_new_symbol = entry.symbol not in open_symbols_at_tick_start
            projected_concurrent = open_positions_at_tick_start + fires_this_tick
            # cs19/ADR-0016 §D9: a read-EXCEPTION on the book count fails CLOSED —
            # treat the unreadable book as AT-CAP for any NEW-looking symbol. A
            # SUCCESSFUL read (empty or populated) leaves rail_read_failed False, so
            # the `or` short-circuits to the normal count test and behavior is byte-
            # identical. Existing-symbol management is unaffected: the `is_new_symbol`
            # guard still exempts a fire on a held symbol (the rail only converts a
            # would-be NEW open into a SILENCE; it never blocks a close/adjustment).
            if is_new_symbol and (
                rail_read_failed or projected_concurrent >= max_concurrent
            ):
                decision.gate = "SILENCE_CONCURRENT_CAP"
                _cap_reason = (
                    "concurrent-cap book read FAILED; failing CLOSED — silencing this "
                    "NEW-symbol open this tick (recoverable next tick)"
                    if rail_read_failed
                    else (
                        f"max_concurrent_positions={max_concurrent} reached "
                        f"({open_positions_at_tick_start} open at tick start "
                        f"+ {fires_this_tick} fired this tick); this NEW-symbol "
                        "signal would have fired but the book is at the cap"
                    )
                )
                decision.details = {
                    "reason": _cap_reason,
                    "would_have_fired": True,
                    "original_gate": gate_result.decision.value,
                    "open_positions_at_tick_start": open_positions_at_tick_start,
                    "rail_read_failed": rail_read_failed,
                }
                result.silences += 1
                result.decisions.append(decision)
                continue

            # FIRE — emit Action (target_position_pct from advisor's
            # risk_gate.kelly_fraction). React only if NOT dry_run.
            rg = (advisor_result or {}).get("risk_gate") or {}
            # Finite-guard the advisor's signed size (source advisor.py float() of
            # action.target_position_pct is unguarded). A non-finite kelly would flow
            # UNGUARDED to the admissibility unit bridge (a -inf kelly is < 0, so it
            # enters the HERMES_QUANT_ADMISSIBILITY short branch and `math.floor` would
            # raise, aborting the whole tick). Coerce non-finite -> 0.0 = no size = no
            # fire (the silence-by-default contract). Defense-in-depth alongside the
            # bridge's own fail-closed guard.
            kelly = float(rg.get("kelly_fraction", 0.0))
            if not math.isfinite(kelly):
                kelly = 0.0
            sig = (advisor_result or {}).get("aggregated_signal") or {}

            # Stop-loss backstop (ADR-0016 §D9 defense-in-depth; deep-review
            # 2026-06-07). Last line against a stopless full-size fire. Opt-in
            # via rails["require_stop_loss"] (default False = legacy byte-
            # identical). The trader's root-cause fix should mean stop_loss is
            # never None here, but if it still is and the would-be size exceeds
            # the allowed stopless band, either size DOWN to the band or SILENCE.
            stopless_meta: dict[str, Any] | None = None
            if rails.get("require_stop_loss"):
                tp = (advisor_result or {}).get("trader_proposal") or {}
                stop = tp.get("stop_loss")
                stopless = stop is None or (
                    isinstance(stop, (int, float)) and not math.isfinite(float(stop))
                )
                limit = float(rails["stopless_max_size_pct"])
                if stopless and abs(kelly) > limit:
                    if rails["stopless_mode"] == "silence":
                        decision.gate = "SILENCE_NO_STOP_LOSS"
                        decision.details = {
                            "reason": (
                                f"stop_loss is None and size {kelly:+.3f} exceeds "
                                f"stopless_max_size_pct={limit:.3f}; silenced "
                                "(stopless_mode=silence)"
                            ),
                            "would_have_fired": True,
                            "original_gate": gate_result.decision.value,
                        }
                        result.silences += 1
                        result.decisions.append(decision)
                        continue
                    # size_down (default): clamp magnitude to the allowed band,
                    # preserving direction.
                    capped = math.copysign(limit, kelly)
                    stopless_meta = {
                        "stopless_backstop": True,
                        "kelly_before": kelly,
                        "kelly_after": capped,
                        "stopless_max_size_pct": limit,
                    }
                    kelly = capped

            # ADR-0071: portfolio-aware Stage-2 clip. Greedy first-come-first-served
            # — earlier picks consume the budget, later picks see the residual room.
            # Order-dependent but operationally simpler than batching the loop.
            effective_size = kelly
            portfolio_clip_meta: dict[str, float | str | bool] | None = None
            if (
                portfolio_caps_enabled
                and portfolio_state is not None
                and portfolio_caps is not None
            ):
                clipped = clip_one_to_remaining_headroom(
                    asset=entry.symbol,
                    per_symbol_target_pct=kelly,
                    state=portfolio_state,
                    caps=portfolio_caps,
                )
                effective_size = clipped.portfolio_target_pct
                portfolio_clip_meta = {
                    "per_symbol_kelly": kelly,
                    "portfolio_target": clipped.portfolio_target_pct,
                    "scale_factor": clipped.scale_factor,
                    "fired": clipped.fired,
                    "silence_reason": clipped.silence_reason or "",
                }
                if not clipped.fired:
                    decision.gate = "SILENCE_PORTFOLIO_CAP"
                    decision.details = {
                        "reason": clipped.silence_reason or "portfolio_cap_bound",
                        "would_have_fired": True,
                        "original_gate": gate_result.decision.value,
                        "per_symbol_kelly": kelly,
                    }
                    result.silences += 1
                    result.decisions.append(decision)
                    continue

            # ADR-0077 pre-trade admissibility (DEFAULT-OFF, HERMES_QUANT_ADMISSIBILITY).
            # A hard, fail-closed precondition UPSTREAM of the ADR-0004 gate: it can only
            # REJECT a proposed short (-> SILENCE), never amplify or override. With the flag
            # OFF this block is skipped entirely -> behavior is bit-for-bit identical to today.
            #
            # Unified seam (H-adm #2): this calls the SAME shared
            # `admissibility.gate_order.admit_or_reject` seam the PaperReactor + HITL paths
            # use, instead of an inline select_oracle + target_pct_to_shares + apply_verdict
            # copy. One seam, no drift. The shared function honors the flag THROUGH
            # select_oracle(); we keep the flag check + the `effective_size < 0` short-circuit
            # here so the NAV / price lookups never run for a flag-OFF or non-short tick
            # (bit-for-bit no-op when OFF, asserted by tests).
            if os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") == "1" and effective_size < 0:
                from hermes_quant.admissibility import admit_or_reject

                # nav: the paper account NAV (`equity_total`), sourced the SAME way the
                # reactor does. It is used BOTH for the NAV-fraction->whole-share UNIT
                # BRIDGE and (as `account_equity`) for the live oracle's < $2,000 floor.
                # available_bp: a LIVE paper-account buying-power fetch (H-adm #1 closed)
                # — reuses the oracle's paper TradingClient via live_buying_power().
                # FAIL-CLOSED: any error / missing creds / non-positive => None, and the
                # short then fails-closed on the BP hard check (never a fabricated
                # sufficiency). Only fetched inside this admissibility-ON short branch.
                # Fail-closed: missing/non-positive NAV or price -> 0 shares -> REJECT.
                from hermes_quant.admissibility.oracle import live_buying_power

                nav = _account_nav_usd()
                price = _decision_price_from_advisor(advisor_result)
                available_bp = live_buying_power()
                verdict = admit_or_reject(
                    entry.symbol,
                    "short",
                    effective_size,
                    nav,
                    price,
                    datetime.now(tz=UTC),
                    account_equity=nav,
                    available_bp=available_bp,
                )
                if not verdict.admitted:
                    decision.gate = "SILENCE_ADMISSIBILITY"
                    decision.details = {
                        "reason": verdict.reason,
                        "admissibility_state": verdict.state.value,
                        "qty_shares": verdict.qty_shares,
                    }
                    result.silences += 1
                    result.decisions.append(decision)
                    continue
                effective_size = verdict.adjusted_target_pct

            decision.action = {
                "target_position_pct": effective_size,
                "reason": rg.get("reason", "autonomous_silence_bias_fire"),
                "direction": int(sig.get("direction", 0)),
            }
            if portfolio_clip_meta is not None:
                decision.action["portfolio_clip"] = portfolio_clip_meta
            if stopless_meta is not None:
                decision.action["stopless_backstop"] = stopless_meta

            if not dry_run:
                try:
                    execution_id = _react(
                        advisor_result,
                        entry,
                        effective_size,
                        paper_zero_costs=bool(rails.get("paper_zero_costs", False)),
                    )
                    # ar38: _react returns None when the reactor RETURNED a no-fill/
                    # silence/reject record (no capital moved, nothing on the bus). Treat
                    # it as a SILENCE — do NOT count a fire, do NOT consume a budget slot,
                    # and do NOT mutate portfolio_state with a phantom position (which
                    # would charge headroom against a position that never existed and
                    # clip/silence subsequent REAL signals this tick).
                    if execution_id is None:
                        decision.gate = "SILENCE_REACTOR_NO_FILL"
                        decision.details = {
                            "reason": "reactor returned a no-fill/silence record",
                            "would_have_fired": True,
                            "original_gate": gate_result.decision.value,
                            "requested_fill_size_pct": effective_size,
                        }
                        result.silences += 1
                        result.decisions.append(decision)
                        continue
                    decision.execution_id = execution_id
                    fires_this_tick += 1
                    # Update running portfolio state so the next pick sees the
                    # post-fire headroom (the canonical state.db helper does
                    # not reload mid-tick — we mutate here).
                    if portfolio_state is not None:
                        # `positions` dict is intentionally mutable here even though
                        # PortfolioState is frozen — the dict is the inner mutable
                        # container that we update without reconstructing the wrapper.
                        portfolio_state.positions[entry.symbol] = effective_size
                except FillSizeInvariantError as exc:
                    logger.warning(
                        "autonomous: fill-size invariant rejected %s: %s",
                        entry.symbol,
                        exc,
                    )
                    decision.gate = "SILENCE_FILL_SIZE_INVARIANT"
                    decision.details = {
                        "reason": str(exc),
                        "would_have_fired": True,
                        "original_gate": gate_result.decision.value,
                        "requested_fill_size_pct": effective_size,
                    }
                    result.silences += 1
                    result.decisions.append(decision)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "autonomous: React failed for %s: %s",
                        entry.symbol,
                        exc,
                        exc_info=True,
                    )
                    decision.error = f"react_failed: {exc}"
                    decision.gate = "ERROR"
                    result.errors += 1
                    result.decisions.append(decision)
                    continue
            else:
                fires_this_tick += 1  # count even in dry-run for cap math
                # In dry-run, simulate the state update so subsequent dry-run
                # picks see headroom consumption.
                if portfolio_state is not None:
                    portfolio_state.positions[entry.symbol] = effective_size

            result.fires += 1
        else:
            result.silences += 1

        result.decisions.append(decision)

    return result


def _react(
    advisor_result: dict[str, Any],
    entry: WatchlistEntry,
    fill_size_pct: float,
    *,
    paper_zero_costs: bool = False,
) -> str | None:
    """Fire the routed reactor for an autonomous decision and return the
    synthesized proposal_id (used as the execution_id surfaced in tick output),
    or ``None`` if the reactor returned a NO-FILL/SILENCE record (ar38) so the
    caller does NOT count a phantom fire or mutate portfolio state.

    We construct a minimal Proposal-shaped object on the fly so we can route it
    through the ONE dispatch chokepoint (``react.dispatch.select_reactor``) the
    HITL/CLI approve seam already uses — instead of HARDCODING ``PaperReactor()``
    here as a second, duplicate reactor-choice site (ra02 / the 2026-06-02
    41.6x-gross mechanism class). The execution lands in executions.jsonl per
    ADR-0015 §D6.

    Byte-identical by default: ``select_reactor`` returns ``PaperReactor`` for an
    equity proposal when BOTH routing flags are OFF (``HERMES_QUANT_ALPACA_PAPER``
    / ``HERMES_QUANT_DETERMINISTIC_EQUITY`` unset — the production default), which
    is the exact type this seam constructed before. With a routing flag ON the
    autonomous fire (correctly) inherits the SAME routed reactor the HITL path uses.
    The synthesized proposal is ``proposal_kind == "equity"`` (the dataclass
    default), so ``select_reactor`` never routes it to the multi-leg reactor.

    Fail-closed guard (paper-mode-only cost-gate override):
      `paper_zero_costs=True` REQUIRES that the routed reactor is named
      'paper'. ``select_reactor`` CAN return a non-paper reactor (alpaca_paper /
      deterministic-equity) when a routing flag is ON, so this guard is now MORE
      load-bearing: if a non-paper reactor were routed here while the flag is set,
      raise ValueError BEFORE any execution side-effect. Live behavior must be
      unaffected by this flag — silence-by-default.
    """
    from hermes_quant.proposals import Proposal, _make_proposal_id, _utc_now
    from hermes_quant.react.dispatch import select_reactor

    # Synthesize a Proposal stand-in FIRST so it can be routed. We DO NOT register
    # it in the proposal store — autonomous fires bypass HITL's pending state; the
    # execution is the audit trail. Synthesis is side-effect-free, so building it
    # before the guard does not move the guard's "before any side-effect" position.
    pid = _make_proposal_id(entry.symbol, _utc_now())
    proposal = Proposal(
        proposal_id=pid,
        state="approved",  # synthetic; never written to store
        symbol=entry.symbol,
        asset_class=entry.asset_class,
        timeframe=entry.timeframe,
        created_at=advisor_result.get("as_of") or _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at="",  # unused for autonomous
        approved_at=_utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        approver_user_id="autonomous",
        advisor_result=advisor_result,
    )

    # Route through the ONE dispatch chokepoint (the landed ADR-0029 §2.5 seam)
    # instead of hardcoding PaperReactor(). Flags-OFF -> PaperReactor (byte-
    # identical); flag-ON -> the same routed reactor the HITL path inherits.
    reactor = select_reactor(proposal)
    if paper_zero_costs and getattr(reactor, "name", None) != "paper":
        raise ValueError("paper_zero_costs is set but reactor is not paper")

    record = reactor.execute(
        proposal,
        fill_size_pct=fill_size_pct,
        approver_user_id="autonomous",
        play_tag="autonomous",  # B13: stamp the autonomous-tick source on the fill
    )

    # ar38: a reactor may RETURN (not raise) a NO-FILL / SILENCE / REJECT record —
    # PaperReactor on a non-finite/<=0 price or a cap clip-to-zero (silenced=True,
    # fill_size_pct=0.0), or DeterministicEquityReactor on a BP refusal / backend
    # error (no_fill=True / bp_rejected=True, fill_size_pct=0.0). It returns-not-raises
    # precisely because this fire loop calls execute() with no try/except. Previously
    # the returned record was DISCARDED and _react returned pid unconditionally, so the
    # loop counted a PHANTOM fire: result.fires++, a consumed per-tick/concurrency slot,
    # and a phantom portfolio_state.positions[symbol] that then charged headroom against
    # a position that never existed (clipping/silencing SUBSEQUENT real signals). This is
    # the autonomous-side gap of the cs02/ar16/ar27 HITL no-fill fixes. Detect the no-fill
    # and return None so the caller treats it as a silence (no fire-count, no state
    # mutation, no phantom headroom). Byte-identical when the reactor actually fills.
    _rmeta = getattr(record, "reactor_metadata", None) or {}
    _realized = getattr(record, "fill_size_pct", None)
    _is_nofill = (
        _rmeta.get("silenced") is True
        or _rmeta.get("no_fill") is True
        or _rmeta.get("bp_rejected") is True
        or _rmeta.get("unfilled_timeout") is True
        or (_realized is not None and _realized == 0.0)
    )
    if _is_nofill:
        logger.info(
            "autonomous: reactor returned a NO-FILL/SILENCE for %s (reason=%s); NOT "
            "counting a fire, NOT mutating portfolio state (phantom-fire guard)",
            entry.symbol,
            _rmeta.get("silence_reason")
            or _rmeta.get("no_fill_reason")
            or _rmeta.get("broker_status")
            or "no_fill",
        )
        return None

    # Append a journal entry tagged with hitl_kind=approve (autonomous
    # treats itself as a non-human approver — the audit trail is uniform
    # across HITL and autonomous).
    try:
        from hermes_quant.journal.writer import append_human_override

        append_human_override(
            proposal,
            kind="approve",
            reason="autonomous_silence_bias_fire",
        )
    except ImportError:
        logger.debug("autonomous: journal not available; skipping audit entry")
    except Exception as exc:  # noqa: BLE001
        logger.warning("autonomous: journal append failed: %s", exc, exc_info=True)

    return pid
