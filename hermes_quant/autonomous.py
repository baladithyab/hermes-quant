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

import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)


# ADR-0092 ph3: resolve the quant state root via the single env-honoring
# resolver instead of binding ``Path.home() / ".hermes" / "quant"`` directly.
# Byte-identical in production (no env -> same default), but a cron/standalone
# process that exports HERMES_QUANT_HOME / HERMES_HOME BEFORE importing this
# module now gets an isolated home — the coupling the operator reproduced where
# the tick ignored the override because the constant was bound to ~/.hermes at
# import. The symbol stays a MODULE GLOBAL (not a function) so the existing
# monkeypatch surface (tests setattr autonomous.QUANT_HOME / .KILL_SWITCH_PATH)
# is preserved unchanged.
QUANT_HOME = _resolve_quant_home()
KILL_SWITCH_PATH = QUANT_HOME / "autonomous_kill_switch.json"
# HERMES_QUANT_POST_LOSS_COOLDOWN seam: durable sidecar recording the latest
# realized-loss timestamp per (account_id, asset_class, asset) so Rule 4 of
# DefaultRiskGate can fire across ticks (gate._cooldowns resets every tick).
# Default path; the constant is module-level so tests can monkeypatch it.
_LOSS_COOLDOWN_SIDECAR_PATH = QUANT_HOME / "loss_cooldown_state.json"


# ar73 / ADR-0016 §D9 concurrent-positions rail — account_id used to key the
# per-account advisory lock. The autonomous paper path writes under the
# execution-bus "paper-default" account sentinel (see react/paper.py +
# compute_cumulative_realized_pnl_pct's same sentinel), so the rail lock is
# keyed the same way: one in-flight rail-region per account.
_AUTONOMOUS_ACCOUNT_ID = "paper-default"

# W5 / ADR-0036: gates threading entry.horizon_set -> recommend_multi_horizon
# (the multi-horizon fan-out) AND the chosen rung's DTE window into the options
# producer. DEFAULT-OFF rail. Bound to a module constant (the `_FLAG` form) so
# the flag-inventory scanner's _CONST + _VIA_CONST regexes pick it up and a typo
# never silently enables the new scan/fan-out path. Read via `== "1"` (fail-
# closed: anything but the literal "1" leaves the tick byte-identical to today —
# the single advisor_recommend(timeframe=entry.timeframe) call, fixed-DTE options).
_MULTI_HORIZON_TICK_FLAG = "HERMES_QUANT_MULTI_HORIZON_TICK"

# Max wall-clock a second overlapping tick waits to acquire the §D9 rail lock
# before giving up and SKIPPING this tick (silence-by-default; recoverable next
# tick). A tick's rail-sensitive region is short (one watchlist pass), so this
# is generous. Bounded so a stuck/dead holder can never wedge the cron forever.
# Overridable via HERMES_QUANT_RAIL_LOCK_TIMEOUT_S (tests / tuning).
_RAIL_LOCK_TIMEOUT_S = 30.0
_RAIL_LOCK_TIMEOUT_ENV = "HERMES_QUANT_RAIL_LOCK_TIMEOUT_S"


def _rail_lock_timeout_s() -> float:
    """Resolve the §D9 rail-lock acquire timeout, honoring the env override.

    ar10 finite-guard posture: reject NaN / inf / negative (an `inf` makes the
    poll deadline never elapse, so a contended lock would spin forever instead
    of SKIPPING the tick). Fall back to the finite default on any bad value.
    """
    raw = os.environ.get(_RAIL_LOCK_TIMEOUT_ENV)
    if raw is None:
        return _RAIL_LOCK_TIMEOUT_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _RAIL_LOCK_TIMEOUT_S
    return val if (math.isfinite(val) and val >= 0.0) else _RAIL_LOCK_TIMEOUT_S


@contextmanager
def _account_rail_lock(account_id: str, *, lock_dir: Path) -> Iterator[bool]:
    """Per-account advisory file lock around the ADR-0016 §D9 read-decide-fire
    window of ``tick()``.

    The §D9 concurrent-positions rail reads the WHOLE open book once at tick
    start (OUTSIDE any lock) and then enforces ``max_concurrent_positions``
    against tick-LOCAL counters (``open_positions_at_tick_start +
    fires_this_tick``). Two overlapping ticks — the 0,30 cron (``hermes quant
    autonomous tick``) and the agent TOOL path (``quant_autonomous_tick``) both
    reach ``tick()`` — would otherwise each read the same stale pre-fire book
    and each admit a DISTINCT new symbol, jointly breaching the account-wide
    cap. The per-symbol bus serialization (``signal_bus.append_locked``) is
    per-WRITE, not per-tick, and never serializes two DIFFERENT new symbols; the
    reaction-layer locks are downstream of the §D9 read+enforce, which both
    complete BEFORE ``execute()`` is entered, so they cannot close this race.

    This lock is an ALWAYS-ON hard safety rail (not a default-OFF sizing
    refinement): the read-decide-fire window must be atomic per account so a
    second overlapping tick either (a) waits for the first to commit and then
    re-reads the now-larger book (correctly silencing via
    SILENCE_CONCURRENT_CAP), or (b) on timeout/contention SKIPS this tick — it
    must NEVER proceed against a stale pre-fire count.

    Yields ``True`` when the lock was acquired and the caller MUST run the rail
    region; yields ``False`` when the caller should SKIP this tick.

    Fail-OPEN-SAFE: ONLY a genuine flock-unsupported infrastructure error (a
    platform without ``fcntl``, a read-only FS, an OSError that is NOT a
    would-block) degrades to running unguarded — matching the documented
    daemon/tick_lock posture (never wedge an always-on money tick on a transient
    fs fault). A would-block / timeout is CONTENTION and SKIPS the tick (it does
    NOT silently proceed unguarded).

    The lock file lives under ``lock_dir`` (the live-fs ``QUANT_HOME``, like
    ``daemon.tick_lock``), so a multiprocessing concurrency test agrees on the
    same lock as long as the processes agree on the home.
    """
    lock_path = lock_dir / f"autonomous-rail-{account_id}.lock"
    fd: int | None = None

    # --- open/create the lock file -------------------------------------------
    # A failure HERE is an infrastructure problem (no fs, no perms) -> FAIL OPEN.
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.warning(
            "autonomous: §D9 rail lock unavailable (%s); proceeding UNGUARDED "
            "(fail-open-safe infra fallback)",
            exc,
        )
        yield True
        return

    acquired = False
    try:
        import errno
        import fcntl

        timeout_s = _rail_lock_timeout_s()
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    # A non-contention flock error (e.g. flock unsupported on
                    # this filesystem). Fail-OPEN-SAFE: run unguarded rather than
                    # wedge the always-on tick on an infra fault.
                    logger.warning(
                        "autonomous: §D9 rail flock failed (%s); proceeding "
                        "UNGUARDED (fail-open-safe infra fallback)",
                        exc,
                    )
                    yield True
                    return
                if time.monotonic() >= deadline:
                    # Contention persisted past the bound. SKIP this tick
                    # (silence-by-default; recoverable next tick) rather than
                    # proceed against a possibly-stale pre-fire count.
                    logger.warning(
                        "autonomous: §D9 rail lock contended for %.1fs on "
                        "account=%s; SKIPPING this tick (recoverable next tick)",
                        timeout_s,
                        account_id,
                    )
                    yield False
                    return
                time.sleep(0.02)
        yield True
    finally:
        try:
            if acquired:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


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
        # ar81: int-count guard on the silence-bias quorum/veto counts (the int analogue of
        # the ar09 finite-threshold guard, reusing the ar61 _positive_int_count helper). A
        # bare int(raw) on these operator-YAML values: inf->OverflowError / nan/float-string
        # ->ValueError ABORTS the whole tick (no try/except at the call site); a float-form
        # token (1.9) silently truncates a 2-of-N quorum to 1; 0/negative makes the quorum
        # NEVER silence so a single-/zero-voice signal can FIRE (contradicts "single-voice is
        # never enough in autonomous mode"). Fall CLOSED to the documented default.
        min_analysts_emitted=_positive_int_count(
            raw.get("min_analysts_emitted", 2), 2, "min_analysts_emitted"
        ),
        max_recent_rejections=_positive_int_count(
            raw.get("max_recent_rejections", 3), 3, "max_recent_rejections"
        ),
        salience_window_hours=_positive_int_count(
            raw.get("salience_window_hours", 168), 168, "salience_window_hours"
        ),
    )


def _positive_int_count(raw: object, default: int, name: str) -> int:
    """Coerce an operator-YAML count to a positive int, failing CLOSED.

    The int-count analogue of the float-threshold finite-guard (the ar08-12
    family). The §D9 safety-rail counts (max_per_tick_opens,
    max_concurrent_positions) are HARD rails — a malformed operator value must
    never silently neuter them or abort the tick.

    Two distinct hazards this guards against, both of which `int(raw)` alone
    mishandles:
      1. Silent fail-OPEN: PyYAML parses a float-form token like
         `1000000000.0` or `1.0e+9` to a Python float; `int(1e9)` succeeds and
         the rail's `>= cap` test never trips -> the autonomous book grows
         UNBOUNDED. We treat a non-int (float/str/…) as malformed and fall
         CLOSED to the documented default.
      2. Crash / DoS of the gate: `.inf` -> int() raises OverflowError; `.nan`
         -> ValueError; `abc` / `1e9` (str) -> ValueError. Uncaught at the
         `rails = _read_safety_rails()` call site, this aborts the ENTIRE tick
         before any gate or kill-switch evaluation runs. We catch and fall
         CLOSED instead.

    A value < 1 would also disable the per-tick / concurrent rail (a cap of 0
    can never be reached as a lower bound, and a negative is nonsense), so it
    likewise falls back to the conservative default.

    Byte-identical for any legal positive int (`int` or an int-valued config).
    """
    # Reject non-int types up front (a float like 1e9 from a YAML float-form
    # token must NOT silently pass through as a billion-slot cap). bool is an
    # int subclass but a True/False cap is nonsense -> treat as malformed.
    if isinstance(raw, bool) or not isinstance(raw, int):
        try:
            val = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            logger.warning(
                "autonomous: safety-rail count %s=%r is not a valid integer; "
                "falling CLOSED to default %d (D9 rail preserved)",
                name,
                raw,
                default,
            )
            return default
        # A float/str token that DID coerce (e.g. 1000000000.0 -> 1000000000):
        # the operator wrote a non-int form for a hard-rail count. Falling
        # closed is the conservative, fail-CLOSED choice — a billion-slot cap
        # is a silent neuter, not a legitimate config.
        logger.warning(
            "autonomous: safety-rail count %s was configured as %r (a non-int "
            "form, e.g. a float); falling CLOSED to default %d so the D9 rail "
            "is not silently neutered",
            name,
            raw,
            default,
        )
        return default
    val = raw
    if val < 1:
        logger.warning(
            "autonomous: safety-rail count %s=%d is < 1 (would disable the D9 "
            "rail); falling CLOSED to default %d",
            name,
            val,
            default,
        )
        return default
    return val


