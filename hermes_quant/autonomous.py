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
from collections.abc import Iterator
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

logger = logging.getLogger(__name__)


QUANT_HOME = Path.home() / ".hermes" / "quant"
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
    result: "TickResult",
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


def _run_per_position_stop_sweep(
    *,
    open_book: dict[str, float],
    stop_pct: float,
    paper_zero_costs: bool,
    result: "TickResult",
) -> set[str]:
    """Force-exit each open position whose unrealized loss breaches the stop threshold.

    Returns the set of symbols that were force-exited this tick (the caller exempts them
    from the watchlist loop + frees their concurrency slot). Each force-exit reuses the
    existing ``_react()`` chokepoint with ``fill_size_pct = -held`` so it inherits the
    SAME routed reactor + no-fill guards as a normal fire. A symbol is HELD (not stopped)
    on any non-computable input (no mark, no entry basis, non-finite) — silence-by-default:
    a missing number never fabricates an exit.
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

    stopped: set[str] = set()
    for symbol, held in open_book.items():
        try:
            if not isinstance(held, (int, float)) or not math.isfinite(held) or held == 0.0:
                continue
            # Mark to the latest close via the same live-data path the watchlist loop uses.
            frame = build_perception_frame_live(symbol, asset_class="equity", timeframe="1d")
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
            entry = WatchlistEntry(symbol=symbol, asset_class="equity", timeframe="1d")
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
                asset_class="equity",
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
            stopped.add(symbol)
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
                asset_class="equity",
                timeframe="1d",
                gate="PER_POSITION_STOP_ERROR",
                error=f"stop_sweep_error: {exc}",
            )
            result.errors += 1
            result.decisions.append(sym_decision)
            continue
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
        open_book_at_tick_start: dict[str, float] = {}  # {symbol: held NAV-fraction}
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
            open_book_at_tick_start = dict(_open)  # snapshot for the per-position stop sweep
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
        _stopped_symbols: set[str] = set()
        if not dry_run and os.environ.get("HERMES_QUANT_PER_POSITION_STOP", "0") == "1":
            try:
                _stopped_symbols = _run_per_position_stop_sweep(
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
                _stopped_symbols = set()
            # A stop-closed symbol's slot is freed and it must NOT be re-opened or
            # adjusted in the SAME tick (avoid double-action against a position we just
            # flattened). Drop it from the concurrent-cap accounting so the freed slot is
            # available to a genuinely-new symbol, and skip it in the watchlist loop below.
            for _sym in _stopped_symbols:
                if _sym in open_symbols_at_tick_start:
                    open_symbols_at_tick_start.discard(_sym)
                    open_positions_at_tick_start = max(0, open_positions_at_tick_start - 1)

        for entry in watchlist:
            if entry.symbol in _stopped_symbols:
                # Force-exited by the stop sweep this tick; do not re-evaluate it now.
                continue
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

    # ar80: return the reactor's REALIZED post-clip fill_size_pct alongside the pid so
    # the caller charges ACTUAL consumption into the running headroom (the reactor may
    # have applied a second, tighter ADR-0087 clip against the persisted book). _realized
    # was captured above; a non-float (None) tells the caller to fall back conservatively.
    realized_size = float(_realized) if isinstance(_realized, (int, float)) else None
    return pid, realized_size
