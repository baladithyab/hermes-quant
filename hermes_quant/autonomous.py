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


def _read_silence_bias_config() -> GateConfig:
    cfg = _read_config()
    raw = ((cfg.get("quant") or {}).get("autonomous") or {}).get("silence_bias") or {}
    return GateConfig(
        min_confidence=float(raw.get("min_confidence", 0.65)),
        min_urgency=float(raw.get("min_urgency", 0.5)),
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
        logger.warning("autonomous: kill-switch read failed: %s", exc)
        return KillSwitchState(
            tripped=False,
            tripped_at=None,
            cumulative_pnl_pct=0.0,
            threshold_pct=0.10,
            reason=None,
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

    # Kill-switch check
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

        portfolio_state = reconstruct_portfolio_state()
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
            advisor_result = advisor_recommend(
                symbol=entry.symbol,
                asset_class=entry.asset_class,
                timeframe=entry.timeframe,
                include_lessons=True,
                perception_frame=_frame,
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

            # FIRE — emit Action (target_position_pct from advisor's
            # risk_gate.kelly_fraction). React only if NOT dry_run.
            rg = (advisor_result or {}).get("risk_gate") or {}
            kelly = float(rg.get("kelly_fraction", 0.0))
            sig = (advisor_result or {}).get("aggregated_signal") or {}

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

            if not dry_run:
                try:
                    execution_id = _react(
                        advisor_result,
                        entry,
                        effective_size,
                        paper_zero_costs=bool(rails.get("paper_zero_costs", False)),
                    )
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
) -> str:
    """Fire the PaperReactor for an autonomous decision and return the
    synthesized proposal_id (used as the execution_id surfaced in tick output).

    We construct a minimal Proposal-shaped object on the fly so we can
    reuse PaperReactor without forcing it to depend on the proposal store.
    The execution lands in executions.jsonl per ADR-0015 §D6.

    Fail-closed guard (paper-mode-only cost-gate override):
      `paper_zero_costs=True` REQUIRES that the active reactor is named
      'paper'. If a non-paper reactor were ever wired in here while the
      flag is set, raise ValueError before any execution side-effect.
      Live behavior must be unaffected by this flag — silence-by-default.
    """
    from hermes_quant.proposals import Proposal, _make_proposal_id, _utc_now
    from hermes_quant.react import PaperReactor

    reactor = PaperReactor()
    if paper_zero_costs and getattr(reactor, "name", None) != "paper":
        raise ValueError("paper_zero_costs is set but reactor is not paper")

    # Synthesize a Proposal stand-in. We DO NOT register it in the
    # proposal store — autonomous fires bypass HITL's pending state.
    # The execution is the audit trail.
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

    reactor.execute(
        proposal,
        fill_size_pct=fill_size_pct,
        approver_user_id="autonomous",
        play_tag="autonomous",  # B13: stamp the autonomous-tick source on the fill
    )

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
