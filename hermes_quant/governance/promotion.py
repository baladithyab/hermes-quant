"""hermes_quant.governance.promotion — paper→live promotion evaluator
(ADR-0031 D5).

Read-only: gathers metrics from the audit log and produces a
`PromotionDecision`. Thresholds are NOT hardcoded here on purpose —
`hermes_quant.react.live` is the single source of truth for the
numerical bounds (ADR-0029 D7). It exports both `LiveTradingApproval`
(the Pydantic validator that ENFORCES the bounds at approval-construction
time) and `LIVE_APPROVAL_THRESHOLDS` (the dict this read-only evaluator
CONSUMES to pre-check those same bounds against audit-log metrics).

react.live is the live binding and a guaranteed-present core module. If
it cannot be imported, or has dropped a key this evaluator depends on, we
fail CLOSED and LOUD (raise) rather than promote on guessed numbers:
duplicating the authoritative thresholds here is exactly the failure mode
ADR-0031 D5 consolidates against, so no local fallback copy survives.

ar125 — two structurally-vacuous sub-gates made non-vacuous:

(1) paper_outcomes_count — previously counted kind='fill' audit events with
    payload.broker='paper', but NO producer ever emits kind='fill' in production.
    The count was always 0 → the 'paper_outcomes_count < min_paper_outcomes'
    sub-gate ALWAYS blocked regardless of how many real trades were made.
    FIX: ALSO derive paper_outcomes_count from the canonical settlement ledger
    (settlement_loop.join_exit_fills on executions.jsonl, filtered to
    account_id='paper-default', asof_exit in the 30d window). The fill-kind audit
    path is KEPT for backward compatibility / a future emitter; the settlement
    path is the load-bearing production source.

(2) sharpe_95ci_lower — previously read evt.payload.get('sharpe_95ci_lower') from
    a promotion_event on the governance audit log, but NO producer ever emits that
    field. The value was always 0.0 → the '0.0 < 1.0' sub-gate ALWAYS blocked.
    FIX: ALSO derive sharpe_95ci_lower from the settled paper-default round-trip
    return series (the same round trips used for paper_outcomes_count). A 95%
    confidence-interval lower bound is computed via a simple non-parametric
    percentile bootstrap (stdlib-only, no numpy/scipy, deterministic seed). If the
    promotion_event path DID provide a sharpe_95ci_lower snapshot (existing tests,
    future emitter), that value wins (latest-wins semantics, backward compat). The
    settlement-derived CI is used only when no in-window promotion_event snapshot
    was found.

    Fail-CLOSED posture is PRESERVED:
    - <10 settled round trips in window → CI returns 0.0 → still blocks (thin data).
    - A non-finite or uncomputable CI → stays 0.0 → blocks.
    - The ar41 finite-guard at the comparison site in evaluate() catches any
      non-finite value that reaches it regardless of derivation path.
    - No OTHER gate is weakened by this change.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hermes_quant.governance import audit_log
from hermes_quant.governance.invariants import IMMUTABLE_INVARIANTS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ar125: settlement-ledger derivation helpers
# ---------------------------------------------------------------------------

# Minimum number of settled paper round trips in the 30d window before we
# attempt a Sharpe CI. Below this the CI estimate is unreliable AND a book with
# fewer than this many completed trades hasn't demonstrated enough activity to be
# promotable — fail-CLOSED by returning 0.0 (the default that blocks the gate).
_MIN_ROUNDS_FOR_CI: int = 10

# Bootstrap iterations for the percentile Sharpe CI. 1000 gives a stable 5th
# percentile at N=10 without being slow (each iteration is a pure Python list op
# on at most a few hundred returns). Deterministic fixed seed for reproducibility.
_BOOTSTRAP_N: int = 1000
_BOOTSTRAP_SEED: int = 42


def _settle_paper_round_trips_in_window(
    window_start: datetime,
    asof: datetime,
    *,
    executions_path: Path | None = None,
) -> list[Any]:  # list[SettledRoundTrip]
    """Return settled paper-default round trips with asof_exit in [window_start, asof].

    ar125: the CANONICAL source for paper_outcomes_count and the realized-return
    series used for sharpe_95ci_lower. Reuses settlement_loop.join_exit_fills — the
    SAME FIFO matcher used by the kill-switch rail (autonomous.compute_cumulative_
    realized_pnl_pct), so the promotion gate's basis agrees with the kill-switch
    basis (same lot matching, same NAV-fraction qty convention).

    Best-effort: any read / parse / match failure returns [] (which leaves
    paper_outcomes_count at whatever the fill-kind audit path provided, and leaves
    sharpe_ci_lower at 0.0 → correctly blocks promotion on a missing/corrupt bus).
    Never raises; never modifies any file.

    Only paper-default round trips whose asof_exit falls in [window_start, asof]
    are counted. The window guard mirrors the 30d rolling window used for all other
    metrics in _collect_metrics. We read the ENTIRE bus (no incremental checkpoint)
    so the FIFO lot matching sees the complete lot history — an open position from
    90 days ago that was closed 10 days ago must be accounted for. The full-bus read
    is acceptable here because _collect_metrics runs at most once per promotion
    evaluation (not on every autonomous tick) and the bus is bounded in practice.
    """
    try:
        from hermes_quant.daemon.settlement_loop import join_exit_fills
        from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH

        path = executions_path if executions_path is not None else EXECUTION_BUS_PATH
        if not path.exists():
            return []

        raw = path.read_bytes()
        last_nl = raw.rfind(b"\n")
        consumed = raw[: last_nl + 1] if last_nl >= 0 else b""
        if not consumed:
            return []

        import json

        records: list[dict] = []
        for line in consumed.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (ValueError, TypeError):
                continue

        if not records:
            return []

        round_trips, _ = join_exit_fills(records)

        # ar125 filter: paper-default account + exit within the 30d window.
        # Mirrors ar34 in _sum_round_trip_realized_fraction (autonomous.py):
        # "restrict to account_id='paper-default'" to avoid cross-account pollution.
        # The asof_exit filter keeps the 30d rolling window semantics.
        result = []
        for rt in round_trips:
            account = getattr(rt, "account_id", "paper-default") or "paper-default"
            if account != "paper-default":
                continue
            asof_exit = getattr(rt, "asof_exit", None)
            if asof_exit is None:
                continue
            # Coerce to UTC tz-aware for comparison (mirrors _collect_metrics asof handling).
            import pandas as pd  # noqa: PLC0415

            if hasattr(asof_exit, "tzinfo"):
                # Python datetime
                if asof_exit.tzinfo is None:
                    asof_exit = asof_exit.replace(tzinfo=UTC)
            else:
                # pandas Timestamp
                try:
                    asof_exit = asof_exit.tz_localize("UTC") if asof_exit.tzinfo is None else asof_exit.tz_convert("UTC")
                    asof_exit = asof_exit.to_pydatetime()
                except Exception:  # noqa: BLE001
                    continue
            if not (window_start <= asof_exit <= asof):
                continue
            result.append(rt)
        return result

    except Exception as exc:  # noqa: BLE001 - best-effort, never raises
        logger.debug("ar125: settlement read for promotion gate failed: %s", exc)
        return []


def _sharpe_95ci_lower_from_round_trips(round_trips: list[Any]) -> float:
    """Derive the 95% CI lower bound on the per-trade Sharpe ratio.

    ar125: the PRIMARY production source for sharpe_95ci_lower (used when no
    in-window promotion_event snapshot is present on the governance audit log,
    which is the case in production today).

    Computation:
    - Extract realized_return from each SettledRoundTrip (holding-period return
      net of fees, already computed by join_exit_fills).
    - If fewer than _MIN_ROUNDS_FOR_CI finite returns, return 0.0 (thin data →
      blocks the gate, as specified in the task's intentional floor).
    - Compute per-trade Sharpe = mean(returns) / std(returns). Non-annualized
      because round-trips are irregular events, not per-bar returns. The gate
      threshold min_sharpe_95ci_lower=1.0 means "the lower CI bound on mean/std
      must be ≥ 1.0" — a trade whose average return is at least one std dev above
      zero.
    - 95% CI via non-parametric percentile bootstrap (stdlib random, no numpy/scipy
      required). Resample with replacement _BOOTSTRAP_N times; each resample
      computes Sharpe = mean/std. The 5th percentile of the bootstrap distribution
      is the one-sided 95% CI lower bound (i.e., "with 95% confidence the true
      per-trade Sharpe is at least this value"). This is the simpler one-sided
      interpretation consistent with how the gate uses it as a FLOOR.
    - A non-finite point estimate or bootstrap CI → return 0.0 (fail-CLOSED).
    - stdlib-only, deterministic (fixed seed), O(N × B) operations where N is
      round-trip count and B=_BOOTSTRAP_N.
    """
    # Collect finite realized returns.
    returns: list[float] = []
    for rt in round_trips:
        rr = getattr(rt, "realized_return", None)
        if rr is None:
            continue
        try:
            v = float(rr)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            returns.append(v)

    if len(returns) < _MIN_ROUNDS_FOR_CI:
        return 0.0  # thin data — intentional fail-CLOSED floor

    import random
    import statistics

    def _point_sharpe(sample: list[float]) -> float:
        if len(sample) < 2:
            return float("nan")
        mn = statistics.fmean(sample)
        sd = statistics.pstdev(sample)
        if sd <= 0:
            # Zero variance: all returns identical. Positive mean → ∞, negative → -∞,
            # zero → 0.0. Return 0.0 (conservative, won't spuriously pass the gate).
            return 0.0
        return mn / sd

    point = _point_sharpe(returns)
    if not math.isfinite(point):
        return 0.0

    n = len(returns)
    rng = random.Random(_BOOTSTRAP_SEED)
    bootstrap_sharpes: list[float] = []
    for _ in range(_BOOTSTRAP_N):
        sample = [returns[rng.randint(0, n - 1)] for _ in range(n)]
        s = _point_sharpe(sample)
        if math.isfinite(s):
            bootstrap_sharpes.append(s)

    if not bootstrap_sharpes:
        return 0.0

    bootstrap_sharpes.sort()
    # 5th percentile index (one-sided 95% CI lower bound).
    idx = max(0, int(math.floor(0.05 * len(bootstrap_sharpes))))
    ci_low = bootstrap_sharpes[idx]
    if not math.isfinite(ci_low):
        return 0.0
    return ci_low


# ---------------------------------------------------------------------------
# Immutable-breach detection (ADR-0031 D6 / ADR-0031:204)
# ---------------------------------------------------------------------------
#
# A paper→live promotion is disqualified by "zero immutable-rule breaches in
# the rolling 30-day window" (ADR-0029 D7 / ADR-0031:91). The breach is read
# from the `reason` of a `gate_rejection` audit row (ADR-0031:204 specifies
# `reason="net_delta_cap"` as the canonical example), NOT from a payload flag.
#
# Historical bug: the detector keyed on `evt.payload['immutable_breach']`, a
# flag NO producer ever writes (risk/gate.py:443-457 `_audit_rejection` emits
# only asset/direction/magnitude/confidence/reason/asof/provenance). That made
# `immutable_breaches_in_window` structurally 0 — a latent fail-OPEN where a
# real immutable breach in the window could not block a paper→live promotion.
#
# The risk gate names circuit-breaker rejections by PREFIX (the numeric
# magnitude is appended): risk/gate.py:544 `drawdown_circuit_breaker_{pct}`,
# :557 `daily_loss_circuit_breaker_{pct}`. The options gate emits exact-match
# reasons (options_gate.py: `net_delta_cap`, `net_delta_cap_at_size`). These
# map onto immutable ADR-0027/ADR-0029 bounds (MAX_DRAWDOWN_PCT,
# MAX_NET_DELTA_PCT_NAV). We also treat a reason that *names* any
# `IMMUTABLE_INVARIANTS` member directly (e.g. `no_naked_short_options`) as a
# breach, so the predicate tracks the single source of truth in invariants.py.
#
# Defined adjacent to the loop so the match set is auditable. Discretionary
# silences (e.g. `cost_gate_below_threshold`, `min_trade_size`) are NOT
# immutable-rule breaches and are deliberately excluded.
_IMMUTABLE_BREACH_REASON_PREFIXES: frozenset[str] = frozenset(
    {
        "drawdown_circuit_breaker",  # MAX_DRAWDOWN_PCT (ADR-0027/0029)
        "daily_loss_circuit_breaker",  # daily-loss immutable breaker
        "net_delta_cap",  # MAX_NET_DELTA_PCT_NAV (ADR-0031:204 canonical)
    }
)


def _reason_is_immutable_breach(reason: str) -> bool:
    """True if a `gate_rejection` reason denotes an immutable-rule breach.

    Matches either a known circuit-breaker/cap PREFIX (the magnitude is
    appended to circuit-breaker reasons) or a reason that contains an
    `IMMUTABLE_INVARIANTS` member name verbatim. Pure / side-effect-free so it
    can be unit-tested in isolation."""
    if not reason:
        return False
    if any(reason.startswith(prefix) for prefix in _IMMUTABLE_BREACH_REASON_PREFIXES):
        return True
    return any(invariant in reason for invariant in IMMUTABLE_INVARIANTS)


# ---------------------------------------------------------------------------
# Threshold binding
# ---------------------------------------------------------------------------

# The prefix-style keys this evaluator reads out of
# `react.live.LIVE_APPROVAL_THRESHOLDS`. react.live owns the *values*
# (ADR-0029 D7); this set is only the contract of *which keys* must be
# present for the gate to make a decision. If react.live ever drops one,
# `_load_thresholds()` raises rather than silently reading a default —
# `test_promotion_threshold_keys_match_react_live` pins this contract so a
# future key rename in react.live fails CI instead of failing open in prod.
_REQUIRED_THRESHOLD_KEYS: frozenset[str] = frozenset(
    {
        "min_paper_outcomes",
        "min_sharpe_95ci_lower",
        "max_rolling_30d_drawdown_pct",
        "max_calibrator_drift",
        "killswitch_window_days",
    }
)


def _load_thresholds() -> dict[str, float]:
    """Return the live promotion thresholds from `react.live` — the single
    source of truth (ADR-0029 D7 / ADR-0031 D5).

    Wire shape: react.live exports both `LiveTradingApproval` (the Pydantic
    model whose validator ENFORCES the bounds at approval-construction time)
    AND `LIVE_APPROVAL_THRESHOLDS` (a dict mirroring those bounds for
    cross-module consumption — this exact function). The dict is the
    integration handle; the validator is the enforcement.

    Fails CLOSED and LOUD. react.live is a guaranteed-present core module,
    so an import failure, a non-dict export, a missing required key, or a
    degenerate value (non-numeric / non-finite / non-positive) is a contract
    breach — not a routine condition to paper over with a local copy of the
    numbers. We raise so the gate never promotes on guessed or meaningless
    thresholds (a silent fallback that drifted from ADR-0029, or a poisoned
    bound, would fail OPEN — the one failure mode a promotion gate must never
    have).
    """
    try:
        from hermes_quant.react.live import LIVE_APPROVAL_THRESHOLDS
    except ImportError as exc:
        raise RuntimeError(
            "hermes_quant.react.live is the single source of truth for "
            "promotion thresholds (ADR-0029 D7) but could not be imported. "
            "Refusing to evaluate the paper→live gate on guessed numbers."
        ) from exc

    if not isinstance(LIVE_APPROVAL_THRESHOLDS, dict) or not LIVE_APPROVAL_THRESHOLDS:
        raise RuntimeError(
            "react.live.LIVE_APPROVAL_THRESHOLDS is not a non-empty dict; "
            "cannot evaluate the paper→live gate. Got: "
            f"{type(LIVE_APPROVAL_THRESHOLDS).__name__}."
        )

    missing = _REQUIRED_THRESHOLD_KEYS - LIVE_APPROVAL_THRESHOLDS.keys()
    if missing:
        raise RuntimeError(
            "react.live.LIVE_APPROVAL_THRESHOLDS is missing keys this "
            f"evaluator depends on: {sorted(missing)}. react.live must keep "
            "these prefix-style keys in sync with ADR-0029 D7 (see "
            "_REQUIRED_THRESHOLD_KEYS)."
        )

    # Structural value sanity — NOT policy magnitudes. A required key present
    # with a degenerate value (None, NaN, a string, 0, or negative) would NOT
    # crash: it would flow into evaluate()'s `metric < threshold` blocks and
    # quietly flip the gate OPEN (`x < 0` / `x < NaN` never blocks). We reject
    # any non-finite or non-positive bound so that "thresholds present" cannot
    # mean "thresholds meaningless". We deliberately do NOT re-assert the
    # ADR-0029 numbers (>=100, >=1.0, ...) here — that magnitude policy lives
    # in react.live alone (ADR-0031 D5); duplicating it is the failure mode
    # this module avoids. We only require each bound be a finite, positive
    # number so the comparison operators in evaluate() behave.
    for key in _REQUIRED_THRESHOLD_KEYS:
        value = LIVE_APPROVAL_THRESHOLDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(
                f"react.live.LIVE_APPROVAL_THRESHOLDS[{key!r}] must be a "
                f"number, got {type(value).__name__}={value!r}. Refusing to "
                "evaluate the paper→live gate on a non-numeric bound."
            )
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                f"react.live.LIVE_APPROVAL_THRESHOLDS[{key!r}]={value!r} is "
                "not a finite positive number. A non-finite or non-positive "
                "bound would fail the gate OPEN (e.g. `x < NaN` never blocks); "
                "refusing to evaluate."
            )

    return dict(LIVE_APPROVAL_THRESHOLDS)


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


class PromotionDecision(BaseModel):
    """Result of `evaluate()`. `promoted=True` only when every promotion
    check passes — a SUPERSET of the LiveTradingApproval validator (ADR-0029
    D7): in addition to the validator's five fields, `evaluate()` also blocks
    on calibrator drift and weekly-retro readiness, so it is strictly at
    least as conservative as the validator, never looser."""

    promoted: bool
    blocked_by: list[str] = Field(default_factory=list)
    paper_outcomes_count: int = 0
    rolling_30d_realized_sharpe: float = 0.0
    sharpe_95ci_lower: float = 0.0
    rolling_30d_max_drawdown_pct: float = 0.0
    no_killswitch_in_trailing_14d: bool = False
    immutable_breaches_in_window: int = 0
    calibrator_drift_max: float = 0.0
    weekly_retro_promotion_readiness: bool = False


# ---------------------------------------------------------------------------
# Metric collection from audit log
# ---------------------------------------------------------------------------

# risk/gate.py Rule 1 emits a `gate_rejection` whose reason encodes the realized
# drawdown magnitude: `drawdown_circuit_breaker_{drawdown_pct:.4f}` (gate.py:544).
# This is the ONLY producer that puts a real paper drawdown on the governance
# audit log: NO live producer emits `rolling_30d_max_drawdown_pct` into a
# `promotion_event` payload (weekly_retro.emit_promotion_readiness emits only
# readiness/belief counts). Without deriving the drawdown from the circuit-breaker
# reason, `_collect_metrics` would leave `rolling_30d_max_drawdown_pct` at its 0.0
# default and the `> max_rolling_30d_drawdown_pct` block at evaluate() would be
# VACUOUS — a strategy that tripped a real 30% drawdown breaker in the window would
# NOT be blocked from paper→live promotion (latent fail-OPEN, same class as the
# never-written `immutable_breach` flag). We parse the magnitude out of the reason
# the breaker actually emits so a silenced/halted drawdown is OBSERVABLE here.
_DRAWDOWN_BREAKER_PREFIX: str = "drawdown_circuit_breaker_"


def _drawdown_from_breaker_reason(reason: Any) -> float | None:
    """Parse the realized drawdown fraction out of a `drawdown_circuit_breaker_*`
    `gate_rejection` reason. Returns the float magnitude, or None if the reason is
    not a drawdown breaker / is unparseable (never raises). Mirrors the way ar77
    derives immutable breaches from the breaker reason rather than a flag no
    producer writes."""
    if not isinstance(reason, str) or not reason.startswith(_DRAWDOWN_BREAKER_PREFIX):
        return None
    try:
        return abs(float(reason[len(_DRAWDOWN_BREAKER_PREFIX):]))
    except (TypeError, ValueError):
        return None


def _max_calibrator_drift_in_window(window_start: datetime, asof: datetime) -> float:
    """ar101: max abs(drift) from the calibrator drift-log within [window_start, asof].

    The drift gate in `evaluate()` keys on `calibrator_drift_max`, but the ONLY
    producer of a drift magnitude — `training.calibrator_drift.append_drift_log` —
    writes its OWN ``~/.hermes/quant/calibrators/drift-log.jsonl`` plane, never a
    governance ``promotion_event``. So `_collect_metrics` read the never-emitted
    payload field, the value stayed 0.0, and the drift sub-gate was VACUOUS (a
    drifted calibrator was NOT blocked from paper->live promotion — fail-OPEN, the
    ar100 sibling). Rather than wire a NEW producer onto the governance log (a
    cross-plane design choice), we teach the gate to READ the drift detector's
    existing log directly — the minimal, source-of-truth derivation. Best-effort:
    a missing/corrupt/unreadable log yields 0.0 (byte-identical to the prior vacuous
    behavior — never blocks promotion on a read failure, never raises). Each row is
    ``{schema_version, asof, drift, ...}`` (calibrator_drift.append_drift_log)."""
    import json
    from pathlib import Path

    from hermes_quant.training.calibrator_drift import DRIFT_LOG_PATH

    drift_max = 0.0
    try:
        path = Path(DRIFT_LOG_PATH)
        if not path.exists():
            return 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue  # tolerate a torn trailing append
            if not isinstance(row, dict):
                continue
            row_asof = _parse_event_asof(row.get("asof"))
            if row_asof is None or not (window_start <= row_asof <= asof):
                continue
            drift = row.get("drift")
            if drift is None:
                continue
            try:
                d = abs(float(drift))
            except (TypeError, ValueError):
                continue
            if math.isfinite(d):
                drift_max = max(drift_max, d)
    except OSError as exc:  # pragma: no cover - fs dependent
        logger.warning("calibrator drift-log read failed (%s); drift gate sees 0.0", exc)
        return 0.0
    return drift_max


def _parse_event_asof(value: Any) -> datetime | None:
    """Parse an ISO-8601 asof string to a tz-aware UTC datetime, or None (never raises)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _collect_metrics(
    asof: datetime,
    *,
    executions_path: Path | None = None,
) -> dict[str, Any]:
    """Walk the audit log and compute the inputs to `PromotionDecision`.

    ar125: also derives paper_outcomes_count and sharpe_95ci_lower from the
    canonical settlement ledger (executions.jsonl via join_exit_fills) when no
    in-window audit-log producer has emitted those values. See module docstring
    for the full fix rationale.

    The ``executions_path`` kwarg is exposed for testing only (lets tests inject
    a tmp_path bus without monkeypatching the global). Production callers omit it;
    the helper uses the live EXECUTION_BUS_PATH.
    """
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)

    window_30d_start = asof - timedelta(days=30)
    window_14d_start = asof - timedelta(days=14)

    # ar125: legacy audit-log fill-kind counter (backward compat / future producer).
    # In production today this stays 0 because no producer emits kind='fill'.
    # The settlement-derived count (computed below) is the load-bearing production source.
    paper_outcomes_from_fills = 0
    fills_pnl: list[float] = []
    killswitch_in_14d = False
    immutable_breach_count = 0
    calibrator_drift_max = 0.0
    weekly_retro_ready = False
    sharpe_ci_lower = 0.0
    # sharpe_95ci_lower gates on `<` (a FLOOR), so it must reflect the CURRENT
    # (latest in-window) snapshot, not the window's single best moment. Reducing
    # with max() is the PERMISSIVE direction — one momentarily-good snapshot would
    # admit promotion even after every later snapshot degraded below the floor (a
    # latent fail-OPEN). We therefore keep the LATEST in-window value, tracked by
    # the snapshot's asof. (drawdown / calibrator_drift gate on `>` so their max()
    # reducers below stay correctly conservative and unchanged.)
    sharpe_ci_latest_asof: datetime | None = None
    rolling_30d_max_drawdown_pct = 0.0

    # ar56 defense-in-depth: read only the kinds this collector consumes. The read side
    # already skips extension kinds (audit_log.read, *_llm_call rows that would raise a
    # ValidationError on GovernanceEvent reconstruction), but an explicit whitelist keeps
    # this consumer robust independent of that skip and mirrors meta_retro's filtered reads.
    for evt in audit_log.read(
        kinds=["fill", "kill_switch_fired", "gate_rejection", "promotion_event"]
    ):
        evt_asof = evt.asof
        if evt_asof.tzinfo is None:
            evt_asof = evt_asof.replace(tzinfo=UTC)

        # Settled paper outcomes (legacy path) — we use `fill` events with broker='paper'.
        # ar125: in production NO producer emits kind='fill'; the settlement-derived count
        # below is the load-bearing source. This branch is kept for backward compatibility
        # and so a future producer writing kind='fill' events works automatically.
        if evt.kind == "fill" and evt.payload.get("broker") == "paper":
            paper_outcomes_from_fills += 1
            pnl = evt.payload.get("realized_pnl")
            if pnl is not None:
                try:
                    fills_pnl.append(float(pnl))
                except (TypeError, ValueError):
                    pass

        # Killswitch in trailing 14d
        if evt.kind == "kill_switch_fired" and evt_asof >= window_14d_start:
            killswitch_in_14d = True

        # Immutable breaches: gate_rejection events in the 30d window whose
        # payload.reason references an IMMUTABLE_INVARIANTS member (ADR-0031:204).
        # We ALSO honor the legacy `immutable_breach` flag for backward
        # compatibility, so a future producer can still set it explicitly — but
        # the reason-based predicate is the load-bearing path, because that is
        # what risk/gate.py actually emits (it never writes the flag).
        if evt.kind == "gate_rejection" and evt_asof >= window_30d_start:
            reason = str(evt.payload.get("reason", ""))
            if (
                evt.payload.get("immutable_breach") is True
                or _reason_is_immutable_breach(reason)
            ):
                immutable_breach_count += 1

        # Realized drawdown from the drawdown circuit breaker (gate.py Rule 1).
        # The breaker's `gate_rejection` reason encodes the magnitude
        # (`drawdown_circuit_breaker_{pct}`) — the ONLY producer that surfaces a
        # real paper drawdown on the governance audit log. Deriving it here makes
        # the silenced/halted drawdown OBSERVABLE to the gate's drawdown block;
        # without it that block reads the 0.0 default and never fires (fail-OPEN).
        if evt.kind == "gate_rejection" and evt_asof >= window_30d_start:
            breaker_dd = _drawdown_from_breaker_reason(evt.payload.get("reason"))
            if breaker_dd is not None:
                # ar100 follow-up: a NON-FINITE breaker magnitude must PROPAGATE so the
                # evaluate() ar41 finite-guard fail-CLOSES on it. A bare
                # `max(rolling, breaker_dd)` SWALLOWS a NaN — `max(0.0, nan)` returns
                # 0.0 (CPython keeps the first arg when no later arg is strictly
                # greater) — so a `drawdown_circuit_breaker_nan` reason would silently
                # read as a clean 0.0 and the drawdown gate would NOT block (fail-OPEN,
                # the one failure mode a money gate must never have). `inf` already
                # reaches the guard via max(); only nan is swallowed. Mirror the ar101
                # drift derivation, but for a money DRAWDOWN treat un-evaluable as
                # un-promotable: carry the non-finite value forward rather than dropping
                # it. (The legitimate producer finite-guards drawdown_pct before
                # emitting, so the happy path is byte-identical.)
                if not math.isfinite(breaker_dd):
                    rolling_30d_max_drawdown_pct = breaker_dd
                else:
                    rolling_30d_max_drawdown_pct = max(rolling_30d_max_drawdown_pct, breaker_dd)

        # Calibrator drift snapshots emitted as promotion_event
        if evt.kind == "promotion_event" and evt_asof >= window_30d_start:
            drift = evt.payload.get("calibrator_drift")
            if drift is not None:
                try:
                    calibrator_drift_max = max(calibrator_drift_max, abs(float(drift)))
                except (TypeError, ValueError):
                    pass

            sharpe_ci = evt.payload.get("sharpe_95ci_lower")
            if sharpe_ci is not None:
                try:
                    val = float(sharpe_ci)
                except (TypeError, ValueError):
                    pass
                else:
                    # Keep the LATEST in-window snapshot (by asof), not the max —
                    # a FLOOR gate must see the current value, not the best historical.
                    if sharpe_ci_latest_asof is None or evt_asof >= sharpe_ci_latest_asof:
                        sharpe_ci_lower = val
                        sharpe_ci_latest_asof = evt_asof

            dd = evt.payload.get("rolling_30d_max_drawdown_pct")
            if dd is not None:
                try:
                    rolling_30d_max_drawdown_pct = max(rolling_30d_max_drawdown_pct, float(dd))
                except (TypeError, ValueError):
                    pass

            if evt.payload.get("weekly_retro_promotion_readiness") is True:
                weekly_retro_ready = True

    # ar101: merge the calibrator drift-log max into calibrator_drift_max. The
    # legacy promotion_event payload read above (`calibrator_drift`) is honored for
    # backward compatibility / an explicit future producer, but the LOAD-BEARING
    # source is the drift detector's own drift-log.jsonl — without this the drift
    # sub-gate read the 0.0 default and never fired (fail-OPEN). max() keeps the
    # conservative `>`-gate direction; the drift-log read is best-effort (0.0 on any
    # failure) so a missing log is byte-identical to the prior vacuous behavior.
    calibrator_drift_max = max(
        calibrator_drift_max, _max_calibrator_drift_in_window(window_30d_start, asof)
    )

    # -----------------------------------------------------------------------
    # ar125: settlement-ledger derivation for paper_outcomes_count and
    # sharpe_95ci_lower — the PRIMARY production sources for both metrics.
    # -----------------------------------------------------------------------
    # Read the full executions.jsonl bus and run FIFO lot matching to find
    # all paper-default round trips in the 30d window. This is the SAME source
    # the kill-switch rail uses (autonomous.compute_cumulative_realized_pnl_pct),
    # so the promotion gate's paper-outcome evidence agrees with the kill-switch
    # evidence. The full-bus read is acceptable here: _collect_metrics runs at
    # most once per promotion evaluation, not on every autonomous tick.
    settled_paper_rts = _settle_paper_round_trips_in_window(
        window_30d_start, asof, executions_path=executions_path
    )
    settlement_count = len(settled_paper_rts)

    # Merge fill-kind (legacy) + settlement (primary) counts. In production:
    # fill-kind count = 0, settlement count = real number of closed trades.
    # In tests that seed fill-kind events but no executions.jsonl:
    # fill-kind count = 100+, settlement count = 0 → total unchanged.
    paper_outcomes = paper_outcomes_from_fills + settlement_count

    # Crude Sharpe point estimate from fills_pnl (mean / std). NOT used
    # for the gate — the gate uses sharpe_ci_lower.
    if len(fills_pnl) >= 2:
        import statistics

        mean = statistics.fmean(fills_pnl)
        sd = statistics.pstdev(fills_pnl)
        rolling_sharpe = mean / sd if sd > 0 else 0.0
    else:
        rolling_sharpe = 0.0

    # ar125: derive sharpe_95ci_lower from the settlement ledger ONLY when no
    # in-window promotion_event snapshot was found (sharpe_ci_latest_asof is None).
    # If a promotion_event did provide a sharpe_95ci_lower (tests, future producer),
    # that value wins — backward-compatible latest-wins semantics unchanged.
    if sharpe_ci_latest_asof is None and settled_paper_rts:
        derived_ci = _sharpe_95ci_lower_from_round_trips(settled_paper_rts)
        # derived_ci is 0.0 on thin data (<10 rounds) or non-finite → still blocks.
        # Use it as the sharpe_ci_lower if it is finite (even if 0.0 — a 0.0
        # derived CI correctly blocks the gate when data is too thin).
        if math.isfinite(derived_ci):
            sharpe_ci_lower = derived_ci

    return {
        "paper_outcomes_count": paper_outcomes,
        "rolling_30d_realized_sharpe": rolling_sharpe,
        "sharpe_95ci_lower": sharpe_ci_lower,
        "rolling_30d_max_drawdown_pct": rolling_30d_max_drawdown_pct,
        "no_killswitch_in_trailing_14d": not killswitch_in_14d,
        "immutable_breaches_in_window": immutable_breach_count,
        "calibrator_drift_max": calibrator_drift_max,
        "weekly_retro_promotion_readiness": weekly_retro_ready,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(asof: datetime) -> PromotionDecision:
    """Compute a PromotionDecision. Always emits one promotion_event row
    to the audit log, regardless of the result (per ADR-0031 D5)."""
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)

    thresholds = _load_thresholds()
    metrics = _collect_metrics(asof)

    blocked: list[str] = []

    if metrics["paper_outcomes_count"] < int(thresholds["min_paper_outcomes"]):
        blocked.append(
            f"paper_outcomes_count={metrics['paper_outcomes_count']} "
            f"< min={int(thresholds['min_paper_outcomes'])}"
        )

    # ar41: finite-guard the candidate METRIC side, not just the threshold side.
    # _load_thresholds() already guards the thresholds (and the docstring warns
    # "x < NaN never blocks ... the one failure mode a promotion gate must never"),
    # but _collect_metrics reads sharpe/drawdown/drift via bare float() which accepts
    # NaN/inf — and `nan < min` / `nan > max` are BOTH False, so a degenerate-bootstrap
    # NaN metric silently bypassed every floor/ceiling. A non-finite candidate metric
    # is un-evaluable, so it must BLOCK (fail-closed), mirroring the threshold guard.
    _sharpe = metrics["sharpe_95ci_lower"]
    if not math.isfinite(_sharpe):
        blocked.append(f"sharpe_95ci_lower={_sharpe!r} is non-finite (un-evaluable; fail-closed)")
    elif _sharpe < float(thresholds["min_sharpe_95ci_lower"]):
        blocked.append(
            f"sharpe_95ci_lower={_sharpe:.4f} "
            f"< min={thresholds['min_sharpe_95ci_lower']:.2f}"
        )

    _dd = metrics["rolling_30d_max_drawdown_pct"]
    if not math.isfinite(_dd):
        blocked.append(f"rolling_30d_max_drawdown_pct={_dd!r} is non-finite (un-evaluable; fail-closed)")
    elif _dd > float(thresholds["max_rolling_30d_drawdown_pct"]):
        blocked.append(
            f"rolling_30d_max_drawdown_pct={_dd:.4f} "
            f"> max={thresholds['max_rolling_30d_drawdown_pct']:.4f}"
        )

    if not metrics["no_killswitch_in_trailing_14d"]:
        blocked.append(
            f"kill switch fired within trailing {int(thresholds['killswitch_window_days'])}d window"
        )

    if metrics["immutable_breaches_in_window"] != 0:
        blocked.append(
            f"immutable_breaches_in_window={metrics['immutable_breaches_in_window']} (must be 0)"
        )

    _drift = metrics["calibrator_drift_max"]
    if not math.isfinite(_drift):
        blocked.append(f"calibrator_drift_max={_drift!r} is non-finite (un-evaluable; fail-closed)")
    elif _drift > float(thresholds["max_calibrator_drift"]):
        blocked.append(
            f"calibrator_drift_max={_drift:.4f} "
            f"> max={thresholds['max_calibrator_drift']:.4f}"
        )

    if not metrics["weekly_retro_promotion_readiness"]:
        blocked.append("weekly_retro_promotion_readiness=False")

    decision = PromotionDecision(
        promoted=len(blocked) == 0,
        blocked_by=blocked,
        **metrics,
    )

    # Always emit one promotion_event row.
    audit_log.append(
        audit_log.GovernanceEvent(
            kind="promotion_event",
            asof=asof,
            source="governance.promotion.evaluate",
            payload={
                "row_type": "evaluate_result",
                "promoted": decision.promoted,
                "blocked_by": decision.blocked_by,
                "metrics": metrics,
            },
        )
    )

    return decision