def _read_safety_rails() -> dict:
    cfg = _read_config()
    auto = (cfg.get("quant") or {}).get("autonomous") or {}
    risk = (cfg.get("quant") or {}).get("risk") or {}
    return {
        "max_per_tick_opens": _positive_int_count(
            auto.get("max_per_tick_opens", 1), 1, "max_per_tick_opens"
        ),
        "max_concurrent_positions": _positive_int_count(
            auto.get("max_concurrent_positions", 5), 5, "max_concurrent_positions"
        ),
        "kill_switch_pct": _finite_threshold(auto.get("kill_switch_pct", 0.10), 0.10, "kill_switch_pct"),
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
        # Per-position UNREALIZED-loss stop (2026-06-17, the June-4 ASTS -20.9% fix).
        # Distinct from the entry-size `require_stop_loss` backstop above: this watches
        # an OPEN position decline and force-exits it when its unrealized loss from
        # entry breaches the threshold. Gated default-OFF by HERMES_QUANT_PER_POSITION_STOP
        # (the env flag is read in tick(), not here). The threshold is finite-guarded to
        # the research-chosen 8% default (1.6% NAV at the 20% max position) so a
        # NaN/inf/<=0 operator value cannot silently disarm the rail (ar08/ar09 family).
        "per_position_take_profit_pct": _finite_threshold(
            auto.get("per_position_take_profit_pct", 0.16), 0.16, "per_position_take_profit_pct"
        ),
        "per_position_stop_loss_pct": _finite_threshold(
            auto.get("per_position_stop_loss_pct", 0.08), 0.08, "per_position_stop_loss_pct"
        ),
        # aegis-agmon2: options take-profit fraction (fraction of a credit structure's
        # MAX GAIN captured before a structure-aware close). Finite-guarded to 0.50 so a
        # NaN/inf/<=0 operator value cannot silently disarm / over-fire the rail.
        "options_take_profit_fraction": _finite_threshold(
            auto.get("options_take_profit_fraction", 0.50), 0.50, "options_take_profit_fraction"
        ),
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


def _account_nav_mtm() -> float | None:
    """MTM-first NAV for the paper account, used by the durable drawdown baseline.

    Called only when HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE=1.  Attempts to
    compute true mark-to-market equity via PortfolioState.get_marked_equity()
    using the latest close price from build_perception_frame_live for each open
    position.  Falls back to cost-basis NAV (_account_nav_usd()) if:
      - no positions are open (cost-basis == MTM for a cash-only book), OR
      - all mark fetches fail (network error, perception unavailable), OR
      - the MTM result is non-finite or <= 0.

    Rationale: cash.equity_total (the source of _account_nav_usd) is written by
    apply_execution() using avg_entry_price — cost-basis arithmetic that is
    self-cancelling for NAV-fraction fills.  A position bought at $50 and marked
    at $40 does NOT reduce equity_total.  The durable HWM would therefore anchor
    to initial_cash and the ADR-0004 drawdown breaker would never trip, even as
    real unrealized losses accumulate (P2 fail-open confirmed on HEAD 6cbab3f).

    This function is purely additive behind the existing flag and does NOT change
    behaviour when the flag is absent (autonomous.py:1745 guards the call).
    """
    cost_basis = _account_nav_usd()
    try:
        from hermes_quant.perception import build_perception_frame_live
        from hermes_quant.state.portfolio_state import get_portfolio_state

        ps = get_portfolio_state()
        positions = ps.get_positions("paper-default")
        if not positions:
            # Cash-only book: cost-basis == MTM; skip the per-symbol fetch.
            return cost_basis

        mark_prices: dict[str | tuple[str, str], float] = {}
        for (asset_class, symbol) in positions:
            try:
                frame = build_perception_frame_live(
                    symbol, asset_class=asset_class, timeframe="1d"
                )
                mark = getattr(frame, "last_close", None) if frame is not None else None
                if mark is not None and math.isfinite(mark) and mark > 0:
                    # cs35-compatible: composite key (asset_class, symbol) first so
                    # get_marked_equity resolves the right mark for same-underlying
                    # equity + us_option positions.
                    mark_prices[(asset_class, symbol)] = mark
                    mark_prices[symbol] = mark  # bare-symbol fallback
            except Exception as _mark_exc:  # noqa: BLE001
                logger.debug(
                    "autonomous: MTM mark fetch failed for %s/%s (falling back "
                    "to avg_entry for this position): %s",
                    asset_class,
                    symbol,
                    _mark_exc,
                )

        me = ps.get_marked_equity("paper-default", mark_prices)
        if me is not None and math.isfinite(me.marked_equity) and me.marked_equity > 0:
            return float(me.marked_equity)
        return cost_basis
    except Exception as exc:  # noqa: BLE001 — fail-closed: cost-basis is always >= MTM for long-only book
        logger.debug(
            "autonomous: MTM nav unavailable, using cost-basis for durable baseline: %s",
            exc,
        )
        return cost_basis


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
    # ar86: fsync the PARENT DIR so the rename ITSELF is crash-durable. fsyncing
    # only the file fd flushes the file DATA but not the directory entry the rename
    # created — a power-loss after os.replace returns can revert the rename, reading
    # the kill-switch back to its PRE-trip state on reboot (a FAIL-OPEN on the
    # ADR-0016 §D9 rail). Same atomic-write-durability pattern as the journal /
    # artifacts writers (wave-6 538b2f6 / 8e69840). Best-effort: a dir-fsync failure
    # must not mask a successful trip, so warn and proceed.
    try:
        dfd = os.open(str(KILL_SWITCH_PATH.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:  # pragma: no cover - platform/fs dependent
        logger.warning(
            "kill-switch: parent-dir fsync failed for %s; the trip rename may not "
            "survive a crash: %s",
            KILL_SWITCH_PATH.parent,
            e,
        )


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

        # ar39 — INCREMENTAL SETTLEMENT (was an unbounded per-tick read). The bus
        # (executions.jsonl) is append-only and NEVER rotated (signal_bus.py: "Bus
        # files are never rotated in v0.1"), so the old `path.read_text().splitlines()`
        # + `join_exit_fills(all_records)` re-parsed and re-matched the ENTIRE lifetime
        # bus on EVERY autonomous tick. Per-tick RSS + CPU grow without bound; the
        # always-on §D9 rail eventually slows past the tick deadline / risks OOM
        # (fail-OPEN on the secondary rail). The matcher already supports incremental
        # settlement: `join_exit_fills(records, open_lots=carry_in)` returns the residual
        # open-lot state to thread into the next call. We persist a durable checkpoint
        # (byte offset consumed, carry-in open_lots, accumulated fraction from already-
        # settled/evicted round-trips, file inode, max asof consumed) and each tick reads
        # ONLY the bytes PAST the offset.
        #
        # CORRECTNESS (a wrong basis is worse than a slow one): the incremental result
        # MUST EQUAL a full replay for ANY bus. A position OPENED in an early batch and
        # CLOSED in a later batch is paired correctly because the carry-in open_lots
        # holds the older opener (a naive tail-slice that dropped it would mis-pair the
        # close — that is exactly what the cross-checkpoint equality test guards). The
        # ONE case the carry-in cannot fix is a late append whose asof sorts BEFORE an
        # already-evicted (settled) round-trip — join_exit_fills re-sorts only the carry-
        # in + new records, not the evicted ones. We therefore guard on the max asof
        # consumed: if any new record predates it, the bus is not asof-monotonic at this
        # boundary and we FALL BACK to a full replay (fail-safe: correctness over speed).
        # A missing/corrupt checkpoint, or a bus that shrank / was rotated (offset > file
        # size, or a different inode), also falls back to a full replay and rebuilds the
        # checkpoint.
        ckpt = _read_incremental_checkpoint(path)
        # The matcher's interpretation of the SAME bytes depends on
        # HERMES_QUANT_DELTA_NORMALIZER (settlement_loop runs the FillDeltaNormalizer
        # pre-pass under flag ON). A checkpoint built under one flag state is INVALID
        # under the other — flipping the flag must change the live basis (ADR-0091 item
        # 11). Invalidate (full replay) when the flag differs from the checkpoint's.
        norm_flag = os.environ.get("HERMES_QUANT_DELTA_NORMALIZER", "0")
        if ckpt is not None and ckpt.get("norm_flag") != norm_flag:
            ckpt = None
        new_lines, new_offset, full_replay = _read_bus_since_checkpoint(path, ckpt)
        if full_replay:
            # Cold start / corrupt checkpoint / shrink / rotation: replay from scratch.
            carried_frac = 0.0
            carry_open: dict | None = None
            prev_max_asof: int | None = None
        else:
            carried_frac = ckpt["cum_frac"]  # type: ignore[index]
            carry_open = _deserialize_open_lots(ckpt["open_lots"])  # type: ignore[index]
            prev_max_asof = ckpt.get("max_asof_ns")  # type: ignore[union-attr]

        new_records: list[dict] = []
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                new_records.append(json.loads(line))
            except (ValueError, TypeError):
                continue

        # Out-of-order guard: a new record older than an already-evicted settled record
        # would diverge from a full replay (the evicted rt is not re-sorted). Fall back.
        new_max_asof = prev_max_asof
        if not full_replay and new_records:
            batch_min, batch_max = _asof_bounds_ns(new_records)
            if batch_min is not None and prev_max_asof is not None and batch_min < prev_max_asof:
                full_replay = True
            elif batch_max is not None:
                new_max_asof = batch_max if prev_max_asof is None else max(prev_max_asof, batch_max)

        if full_replay:
            # Replay from scratch. Read as BYTES and consume only up to the last complete
            # newline so the rebuilt offset never lands mid-line (a concurrent settlement
            # writer may have a partial trailing line in flight).
            raw = path.read_bytes()
            last_nl = raw.rfind(b"\n")
            consumed = raw[: last_nl + 1] if last_nl >= 0 else b""
            new_records = []
            for line in consumed.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    new_records.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
            carried_frac = 0.0
            carry_open = None
            new_offset = len(consumed)
            _, new_max_asof = _asof_bounds_ns(new_records)

        if not new_records and full_replay:
            return 0.0

        round_trips, residual_open = join_exit_fills(new_records, open_lots=carry_open)
        frac = carried_frac + _sum_round_trip_realized_fraction(round_trips)
        if not math.isfinite(frac):
            return 0.0

        # HERMES_QUANT_POST_LOSS_COOLDOWN seam: persist new realized losses to the
        # durable sidecar so the next tick's gate can pre-seed Rule 4's _cooldowns.
        # Flag OFF (default) => no sidecar write => byte-identical to pre-fix behavior.
        if os.environ.get("HERMES_QUANT_POST_LOSS_COOLDOWN", "0") == "1" and round_trips:
            _persist_round_trip_losses(round_trips, _LOSS_COOLDOWN_SIDECAR_PATH)

        # Advance + persist the checkpoint so the NEXT tick reads only bytes past
        # new_offset and carries forward the residual open lots + accumulated fraction.
        _persist_incremental_checkpoint(
            path,
            offset=new_offset,
            cum_frac=frac,
            open_lots=residual_open,
            max_asof_ns=new_max_asof,
            norm_flag=norm_flag,
        )
        _persist_last_known_cum_pnl(frac)
        return frac
    except Exception as exc:  # noqa: BLE001 - degraded rail: carry last-known forward, never silently re-arm
        logger.warning("autonomous: cumulative-PnL computation failed: %s", exc)
        last_known = _read_last_known_cum_pnl()
        _emit_killswitch_degraded_audit(exc, last_known)
        return last_known if last_known is not None else 0.0


def _sum_round_trip_realized_fraction(round_trips) -> float:  # noqa: ANN001
    """Sum the signed realized NAV-fraction contribution of a set of round-trips.

    Factored out of compute_cumulative_realized_pnl_pct so the incremental path (ar39)
    can sum ONLY the new batch's round-trips and add to the carried-forward accumulated
    fraction, while preserving the exact ar34 / ar25 / ar57 weighting used by full replay.

    ar34: this rail is the AUTONOMOUS lane's realized-drawdown as a fraction of the
    paper-default NAV. The shared executions.jsonl ALSO carries other accounts whose
    qty is in a DIFFERENT unit system — notably the freqtrade crypto consumer writes
    account_id="freqtrade" with qty = RAW COIN COUNT (e.g. 0.5 ETH), not a NAV fraction.
    Pooling a raw-coin qty into `Σ realized_return × qty` corrupts the paper-NAV fraction
    (0.5 coins reads as 50% of NAV) and can spuriously trip OR mask the kill-switch.
    Restrict to the autonomous lane's own account (paper-default — the sentinel
    _normalize_exec_record assigns when no explicit account_id is set); other accounts
    have their own rails and must not pollute this NAV-fraction basis.

    ar25: within paper-default, a SINGLE-LEG fill's qty is ALREADY a NAV-fraction, so its
    NAV-fraction P&L is realized_return × qty. Sum directly — NAV cancels (do NOT multiply
    by entry_price, do NOT divide by NAV). A non-finite term is skipped.

    ar57: a MULTI-LEG per-leg child is the EXCEPTION. MultiLegPaperReactor._build_records
    writes EVERY leg's fill_size_pct == the WHOLE family's NAV fraction F (a proxy), so
    qty == F for every leg — NOT a true per-leg weight. Summing realized_return × F per leg
    (a) over-counts F once per leg and (b) weights legs of vastly different true notionals
    EQUALLY, so a small offsetting option leg masks a large stock-leg loss → the basis
    biases POSITIVE and the kill-switch fails to trip (fail-OPEN). For a multi-leg leg the
    authoritative size is reactor_metadata.quantity (signed TRUE units, carried as
    rt.true_units); its real NAV-fraction P&L is
        realized_return × (|true_units| × entry_price × contract_multiplier) / NAV
    which makes Σ over a family's legs equal the family's true net realized NAV fraction.
    We read NAV ONLY for these legs; a multi-leg leg with NAV unreadable or missing
    true_units fails CLOSED to the equal-F proxy (never silently drops a realized loss).
    """
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
    return frac


# --------------------------------------------------------------------------- #
# ar39 — incremental-settlement checkpoint (durable sidecar per bus path)
# --------------------------------------------------------------------------- #
def _incremental_checkpoint_path(bus_path: Path) -> Path:
    """Durable checkpoint sidecar for the incremental kill-switch basis (ar39).

    Keyed to the bus filename so a test bus and the production bus never share a
    checkpoint. Lives next to the bus (under QUANT_HOME in production)."""
    return bus_path.with_name(bus_path.name + ".killswitch_cum_pnl_ckpt.json")


def _serialize_open_lots(open_lots: dict | None) -> list:
    """Serialize join_exit_fills' carry-out open_lots to a JSON-safe structure.

    open_lots is {bucket_key_tuple: [lot_dict, ...]}; bucket keys are tuples (a real
    bucket (account, asset_class, asset) or a namespaced ("_deferred", account,
    asset_class, asset)); lot dicts may carry a pd.Timestamp ``asof``. We emit
    [[key_as_list, [lot_with_isoformat_asof, ...]], ...]. On deserialize the asof goes
    back through settlement_loop._coerce_asof (accepts ISO strings), so it round-trips
    faithfully — the equality test vs full replay catches any drift."""
    if not open_lots:
        return []
    out: list = []
    for key, lots in open_lots.items():
        key_list = list(key)
        ser_lots = []
        for lot in lots:
            lot_copy = dict(lot)
            asof = lot_copy.get("asof")
            if asof is not None and hasattr(asof, "isoformat"):
                lot_copy["asof"] = asof.isoformat()
            ser_lots.append(lot_copy)
        out.append([key_list, ser_lots])
    return out


def _deserialize_open_lots(serialized: list | None) -> dict | None:
    """Inverse of _serialize_open_lots: rebuild the {tuple_key: [lot, ...]} carry-in.

    asof stays an ISO string; join_exit_fills re-coerces it via _coerce_asof on carry-in."""
    if not serialized:
        return None
    out: dict = {}
    for entry in serialized:
        key_list, lots = entry
        out[tuple(key_list)] = [dict(lot) for lot in lots]
    return out


def _asof_bounds_ns(records: list[dict]) -> tuple[int | None, int | None]:
    """Return (min, max) of the records' asof in integer nanoseconds, ignoring
    unparseable / missing asof. Used for the ar39 out-of-order monotonicity guard."""
    from hermes_quant.daemon.settlement_loop import _coerce_asof

    lo: int | None = None
    hi: int | None = None
    for rec in records:
        asof = _coerce_asof(rec.get("asof_execution") or rec.get("asof"))
        if asof is None:
            continue
        ns = asof.value
        lo = ns if lo is None else min(lo, ns)
        hi = ns if hi is None else max(hi, ns)
    return lo, hi


_CKPT_FINGERPRINT_BYTES = 4096


def _prefix_fingerprint(bus_path: Path, offset: int) -> str:
    """Fingerprint the LAST <=4KB of the consumed prefix [0, offset).

    Cheap (bounded read) rotation detector that does NOT rely on inode stability —
    inodes can be reused after unlink+recreate, and a rotated bus of the SAME byte size
    would otherwise leave offset==size with stale carried state (a real divergence from
    full replay). If the bytes ending at the checkpoint offset differ from what we
    consumed, the file was rewritten => full replay (fail-safe). offset==0 fingerprints
    the empty prefix (a valid cold checkpoint)."""
    if offset <= 0:
        return hashlib.sha256(b"").hexdigest()
    start = max(0, offset - _CKPT_FINGERPRINT_BYTES)
    try:
        with open(bus_path, "rb") as f:
            f.seek(start)
            chunk = f.read(offset - start)
    except OSError:
        return ""
    h = hashlib.sha256()
    h.update(str(offset).encode())
    h.update(b"|")
    h.update(chunk)
    return h.hexdigest()


def _read_incremental_checkpoint(bus_path: Path) -> dict | None:
    """Read the incremental checkpoint, or None if absent/corrupt/schema-mismatch.

    A None return forces a full replay (fail-safe). The checkpoint carries the consumed
    byte offset, the file inode it was built against, a fingerprint of the consumed prefix
    tail, the accumulated cumulative fraction, the serialized carry-in open_lots, and the
    max asof (ns) consumed so far."""
    path = _incremental_checkpoint_path(bus_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Required keys; a missing one is treated as a corrupt checkpoint (full replay).
        offset = int(data["offset"])
        inode = int(data["inode"])
        cum_frac = float(data["cum_frac"])
        open_lots = data["open_lots"]
        prefix_fp = str(data["prefix_fp"])
        if not math.isfinite(cum_frac):
            return None
        return {
            "offset": offset,
            "inode": inode,
            "cum_frac": cum_frac,
            "open_lots": open_lots,
            "max_asof_ns": data.get("max_asof_ns"),
            "prefix_fp": prefix_fp,
            "norm_flag": data.get("norm_flag"),
        }
    except Exception:  # noqa: BLE001 - a corrupt/partial checkpoint => full replay
        return None


def _read_bus_since_checkpoint(
    bus_path: Path, ckpt: dict | None
) -> tuple[list[str], int, bool]:
    """Read ONLY the bus bytes past the checkpoint offset.

    Returns (new_lines, new_offset, full_replay_required). full_replay_required is True
    when there is no usable checkpoint, OR the bus shrank (offset > current size — the
    file was truncated/rotated in place), OR the bus inode changed (rotated/recreated).
    In all those cases the caller replays from scratch and rebuilds the checkpoint."""
    try:
        st = bus_path.stat()
    except OSError:
        return [], 0, True
    cur_size = st.st_size
    cur_inode = st.st_ino
    if ckpt is None:
        return [], cur_size, True
    if ckpt["inode"] != cur_inode:
        # Rotated / recreated under the same name — stale offset, replay.
        return [], cur_size, True
    if ckpt["offset"] > cur_size:
        # Bus shrank (truncated in place) — stale offset, replay.
        return [], cur_size, True
    # Content check: the consumed-prefix tail must still match. Catches an in-place
    # rewrite / same-size rotation / inode-reuse that the size+inode checks miss
    # (correctness over speed: a mismatch => full replay).
    if _prefix_fingerprint(bus_path, ckpt["offset"]) != ckpt["prefix_fp"]:
        return [], cur_size, True
    # Healthy incremental read: only the bytes appended since the checkpoint.
    with open(bus_path, "rb") as f:
        f.seek(ckpt["offset"])
        tail = f.read()
    # Consume only up to the LAST complete newline — a concurrent settlement writer may
    # have a partial trailing line in flight. Leaving the partial line unconsumed (the
    # offset stops at the last "\n") means the next tick re-reads it once it is complete,
    # never dropping a fill (mirrors signal_bus.tail's partial-line buffering).
    last_nl = tail.rfind(b"\n")
    consumed = tail[: last_nl + 1] if last_nl >= 0 else b""
    new_lines = consumed.decode("utf-8", errors="replace").splitlines()
    new_offset = ckpt["offset"] + len(consumed)
    return new_lines, new_offset, False


def _persist_incremental_checkpoint(
    bus_path: Path,
    *,
    offset: int,
    cum_frac: float,
    open_lots: dict | None,
    max_asof_ns: int | None,
    norm_flag: str,
) -> None:
    """Atomically persist the incremental checkpoint (ar39). Atomic tmp+fsync+rename,
    best-effort — a persist failure must never break the healthy compute (next tick just
    full-replays). Records the inode + consumed-prefix fingerprint so a rotation
    invalidates the offset, and the normalizer-flag state so a flag flip forces a replay."""
    path = _incremental_checkpoint_path(bus_path)
    try:
        try:
            inode = bus_path.stat().st_ino
        except OSError:
            inode = 0
        payload = {
            "offset": int(offset),
            "inode": int(inode),
            "cum_frac": float(cum_frac),
            "open_lots": _serialize_open_lots(open_lots),
            "max_asof_ns": int(max_asof_ns) if max_asof_ns is not None else None,
            "prefix_fp": _prefix_fingerprint(bus_path, int(offset)),
            "norm_flag": str(norm_flag),
            "asof": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort, never break the rail
        logger.warning("autonomous: failed to persist incremental kill-switch checkpoint: %s", exc)


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


# ---------------------------------------------------------------------------
# HERMES_QUANT_POST_LOSS_COOLDOWN seam — Rule 4 live-path fix
# ---------------------------------------------------------------------------


def _persist_round_trip_losses(
    round_trips: list,
    sidecar_path: Path,
) -> None:
    """Persist realized-loss timestamps from new round-trips to the loss-cooldown
    sidecar so the next tick's gate can pre-seed Rule 4's _cooldowns.

    Only processes paper-default account_id round-trips (same filter as the
    kill-switch basis) and only those whose realized_return is finite and < 0.
    A loss is identified by realized_return < 0 (the FIFO matcher's sign convention:
    positive = profit, negative = loss). asof_exit is used as loss_at (the time the
    loss became locked in), UTC-normalized for clean Rule-4 comparison.

    Best-effort — a persist failure must never break the healthy tick.
    """
    try:
        from hermes_quant.daemon.settlement_loop import (
            _coerce_asof,
            persist_loss_cooldown_sidecar,
        )

        losses: dict[tuple[str, str, str], object] = {}
        for rt in round_trips:
            if getattr(rt, "account_id", "paper-default") != "paper-default":
                continue
            ret = getattr(rt, "realized_return", None)
            if ret is None or not math.isfinite(ret) or ret >= 0.0:
                continue
            # asof_exit is the fill-time of the closing fill; UTC-normalized.
            raw_asof = getattr(rt, "asof_exit", None)
            ts = _coerce_asof(raw_asof)
            if ts is None:
                continue
            key = (
                str(getattr(rt, "account_id", "paper-default")),
                str(getattr(rt, "asset_class", "equity")),
                str(getattr(rt, "asset", "")),
            )
            # Keep the most recent loss per bucket.
            existing = losses.get(key)
            if existing is None or ts > existing:  # type: ignore[operator]
                losses[key] = ts
        if losses:
            persist_loss_cooldown_sidecar(losses, sidecar_path)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - best-effort; never block the tick
        logger.warning("autonomous: failed to persist round-trip losses to sidecar: %s", exc)


def _build_gate_with_cooldowns(sidecar_path: Path):
    """Build a DefaultRiskGate pre-seeded with loss timestamps from the sidecar.

    This restores cross-tick Rule-4 state that was lost because the gate is
    constructed fresh every tick (the gate is stateless; the sidecar is the
    durable state). Returns a plain DefaultRiskGate (no injected cooldowns) on
    any read failure — fail-silent so a corrupt sidecar cannot block trading.

    Called ONLY when HERMES_QUANT_POST_LOSS_COOLDOWN=1 (the flag is checked by the
    caller). When the flag is OFF the caller skips this entirely and the gate is
    constructed normally by advisor.recommend() — byte-identical to pre-fix.
    """
    try:
        from hermes_quant.daemon.settlement_loop import load_loss_cooldown_sidecar
        from hermes_quant.risk.gate import DefaultRiskGate

        gate = DefaultRiskGate()
        cooldowns = load_loss_cooldown_sidecar(sidecar_path)
        for (account_id, asset_class, asset), ts in cooldowns.items():
            gate.record_loss(account_id, asset_class, asset, ts)
        return gate
    except Exception as exc:  # noqa: BLE001 - fail-silent; a broken gate is no gate
        logger.warning("autonomous: could not build seeded gate from sidecar: %s", exc)
        try:
            from hermes_quant.risk.gate import DefaultRiskGate

            return DefaultRiskGate()
        except Exception:  # noqa: BLE001
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


def _emit_per_position_stop_audit(
    *, symbol: str, loss_pct: float, threshold_pct: float, held_fraction: float
) -> None:
    """Emit a ``per_position_stop_fired`` governance audit event (best-effort).

    Mirrors ``_emit_killswitch_fired_audit``: the forced exit is the rail; the audit is
    the operator-observability side-effect (a stop with no audit trace is an invisible
    money action — the ar28 lesson). Never breaks the stop on an append failure.
    """
    try:
        from hermes_quant.governance import audit_log
        from hermes_quant.governance.audit_log import GovernanceEvent

        audit_log.append(
            GovernanceEvent(
                kind="per_position_stop_fired",
                asof=datetime.now(tz=UTC),
                source="autonomous_per_position_stop",
                payload={
                    "rail": "per_position_unrealized_loss_pct",
                    "symbol": symbol,
                    "unrealized_loss_pct": loss_pct,
                    "threshold_pct": threshold_pct,
                    "held_fraction": held_fraction,
                },
            )
        )
    except Exception as audit_exc:  # noqa: BLE001 - audit is best-effort
        logger.warning(
            "autonomous: failed to emit per_position_stop_fired audit for %s: %s",
            symbol,
            audit_exc,
        )


def _emit_per_position_take_profit_audit(
    *, symbol: str, gain_pct: float, threshold_pct: float, held_fraction: float
) -> None:
    """Emit a ``per_position_take_profit_fired`` governance audit event (best-effort).

    Mirrors ``_emit_per_position_stop_audit`` (the ar28 observability lesson: a money
    action with no audit trace is invisible to the governance log + downstream consumers).
    The ``per_position_take_profit_fired`` kind MUST be registered in
    governance/audit_log.py EventKind/VALID_KINDS or pydantic silently rejects it (the
    exact ar28-pattern defect the iter-2 review caught on the stop audit)."""
    try:
        from hermes_quant.governance import audit_log
        from hermes_quant.governance.audit_log import GovernanceEvent

        audit_log.append(
            GovernanceEvent(
                kind="per_position_take_profit_fired",
                asof=datetime.now(tz=UTC),
                source="autonomous_per_position_take_profit",
                payload={
                    "rail": "per_position_unrealized_gain_pct",
                    "symbol": symbol,
                    "unrealized_gain_pct": gain_pct,
                    "threshold_pct": threshold_pct,
                    "held_fraction": held_fraction,
                },
            )
        )
    except Exception as audit_exc:  # noqa: BLE001 - audit is best-effort
        logger.warning(
            "autonomous: failed to emit per_position_take_profit_fired audit for %s: %s",
            symbol,
            audit_exc,
        )


def _establishing_avg_entry_price(symbol: str) -> float | None:
    """FIFO-consistent weighted-average entry price for an OPEN paper-default position.

    Reuses the CANONICAL settlement matcher (``settlement_loop.join_exit_fills`` ->
    ``open_lots``) — the SAME lot-matching the kill-switch basis and settlement use, so
    the stop's cost basis agrees with them rather than re-deriving lot logic (the
    duplicated-metric defect family). Returns None on any read/parse failure or when the
    symbol has no open paper-default lots (caller treats None as "no basis -> HOLD").
    """
    try:
        from hermes_quant.daemon.settlement_loop import join_exit_fills
        from hermes_quant.risk.per_position_stop import weighted_avg_entry_from_lots

        path = QUANT_HOME / "executions.jsonl"
        if not path.exists():
            return None
        recs: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                recs.append(rec)
        _rts, open_lots = join_exit_fills(recs)
        # open_lots is keyed (account_id, asset_class, asset); sum the lots for THIS
        # symbol on the paper-default account across asset_classes (equity is the only
        # one with a per-position stop today; the key match is by symbol + account).
        lots: list[dict] = []
        for (acct, _ac, asset), bucket in open_lots.items():
            if acct == "paper-default" and asset == symbol:
                lots.extend(bucket)
        return weighted_avg_entry_from_lots(lots)
    except Exception as exc:  # noqa: BLE001 - fail-soft: no basis -> HOLD
        logger.warning(
            "autonomous: could not resolve entry basis for stop on %s (HOLD): %s",
            symbol,
            exc,
        )
        return None


def _maybe_take_tranche(
    *,
    symbol: str,
    held: float,
    entry_price: float,
    mark: float,
    stop_pct: float,
    gain_pct: float,
    watch_reg,  # noqa: ANN001 - WatchRegistry; avoid the import at module top
    paper_zero_costs: bool,
    result: TickResult,
) -> bool:
    """Evaluate + execute a tranche/trailing PARTIAL exit (tp1/tp2). Returns True iff a
    tranche action FIRED (a partial exit was attempted) — the caller then skips the
    full-TP path for this symbol. Returns False when no tranche action applies (the caller
    falls through to the all-at-once TP backstop).

    A tranche step does a PARTIAL exit: the new absolute post-fill target is
    ``held - signed_rung`` (one 0.05 NAV-fraction rung toward zero, sign-preserving), NOT
    the flat 0.0 a full stop/TP uses. The position stays OPEN (lighter), so it NEVER joins
    the sweep's ``stopped`` set — it keeps its concurrency slot and remains managed. The
    registry's tranches_taken is incremented so the next tick does not re-fire tranche-1.
    Best-effort + silence-by-default: any error HOLDS (returns False).
    """
    from hermes_quant.risk.exit_strategy import TRANCHE_RUNG, evaluate_tranche

    try:
        state = watch_reg.get(symbol)
    except Exception:  # noqa: BLE001
        return False
    tranches_taken = int(getattr(state, "tranches_taken", 0)) if state is not None else 0
    peak = getattr(state, "peak_gain_pct", None) if state is not None else None

    td = evaluate_tranche(
        symbol=symbol,
        gain_pct=gain_pct,
        held_fraction=held,
        tranches_taken=tranches_taken,
        stop_pct=stop_pct,
        peak_gain_pct=peak,
    )
    if td.action == "hold":
        return False

    # Compute the PARTIAL post-fill target. exit_fraction is a positive NAV-fraction to
    # close NOW; the new absolute target moves `held` toward 0 by that amount (sign-aware).
    sign = 1.0 if held > 0 else -1.0
    close_amt = min(abs(td.exit_fraction), abs(held))
    new_target = sign * (abs(held) - close_amt)  # tranche_2/trail_exit close the residual -> 0.0
    if abs(new_target) < (TRANCHE_RUNG / 2):
        new_target = 0.0  # closing the whole residual

    entry = WatchlistEntry(symbol=symbol, asset_class="equity", timeframe="1d")
    advisor_result = {
        "decision_price": float(mark),
        "as_of": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": f"autonomous_tranche_{td.action}",
    }
    try:
        react_out = _react(advisor_result, entry, float(new_target), paper_zero_costs=paper_zero_costs)
    except Exception as exc:  # noqa: BLE001 - one symbol's failure must not abort the sweep
        logger.warning("autonomous: tranche _react failed for %s: %s", symbol, exc, exc_info=True)
        result.errors += 1
        result.decisions.append(SymbolDecision(
            symbol=symbol, asset_class="equity", timeframe="1d",
            gate="PER_POSITION_TRANCHE_ERROR", error=f"tranche_react_error: {exc}"))
        # wave3-wiring-review DEFECT-1 FIX: a RAISE means NO trade executed -> return False
        # so the caller still runs the full-TP backstop THIS tick (returning True silenced
        # the TP safety floor for a position past +2R — a fail-open). And tranches_taken is
        # NOT advanced (nothing committed), so the re-fire guard is the backstop closing it.
        return False
    sym_decision = SymbolDecision(
        symbol=symbol, asset_class="equity", timeframe="1d",
        gate="PER_POSITION_TRANCHE_FIRED",
        details={
            "tranche_action": td.action, "exit_fraction": close_amt,
            "new_target_pct": new_target, "gain_pct": gain_pct,
            "tranches_taken_before": tranches_taken, "reason": td.reason,
        },
    )
    if react_out is None:
        sym_decision.gate = "PER_POSITION_TRANCHE_NO_FILL"
        sym_decision.details["no_fill"] = True
        result.silences += 1
        result.decisions.append(sym_decision)
        # wave3-wiring-review DEFECT-1 FIX: a NO-FILL means nothing was executed and
        # tranches_taken is NOT advanced -> returning True would (a) re-fire tranche-1 every
        # tick (the reactor keeps not-filling at the same gain) AND (b) bypass the full-TP
        # backstop. Return False so the backstop runs (it will full-flatten a position past
        # its TP target, ending the loop). The position is unchanged; no double-action risk.
        return False
    execution_id, _realized = react_out
    sym_decision.execution_id = execution_id
    result.fires += 1
    result.decisions.append(sym_decision)
    # Record the tranche on the registry so the next tick advances (tranche_1 -> tranche_2).
    # A full-residual close (new_target == 0) drops the play; a partial marks the tranche.
    try:
        if new_target == 0.0:
            watch_reg.drop(symbol)
        else:
            watch_reg.mark_tranche(symbol)
    except Exception as exc:  # noqa: BLE001 - never block the rail
        logger.warning("autonomous: tranche registry update failed for %s: %s", symbol, exc)
    logger.info(
        "autonomous: PER-POSITION TRANCHE %s on %s (gain %.2f%%): close %.4f -> target %.4f via %s",
        td.action, symbol, gain_pct * 100, close_amt, new_target, execution_id,
    )
    return True


def _originate_mleg_proposal(
    *,
    symbol: str,
    asof: datetime,
    advisor_result: dict[str, Any],
    nav: float,
    options_buying_power: float,
    iv_rank: float | None,
    structure_intent: Any = None,
    paper_zero_costs: bool = False,
    result: TickResult,
    horizon_rung: str | None = None,
) -> str | None:
    """Originate an OPTIONS (multi-leg) play for a symbol (agdec1/agreact1). Returns the
    execution_id of a filled mleg proposal, or None (abstain) at any missing precondition.

    W5 (HERMES_QUANT_MULTI_HORIZON_TICK seam): ``horizon_rung`` is the decision rung
    chosen for this symbol this tick (a HORIZONS label like "30D"). When provided, the
    rung's DTE window (``dte_bucket_for_horizon`` in playbook/horizons.py) is threaded
    into ``build_and_persist_multi_leg(dte_min=, dte_max=)`` so structure_select's KIND
    and the horizon's DTE both flow to the producer. The 30D rung resolves to (25, 45)
    == the producer's fixed ``_DEFAULT_DTE_MIN/MAX``, so a 30D-only path is byte-identical
    to today. When ``horizon_rung`` is None (the production default and every flag-OFF
    tick), NO dte kwargs are injected and the producer keeps its own fixed default —
    byte-identical. The decision gate stays final: structure_select picks the kind, the
    horizon picks the DTE window, the options_gate admits/rejects.

    DEFAULT-OFF + FAIL-CLOSED chain — abstains (None, no side effect) unless ALL hold:
      * HERMES_QUANT_AUTONOMOUS_OPTIONS=1 (the master autonomous-origination flag), AND
      * structure_select_for_plan returns a producible StrategyKind (itself gated by
        HERMES_QUANT_STRUCTURE_SELECT + needs a non-None structure_intent + a usable
        iv_rank/regime — so this is INERT until the perception layer sources an as-of IV
        rank; abstaining on iv_rank=None is honest, not a bug), AND
      * the producer admits (build_and_persist_multi_leg runs the options_gate, itself
        gated by HERMES_QUANT_OPTIONS_GATE; a rejected/ungated structure persists nothing
        that can fill), AND
      * the routed reactor fills (MultiLegPaperReactor self-gates on
        HERMES_QUANT_MULTILEG_REACTOR; raises/no-fills otherwise).
    Every layer is independently default-OFF, so with HERMES_QUANT_AUTONOMOUS_OPTIONS unset
    the tick is BYTE-IDENTICAL (this function is never entered). Best-effort: any error is
    a logged abstain, never a tick-abort. The deterministic gate stays final; the LLM never
    picks legs (structure_select is a table); no naked/undefined-risk (ADR-0098 producer set).
    """
    if os.environ.get("HERMES_QUANT_AUTONOMOUS_OPTIONS", "0") != "1":
        return None
    try:
        # 1) Distil the structure (deterministic table; abstains -> None on any gap).
        # d9d7: select_structure_for_plan -> direction_from_rating reads rating.signed_intensity,
        # a @property on the PortfolioRating StrEnum. The live advisor_result has NO top-level
        # "recommendation" key and its "aggregated_signal" is a plain DICT (int direction /
        # float magnitude) with no .signed_intensity — feeding that dict raised AttributeError
        # that the outer except swallowed into the equity fallback, so options NEVER originated
        # on the real advisor path. Build a real PortfolioRating from the aggregated signal's
        # SIGN (the table only keys on sign): +dir -> OVERWEIGHT, -dir -> UNDERWEIGHT, 0 -> HOLD.
        # The LLM never picks legs; this is a deterministic sign distillation, gate stays final.
        from hermes_quant.agents.research_debate.schemas import PortfolioRating
        from hermes_quant.options.recipes import build_and_persist_multi_leg
        from hermes_quant.options.structure_select import select_structure_for_plan
        from hermes_quant.proposals import get_default_store
        from hermes_quant.react.dispatch import select_reactor

        _agg = advisor_result.get("aggregated_signal") or {}
        try:
            _dir = int(_agg.get("direction", 0))
        except (TypeError, ValueError):
            _dir = 0
        if _dir > 0:
            _rating = PortfolioRating.OVERWEIGHT
        elif _dir < 0:
            _rating = PortfolioRating.UNDERWEIGHT
        else:
            _rating = PortfolioRating.HOLD

        class _Plan:
            recommendation = _rating

        plan = _Plan()
        plan.structure_intent = structure_intent  # type: ignore[attr-defined]
        strategy_kind = select_structure_for_plan(plan, iv_rank=iv_rank)
        if strategy_kind is None:
            return None  # abstain -> the equity path stands (byte-identical to today)

        # W5: resolve the chosen rung's DTE window and thread it into the producer.
        # structure_select above already picked the KIND (it stays the FINAL kind
        # authority); the horizon only picks the DTE WINDOW. When horizon_rung is None
        # (flag-OFF / no rung) we inject NOTHING and the producer keeps its fixed
        # _DEFAULT_DTE_MIN/MAX (25/45) — byte-identical. A 30D rung also resolves to
        # (25, 45), so 30D-only is byte-identical too. Best-effort: a missing horizons
        # module / unknown rung is a logged no-op (no dte kwargs) — never a tick-abort.
        _dte_kwargs: dict[str, int] = {}
        if horizon_rung is not None:
            try:
                from hermes_quant.playbook.horizons import dte_bucket_for_horizon

                _dte_min, _dte_max = dte_bucket_for_horizon(horizon_rung)
                _dte_kwargs = {"dte_min": int(_dte_min), "dte_max": int(_dte_max)}
            except Exception as _h_exc:  # noqa: BLE001 — fail-soft to the fixed default
                logger.warning(
                    "autonomous: horizon DTE resolve failed for rung %r (%s) — "
                    "using the producer's fixed default DTE window",
                    horizon_rung,
                    _h_exc,
                )
                _dte_kwargs = {}

        # 2) Build + gate + persist (options_gate runs inside; rejects persist nothing fillable).
        store = get_default_store()
        build_result, persisted = build_and_persist_multi_leg(
            store=store,
            symbol=symbol,
            asof=asof,
            strategy_kind=strategy_kind,
            nav=float(nav),
            options_buying_power=float(options_buying_power),
            advisor_result=advisor_result,
            **_dte_kwargs,
        )
        if persisted is None:
            # Gate rejected the structure (inadmissible/over-cap/non-finite) -> abstain.
            result.decisions.append(SymbolDecision(
                symbol=symbol, asset_class="us_option", timeframe="1d",
                gate="AUTONOMOUS_OPTIONS_GATE_REJECT",
                details={"strategy_kind": str(strategy_kind),
                         "reason": getattr(build_result, "reason", "gate_reject")}))
            return None

        # 3) Route through the ONE dispatch chokepoint (select_reactor); the multileg reactor
        # self-gates on HERMES_QUANT_MULTILEG_REACTOR (raises MultiLegReactorDisabled if off).
        mleg = getattr(persisted, "multi_leg_proposal", None) or persisted
        reactor = select_reactor(mleg)
        if paper_zero_costs and getattr(reactor, "name", None) not in ("paper", "multileg-paper"):
            return None  # fail-closed: paper_zero_costs requires a paper reactor
        record = reactor.execute(mleg, fill_size_pct=0.0, approver_user_id="autonomous",
                                 play_tag="autonomous_options")
        # agreact1: route the executed record through the SHARED accounting tail so the
        # options path inherits the SAME ar38 phantom-fire guard + the UNIFORM
        # append_human_override journal write the equity fire uses (instead of a divergent
        # inline no-fill copy that drifts from the canonical guard). A no-fill returns None
        # (recorded as a silence below); a real fill journals + yields the realized size.
        fired = _apply_fire_accounting(
            record, mleg, symbol=symbol, journal_reason="autonomous_options_fire"
        )
        if fired is None:
            result.silences += 1
            result.decisions.append(SymbolDecision(
                symbol=symbol, asset_class="us_option", timeframe="1d",
                gate="AUTONOMOUS_OPTIONS_NO_FILL",
                details={"strategy_kind": str(strategy_kind)}))
            return None
        execution_id, _realized = fired
        if not execution_id:  # the record carried no usable id — fall back to the persisted one
            execution_id = str(getattr(persisted, "proposal_id", "") or "")
        result.fires += 1
        # ml00b: persist the composite-play lifecycle row WITH its option_legs so the
        # agmon1/agmon2 sweep can enumerate the open composite (multi_leg_id) and mark +
        # sign its net P&L by leg (OCC symbol + side). The fire above is already CONFIRMED
        # and accounted; this row is OBSERVABILITY for the sweep, so a store-write failure
        # is logged + swallowed and NEVER aborts the real fire (best-effort). A write that
        # IS attempted is correct (legs validated by open_composite — a legless row raises,
        # never silently re-creating the agmon1 dead-path). This whole helper is inside the
        # HERMES_QUANT_AUTONOMOUS_OPTIONS gate, so it is unreached + byte-identical when off.
        _persist_composite_play(mleg, persisted=persisted, execution_id=execution_id)
        result.decisions.append(SymbolDecision(
            symbol=symbol, asset_class="us_option", timeframe="1d",
            gate="AUTONOMOUS_OPTIONS_FIRED", execution_id=execution_id,
            details={"strategy_kind": str(strategy_kind), "iv_rank": iv_rank}))
        logger.info("autonomous: ORIGINATED options play %s on %s via %s",
                    strategy_kind, symbol, execution_id)
        return execution_id
    except Exception as exc:  # noqa: BLE001 - origination must never abort the tick
        logger.warning("autonomous: options origination failed for %s (abstain): %s",
                        symbol, exc, exc_info=True)
        return None


def _options_evidence_gate_ok() -> bool:
    """cx0 [HIGH]: the GATE-2 evidence gate for autonomous options origination.

    ADR-0029 evidence-before-live + EQUITY-EDGE-FIRST require that options NEVER
    originate until the clean-window evidence gate (GATE-2: N>=50 settled round-trips
    over >=60 calendar days) has actually CLEARED — a verdict persisted by the eval
    cron as ``quant/options_unlock.json`` (read via ``read_options_unlocked``).

    The PRIOR design consulted ``read_options_unlocked()`` ONLY when
    ``HERMES_QUANT_OPTIONS_EVIDENCE_GATE=1`` — so arming ``HERMES_QUANT_AUTONOMOUS_OPTIONS=1``
    WITHOUT that flag let options originate with ZERO GATE-2 enforcement. That inverted
    the gate (the flag was the on/off switch for WHETHER the gate ran). cx0 makes the
    gate MANDATORY: whenever autonomous options are armed this is ALWAYS consulted and
    must return True.

    FAIL-CLOSED: an absent / unreadable / not-cleared marker, OR any read error, returns
    False (LOCKED). Arming the options flags alone NEVER unlocks origination.

    EMERGENCY BYPASS (dangerous, default-OFF): ``HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE=1``
    is the ONLY escape — it skips the marker read and returns True. This is a separately
    named, explicitly-dangerous operator flag (NOT the prior gate-flag), documented as a
    deliberate evidence-gate bypass. Default-OFF => the gate is enforced.
    """
    # The explicit, separately-named emergency override (default-OFF, dangerous).
    if os.environ.get("HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE", "0") == "1":
        logger.warning(
            "autonomous: HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE=1 — BYPASSING the GATE-2 "
            "options evidence gate (DANGEROUS: options may originate before the clean-window "
            "evidence gate has cleared)"
        )
        return True
    try:
        from hermes_quant.eval.clean_window import read_options_unlocked

        return bool(read_options_unlocked())
    except Exception as _ev_exc:  # noqa: BLE001 — fail-CLOSED: any error => locked
        logger.warning(
            "autonomous: options evidence-gate read failed (%s) — LOCKING options",
            _ev_exc,
        )
        return False


def _persist_composite_play(
    mleg: Any,
    *,
    persisted: Any,
    execution_id: str,
) -> None:
    """ml00b: persist the composite-play lifecycle row + its option_legs after a
    CONFIRMED options fire (the agmon1/agmon2 unblock).

    The legs stored are ``[{symbol, side, position_intent} for leg in
    mleg.option_legs]`` — the OCC symbol + side agmon1/agmon2 need to mark and
    sign the net P&L of the open composite. The composite is keyed on its
    multi_leg proposal id (``mleg.proposal_id`` -> ``persisted.proposal_id`` ->
    ``execution_id``) so the sweep can re-find it.

    BEST-EFFORT: the fire is already real + accounted. A store-write failure
    (bad path, locked db, duplicate id, a malformed leg) is logged and swallowed
    so it never rolls back a real fire.

    cx3-legless [P2]: a mleg with NO option_legs is SKIPPED outright (logged, no
    row). ``open_composite`` does NOT raise on an empty leg-set (``_encode_legs([])``
    serializes to '[]' without error — it only rejects a leg that is present but has
    no symbol), so without this skip a legless [] row WOULD be persisted and then
    skipped forever by the agmon1/agmon2 sweep. We leave NO row instead (fail-CLOSED
    on the malformed row, fail-OPEN on the fire that already happened).
    """
    try:
        from hermes_quant.state.composite_plays import CompositePlaysStore

        legs = list(getattr(mleg, "option_legs", ()) or ())
        # cx3-legless [P2]: a legless mleg must NEVER persist a durable composite row.
        # _encode_legs([]) serializes [] to '[]' WITHOUT raising (it only rejects a leg
        # that IS present but has no symbol), so open_composite would silently write a
        # legless [] row with expected_leg_count=0. agmon1/agmon2 then skip that row every
        # sweep FOREVER (no legs to mark) — exactly the dead-path the persist is meant to
        # prevent. SKIP (log loudly, write nothing) — fail-CLOSED on the malformed row, the
        # already-confirmed fire stays real (the caller swallows this best-effort helper).
        if not legs:
            logger.warning(
                "autonomous: ml00b composite persist SKIPPED for fire %s — the mleg "
                "has NO option_legs (a legless [] row would be skipped forever by the "
                "agmon1/agmon2 sweep; the fire is still real)",
                execution_id,
            )
            return
        option_legs = [
            {
                "symbol": str(getattr(leg, "symbol", "") or ""),
                "side": str(getattr(leg, "side", "") or ""),
                "position_intent": str(getattr(leg, "position_intent", "") or ""),
            }
            for leg in legs
        ]

        multi_leg_id = str(
            getattr(mleg, "proposal_id", None)
            or getattr(persisted, "proposal_id", None)
            or execution_id
            or ""
        )
        if not multi_leg_id:
            logger.warning(
                "autonomous: ml00b composite persist skipped — no multi_leg_id "
                "(fire %s still real)", execution_id,
            )
            return

        # net_debit_credit is a signed Decimal (+debit paid / -credit received,
        # the HITL price). aegis-agmon1: store the SIGNED value as net_entry_price
        # (NOT abs()) — the sign is the ONLY thing that tells the agmon1/agmon2
        # stop/TP sweep whether this is a CREDIT structure (profits as legs cheapen)
        # or a DEBIT structure (profits as legs richen). The prior abs() collapsed
        # both to a positive number, leaving the sweep unable to compute a
        # sign-correct net P&L vs the real store (the discarded build's tests hid
        # this by injecting a negative value through a _Store double). The store's
        # _assert_finite allows a negative net_entry_price. max_loss is Decimal|None.
        _ndc = getattr(mleg, "net_debit_credit", None)
        net_entry_price = float(_ndc) if _ndc is not None else 0.0
        _max_loss = getattr(mleg, "max_loss", None)
        max_loss = float(_max_loss) if _max_loss is not None else None

        store = CompositePlaysStore(db_path=QUANT_HOME / "state.db")
        store.open_composite(
            multi_leg_id=multi_leg_id,
            underlying=str(getattr(mleg, "underlying", "") or ""),
            strategy_kind=str(getattr(mleg, "strategy_kind", "") or ""),
            outer_qty=int(getattr(mleg, "outer_qty", 0) or 0),
            net_entry_price=net_entry_price,
            fill_size_pct=0.0,  # options size by contracts, not NAV fraction (0aa6)
            expected_leg_count=len(option_legs),
            max_loss=max_loss,
            option_legs=option_legs,
        )
        logger.info(
            "autonomous: ml00b persisted composite %s with %d legs",
            multi_leg_id, len(option_legs),
        )
    except Exception as exc:  # noqa: BLE001 - observability row, never abort a real fire
        logger.warning(
            "autonomous: ml00b composite persist failed for fire %s (continuing — "
            "the fire is real, only the lifecycle row is missing): %s",
            execution_id, exc, exc_info=True,
        )


# ---------------------------------------------------------------------------
# aegis-agmon1: options/combo per-position STOP-LOSS sweep (REBUILT, iter-5).
# ---------------------------------------------------------------------------
# When the composite open book holds an options position (a multi_leg parent),
# read its option_legs OFF THE REAL ml00b store row (a list of {symbol, side,
# position_intent} DICTS — NOT objects), mark each leg via the REAL
# ChainSnapshotReader.replay_chain parquet, compute the NET multi-leg unrealized
# P&L vs the SIGNED entry net_entry_price, and fire a CLOSE MultiLegProposal
# through the SHARED options dispatch tail (the same select_reactor +
# _apply_fire_accounting chokepoint _originate_mleg_proposal uses) when the stop
# threshold is breached.
#
# POSTURE (money-software): silence-by-default + fail-CLOSED. A missing parquet /
# missing OCC row / NaN/inf leg mark makes the WHOLE composite unmarkable => HOLD
# (never partial-mark, never fabricate a close). Gated behind
# HERMES_QUANT_OPTIONS_MONITOR (source-default OFF) inside the existing
# HERMES_QUANT_PER_POSITION_STOP guard. Flag OFF => no composite store read =>
# byte-identical (the equity sweep is untouched). The leg-mark source is the REAL
# replay_chain reader (resolved per-tick at the composite's underlying + asof);
# tests inject a mark_leg built over a FIXTURE parquet so the sweep is exercised
# against a real chain, not a double.


def _leg_field(leg: Any, key: str) -> Any:
    """Read a leg field from EITHER a dict (the real ml00b store shape) OR an
    object (an OptionLeg). The ml00b store persists legs as
    ``{symbol, side, position_intent}`` dicts, so the sweep must dict-access them;
    a defensive object fallback keeps the helper robust if a caller passes
    OptionLegs directly."""
    if isinstance(leg, dict):
        return leg.get(key)
    return getattr(leg, key, None)


def _resolve_options_leg_mark(
    underlying: str, asof: datetime
) -> Callable[[str], float | None] | None:
    """Build the per-leg mark source for one composite, backed by the REAL
    ``ChainSnapshotReader.replay_chain`` parquet (hermes_quant/options/data.py).

    Returns a callable ``occ_symbol -> mid (USD/share) | None`` that resolves a
    leg's mark from the recorded chain snapshot at ``(underlying, asof)``. The mark
    is the snapshot's ``mid = (bid + ask) / 2`` (the same liquidation reference the
    rest of the options stack uses). FAIL-CLOSED at every layer:

      * no parquet for (underlying, asof.date()) / <2 contracts / a read error =>
        returns None (the WHOLE sweep then HOLDs this composite — never fabricates).
      * the OCC symbol not in the chain, or its mid missing / non-finite =>
        the callable returns None for THAT leg => _net_close_cost_from_legs HOLDs
        the whole composite (never partial-mark).

    The chains live at ~/.hermes/quant/option_chains/<U>/<YYYY-MM-DD>.parquet; the
    reader needs no creds, no network, no flag (replay path). asof is the SWEEP
    time so the no-lookahead filter (fetched_at <= asof) holds.
    """
    try:
        from hermes_quant.options.data import ChainSnapshotReader

        reader = ChainSnapshotReader(chains_dir=QUANT_HOME / "option_chains")
        chain = reader.replay_chain(underlying, asof)
    except Exception as exc:  # noqa: BLE001 - a missing/unreadable chain is a HOLD, never a fabricated mark
        logger.info(
            "autonomous: no replayable option chain for %s at %s (HOLD composite): %s",
            underlying, asof, exc,
        )
        return None

    by_symbol = {snap.symbol: snap for snap in chain.snapshots}

    def _mark(occ_symbol: str) -> float | None:
        snap = by_symbol.get(occ_symbol)
        if snap is None:
            return None  # OCC not in the recorded chain -> unmarkable leg -> HOLD
        mid = snap.mid  # None when bid or ask is missing
        if mid is None or not isinstance(mid, (int, float)) or isinstance(mid, bool):
            return None
        mid = float(mid)
        if not math.isfinite(mid) or mid < 0.0:
            return None  # NaN/inf/negative mid -> unmarkable -> HOLD
        return mid

    return _mark


def _net_close_cost_from_legs(
    legs: Any, mark_leg: Callable[[str], float | None] | None
) -> float | None:
    """Net DEBIT (USD/share, signed) to CLOSE the composite at current leg marks.

    REUSES the verified sign-math from the iter-3-reviewed build (commit 3e8f121),
    reconciled to the REAL ml00b store shape: legs are ``{symbol, side,
    position_intent}`` DICTS (read via _leg_field), and the persisted dict carries
    NO ratio_qty (defaults to 1 — the composite's outer_qty scales the whole
    structure uniformly, so the per-spread net is ratio-1 per leg).

    For each option leg, closing REVERSES the open side: a leg opened LONG (side
    "buy") is SOLD to close (we RECEIVE its mark); a leg opened SHORT (side "sell")
    is BOUGHT to close (we PAY its mark). So the signed net cost-to-close is:

        sum over legs of   (+mark if leg.side == "sell" else -mark)

    i.e. a positive result = we must PAY to close. FAIL-CLOSED: a None mark source,
    a leg with no symbol, or any leg whose mark is missing / non-finite returns None
    (the caller HOLDS the WHOLE composite — never partial-marks, never fabricates).
    """
    if mark_leg is None:
        return None
    legs_list = list(legs or ())
    if not legs_list:
        return None
    total = 0.0
    for leg in legs_list:
        sym = _leg_field(leg, "symbol")
        if not sym or not isinstance(sym, str):
            return None
        try:
            m = mark_leg(sym)
        except Exception:  # noqa: BLE001 - a marking error is a HOLD, never a fabricated close
            return None
        if m is None or not isinstance(m, (int, float)) or isinstance(m, bool):
            return None
        m = float(m)
        if not math.isfinite(m):
            return None
        side = _leg_field(leg, "side")
        signed = m if side == "sell" else -m
        total += signed
    return total


def _options_position_loss_pct(
    *, net_entry_price: float, net_close_cost: float
) -> float | None:
    """Unrealized LOSS fraction of a composite vs its entry, sign-correct + finite-guarded.

    REUSES the verified math from commit 3e8f121. ``net_entry_price`` is the SIGNED
    entry net_debit_credit (the ml00b store now persists the sign: +debit paid /
    -credit received). ``net_close_cost`` is the signed cost-to-close from
    :func:`_net_close_cost_from_legs` (positive = must pay to close).

    A CREDIT structure (net_entry_price < 0) received ``credit = -net_entry_price``;
    the unrealized P&L per spread = ``credit - net_close_cost`` (we keep the credit,
    minus what we must pay to buy it back). The loss FRACTION (positive = losing) is
    ``-pnl / credit``.

    A DEBIT structure (net_entry_price > 0) paid ``debit``; its current liquidation
    value is ``-net_close_cost`` (we RECEIVE when we sell to close a long debit
    structure). pnl = liquidation - debit; loss fraction = ``-pnl / debit``.

    Returns a positive loss fraction (0.0 if not losing), or None on any non-finite
    input or a degenerate (zero) basis (fail-CLOSED HOLD).
    """
    if not (math.isfinite(net_entry_price) and math.isfinite(net_close_cost)):
        return None
    if net_entry_price < 0.0:  # CREDIT structure
        credit = -net_entry_price
        if credit <= 0.0:
            return None
        pnl = credit - net_close_cost
        return max(-pnl / credit, 0.0)
    if net_entry_price > 0.0:  # DEBIT structure
        debit = net_entry_price
        liquidation = -net_close_cost
        pnl = liquidation - debit
        return max(-pnl / debit, 0.0)
    return None  # zero basis -> no computable fraction (HOLD)


def _build_close_mleg_proposal(row: Any, *, reason: str) -> Any | None:
    """Reconstruct a CLOSE MultiLegProposal from a stored composite row's legs.

    The ml00b store row carries leg DICTS ``{symbol, side, position_intent}``; this
    rebuilds real ``OptionLeg`` objects with the open intents REVERSED to the
    matching ``*_to_close`` intent (sell_to_open -> buy_to_close, buy_to_open ->
    sell_to_close, and the side flipped accordingly) so the reactor fills an EXIT,
    not a new position. The proposal is minted through ``from_gate_result`` with a
    hand-built ADMITTED gate verdict — a CLOSE is always admissible (you must be able
    to EXIT a position); the reactor's ``risk_gate_pass is True`` lock is satisfied
    via the blessed mint seam, never a forged direct construction.

    Returns the proposal, or None if a leg is unparseable / has no usable intent
    (fail-CLOSED: a composite we cannot reconstruct a clean close for is HELD).
    """
    try:
        from decimal import Decimal

        from hermes_quant.options.data import NetGreeks, OptionLeg
        from hermes_quant.options.multileg import MultiLegProposal
        from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket

        _reverse_intent = {
            "sell_to_open": ("buy", "buy_to_close"),
            "buy_to_open": ("sell", "sell_to_close"),
            # tolerate an already-close intent (defensive; re-close is idempotent)
            "buy_to_close": ("buy", "buy_to_close"),
            "sell_to_close": ("sell", "sell_to_close"),
        }
        close_legs: list[Any] = []
        for leg in getattr(row, "option_legs", None) or ():
            sym = _leg_field(leg, "symbol")
            intent = _leg_field(leg, "position_intent")
            if not sym or not isinstance(sym, str):
                return None
            mapped = _reverse_intent.get(str(intent))
            if mapped is None:
                # Fall back to the open SIDE if intent is missing/unknown.
                side = _leg_field(leg, "side")
                if side == "sell":
                    mapped = ("buy", "buy_to_close")
                elif side == "buy":
                    mapped = ("sell", "sell_to_close")
                else:
                    return None
            new_side, new_intent = mapped
            close_legs.append(
                OptionLeg(symbol=sym, side=new_side, position_intent=new_intent)
            )
        if not close_legs:
            return None

        gate_result = OptionsGateResult(
            admitted=True,
            bucket=StructureBucket.DEFINED_RISK,
            reason=None,
            net_greeks=NetGreeks.zero(),
            bpr_estimate=0.0,
            max_loss=None,
            contracts=max(int(getattr(row, "outer_qty", 1) or 1), 1),
            warnings=("autonomous_options_close",),
        )
        return MultiLegProposal.from_gate_result(
            gate_result=gate_result,
            proposal_id=str(getattr(row, "multi_leg_id", "") or "") + "_close",
            asof=datetime.now(UTC),
            strategy_kind=str(getattr(row, "strategy_kind", "") or "close"),
            underlying=str(getattr(row, "underlying", "") or ""),
            option_legs=tuple(close_legs),
            stock_leg=None,
            outer_qty=max(int(getattr(row, "outer_qty", 1) or 1), 1),
            # The close is the inverse cash flow of the open; sign is observability
            # only here (the reactor fills the legs at the live NBBO). Use 0.0 so we
            # never assert a fabricated close price.
            net_debit_credit=Decimal("0"),
            max_gain=None,
            breakeven_underlying=(),
            rationale=reason,
            source_recipe_id="autonomous_options_close",
        )
    except Exception as exc:  # noqa: BLE001 - cannot mint a clean close -> HOLD (never fabricate)
        logger.warning(
            "autonomous: could not reconstruct a close proposal for composite %s (HOLD): %s",
            getattr(row, "multi_leg_id", "?"), exc,
        )
        return None


def _fire_options_close(row: Any, *, reason: str) -> tuple[str, float | None] | None:
    """Route a CLOSE of an open composite through the SHARED options dispatch tail.

    Reconstructs a close MultiLegProposal off the stored row (:func:`_build_close_mleg_proposal`)
    then mirrors _originate_mleg_proposal's chokepoint EXACTLY: select_reactor(mleg) ->
    reactor.execute(...) -> _apply_fire_accounting (the ar38 phantom-fire guard + the
    uniform append_human_override journal write). The MultiLegPaperReactor self-gates on
    HERMES_QUANT_MULTILEG_REACTOR (raises MultiLegReactorDisabled when off), so this is
    fail-closed at the execution layer too: a disabled reactor surfaces as a no-fill (None)
    and the composite stays open (HOLD). Returns (execution_id, realized) on a fill, else None.
    """
    from hermes_quant.react.dispatch import select_reactor

    mleg = _build_close_mleg_proposal(row, reason=reason)
    if mleg is None:
        return None  # could not reconstruct a clean close -> HOLD
    try:
        reactor = select_reactor(mleg)
        record = reactor.execute(
            mleg, fill_size_pct=0.0, approver_user_id="autonomous",
            play_tag="autonomous_options_close",
        )
    except Exception as exc:  # noqa: BLE001 - a disabled/refusing reactor is a no-fill HOLD, never a fabricated close
        logger.info(
            "autonomous: options close reactor refused/disabled for composite %s (HOLD): %s",
            getattr(row, "multi_leg_id", "?"), exc,
        )
        return None
    return _apply_fire_accounting(
        record, mleg, symbol=str(getattr(row, "underlying", "") or ""), journal_reason=reason
    )


def _run_options_position_stop_sweep(
    *,
    store: Any,
    stop_pct: float,
    mark_leg_for: Callable[[str, datetime], Callable[[str], float | None] | None],
    asof: datetime,
    result: TickResult,
) -> set[str]:
    """Force-CLOSE each OPEN composite whose net unrealized loss breaches ``stop_pct``.

    Enumerates ``store.list_open()`` (the REAL CompositePlaysStore open composites),
    reads each row's ``option_legs`` (the ml00b leg dicts), marks them via
    ``mark_leg_for(underlying, asof)`` (the REAL replay-chain mark source, resolved
    per composite at its underlying + the sweep asof), computes the sign-correct loss
    fraction vs the SIGNED net_entry_price, and fires a CLOSE through
    :func:`_fire_options_close` when the loss breaches ``stop_pct``.

    Returns the set of ``multi_leg_id`` closed this tick. FAIL-CLOSED: an unresolvable
    mark source / any missing / non-finite leg mark HOLDS that composite
    (silence-by-default). One composite's failure never aborts the sweep (mirrors the
    equity sweep's BLE001 guard).
    """
    closed: set[str] = set()
    try:
        open_rows = store.list_open()
    except Exception as exc:  # noqa: BLE001 - a store read failure is a HOLD-all, never a fabricated close
        logger.warning("autonomous: options stop sweep could not read open composites (HOLD): %s", exc)
        return closed

    for row in open_rows:
        mlid = getattr(row, "multi_leg_id", None)
        try:
            if not mlid:
                continue
            net_entry_price = getattr(row, "net_entry_price", None)
            if net_entry_price is None or not math.isfinite(float(net_entry_price)):
                continue  # no usable entry basis -> HOLD
            legs = getattr(row, "option_legs", None)
            if not legs:
                continue  # legless row (legacy / pre-ml00b) -> HOLD
            underlying = str(getattr(row, "underlying", "") or "")
            if not underlying:
                continue
            mark_leg = mark_leg_for(underlying, asof)
            net_close_cost = _net_close_cost_from_legs(legs, mark_leg)
            if net_close_cost is None:
                continue  # a leg mark is missing / non-finite -> HOLD (silence-by-default)
            loss_pct = _options_position_loss_pct(
                net_entry_price=float(net_entry_price), net_close_cost=net_close_cost
            )
            if loss_pct is None:
                continue  # non-computable -> HOLD
            if loss_pct < abs(stop_pct):
                continue  # not past the stop
            # ml01b: when HERMES_QUANT_COMPOSITE_LEG_OPS=1, route the close as a LIVE
            # DECOMPOSE (the breach invalidates the combo thesis) so the composite
            # transitions open -> decomposed via the REAL ml00b store and the leg-close
            # fires through the shared MLEG dispatch tail. Flag OFF => returns None =>
            # the full-close path below runs (byte-identical to the agmon1 commit).
            _decomp_state = _maybe_decompose_on_close(
                store=store, row=row, reason="autonomous_options_per_position_stop"
            )
            if _decomp_state == "decomposed":
                closed.add(mlid)
                result.fires += 1
                result.decisions.append(SymbolDecision(
                    symbol=underlying or str(mlid),
                    asset_class="multi_leg",
                    timeframe="",
                    gate="OPTIONS_PER_POSITION_STOP_DECOMPOSED",
                    details={
                        "multi_leg_id": mlid,
                        "strategy_kind": getattr(row, "strategy_kind", None),
                        "loss_pct": loss_pct,
                        "threshold_pct": abs(stop_pct),
                        "leg_op": "decompose",
                        "state": _decomp_state,
                    },
                ))
                logger.info(
                    "autonomous: OPTIONS PER-POSITION STOP decomposed composite %s (%s) "
                    "loss %.2f%% >= %.2f%% (leg-ops live)",
                    mlid, underlying, loss_pct * 100, abs(stop_pct) * 100,
                )
                continue
            # Breach -> fire a close through the shared options dispatch tail.
            fired = _fire_options_close(row, reason="autonomous_options_per_position_stop")
            sym_decision = SymbolDecision(
                symbol=underlying or str(mlid),
                asset_class="multi_leg",
                timeframe="",
                gate="OPTIONS_PER_POSITION_STOP_FIRED",
                details={
                    "multi_leg_id": mlid,
                    "strategy_kind": getattr(row, "strategy_kind", None),
                    "net_entry_price": float(net_entry_price),
                    "net_close_cost": net_close_cost,
                    "loss_pct": loss_pct,
                    "threshold_pct": abs(stop_pct),
                },
            )
            if fired is None:
                # no-fill / disabled reactor -> the composite was NOT closed; record a silence.
                sym_decision.gate = "OPTIONS_PER_POSITION_STOP_NO_FILL"
                sym_decision.details["no_fill"] = True
                result.silences += 1
                result.decisions.append(sym_decision)
                continue
            execution_id, _realized = fired
            sym_decision.execution_id = execution_id
            closed.add(mlid)
            result.fires += 1
            result.decisions.append(sym_decision)
            logger.info(
                "autonomous: OPTIONS PER-POSITION STOP fired on composite %s (%s) "
                "loss %.2f%% >= %.2f%%; closed via %s",
                mlid, underlying, loss_pct * 100, abs(stop_pct) * 100, execution_id,
            )
        except Exception as exc:  # noqa: BLE001 - one composite's failure must not abort the sweep
            logger.warning(
                "autonomous: options stop sweep error on composite %s (HOLD): %s",
                mlid, exc, exc_info=True,
            )
            sym_decision = SymbolDecision(
                symbol=str(mlid or "UNKNOWN"),
                asset_class="multi_leg",
                timeframe="",
                gate="OPTIONS_PER_POSITION_STOP_ERROR",
                error=f"options_stop_sweep_error: {exc}",
            )
            result.errors += 1
            result.decisions.append(sym_decision)
            continue
    return closed


# ---------------------------------------------------------------------------
# aegis-agmon2: options-aware TAKE-PROFIT sweep (REBUILT, iter-5).
# ---------------------------------------------------------------------------
# Same real-store + replay-chain wiring as the stop sweep, on the GAIN side. For an
# OPTIONS position, mark the legs, compute the premium-recovery / fraction of MAX
# GAIN captured, and fire a structure-aware CLOSE when >= tp_fraction (default 0.50)
# of max gain is captured. STOP PRECEDENCE: a composite the STOP sweep already closed
# this tick is SKIPPED (never double-acted). Same fail-CLOSED / silence-by-default
# posture. A DEBIT structure with no bounded max-gain source returns None (HOLD —
# acceptable deferred; the stop sweep owns the loss side either way).


def _options_position_gain_pct(
    *, net_entry_price: float, net_close_cost: float
) -> float | None:
    """Fraction of MAX GAIN captured on a composite, sign-correct + finite-guarded.

    REUSES the verified math from commit ed0c314. For a CREDIT structure
    (net_entry_price < 0) the max gain is the full credit received
    (``credit = -net_entry_price``); the realized P&L per spread is
    ``credit - net_close_cost`` (we keep the credit minus the buy-back cost), so the
    fraction of max gain captured = ``pnl / credit``.

    For a DEBIT structure (net_entry_price > 0) the max gain is NOT bounded by the
    debit (it depends on the wing widths), and the caller does not supply it here, so
    this CONSERVATIVELY returns None for debit structures (no fabricated TP on an
    unbounded-gain estimate) — deferred until a max_gain source is wired. Returns
    None on any non-finite input or zero basis. The returned fraction is clamped at
    >= 0 (a losing position has captured 0 of its max gain — never a TP candidate).
    """
    if not (math.isfinite(net_entry_price) and math.isfinite(net_close_cost)):
        return None
    if net_entry_price < 0.0:  # CREDIT structure: max gain = credit received
        credit = -net_entry_price
        if credit <= 0.0:
            return None
        pnl = credit - net_close_cost
        return max(pnl / credit, 0.0)
    # DEBIT structure (or zero basis): no bounded max-gain source here -> HOLD.
    return None


def _sort_legs_short_first(legs: Any) -> list[Any]:
    """Order legs so SHORT legs (side == "sell") close FIRST (BUY_TO_CLOSE the short
    legs before SELL_TO_CLOSE the longs). agmon2 structure-aware close: buying back the
    short leg first removes the undefined-risk side before the protective long is sold,
    so the structure is never momentarily naked during the unwind."""
    legs_list = list(legs or ())
    return sorted(legs_list, key=lambda leg: 0 if _leg_field(leg, "side") == "sell" else 1)


def _run_options_position_tp_sweep(
    *,
    store: Any,
    tp_fraction: float,
    mark_leg_for: Callable[[str, datetime], Callable[[str], float | None] | None],
    asof: datetime,
    result: TickResult,
    already_closed: set[str],
) -> set[str]:
    """Force-CLOSE each OPEN composite that has captured >= ``tp_fraction`` of max gain.

    STOP PRECEDENCE: a composite in ``already_closed`` (the stop sweep closed it THIS
    tick) is SKIPPED — never double-acted. Same enumerate / mark / fire chain as the
    stop sweep, but on the GAIN side, and the close is structure-aware (short legs
    BUY_TO_CLOSE first). FAIL-CLOSED: a missing/non-finite leg mark or a non-computable
    gain HOLDS (silence-by-default). Returns the set of multi_leg_id closed for
    take-profit this tick.
    """
    closed: set[str] = set()
    try:
        open_rows = store.list_open()
    except Exception as exc:  # noqa: BLE001 - a store read failure is a HOLD-all
        logger.warning("autonomous: options TP sweep could not read open composites (HOLD): %s", exc)
        return closed

    for row in open_rows:
        mlid = getattr(row, "multi_leg_id", None)
        try:
            if not mlid or mlid in already_closed:
                continue  # STOP PRECEDENCE: already closed this tick -> skip
            net_entry_price = getattr(row, "net_entry_price", None)
            if net_entry_price is None or not math.isfinite(float(net_entry_price)):
                continue
            legs = getattr(row, "option_legs", None)
            if not legs:
                continue
            underlying = str(getattr(row, "underlying", "") or "")
            if not underlying:
                continue
            mark_leg = mark_leg_for(underlying, asof)
            # Structure-aware order: BUY_TO_CLOSE the short legs first. The net cost is
            # order-independent (a sum), but we surface the close order on the decision.
            ordered_legs = _sort_legs_short_first(legs)
            net_close_cost = _net_close_cost_from_legs(ordered_legs, mark_leg)
            if net_close_cost is None:
                continue  # missing/non-finite leg mark -> HOLD
            gain_pct = _options_position_gain_pct(
                net_entry_price=float(net_entry_price), net_close_cost=net_close_cost
            )
            if gain_pct is None:
                continue  # debit / non-computable -> HOLD (deferred)
            if gain_pct < abs(tp_fraction):
                continue  # not yet at the TP threshold
            fired = _fire_options_close(row, reason="autonomous_options_per_position_take_profit")
            sym_decision = SymbolDecision(
                symbol=underlying or str(mlid),
                asset_class="multi_leg",
                timeframe="",
                gate="OPTIONS_PER_POSITION_TAKE_PROFIT_FIRED",
                details={
                    "multi_leg_id": mlid,
                    "strategy_kind": getattr(row, "strategy_kind", None),
                    "net_entry_price": float(net_entry_price),
                    "net_close_cost": net_close_cost,
                    "gain_pct": gain_pct,
                    "threshold_pct": abs(tp_fraction),
                    "exit_kind": "take_profit",
                    "close_order": [_leg_field(leg, "symbol") for leg in ordered_legs],
                },
            )
            if fired is None:
                sym_decision.gate = "OPTIONS_PER_POSITION_TAKE_PROFIT_NO_FILL"
                sym_decision.details["no_fill"] = True
                result.silences += 1
                result.decisions.append(sym_decision)
                continue
            execution_id, _realized = fired
            sym_decision.execution_id = execution_id
            closed.add(mlid)
            result.fires += 1
            result.decisions.append(sym_decision)
            logger.info(
                "autonomous: OPTIONS PER-POSITION TAKE-PROFIT fired on composite %s (%s) "
                "captured %.2f%% of max gain >= %.2f%%; closed via %s",
                mlid, underlying, gain_pct * 100, abs(tp_fraction) * 100, execution_id,
            )
        except Exception as exc:  # noqa: BLE001 - one composite's failure must not abort the sweep
            logger.warning(
                "autonomous: options TP sweep error on composite %s (HOLD): %s",
                mlid, exc, exc_info=True,
            )
            sym_decision = SymbolDecision(
                symbol=str(mlid or "UNKNOWN"),
                asset_class="multi_leg",
                timeframe="",
                gate="OPTIONS_PER_POSITION_TAKE_PROFIT_ERROR",
                error=f"options_tp_sweep_error: {exc}",
            )
            result.errors += 1
            result.decisions.append(sym_decision)
            continue
    return closed


# ---------------------------------------------------------------------------
# aegis-ml01b: wire the LIVE executor for the ml01 leg-op apply_* drivers (REBUILD).
# ---------------------------------------------------------------------------
# ml01 landed the DECISION layer (decompose / convert + the apply_* drivers that
# take INJECTED executor callables). The prior build left _apply_convert_live /
# _apply_decompose_live with ZERO callers (orphaned + _apply_convert_live untested).
# ml01b (iter-5) builds the LIVE executor in the host AND wires a REAL trigger: when
# the options stop sweep would close a composite AND HERMES_QUANT_COMPOSITE_LEG_OPS=1,
# the close routes through _apply_decompose_live so the composite transitions LIVE
# (open -> decomposed via the REAL ml00b store) and the leg-close order fires through
# the shared MLEG dispatch tail — instead of the leg-op decision being computed + discarded.
#
# Byte-identical when HERMES_QUANT_COMPOSITE_LEG_OPS off: leg_ops_enabled() False =>
# apply_decompose / apply_convert short-circuit (no store transition) AND the live
# executor-firing gate is OFF => no reactor call. The composite stays managed-whole and
# the agmon1 full-close path runs exactly as in the agmon1 commit.


def _legs_as_option_objects(legs: Any) -> list[Any]:
    """Reconstruct OptionLeg objects from the ml00b store's leg DICTS.

    The composite row stores legs as ``{symbol, side, position_intent}`` dicts; the
    leg-op executor + the no-naked guard operate on OptionLeg objects. A leg that
    cannot be reconstructed (no symbol) is DROPPED (the caller fail-CLOSEs on an
    empty/short leg-set). Returns the OptionLeg list in row order."""
    from hermes_quant.options.data import OptionLeg

    out: list[Any] = []
    for leg in legs or ():
        sym = _leg_field(leg, "symbol")
        side = _leg_field(leg, "side")
        intent = _leg_field(leg, "position_intent")
        if not sym or not isinstance(sym, str):
            continue
        # A valid OptionLeg needs a side + position_intent; fall back conservatively.
        side = side if side in ("buy", "sell") else "buy"
        intent = intent if intent in (
            "buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"
        ) else ("buy_to_open" if side == "buy" else "sell_to_open")
        try:
            out.append(OptionLeg(symbol=sym, side=side, position_intent=intent))
        except Exception:  # noqa: BLE001 - an unparseable OCC is dropped (fail-closed)
            continue
    return out


def _build_live_leg_mleg_executor(
    *,
    underlying: str,
    play_tag: str,
    outer_qty: int = 1,
    multi_leg_id: str = "",
) -> Callable[[list[Any]], None]:
    """Return a LIVE executor that fires a leg-set as an MLEG order through the reactor.

    REUSES the verified executor from commit f5c3dfe. The returned callable takes a
    list of OptionLegs (the add-leg list for a convert, or the leg(s) being broken out
    for a decompose) and routes them through the ONE dispatch chokepoint
    (``react.dispatch.select_reactor``) so the broker MLEG order actually executes.
    Routing failures PROPAGATE (apply_convert's H4 add-before-remove atomicity REQUIRES
    the add executor to RAISE on a broker reject so the remove half never runs and no
    naked leg is stranded). The leg-set is wrapped in a close MultiLegProposal minted
    via the blessed gate seam so it routes to the multi-leg reactor (self-gating on
    HERMES_QUANT_MULTILEG_REACTOR — no-fills / raises when off).

    cx1 [P1]: the leg-op order carries the composite's REAL ``outer_qty`` (and the gate
    result's ``contracts``) so a >1-wide composite submits a leg-op for ALL contracts
    rather than ONE spread (which left the residual contracts unmanaged after the whole
    composite was marked decomposed). And the ``proposal_id`` is keyed on the composite's
    ``multi_leg_id`` (when supplied) so two stopped composites on the SAME underlying mint
    DISTINCT proposal_ids — without this, the reactor idempotency returned the prior
    parent for the second composite and never sent a new order. ``outer_qty`` defaults to
    1 and ``multi_leg_id`` to "" so callers without a composite row keep the prior
    behavior (the underlying+play_tag id) — byte-identical for those callers.
    """
    from decimal import Decimal

    from hermes_quant.options.data import NetGreeks
    from hermes_quant.options.multileg import MultiLegProposal
    from hermes_quant.react.dispatch import select_reactor
    from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket

    # cx1: a composite carries outer_qty>=1 spreads; the leg-op must move them ALL. Guard
    # a non-positive / non-int outer_qty back to 1 (fail toward the prior single-spread
    # behavior, never zero — a 0-contract order is meaningless).
    try:
        _qty = int(outer_qty)
    except (TypeError, ValueError):
        _qty = 1
    if _qty < 1:
        _qty = 1
    # cx1: key the leg-op proposal_id on the composite's multi_leg_id so two same-underlying
    # composites are DISTINCT. The play_tag stays in the id so a convert's add/remove halves
    # (which share an mlid) remain distinct ids. No multi_leg_id (orphan callers) => the prior
    # id (byte-identical to the pre-cx1 f"legop_{underlying}_{play_tag}").
    _proposal_id = (
        f"legop_{multi_leg_id}_{play_tag}" if multi_leg_id else f"legop_{underlying}_{play_tag}"
    )

    def _executor(legs: list[Any]) -> None:
        if not legs:
            return
        gate_result = OptionsGateResult(
            admitted=True,
            bucket=StructureBucket.DEFINED_RISK,
            reason=None,
            net_greeks=NetGreeks.zero(),
            bpr_estimate=0.0,
            max_loss=None,
            contracts=_qty,
            warnings=(play_tag,),
        )
        order = MultiLegProposal.from_gate_result(
            gate_result=gate_result,
            proposal_id=_proposal_id,
            asof=datetime.now(UTC),
            strategy_kind="leg_op",
            underlying=underlying,
            option_legs=tuple(legs),
            stock_leg=None,
            outer_qty=_qty,
            net_debit_credit=Decimal("0"),
            max_gain=None,
            breakeven_underlying=(),
            rationale=play_tag,
            source_recipe_id=play_tag,
        )
        reactor = select_reactor(order)
        reactor.execute(order, fill_size_pct=0.0, approver_user_id="autonomous", play_tag=play_tag)

    return _executor


def _composite_state(store: Any, multi_leg_id: str) -> str:
    """Best-effort current state of a composite (or "" if absent / unreadable)."""
    try:
        row = store.get(multi_leg_id)
        return row.state if row is not None else ""
    except Exception:  # noqa: BLE001 - a read failure is a benign "" (caller treats as no-op)
        return ""


def _apply_decompose_live(
    *,
    store: Any,
    multi_leg_id: str,
    underlying: str,
    decision: dict[str, Any],
    legs_remaining_after: int,
    legs_to_close: list[Any] | None = None,
    outer_qty: int = 1,
) -> str:
    """Route a decompose decision through apply_decompose with the LIVE executor wired.

    REUSES commit f5c3dfe. apply_decompose drives the composite_plays store transition
    (H1 no-orphan: open -> partial when some legs remain, -> decomposed when none). The
    leg-close ORDER is fired by the LIVE executor against ``legs_to_close`` (OptionLeg
    objects). apply_decompose short-circuits when HERMES_QUANT_COMPOSITE_LEG_OPS is off
    (byte-identical: no transition, no executor call). Returns the composite's state
    after the (possible) transition. A broker reject leaves the composite managed-whole
    (no transition driven) — fail-CLOSED, never strand a half-decomposed structure.
    """
    from hermes_quant.options.leg_ops import apply_decompose, leg_ops_enabled

    if leg_ops_enabled() and decision.get("decompose") and legs_to_close:
        try:
            executor = _build_live_leg_mleg_executor(
                underlying=underlying,
                play_tag="autonomous_leg_decompose",
                outer_qty=outer_qty,  # cx1: carry the composite's REAL outer_qty
                multi_leg_id=multi_leg_id,  # cx1: unique proposal_id per composite
            )
            executor(legs_to_close)
        except Exception as exc:  # noqa: BLE001 - a broker reject leaves the composite managed-whole
            logger.warning(
                "autonomous: live decompose leg-close failed for %s (composite left whole): %s",
                multi_leg_id, exc,
            )
            return _composite_state(store, multi_leg_id)

    return apply_decompose(
        store=store,
        multi_leg_id=multi_leg_id,
        decision=decision,
        legs_remaining_after=legs_remaining_after,
    )


def _apply_convert_live(
    *,
    store: Any,
    multi_leg_id: str,
    underlying: str,
    decision: dict[str, Any],
    current_legs: list[Any],
    outer_qty: int = 1,
) -> str:
    """Route a convert decision through apply_convert with the LIVE add/remove executors.

    REUSES commit f5c3dfe. apply_convert enforces the H4 atomicity contract (ADD half
    FIRST; a failed add raises ConvertExecutionError and leaves the composite UNCHANGED
    so no naked leg is stranded). Byte-identical when the flag is off (apply_convert
    short-circuits). Returns the composite's state after the convert.
    """
    from hermes_quant.options.leg_ops import apply_convert

    add_executor = _build_live_leg_mleg_executor(
        underlying=underlying,
        play_tag="autonomous_leg_convert_add",
        outer_qty=outer_qty,  # cx1: carry the composite's REAL outer_qty
        multi_leg_id=multi_leg_id,  # cx1: unique proposal_id per composite
    )
    remove_executor = _build_live_leg_mleg_executor(
        underlying=underlying,
        play_tag="autonomous_leg_convert_remove",
        outer_qty=outer_qty,
        multi_leg_id=multi_leg_id,
    )
    return apply_convert(
        store=store,
        multi_leg_id=multi_leg_id,
        decision=decision,
        current_legs=current_legs,
        add_executor=add_executor,
        remove_executor=remove_executor,
    )


def _maybe_decompose_on_close(
    *, store: Any, row: Any, reason: str
) -> str | None:
    """ml01b REAL TRIGGER: when the options sweep is about to CLOSE a composite AND
    HERMES_QUANT_COMPOSITE_LEG_OPS=1, route the close as a DECOMPOSE through
    _apply_decompose_live so the composite transitions LIVE (open -> decomposed via the
    ml00b store) and the leg-close fires through the shared MLEG dispatch tail.

    Returns the post-transition composite state (e.g. "decomposed") when the leg-op path
    drove a transition, else None (the caller falls back to the full-close path). Flag
    OFF => returns None immediately (byte-identical: the agmon1 full-close path runs).

    This is the wire that turns the orphaned _apply_decompose_live into a LIVE driver:
    a stop/TP breach on a composite is the natural "this structure is no longer wanted
    as a combo" trigger, so the whole structure decomposes (every leg independent)."""
    from hermes_quant.options.leg_ops import decompose_decision, leg_ops_enabled

    if not leg_ops_enabled():
        return None
    try:
        mlid = str(getattr(row, "multi_leg_id", "") or "")
        underlying = str(getattr(row, "underlying", "") or "")
        legs = _legs_as_option_objects(getattr(row, "option_legs", None))
        if not mlid or not legs:
            return None
        # cx1: carry the composite's REAL outer_qty into the leg-op order so a >1-wide
        # composite decomposes ALL contracts (not just one spread). Guard back to 1 on a
        # missing / non-positive value (fail toward the prior single-spread behavior).
        try:
            _outer_qty = int(getattr(row, "outer_qty", 1) or 1)
        except (TypeError, ValueError):
            _outer_qty = 1
        if _outer_qty < 1:
            _outer_qty = 1
        # A stop/TP breach invalidates the composite thesis -> decompose ALL legs (the
        # structure is no longer wanted as a combo). decompose_decision is deterministic
        # and self-gates on the flag (returns no_action when off — but we already gated).
        decision = decompose_decision(
            legs=legs, leg_signals=[], thesis_invalidated=True
        )
        if not decision.get("decompose"):
            return None
        state = _apply_decompose_live(
            store=store,
            multi_leg_id=mlid,
            underlying=underlying,
            decision=decision,
            legs_remaining_after=0,  # thesis invalidated -> ALL legs independent
            legs_to_close=legs,
            outer_qty=_outer_qty,  # cx1: carry the composite's REAL outer_qty
        )
        logger.info(
            "autonomous: ml01b LIVE decompose on composite %s (%s) -> state=%s (reason=%s)",
            mlid, underlying, state, reason,
        )
        return state
    except Exception as exc:  # noqa: BLE001 - a leg-op failure falls back to the full close (never aborts)
        logger.warning(
            "autonomous: ml01b live decompose failed for %s (falling back to full close): %s",
            getattr(row, "multi_leg_id", "?"), exc,
        )
        return None


# ---------------------------------------------------------------------------
# aegis-ag01b: route the candidate basket through the portfolio-variance gate (REBUILD).
# ---------------------------------------------------------------------------
# ag01 landed the pdr_core math (shrink_covariance + portfolio_variance_haircut) and the
# default-OFF gate hook (DefaultRiskGate.apply_portfolio_variance_sizing). The prior build
# left _apply_portfolio_variance_sizing_to_basket ORPHANED (zero callers). ag01b (iter-5)
# builds the host-side caller AND wires it into tick(): assemble the candidate basket of
# per-name quarter-Kelly targets THIS tick, build a returns matrix + SHRUNK covariance over
# those names, route the targets through the gate's BASKET method BEFORE the fire — closing
# the gap where five correlated names each at the per-name cap form a ~100% beta bet.
#
# Byte-identical when HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING off (source-default '0'): the
# helper returns the targets UNCHANGED without building any covariance (the returns_provider
# is NEVER called), and the tick's two-pass assembly is skipped (single-pass, byte-identical).
# FAIL-CLOSED on the ON path: a missing / failing returns source / a degenerate covariance /
# any error returns the per-name targets UNCHANGED — never sizes up (a missing covariance
# fails toward LESS leverage).


def _apply_portfolio_variance_sizing_to_basket(
    targets: list[tuple[str, float]],
    *,
    returns_provider: Callable[[list[str]], Any] | None,
) -> list[tuple[str, float]]:
    """De-lever a candidate basket via DefaultRiskGate.apply_portfolio_variance_sizing.

    REUSES the verified de-lever math from commit 5ef538b. ``targets`` are the per-name
    quarter-Kelly outputs (each already clipped to the per-name cap upstream); order/identity
    is preserved. ``returns_provider`` maps ``names -> (n_obs, n_names)`` returns matrix over
    the SAME names in the SAME order, or None. Only invoked when the flag is ON. Flag OFF =>
    the EXACT input list (byte-identical). A None provider / a provider that raises / a
    degenerate matrix => fail-CLOSED pass-through (targets unchanged). |out_i| <= |in_i| always.
    """
    from hermes_quant.pdr_core.portfolio_sizing import portfolio_variance_enabled_from_env

    if not portfolio_variance_enabled_from_env():
        return targets
    if not targets:
        return targets
    if returns_provider is None:
        return targets  # fail-CLOSED: no covariance source -> never size up

    try:
        import numpy as np

        from hermes_quant.pdr_core.gate import DefaultRiskGate, RiskConfig
        from hermes_quant.pdr_core.portfolio_sizing import (
            PortfolioVarianceConfig,
            shrink_covariance,
        )

        names = [n for n, _ in targets]
        returns = returns_provider(names)
        r = np.asarray(returns, dtype=float)
        # A degenerate / wrong-shape returns matrix -> fail-CLOSED pass-through.
        if r.ndim != 2 or r.shape[1] != len(names) or r.shape[0] < 2:
            return targets

        cfg = PortfolioVarianceConfig()
        cov = shrink_covariance(r, config=cfg)
        gate = DefaultRiskGate(
            RiskConfig(
                portfolio_variance_sizing_enabled=True,
                portfolio_variance_cap=cfg.variance_cap,
            )
        )
        return gate.apply_portfolio_variance_sizing(targets, cov)
    except Exception as exc:  # noqa: BLE001 - fail-CLOSED: a sizing error never sizes up / aborts
        logger.warning(
            "autonomous: portfolio-variance basket sizing failed (pass-through, never size up): %s",
            exc, exc_info=True,
        )
        return targets


def _build_tick_returns_provider(
    *, timeframe: str = "1d", lookback: int = 60
) -> Callable[[list[str]], Any]:
    """Return a returns_provider that fetches per-name close history (the REAL tick basket
    source) and builds an aligned ``(n_obs, n_names)`` simple-returns matrix.

    Pulls each name's daily closes via the default equity provider's ``fetch_bars`` (the
    SAME canonical fetch path the perception builder uses), computes simple returns, and
    aligns all names to the SHORTEST common length (the most recent ``min_len-1`` returns)
    so the matrix is rectangular. FAIL-CLOSED: a name with <2 closes / a fetch error / an
    empty alignment makes the returns matrix degenerate (the basket helper then passes the
    targets through UNCHANGED — never sizes up). The provider is injected (a seam) so tests
    supply a deterministic returns matrix; production uses this live fetch.
    """
    def _provider(names: list[str]) -> Any:
        import numpy as np
        import pandas as pd

        from hermes_quant.advisor import _get_default_provider

        provider = _get_default_provider("equity")
        end = pd.Timestamp.now(tz="UTC")
        start = end - pd.Timedelta(days=max(lookback * 2, 30))
        series: list[np.ndarray] = []
        for name in names:
            try:
                bars = provider.fetch_bars(name, timeframe, start, end, as_of=end)
                closes = np.asarray(bars["close"].to_numpy(), dtype=float)
            except Exception:  # noqa: BLE001 - a fetch failure makes this name's column empty -> degenerate
                series.append(np.array([], dtype=float))
                continue
            closes = closes[np.isfinite(closes)]
            if closes.size < 2:
                series.append(np.array([], dtype=float))
                continue
            rets = np.diff(closes) / closes[:-1]
            series.append(rets[np.isfinite(rets)])
        # Align to the shortest common length (most-recent tail). Any empty column =>
        # min_len 0 => a degenerate (n_obs<2) matrix => the helper passes targets through.
        if not series or min(s.size for s in series) < 2:
            return np.empty((0, len(names)), dtype=float)
        min_len = min(s.size for s in series)
        cols = [s[-min_len:] for s in series]
        return np.column_stack(cols)

    return _provider


def _options_monitor_enabled() -> bool:
    """True iff HERMES_QUANT_OPTIONS_MONITOR=1 (NEW, source-default OFF; read at call time)."""
    return os.environ.get("HERMES_QUANT_OPTIONS_MONITOR", "0") == "1"


def _maybe_run_options_stop_sweep(
    *, stop_pct: float, asof: datetime, result: TickResult
) -> set[str]:
    """Run the options STOP (+ TP, agmon2) sweeps iff HERMES_QUANT_OPTIONS_MONITOR=1.

    Default-OFF: the flag absent => returns an empty set without reading the composite
    store (byte-identical inert). Runs the STOP sweep first, then — when
    HERMES_QUANT_TAKE_PROFIT_SWEEP=1 (agmon2) — the TP sweep with STOP PRECEDENCE (a
    composite the stop already closed this tick is skipped). Returns the UNION of closed
    composite ids (stop ∪ take-profit). Best-effort: any failure is a logged HOLD-all,
    never a tick-abort. The mark source is the REAL replay-chain reader (resolved per
    composite at its underlying + asof).
    """
    if not _options_monitor_enabled():
        return set()
    try:
        from hermes_quant.state.composite_plays import CompositePlaysStore

        store = CompositePlaysStore(db_path=QUANT_HOME / "state.db")
        stopped = _run_options_position_stop_sweep(
            store=store, stop_pct=stop_pct, mark_leg_for=_resolve_options_leg_mark,
            asof=asof, result=result,
        )
        # agmon2: options TP, gated on HERMES_QUANT_TAKE_PROFIT_SWEEP (the SAME flag the
        # equity TP uses) so the equity-TP-OFF path stays byte-identical. STOP precedence:
        # the stopped set is passed as already_closed so a stopped composite is skipped.
        closed = set(stopped)
        if os.environ.get("HERMES_QUANT_TAKE_PROFIT_SWEEP", "0") == "1":
            tp_fraction = float(_read_safety_rails().get("options_take_profit_fraction", 0.50))
            tp_closed = _run_options_position_tp_sweep(
                store=store, tp_fraction=tp_fraction, mark_leg_for=_resolve_options_leg_mark,
                asof=asof, result=result, already_closed=stopped,
            )
            closed |= tp_closed
        return closed
    except Exception as exc:  # noqa: BLE001 - never block the tick on the options sweep
        logger.warning("autonomous: options stop/TP sweep failed (HOLD-all): %s", exc, exc_info=True)
        return set()


def _run_per_position_stop_sweep(
    *,
    open_book: dict[tuple[str, str], float],
    stop_pct: float,
    paper_zero_costs: bool,
    result: TickResult,
) -> set[tuple[str, str]]:
    """Force-exit each open position whose unrealized loss breaches the stop threshold.

    aegis-ageq2: ``open_book`` is COMPOSITE-keyed by ``(asset_class, symbol)`` (the
    ``reconstruct_open_book_composite`` view) so an options entry routes to the options
    path instead of being hardcoded "equity". Returns the set of ``(asset_class, symbol)``
    tuples force-exited this tick (the caller exempts them from the watchlist loop + frees
    their concurrency slot). For today's equity-only book this is byte-identical: the
    equity branch fires exactly as before, just keyed by the tuple. A non-equity entry is
    HELD here until agmon1 wires the options sweep (silence-by-default — never run an OCC
    symbol through the equity mark/entry primitives).

    Each force-exit reuses the existing ``_react()`` chokepoint with ``fill_size_pct = 0.0``
    (the ADR-0091 Option E flat absolute target) so it inherits the SAME routed reactor +
    no-fill guards as a normal fire. A symbol is HELD (not stopped) on any non-computable
    input (no mark, no entry basis, non-finite) — silence-by-default: a missing number
    never fabricates an exit.
    """
    from hermes_quant.perception import build_perception_frame_live
    from hermes_quant.risk.per_position_stop import evaluate_stop, evaluate_take_profit

    # AG-EQ-1 (HERMES_QUANT_TAKE_PROFIT_SWEEP, default-OFF): when ON, the SAME sweep also
    # force-exits a WINNING position whose unrealized gain breaches the take-profit target.
    # Byte-identical when the flag is absent (tp_enabled stays False => only the stop fires,
    # exactly as before). SL and TP share one mark + one entry-basis read per symbol and the
    # one sign-correct primitive, so they can never disagree on direction.
    tp_enabled = os.environ.get("HERMES_QUANT_TAKE_PROFIT_SWEEP", "0") == "1"
    tp_pct = float(_read_safety_rails().get("per_position_take_profit_pct", 0.16))

    # tp1/tp2 (HERMES_QUANT_TP_TRANCHE, default-OFF): scale-out + trailing. Requires the
    # WatchRegistry (it needs tranches_taken + peak_gain_pct across ticks); if the registry
    # is unavailable the tranche path is inert (falls back to full TP). When ON, a winning
    # position takes a PARTIAL exit (one 0.05 rung at +1R, residual at +2R or on the trailing
    # stop) instead of a full flatten — a partial NEVER enters `stopped` (the position is
    # still open, keeps its slot). Byte-identical when the flag is absent.
    tranche_enabled = os.environ.get("HERMES_QUANT_TP_TRANCHE", "0") == "1"

    # AG-EQ-3 (HERMES_QUANT_WATCH_REGISTRY, default-OFF): when ON, record each open play +
    # ratchet its peak gain across ticks into the durable WatchRegistry. This is PURE STATE
    # tracking — it does NOT change which positions the sweep exits (the tranche/trailing
    # ACTION that consumes this state is a separate flag/increment). Byte-identical when the
    # flag is absent (watch_reg stays None). Best-effort: a registry error never blocks the
    # sweep's stop/TP rails (silence-by-default on the observability layer).
    watch_reg = None
    if os.environ.get("HERMES_QUANT_WATCH_REGISTRY", "0") == "1":
        try:
            from hermes_quant.risk.watch_registry import WatchRegistry

            watch_reg = WatchRegistry(
                db_path=QUANT_HOME / "watch_registry.db",
                mirror_path=QUANT_HOME / "watch_registry.json",
            )
        except Exception as _wr_exc:  # noqa: BLE001 - registry is observability, never block the rails
            logger.warning("autonomous: WatchRegistry init failed (continuing without it): %s", _wr_exc)
            watch_reg = None

    stopped: set[tuple[str, str]] = set()
    for (asset_class, symbol), held in open_book.items():
        try:
            if not isinstance(held, (int, float)) or not math.isfinite(held) or held == 0.0:
                continue
            # aegis-ageq2: only the EQUITY path is wired here. A non-equity (us_option /
            # multi_leg) entry is HELD until agmon1 adds the options sweep — never run an
            # OCC symbol through the equity mark/entry primitives (silence-by-default).
            if asset_class != "equity":
                continue
            # Mark to the latest close via the same live-data path the watchlist loop uses.
            frame = build_perception_frame_live(symbol, asset_class=asset_class, timeframe="1d")
            mark = getattr(frame, "last_close", None) if frame is not None else None
            if mark is None:
                continue  # no usable mark -> HOLD
            entry_price = _establishing_avg_entry_price(symbol)
            if entry_price is None:
                continue  # no cost basis -> HOLD
            decision = evaluate_stop(
                symbol=symbol,
                held_fraction=float(held),
                entry_price=float(entry_price),
                mark_price=float(mark),
                threshold_pct=stop_pct,
            )
            # AG-EQ-3: record the play (idempotent) + ratchet its peak gain. PURE state;
            # the gain is exactly -loss_pct (the same sign-correct primitive). Best-effort.
            if watch_reg is not None and decision.loss_pct is not None:
                try:
                    watch_reg.record_open(symbol, entry_price=float(entry_price), stop_pct=abs(stop_pct))
                    watch_reg.update_peak(symbol, -float(decision.loss_pct))  # gain = -loss
                except Exception as _wr_exc:  # noqa: BLE001 - never block the rails
                    logger.warning("autonomous: WatchRegistry update failed for %s: %s", symbol, _wr_exc)
            # Determine the exit reason: STOP takes precedence over TP (a position cannot be
            # both past its loss stop and its gain target; the stop is the safety rail). TP
            # only when the flag is ON and the stop did NOT fire.
            exit_kind: str | None = None
            tp_decision = None
            if decision.should_stop:
                exit_kind = "stop"
            elif tranche_enabled and watch_reg is not None and decision.loss_pct is not None:
                # tp1/tp2 PARTIAL scale-out + trailing. The stop did NOT fire (not a loser
                # past its stop). The `decision.loss_pct is not None` guard is wave3-wiring-
                # review DEFECT-2 FIX: a NaN/non-computable mark makes loss_pct None;
                # -float(None) would raise TypeError (mis-labeled PER_POSITION_STOP_ERROR +
                # spurious result.errors++). A None loss_pct -> skip the tranche path; the
                # full-TP check below also guards None, so the net is a clean HOLD (the mark
                # is already past the `if mark is None: continue` guard but NaN slips that).
                # Consult evaluate_tranche with the registry's cross-tick state; a tranche/
                # trail action does a PARTIAL exit handled inline (does NOT join `stopped`).
                if _maybe_take_tranche(
                    symbol=symbol, held=float(held), entry_price=float(entry_price),
                    mark=float(mark), stop_pct=stop_pct, gain_pct=-float(decision.loss_pct),
                    watch_reg=watch_reg, paper_zero_costs=paper_zero_costs, result=result,
                ):
                    continue  # a partial tranche/trail exit fired (or recorded); next symbol
                # No tranche action -> fall through to the full-TP check below (the
                # all-at-once TP is still the backstop for a position past +2R that the
                # tranche logic chose not to fully close, e.g. tranches_taken==0 below +1R).
                if tp_enabled:
                    tp_decision = evaluate_take_profit(
                        symbol=symbol, held_fraction=float(held), entry_price=float(entry_price),
                        mark_price=float(mark), threshold_pct=tp_pct,
                    )
                    if tp_decision.should_take:
                        exit_kind = "take_profit"
            elif tp_enabled:
                tp_decision = evaluate_take_profit(
                    symbol=symbol,
                    held_fraction=float(held),
                    entry_price=float(entry_price),
                    mark_price=float(mark),
                    threshold_pct=tp_pct,
                )
                if tp_decision.should_take:
                    exit_kind = "take_profit"
            if exit_kind is None:
                continue
            # Breach -> force-exit through the existing reactor chokepoint.
            entry = WatchlistEntry(symbol=symbol, asset_class=asset_class, timeframe="1d")
            _reason = (
                "autonomous_per_position_stop" if exit_kind == "stop"
                else "autonomous_per_position_take_profit"
            )
            advisor_result = {
                "decision_price": float(mark),
                "as_of": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": _reason,
            }
            react_out = _react(
                advisor_result,
                entry,
                0.0,  # POST-FILL flat target (ADR-0091 Option E absolute target, not the delta)
                paper_zero_costs=paper_zero_costs,
            )
            _fired_gate = "PER_POSITION_STOP_FIRED" if exit_kind == "stop" else "PER_POSITION_TAKE_PROFIT_FIRED"
            _nofill_gate = "PER_POSITION_STOP_NO_FILL" if exit_kind == "stop" else "PER_POSITION_TAKE_PROFIT_NO_FILL"
            sym_decision = SymbolDecision(
                symbol=symbol,
                asset_class=asset_class,
                timeframe="1d",
                gate=_fired_gate,
                details={
                    "unrealized_loss_pct": decision.loss_pct,
                    "gain_pct": (tp_decision.gain_pct if tp_decision is not None else None),
                    "threshold_pct": (abs(stop_pct) if exit_kind == "stop" else abs(tp_pct)),
                    "exit_kind": exit_kind,
                    "held_fraction": float(held),
                    "mark_price": float(mark),
                    "entry_price": float(entry_price),
                    "reason": (decision.reason if exit_kind == "stop" else tp_decision.reason),  # type: ignore[union-attr]
                },
            )
            if react_out is None:
                # The reactor returned a no-fill/silence (e.g. a clip-to-zero); the
                # position was NOT flattened, so do NOT exempt it (let the normal loop
                # still manage it) and record the attempt as a silence.
                sym_decision.gate = _nofill_gate
                sym_decision.details["no_fill"] = True
                result.silences += 1
                result.decisions.append(sym_decision)
                continue
            execution_id, _realized = react_out
            sym_decision.execution_id = execution_id
            stopped.add((asset_class, symbol))
            result.fires += 1
            result.decisions.append(sym_decision)
            # AG-EQ-3: the play is fully closed -> drop it from the registry (best-effort).
            if watch_reg is not None:
                try:
                    watch_reg.drop(symbol)
                except Exception as _wr_exc:  # noqa: BLE001 - never block the rails
                    logger.warning("autonomous: WatchRegistry drop failed for %s: %s", symbol, _wr_exc)
            if exit_kind == "stop":
                _emit_per_position_stop_audit(
                    symbol=symbol,
                    loss_pct=float(decision.loss_pct) if decision.loss_pct is not None else 0.0,
                    threshold_pct=abs(stop_pct),
                    held_fraction=float(held),
                )
                logger.info(
                    "autonomous: PER-POSITION STOP fired on %s (loss %.2f%% >= %.2f%%); "
                    "force-exited %.4f NAV-fraction via %s",
                    symbol,
                    (decision.loss_pct or 0.0) * 100,
                    abs(stop_pct) * 100,
                    held,
                    execution_id,
                )
            else:
                _gain = tp_decision.gain_pct if tp_decision is not None else 0.0
                _emit_per_position_take_profit_audit(
                    symbol=symbol,
                    gain_pct=float(_gain) if _gain is not None else 0.0,
                    threshold_pct=abs(tp_pct),
                    held_fraction=float(held),
                )
                logger.info(
                    "autonomous: PER-POSITION TAKE-PROFIT fired on %s (gain %.2f%% >= %.2f%%); "
                    "force-exited %.4f NAV-fraction via %s",
                    symbol,
                    (_gain or 0.0) * 100,
                    abs(tp_pct) * 100,
                    held,
                    execution_id,
                )
        except Exception as exc:  # noqa: BLE001 - one symbol's failure must not abort the sweep
            logger.warning(
                "autonomous: per-position stop sweep error on %s (HOLD): %s",
                symbol,
                exc,
                exc_info=True,
            )
            # Mirror the watchlist loop's BLE001 handler (line ~2144): record a
            # gate=PER_POSITION_STOP_ERROR decision so the error is observable on the
            # tick output and result.errors is incremented. A bare `continue` here is
            # fail-open: the symbol is silently omitted from `stopped` with no audit
            # trail (the per-position stop rail is a safety rail — silence defeats it).
            sym_decision = SymbolDecision(
                symbol=symbol,
                asset_class=asset_class,
                timeframe="1d",
                gate="PER_POSITION_STOP_ERROR",
                error=f"stop_sweep_error: {exc}",
            )
            result.errors += 1
            result.decisions.append(sym_decision)
            continue

    # aegis-agmon1: options/combo per-position STOP-LOSS (+ agmon2 TP). Gated behind
    # HERMES_QUANT_OPTIONS_MONITOR (source-default OFF) INSIDE this existing
    # PER_POSITION_STOP guard. Flag OFF (production default) => no composite store read
    # => byte-identical (the equity sweep above is untouched). When ON, force-close each
    # OPEN composite whose net unrealized loss breaches the SAME stop_pct (and, when
    # HERMES_QUANT_TAKE_PROFIT_SWEEP=1, capture take-profit). A closed composite joins
    # `stopped` as a ("multi_leg", multi_leg_id) tuple so the caller's watchlist
    # exemption / slot accounting can see it under the composite-key contract (ageq2).
    # asof = the sweep wall-clock (the no-lookahead replay filter is fetched_at <= asof).
    for _mlid in _maybe_run_options_stop_sweep(
        stop_pct=stop_pct, asof=datetime.now(UTC), result=result
    ):
        stopped.add(("multi_leg", _mlid))

    return stopped


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


# --------------------------------------------------------------------------- #
# W5 — multi-horizon tick wiring (HERMES_QUANT_MULTI_HORIZON_TICK, default-OFF)
# --------------------------------------------------------------------------- #


def _multi_horizon_enabled(entry: Any) -> bool:
    """True iff the W5 fan-out should run for this entry.

    Requires BOTH (a) HERMES_QUANT_MULTI_HORIZON_TICK=1 (read at call time) AND
    (b) a non-empty ``entry.horizon_set`` (a W4 add-only field; absent/None on a
    pre-W4 entry -> fall back to the single-timeframe path). Read via ``getattr``
    so the seam composes without hard-importing W4's field. FAIL-CLOSED: anything
    but the literal "1" / a missing horizon_set -> False -> byte-identical."""
    if os.environ.get(_MULTI_HORIZON_TICK_FLAG, "0") != "1":
        return False
    hs = getattr(entry, "horizon_set", None)
    return bool(hs)


def _horizon_timeframes(entry: Any) -> list[str]:
    """Resolve ``entry.horizon_set`` -> the ordered rung timeframes for the fan-out.

    Maps each rung label through ``HORIZONS[rung].timeframe`` (playbook/horizons.py,
    W2). Order-preserving (recommend_multi_horizon dedupes internally). An unknown
    rung is skipped (best-effort). Empty/unresolvable -> [] (the caller falls back
    to the single entry.timeframe)."""
    hs = getattr(entry, "horizon_set", None) or []
    try:
        from hermes_quant.playbook.horizons import HORIZONS
    except Exception as exc:  # noqa: BLE001 — no horizons module yet -> single-tf fallback
        logger.warning(
            "autonomous: playbook.horizons unavailable (%s) — single-timeframe fallback",
            exc,
        )
        return []
    out: list[str] = []
    for rung in hs:
        rung_def = HORIZONS.get(rung)
        if rung_def is None:
            continue
        tf = getattr(rung_def, "timeframe", None)
        if tf:
            out.append(str(tf))
    return out


def _decision_rung(entry: Any) -> str | None:
    """Pick the rung whose DTE window drives the options producer for this tick.

    The decision layer (structure_select + the gate) picks WHICH structure trades;
    the horizon picks the DTE WINDOW. We choose the LAST rung in the set as the
    decision rung — by the W2 contract the set is ordered short->long and the longest
    present rung resolves to the producer's fixed (25, 45) default (30D), so a
    default 1D..30D set keeps the options path byte-identical (last==30D==(25,45)).
    Returns None when the fan-out is not enabled (-> no DTE thread -> fixed default)."""
    if not _multi_horizon_enabled(entry):
        return None
    hs = getattr(entry, "horizon_set", None) or []
    return str(hs[-1]) if hs else None


def _run_multi_horizon_fanout(
    *,
    entry: Any,
    asof: str,
) -> None:
    """Fan the analyst views out across the entry's rung timeframes (ADR-0036).

    Calls ``advisor.recommend_multi_horizon(symbol, horizons=[rung timeframes])`` so
    every rung produces a (analyst, horizon) view. This is the literal W5 thread:
    ``entry.horizon_set -> [HORIZONS[r].timeframe for r in set] ->
    recommend_multi_horizon``. The function already dedupes + fail-softs per horizon.
    Best-effort: a provider/import error is a logged no-op (never a tick-abort) — the
    single-timeframe ``recommend()`` spine above already produced the gate-ready
    advisor_result, so a failed fan-out leaves the decision intact. The returned views
    are not yet consumed by the gate (the single-tf spine remains the decision input);
    this seam establishes the fan-out so a downstream increment can blend the rungs."""
    timeframes = _horizon_timeframes(entry)
    if not timeframes:
        return
    try:
        from hermes_quant.advisor import recommend_multi_horizon

        recommend_multi_horizon(
            entry.symbol,
            horizons=timeframes,
            asset_class=getattr(entry, "asset_class", "equity"),
            as_of=asof,
        )
    except Exception as exc:  # noqa: BLE001 — fan-out is best-effort, never aborts the tick
        logger.warning(
            "autonomous: multi-horizon fan-out failed for %s (continuing single-tf): %s",
            getattr(entry, "symbol", "?"),
            exc,
        )


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

    # Empty-watchlist short-circuit. BYTE-IDENTICAL when the per-position stop is OFF
    # (the production default): no watchlist -> no work -> return. But an open position
    # can need STOPPING regardless of whether there are new signals to evaluate, so when
    # HERMES_QUANT_PER_POSITION_STOP=1 we fall through to run the stop sweep against the
    # open book even with an empty watchlist (the watchlist loop below still iterates
    # zero times — the only added work is the stop sweep on already-open positions).
    if not watchlist and os.environ.get("HERMES_QUANT_PER_POSITION_STOP", "0") != "1":
        return result

    # ar73 / ADR-0016 §D9 concurrent-positions rail atomicity (cross-tick race fix).
    # The open-book read + per-symbol enforce + fire loop below MUST be ATOMIC per
    # account: two overlapping ticks (the 0,30 cron and the agent TOOL path both
    # reach tick()) would otherwise each read the same stale pre-fire book and each
    # admit a DISTINCT new symbol, jointly breaching max_concurrent_positions. The
    # always-on per-account advisory lock makes a second overlapping tick wait for
    # the first to commit and re-read the now-larger book (correctly silencing via
    # SILENCE_CONCURRENT_CAP), or — on prolonged contention — SKIP this tick
    # (silence-by-default; recoverable next tick). It is fail-open-safe to today's
    # unguarded behavior ONLY on a genuine flock-unsupported infra error.
    with _account_rail_lock(_AUTONOMOUS_ACCOUNT_ID, lock_dir=QUANT_HOME) as _rail_ok:
        if not _rail_ok:
            # Another tick holds the rail for this account and contention persisted
            # past the bound. SKIP — do not fire against a stale pre-fire count. The
            # empty result is honest: zero fires this tick, recoverable on the next.
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
        # aegis-ageq2: the per-position stop sweep consumes a COMPOSITE-keyed book
        # {(asset_class, symbol): held NAV-fraction} so an options entry routes to the
        # options path instead of being hardcoded "equity". The §D9 concurrent-cap rail
        # below keeps its EXISTING symbol-keyed count (byte-identical) — only the sweep's
        # book gains the asset_class dimension.
        open_book_at_tick_start: dict[tuple[str, str], float] = {}
        rail_read_failed = False
        try:
            from hermes_quant.portfolio.state import (
                reconstruct_open_book_composite as _recon_composite,
            )
            from hermes_quant.portfolio.state import reconstruct_portfolio_state as _recon

            # Read from QUANT_HOME's bus explicitly (not the helper's hard-coded
            # default) so the rail honors the same home the rest of this module uses
            # — keeps it test-isolatable via the QUANT_HOME monkeypatch and correct
            # when the home is reconfigured.
            _open = _recon(QUANT_HOME / "executions.jsonl", reactor_filter=None).positions
            open_symbols_at_tick_start = set(_open)
            open_positions_at_tick_start = len(_open)
            # aegis-ageq2: composite-keyed snapshot from the SAME canonical source for the
            # per-position stop sweep. For today's equity-only book the equity entries are
            # byte-identical to dict(_open) projected through ("equity", symbol).
            open_book_at_tick_start = _recon_composite(
                QUANT_HOME / "executions.jsonl", reactor_filter=None
            )
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
            # not just the legacy paper-only slice — same rationale as the D9 rail
            # above, so headroom is computed against the true book.
            #
            # ar114: scope to account="paper-default" (the {paper,
            # deterministic-equity} family this cap governs), NOT account=None. The
            # §D9 COUNT rail above can over-count symbols safely under account=None —
            # more cardinality only BLOCKS new opens. But THIS headroom path sums
            # GROSS exposure, and reconstruct_portfolio_state collapses each asset to
            # its LATEST-asof target (it does NOT sum across books). Under account=None
            # a smaller, more-recent alpaca-paper SHADOW target for a ticker REPLACES
            # the larger real paper-default position — UNDER-counting gross, inflating
            # headroom, and over-trading (fail-open). Scoping by account drops the
            # shadow book BEFORE the collapse (cs18 partition; mirrors the cs25 flatten
            # seam). alpaca_paper is default-OFF, so this is a byte-identical no-op on
            # the live single-book bus and closes the fail-open the moment
            # HERMES_QUANT_ALPACA_PAPER is flipped on. Explicit QUANT_HOME bus path for
            # home-consistency + test isolation, like the §D9 rail above.
            portfolio_state = reconstruct_portfolio_state(
                QUANT_HOME / "executions.jsonl",
                reactor_filter=None,
                account="paper-default",
            )
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
        # no store, no state.db write). `_account_nav_mtm()` is fail-closed (returns
        # None on any failure, falls back to cost-basis if marks are unavailable);
        # a None NAV flows through to recommend()'s flag-ON fail-CLOSED branch
        # (durable_baseline_nav_unavailable), never a fall-open.
        # ar_durable_mtm: use _account_nav_mtm() (not _account_nav_usd()) so the
        # durable HWM is anchored to true MTM equity — including open unrealized
        # losses — rather than cost-basis equity_total which is self-cancelling for
        # NAV-fraction fills (equity_total stays at initial_cash after a BUY).
        _durable_baseline = (
            os.environ.get("HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE", "0") == "1"
        )
        _durable_nav = _account_nav_mtm() if _durable_baseline else None

        # HERMES_QUANT_POST_LOSS_COOLDOWN seam: build a DefaultRiskGate pre-seeded with
        # loss timestamps from the durable sidecar ONCE per tick, then pass it into each
        # advisor_recommend() call below via risk_gate=. This restores Rule 4's cross-tick
        # state — the gate is normally constructed fresh per call (empty _cooldowns) so
        # Rule 4 was structurally dead on the live path (the cooldown was recorded only in
        # the backtest's _settle_due wire). Flag OFF (production default) => _seeded_gate
        # stays None => advisor_recommend() is called WITHOUT risk_gate= => byte-identical
        # to pre-fix behavior. Flag ON: the seeded gate is constructed from the sidecar;
        # a sidecar read-failure falls back to a plain fresh gate (never None when ON).
        _post_loss_cooldown = os.environ.get("HERMES_QUANT_POST_LOSS_COOLDOWN", "0") == "1"
        _seeded_gate = _build_gate_with_cooldowns(_LOSS_COOLDOWN_SIDECAR_PATH) if _post_loss_cooldown else None

        # Per-position UNREALIZED-loss stop sweep (2026-06-17, the June-4 ASTS -20.9%
        # fix). DEFAULT-OFF: HERMES_QUANT_PER_POSITION_STOP unset => the whole block is
        # skipped and the tick is BYTE-IDENTICAL. When ON, before the watchlist loop we
        # mark each OPEN position to its latest close and force-exit any whose unrealized
        # loss from its FIFO entry basis breaches per_position_stop_loss_pct. This is the
        # ONLY rail that sees a single open position bleeding (the kill-switch is
        # realized-only, autonomous.py:578; the portfolio drawdown breaker can't see a
        # -4%-NAV single position under its 15% threshold). The forced exit REUSES the
        # existing _react() chokepoint (fill_size_pct = -held), so it inherits the same
        # routed reactor + cap-clip + no-fill guards as a normal fire.
        # aegis-ageq2: _stopped_keys is COMPOSITE (asset_class, symbol). The watchlist
        # loop + §D9 slot accounting key on the plain SYMBOL (the equity book the watchlist
        # iterates), so derive _stopped_symbols (plain) from the composite set. For today's
        # equity-only book the projection is exact (each key is ("equity", symbol)).
        _stopped_keys: set[tuple[str, str]] = set()
        _stopped_symbols: set[str] = set()
        if not dry_run and os.environ.get("HERMES_QUANT_PER_POSITION_STOP", "0") == "1":
            try:
                _stopped_keys = _run_per_position_stop_sweep(
                    open_book=open_book_at_tick_start,
                    stop_pct=float(rails.get("per_position_stop_loss_pct", 0.08)),
                    paper_zero_costs=bool(rails.get("paper_zero_costs", False)),
                    result=result,
                )
            except Exception as _stop_exc:  # noqa: BLE001 - never block the tick on the stop sweep
                logger.warning(
                    "autonomous: per-position stop sweep failed (continuing tick): %s",
                    _stop_exc,
                    exc_info=True,
                )
                _stopped_keys = set()
            _stopped_symbols = {sym for (_ac, sym) in _stopped_keys}
            # A stop-closed symbol's slot is freed and it must NOT be re-opened or
            # adjusted in the SAME tick (avoid double-action against a position we just
            # flattened). Drop it from the concurrent-cap accounting so the freed slot is
            # available to a genuinely-new symbol, and skip it in the watchlist loop below.
            for _sym in _stopped_symbols:
                if _sym in open_symbols_at_tick_start:
                    open_symbols_at_tick_start.discard(_sym)
                    open_positions_at_tick_start = max(0, open_positions_at_tick_start - 1)

        # aegis-ag01b: tick-scoped candidate basket for the portfolio-variance de-lever.
        # DEFAULT-OFF (HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING, source-default '0'): the
        # flag-OFF path never builds this basket / never calls the returns provider, so the
        # watchlist loop is single-pass byte-identical. When ON, each fire-eligible name's
        # quarter-Kelly is appended; the basket is routed through
        # _apply_portfolio_variance_sizing_to_basket (shrunk covariance over the names'
        # live returns) so a correlated basket is de-levered TOGETHER BEFORE the fire. The
        # returns source is the REAL per-name fetch_bars path (injected as a seam for tests).
        _variance_sizing_on = (
            os.environ.get("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "0") == "1"
        )
        _variance_basket: list[tuple[str, float]] = []
        _variance_returns_provider = (
            _build_tick_returns_provider() if _variance_sizing_on else None
        )

        for entry in watchlist:
            if entry.symbol in _stopped_symbols:
                # Force-exited by the stop sweep this tick; do not re-evaluate it now.
                continue
            try:
                _frame = None
                if _inject_frame:
                    from hermes_quant.perception import build_perception_frame_live

                    # 78b3: thread the per-symbol options_eligible opt-in into the live
                    # frame builder so Perceive populates options_chain + iv_rank in step 5e
                    # (BOTH HERMES_QUANT_OPTIONS_PERCEIVE and entry.options_eligible required,
                    # checked inside the builder). This unifies P->D: the decision layer reads
                    # iv_rank from the FRAME instead of a separate compute_iv_rank_asof call.
                    # Default-OFF / not-eligible -> options_eligible=False -> step 5e skipped ->
                    # frame.iv_rank stays None -> byte-identical.
                    _options_eligible = bool(getattr(entry, "options_eligible", False))
                    _frame = build_perception_frame_live(
                        entry.symbol,
                        asset_class=entry.asset_class,
                        timeframe=entry.timeframe,
                        options_eligible=_options_eligible,
                    )
                _durable_equity_account = (
                    ("paper-default", entry.asset_class, _durable_nav)
                    if _durable_baseline
                    else None
                )
                # HERMES_QUANT_POST_LOSS_COOLDOWN: inject pre-seeded gate when ON
                # so Rule 4 (post-loss cooldown) is active across ticks. When OFF
                # (default), _seeded_gate is None and recommend() builds its own
                # gate — byte-identical to pre-fix behavior.
                advisor_result = advisor_recommend(
                    symbol=entry.symbol,
                    asset_class=entry.asset_class,
                    timeframe=entry.timeframe,
                    include_lessons=True,
                    perception_frame=_frame,
                    durable_equity_account=_durable_equity_account,
                    risk_gate=_seeded_gate,
                )
                # W5 (HERMES_QUANT_MULTI_HORIZON_TICK, default-OFF): when ON AND the
                # entry carries a horizon_set, fan the analyst views out across the
                # rung timeframes via recommend_multi_horizon (ADR-0036). This threads
                # [HORIZONS[r].timeframe for r in set] into the multi-horizon entry
                # point — the literal W5 thread. Flag OFF / no horizon_set => this is a
                # no-op and the single advisor_recommend() above is the sole call
                # (byte-identical). The fan-out is best-effort + additive: the single-tf
                # spine remains the gate's decision input, so a failed/empty fan-out
                # never changes the decision (silence-by-default preserved).
                if _multi_horizon_enabled(entry):
                    _run_multi_horizon_fanout(entry=entry, asof=asof)
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

            # agdec1/agreact1 (HERMES_QUANT_AUTONOMOUS_OPTIONS, default-OFF): attempt an
            # OPTIONS origination for this symbol BEFORE the equity path. If it fires a
            # multi-leg play, skip the equity decision for this symbol this tick (one play
            # per symbol per tick). Abstains (returns None) at every missing precondition.
            # Byte-identical when the flag is unset (the helper returns None on the first
            # guard, never touching the equity path). Best-effort inside the helper.
            # cx0 [HIGH]: the GATE-2 evidence guard is now MANDATORY whenever autonomous
            # options are armed (NOT gated behind a separate on/off flag). _options_evidence_gate_ok()
            # ALWAYS consults the persisted clean-window unlock marker (read_options_unlocked =
            # GATE-2 cleared: N>=50 over >=60d) and FAILS-CLOSED (absent/unreadable/not-cleared =>
            # LOCKED). So arming HERMES_QUANT_AUTONOMOUS_OPTIONS ALONE does NOT unlock origination —
            # the evidence gate must have actually cleared (ADR-0029 evidence-before-live +
            # EQUITY-EDGE-FIRST). The ONLY escape is the explicit, separately-named, default-OFF
            # HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE (dangerous). The gate is only evaluated when
            # options are armed (short-circuit && below), so the equity-only path is byte-identical
            # (no extra marker read on a tick that never originates options). Account-wide state.
            if (
                not dry_run
                and os.environ.get("HERMES_QUANT_AUTONOMOUS_OPTIONS", "0") == "1"
                and _options_evidence_gate_ok()  # cx0: MANDATORY GATE-2 evidence guard (fail-CLOSED)
                # agreact1: the §D9 max_per_tick_opens cap binds options origination too —
                # check it BEFORE firing (the equity path checks the same cap below). Without
                # this, an options play could fire past the per-tick cap because the hook runs
                # before the equity cap gate. fires_this_tick counts equity AND options fires.
                and fires_this_tick < rails["max_per_tick_opens"]
            ):
                _nav_for_opts = _account_nav_mtm()
                if _nav_for_opts is None:
                    from hermes_quant.state.portfolio_state import _default_initial_cash
                    _nav_for_opts = _default_initial_cash()
                # 78b3: source the as-of IV rank from the PerceptionFrame (Perceive step 5e)
                # — the unified P->D path — when the frame carries it. The frame builder above
                # ran step 5e iff HERMES_QUANT_OPTIONS_PERCEIVE=1 AND entry.options_eligible, so
                # _frame.iv_rank is the perceived rank (None otherwise). Fall back to a direct
                # compute_iv_rank_asof ONLY when the flag+eligible hold but the frame is absent
                # (e.g. _inject_frame off / frame build returned None) — same fail-closed seam
                # (returns None on missing/<30-point/corrupt history -> the helper abstains).
                # PERCEIVE off / not eligible => iv_rank stays None => byte-identical INERT state.
                _iv_rank: float | None = getattr(_frame, "iv_rank", None)
                if (
                    _iv_rank is None
                    and os.environ.get("HERMES_QUANT_OPTIONS_PERCEIVE", "0") == "1"
                    and getattr(entry, "options_eligible", False)
                ):
                    try:
                        from hermes_quant.options.iv_rank import compute_iv_rank_asof

                        _iv_rank = compute_iv_rank_asof(entry.symbol, datetime.now(tz=UTC))
                    except Exception as _iv_exc:  # noqa: BLE001 — fail-closed: abstain on any error
                        logger.warning(
                            "autonomous: iv_rank source failed for %s: %s — abstaining options",
                            entry.symbol,
                            _iv_exc,
                        )
                        _iv_rank = None
                _opts_exec = _originate_mleg_proposal(
                    symbol=entry.symbol,
                    asof=datetime.now(tz=UTC),
                    advisor_result=advisor_result,
                    nav=float(_nav_for_opts),
                    # No live options-BP source in the tick yet -> 0.0 makes the options_gate's
                    # BP check fail-closed (abstain) until a real options-BP read is wired
                    # (the operator's Alpaca options BP, a future increment). Honest + safe.
                    options_buying_power=0.0,
                    iv_rank=_iv_rank,  # agperc1 seam (None until >=30d recorded IV history exists)
                    structure_intent=advisor_result.get("structure_intent"),
                    paper_zero_costs=bool(rails.get("paper_zero_costs", False)),
                    result=result,
                    # W5: thread the decision rung so the horizon's DTE window flows to
                    # the producer (structure_select still picks the KIND). None when
                    # the multi-horizon flag is OFF / no horizon_set => fixed-DTE default
                    # => byte-identical. 30D resolves to (25, 45) == today's default too.
                    horizon_rung=_decision_rung(entry),
                )
                if _opts_exec is not None:
                    # agreact1: a fired options play consumes a concurrency slot exactly
                    # like an equity fire — charge the SAME tick accounting the equity
                    # branch does (fires_this_tick for the per-tick/concurrency cap +
                    # open_symbols_at_tick_start so a later pick sees the slot consumed)
                    # so the §D9 max_per_tick_opens / max-concurrent rails bind options
                    # plays too (previously an options fire was invisible to these caps).
                    fires_this_tick += 1
                    open_symbols_at_tick_start.add(entry.symbol)
                    continue  # an options play fired for this symbol; skip the equity path

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

                # aegis-ag01b: portfolio-variance basket de-lever (DEFAULT-OFF). When ON,
                # the candidate basket of fire-eligible per-name quarter-Kelly targets is
                # routed through DefaultRiskGate.apply_portfolio_variance_sizing (shrunk
                # covariance over the names' live returns) BEFORE the ladder snap / fire, so
                # a correlated basket is de-levered TOGETHER. Greedy + order-dependent like
                # the ADR-0071 portfolio clip: this name's kelly is scaled by the basket λ
                # given the already-committed names + itself; |out| <= |in| always (a
                # de-lever can only shrink). Flag OFF => byte-identical (no basket built, no
                # returns provider call, kelly unchanged). FAIL-CLOSED: a missing/failing
                # returns source returns the targets UNCHANGED (never sizes up).
                variance_sizing_meta: dict[str, float] | None = None
                if _variance_sizing_on and kelly != 0.0:
                    _tentative = [*_variance_basket, (entry.symbol, kelly)]
                    _haircut = _apply_portfolio_variance_sizing_to_basket(
                        _tentative, returns_provider=_variance_returns_provider
                    )
                    # The current name is the LAST entry; apply its de-levered size.
                    _haircut_map = dict(_haircut)
                    _new_kelly = float(_haircut_map.get(entry.symbol, kelly))
                    if math.isfinite(_new_kelly) and abs(_new_kelly) <= abs(kelly):
                        if _new_kelly != kelly:
                            variance_sizing_meta = {
                                "kelly_before": kelly,
                                "kelly_after": _new_kelly,
                            }
                        kelly = _new_kelly

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
                if variance_sizing_meta is not None:
                    decision.action["portfolio_variance_sizing"] = variance_sizing_meta

                if not dry_run:
                    try:
                        react_out = _react(
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
                        if react_out is None:
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
                        # ar80: _react returns (pid, realized_fill_size_pct). Charge the
                        # reactor's REALIZED post-clip size into the running headroom, NOT
                        # the pre-reactor-clip REQUESTED effective_size — the reactor
                        # (PaperReactor._portfolio_cap_clip) independently re-reads the
                        # PERSISTED book and may apply a SECOND, tighter clip. Charging the
                        # requested size would OVER-charge the in-memory running headroom and
                        # spuriously shrink/silence later picks. Fail toward the larger
                        # effective_size (conservative — never UNDER-charge) if the realized
                        # value is missing or non-finite or somehow larger than requested.
                        execution_id, realized_size = react_out
                        decision.execution_id = execution_id
                        fires_this_tick += 1
                        charged = effective_size
                        if (
                            realized_size is not None
                            and isinstance(realized_size, (int, float))
                            and math.isfinite(float(realized_size))
                            and abs(float(realized_size)) <= abs(effective_size)
                        ):
                            charged = float(realized_size)
                        # Update running portfolio state so the next pick sees the
                        # post-fire headroom (the canonical state.db helper does
                        # not reload mid-tick — we mutate here).
                        if portfolio_state is not None:
                            # `positions` dict is intentionally mutable here even though
                            # PortfolioState is frozen — the dict is the inner mutable
                            # container that we update without reconstructing the wrapper.
                            portfolio_state.positions[entry.symbol] = charged
                        # aegis-ag01b: COMMIT this fired name to the tick basket so the
                        # NEXT correlated pick sees it in the covariance de-lever (the
                        # marginal-name greedy semantics). Flag OFF => _variance_sizing_on
                        # False => the basket stays empty (byte-identical).
                        if _variance_sizing_on:
                            _variance_basket.append((entry.symbol, kelly))
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


def _reactor_record_is_nofill(record: Any) -> bool:
    """ar38/cs02/ar16/ar27 phantom-fire guard — the ONE place that decides a reactor
    record is a NO-FILL / SILENCE / REJECT (no capital moved, nothing durable on the bus).

    A reactor may RETURN (not raise) such a record: PaperReactor on a non-finite/<=0 price
    or a cap clip-to-zero (silenced=True, fill_size_pct=0.0); DeterministicEquityReactor on
    a BP refusal / backend error (no_fill / bp_rejected, fill_size_pct=0.0);
    MultiLegPaperReactor on a gate-reject / unfilled leg. Treating any of these as a fire
    counts a PHANTOM fire (consumed slot + phantom headroom that silences later real
    signals). This is the UNION of every reactor's no-fill signal so the equity AND the
    options origination paths share ONE detection (agreact1 — kills the divergent inline
    copy the options helper used to carry).

    UNITS HAZARD (0aa6): the EQUITY reactors size by NAV-FRACTION, so fill_size_pct==0.0
    means "no capital moved" = a no-fill. But a MULTI-LEG parent record sizes by CONTRACTS
    (``outer_qty``); ``_originate_mleg_proposal`` calls execute(fill_size_pct=0.0) and the
    parent record copies that 0.0 even on a REAL fill (multileg.py — target/fill_size_pct
    mirror the passed value; the real fill is in ``outer_qty`` + ``parent_status='filled'``
    + the moved legs). So fill_size_pct==0.0 must NOT, by itself, mark a multi-leg parent as
    a no-fill — that mis-counted a genuine options fill as a silence, skipping the journal +
    concurrency accounting while the legs moved the book. For a multi-leg parent we key the
    no-fill ONLY on the explicit reject signals (silenced/no_fill/unfilled_timeout) + a
    non-'filled' parent_status / non-positive outer_qty; fill_size_pct is ignored. A None
    realized size is still a no-fill for the equity path (conservative).
    """
    rmeta = getattr(record, "reactor_metadata", None) or {}
    realized = getattr(record, "fill_size_pct", None)

    explicit_nofill = bool(
        rmeta.get("silenced") is True
        or rmeta.get("no_fill") is True
        or rmeta.get("bp_rejected") is True
        or rmeta.get("unfilled_timeout") is True
    )

    # A multi-leg PARENT sizes by contracts, not NAV-fraction — fill_size_pct==0.0 is
    # NOT a no-fill signal there (0aa6). Detect the parent and key off the contract-units
    # signals instead.
    is_mleg_parent = (
        getattr(record, "asset_class", None) == "multi_leg"
        or rmeta.get("role") == "parent"
    )
    if is_mleg_parent:
        outer_qty = rmeta.get("outer_qty")
        parent_status = rmeta.get("parent_status")
        partial_fill = rmeta.get("partial_fill") is True
        status_nofill = (
            parent_status is not None and parent_status != "filled" and not partial_fill
        )
        qty_nofill = isinstance(outer_qty, (int, float)) and outer_qty <= 0
        return bool(explicit_nofill or status_nofill or qty_nofill)

    # Equity / single-name path: fill_size_pct is the NAV-fraction. A 0.0 fill = no capital
    # moved = no-fill. A None realized is NOT a no-fill here — it is byte-identical to the
    # pre-agreact1 _react, which keyed only on `(_realized is not None and _realized == 0.0)`
    # and FIRED on a None (a record that carries no usable fill_size_pct -> the caller's ar80
    # fallback charges the requested size conservatively). A real reactor always returns a
    # float fill_size_pct, so this only differs for a stub/None record, and the equity path
    # must preserve the original fire-on-None semantics (regression caught by the
    # select_reactor_inc2 / paper_zero_costs_guard / admissibility-units suites).
    return bool(explicit_nofill or (realized is not None and realized == 0.0))


def _apply_fire_accounting(
    record: Any,
    proposal: Any,
    *,
    symbol: str,
    journal_reason: str,
) -> tuple[str, float | None] | None:
    """Shared post-execution accounting tail for an autonomous fire (agreact1).

    Both the equity path (``_react``) and the options-origination path
    (``_originate_mleg_proposal``) call this AFTER ``reactor.execute()`` so they share
    the SAME safety-critical tail instead of carrying divergent copies:
      1. the ar38 phantom-fire guard (``_reactor_record_is_nofill``) — return None on a
         no-fill so the caller counts NO fire + mutates NO state;
      2. the UNIFORM ``append_human_override(kind='approve')`` journal write (the audit
         trail is identical across HITL, equity-autonomous, and options-autonomous);
      3. the ar80 return shape ``(execution_id, realized_post_clip_fill_size_pct)`` so the
         caller charges ACTUAL consumption into the running headroom.

    Returns ``None`` on a no-fill/silence (the caller treats it as a silence), else
    ``(proposal_id, realized_size)``. ``realized_size`` is the reactor's post-clip
    ``record.fill_size_pct`` (a second tighter ADR-0087 clip may have shrunk it), or None
    if the record carries no usable float (caller falls back to the requested size —
    conservative, never under-charges headroom).
    """
    if _reactor_record_is_nofill(record):
        rmeta = getattr(record, "reactor_metadata", None) or {}
        logger.info(
            "autonomous: reactor returned a NO-FILL/SILENCE for %s (reason=%s); NOT "
            "counting a fire, NOT mutating portfolio state (phantom-fire guard)",
            symbol,
            rmeta.get("silence_reason")
            or rmeta.get("no_fill_reason")
            or rmeta.get("broker_status")
            or "no_fill",
        )
        return None

    # Uniform audit trail: an autonomous fire journals as a non-human 'approve' override,
    # identical across the HITL, equity-autonomous, and options-autonomous seams.
    try:
        from hermes_quant.journal.writer import append_human_override

        append_human_override(proposal, kind="approve", reason=journal_reason)
    except ImportError:
        logger.debug("autonomous: journal not available; skipping audit entry")
    except Exception as exc:  # noqa: BLE001
        logger.warning("autonomous: journal append failed: %s", exc, exc_info=True)

    _realized = getattr(record, "fill_size_pct", None)
    realized_size = float(_realized) if isinstance(_realized, (int, float)) else None
    pid = (
        getattr(proposal, "proposal_id", None)
        or getattr(record, "proposal_id", None)
        or ""
    )
    return str(pid), realized_size


def _react(
    advisor_result: dict[str, Any],
    entry: WatchlistEntry,
    fill_size_pct: float,
    *,
    paper_zero_costs: bool = False,
) -> tuple[str, float | None] | None:
    """Fire the routed reactor for an autonomous decision.

    Returns ``None`` if the reactor returned a NO-FILL/SILENCE record (ar38) so the
    caller does NOT count a phantom fire or mutate portfolio state. Otherwise returns
    a ``(proposal_id, realized_fill_size_pct)`` pair (ar80): the proposal_id is the
    synthesized execution_id surfaced in tick output, and realized_fill_size_pct is the
    reactor's POST-clip ``record.fill_size_pct`` — PaperReactor.execute() independently
    re-reads the PERSISTED book and may apply a SECOND, tighter portfolio-cap clip
    (ADR-0087), so the realized size can be SMALLER than the requested fill_size_pct. The
    caller charges this realized size into the in-memory running headroom so the next
    pick sees ACTUAL consumption (not the over-charged requested size). realized is
    ``None`` only if the record carries no usable fill_size_pct (caller falls back to the
    requested size — conservative: never under-charges headroom).

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

    # agreact1: the ar38 phantom-fire guard + the uniform journal write + the ar80 return
    # shape are now the SHARED _apply_fire_accounting tail (the equity AND options paths
    # call the same code — no divergent inline copy). Byte-identical to the prior inline
    # logic: a no-fill returns None (caller counts no fire, mutates no state), a real fill
    # journals as 'approve' and returns (pid, realized_post_clip_size). The synthesized
    # equity proposal carries `pid`, so _apply_fire_accounting resolves the same id.
    return _apply_fire_accounting(
        record,
        proposal,
        symbol=entry.symbol,
        journal_reason="autonomous_silence_bias_fire",
    )
