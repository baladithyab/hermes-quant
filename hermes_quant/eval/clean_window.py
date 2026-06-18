"""hermes_quant.eval.clean_window — ADR-0099 Part C: clean-window gate hierarchy.

The unlock path (ADR-0099 §C):

  GATE-0  Operator reset + arm writes ``clean_window_start.json`` (the t0 anchor).
          Any round-trip settled BEFORE t0 is PRE-GATE-0 and is DISCARDED.
          Until t0 is set ALL gates fail-CLOSED.

  GATE-1  Survival (N>=20 round-trips):
          win_rate >= 0.40 (Wilson CI lower bound clears 0),
          zero kill-switch fires in the window,
          rolling-30d max drawdown <= 8% NAV.
          Failure => strategy review, NOT options unlock.

  GATE-2  Unlocks options HITL-paper origination (N>=50 AND >=60 calendar days):
          profit_factor >= 1.3, win_rate >= 0.50, rolling-90d Sharpe >= 0.8,
          max_consecutive_losses <= 8, drawdown <= 3%.
          Point estimates only at N=50 (CI too wide) => labeled provisional.

  GATE-3  Options-live (N_options>=100, bootstrap sharpe_95ci_lower >= 1.0,
          drawdown <= 1%, no kill-switch in 14d).

DEFAULT-OFF: this module is a pure metric-harness; nothing on the live path
consumes it yet (a future tick/retro reads it behind a flag).

All thresholds are EVAL-GATE-PENDING (flagged explicitly below with
``# EVAL-GATE-PENDING``) — confirmed starting points from Lo 2002 + Bailey/LdP;
calibrate across >=2 clean windows before treating as fixed policy.

MONEY-SOFTWARE POSTURE: fail-CLOSED; non-finite / thin / absent-t0 =>
gate NOT cleared. Finite-guard every numeric input (NaN defeats every <= gate).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Local imports — reuse existing seams; never reinvent.
# ---------------------------------------------------------------------------
from hermes_quant.evaluation.validation import (
    BootstrapCI,
    _block_length_for_sharpe,
    _bootstrap_ci,
    _sharpe,
    _to_array,
)

# ADR-0097 slippage haircut: the conservative live-vs-paper execution penalty.
# Imported at module level so the b61c haircut seam (compute_gate_metrics's
# apply_haircut path) reuses the canonical estimator AND so tests can monkeypatch
# ``clean_window.estimate_live_penalty``. DEFAULT-OFF: only consulted when the
# caller passes apply_haircut=True (wired from HERMES_QUANT_SLIPPAGE_HAIRCUT).
from hermes_quant.risk.slippage_haircut import (
    SHADOW_DIVERGENCE_PATH,
    _DEFAULT_PRIOR,
    estimate_live_penalty,
)

# ---------------------------------------------------------------------------
# Wilson lower bound (closed-form, no scipy).
# Reuse the implementation from rule_mining but duplicate the tiny helper here
# so eval/ has no cross-module dependency on shadow/.
# Reference: Brown, Cai & DasGupta 2001.
# ---------------------------------------------------------------------------
_Z_95: float = 1.959963984540054  # z_{0.975}


def _wilson_lower_bound(wins: int, n: int, *, z: float = _Z_95) -> float:
    """95% Wilson score interval LOWER bound for a binomial proportion.

    Returns 0.0 for n <= 0 (no opinion). Closed form; no scipy dependency.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, centre - half)


# ---------------------------------------------------------------------------
# EVAL-GATE-PENDING thresholds (ADR-0099 §C, flagged for calibration).
# These are starting points — confirmed across >=2 clean windows before policy.
# ---------------------------------------------------------------------------

# GATE-1 thresholds (N>=20)
_G1_MIN_N: int = 20  # EVAL-GATE-PENDING
_G1_WIN_RATE_MIN: float = 0.40  # point, Wilson LB must clear 0  # EVAL-GATE-PENDING
_G1_DRAWDOWN_MAX: float = 0.08  # rolling-30d NAV fraction  # EVAL-GATE-PENDING

# GATE-2 thresholds (N>=50, >=60 calendar days)
_G2_MIN_N: int = 50  # EVAL-GATE-PENDING
_G2_MIN_DAYS: int = 60  # EVAL-GATE-PENDING
_G2_PROFIT_FACTOR_MIN: float = 1.3  # EVAL-GATE-PENDING
_G2_WIN_RATE_MIN: float = 0.50  # EVAL-GATE-PENDING
_G2_SHARPE_MIN: float = 0.8  # rolling-90d  # EVAL-GATE-PENDING
_G2_MAX_CONSEC_LOSSES: int = 8  # EVAL-GATE-PENDING
_G2_DRAWDOWN_MAX: float = 0.03  # EVAL-GATE-PENDING

# GATE-3 thresholds (N_options>=100, bootstrap)
_G3_MIN_N_OPTIONS: int = 100  # EVAL-GATE-PENDING
_G3_SHARPE_CI_LOWER: float = 1.0  # bootstrap sharpe_95ci_lower  # EVAL-GATE-PENDING
_G3_DRAWDOWN_MAX: float = 0.01  # EVAL-GATE-PENDING
_G3_NO_KS_DAYS: int = 14  # no kill-switch fires in last 14d  # EVAL-GATE-PENDING

# Bootstrap config (matches evaluation/validation.py defaults)
_N_RESAMPLES: int = 9999
_CONFIDENCE_LEVEL: float = 0.95
_BOOTSTRAP_SEED: int = 42


# ---------------------------------------------------------------------------
# Clean-window anchor: read / write
# ---------------------------------------------------------------------------
_CW_FILE = "quant/clean_window_start.json"


def _home_path(home: str | Path | None) -> Path:
    """Resolve the operator home directory (injectable for tests)."""
    if home is not None:
        return Path(home)
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def read_clean_window_start(home: str | Path | None = None) -> datetime | None:
    """Read the GATE-0 t0 anchor from ``~/.hermes/quant/clean_window_start.json``.

    Returns a UTC-aware datetime, or None if the file is absent / unreadable.
    None => GATE-0 has NOT been run => ALL gates fail-CLOSED.

    Args:
        home: Override operator home directory (default: HERMES_HOME or ~/.hermes).
              Injected in tests so no real filesystem state is needed.
    """
    path = _home_path(home) / _CW_FILE
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        t0_str = data.get("t0")
        if t0_str is None:
            return None
        dt = datetime.fromisoformat(str(t0_str))
        # Normalise to UTC-aware so comparisons against asof_exit are well-formed.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError, OSError):
        return None


def write_clean_window_start(
    home: str | Path | None,
    asof: datetime,
    armed_flags: dict[str, Any] | None = None,
) -> Path:
    """Write the GATE-0 anchor and the armed-flag snapshot for the run-card.

    Creates intermediate directories if needed. Overwrites any prior file
    (operator reset is an explicit action; overwrite is intentional).

    Args:
        home: Operator home directory (or None => default).
        asof: The t0 datetime.  Stored as UTC ISO-8601.
        armed_flags: Optional snapshot of the live armed-flag values included
                     in the run-card per ADR-0034 (e.g. DURABLE_DRAWDOWN_BASELINE).

    Returns:
        The Path that was written.
    """
    path = _home_path(home) / _CW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Normalise to UTC-aware.
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    payload: dict[str, Any] = {
        "t0": asof.isoformat(),
        "written_at": datetime.now(tz=timezone.utc).isoformat(),
        "armed_flags": armed_flags or {},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Round-trip protocol
# ---------------------------------------------------------------------------
@dataclass
class RoundTrip:
    """Minimal round-trip view needed by the gate harness.

    Callers may build these directly from ``SettledRoundTrip`` objects:

        rt = RoundTrip(
            asof_exit=srt.asof_exit,
            realized_return=srt.realized_return,
            is_options=srt.asset_class in ("us_option", "option"),
        )

    The harness only touches the three fields above; any additional context
    (asset, account_id, …) stays on the SettledRoundTrip and is irrelevant
    to gate metrics.
    """

    asof_exit: datetime
    """UTC-aware (or naive-UTC) datetime of the closing fill."""

    realized_return: float
    """Holding-period return on the matched quantity, net of prorated fees.
    Positive = the lot made money. Must be a finite float; non-finite trips
    are treated as missing and excluded from all metrics.
    """

    is_options: bool = False
    """True for multi-leg options outcomes (relevant only for GATE-3 N_options
    count). Equity round-trips leave this False.
    """


# ---------------------------------------------------------------------------
# GateMetrics dataclass (output of compute_gate_metrics)
# ---------------------------------------------------------------------------
@dataclass
class GateMetrics:
    """Computed gate metrics for the clean-window sample.

    All fields are NaN when the sample is degenerate (thin / all-same-sign
    profit factor / etc.).  A gate evaluates each field with explicit
    ``math.isfinite`` guards: NaN / inf defeat every ``>= threshold`` check.
    """

    n: int = 0
    """Number of round-trips after the t0 filter and finite-return guard."""

    n_options: int = 0
    """Subset of n where is_options=True (GATE-3 N_options count)."""

    win_rate: float = float("nan")
    """Fraction of n with realized_return > 0."""

    win_rate_wilson_lb: float = float("nan")
    """95% Wilson lower bound on win_rate (GATE-1 uses this, not the point)."""

    profit_factor: float = float("nan")
    """Sum of winning returns / abs(sum of losing returns). NaN when no losses."""

    sharpe: float = float("nan")
    """Annualized Sharpe ratio over the sample (bars_per_year=252 daily proxy)."""

    sharpe_95ci_lower: float = float("nan")
    """Bootstrap 95% CI lower bound on Sharpe (GATE-3)."""

    max_consecutive_losses: int = 0
    """Longest streak of consecutive losing round-trips (realized_return <= 0)."""

    max_drawdown: float = float("nan")
    """Maximum peak-to-trough drawdown on the cumulative P&L series (fraction).
    Always <= 0 by definition (positive means no drawdown ever occurred —
    degenerate; reported as 0.0 in that case).
    The gate compares abs(max_drawdown) against the threshold.
    """

    calendar_days: float = float("nan")
    """Calendar days from first to last exit in the sample (GATE-2 >=60d check)."""

    days_since_last_kill_switch: float | None = None
    """If kill-switch events are present: days since the most recent event.
    None means no kill-switch event was observed in the sample.
    """

    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# b61c — ADR-0097 haircut-adjusted ('live_realistic') return series
# ---------------------------------------------------------------------------
def _trip_asset_class(rt: RoundTrip) -> str:
    """The asset_class key for the per-trip live-execution penalty.

    RoundTrip carries only ``is_options`` (the harness contract). Options trips
    are priced with the larger ``us_option`` prior; everything else is treated as
    ``equity`` (the conservative single-name default). This is intentionally
    coarse — the penalty is a CONSERVATIVE prior, not a precise per-instrument cost.
    """
    return "us_option" if rt.is_options else "equity"


def _haircut_adjusted_returns(
    filtered: list[RoundTrip],
    *,
    shadow_log: Path,
    warnings: list[str],
) -> list[float]:
    """Return the ADR-0097 'live_realistic' adjusted return for each filtered trip.

    adjusted = raw_realized_return - penalty, where ``penalty`` is the conservative
    per-asset-class live-vs-paper cost from ``estimate_live_penalty`` (estimated ONCE
    per asset_class and reused). The penalty is ALWAYS a positive cost, so the
    adjustment moves the return TOWARD zero/loss and NEVER improves it
    (haircut-toward-silence).

    FAIL-CLOSED finite-guard: a non-finite penalty (NaN/inf — which should never
    come out of the contractually-finite estimator, but might if it is patched /
    drifts) must NOT become a free pass. It falls back to the conservative
    ``_DEFAULT_PRIOR`` floor (a positive cost), so the adjusted return is
    raw-minus-floor — never raw-unchanged-or-better.
    """
    # Estimate the penalty once per asset_class (mixed-class series reuse).
    penalty_by_ac: dict[str, float] = {}
    for rt in filtered:
        ac = _trip_asset_class(rt)
        if ac in penalty_by_ac:
            continue
        est = estimate_live_penalty(ac, shadow_log=shadow_log)
        pen = est.penalty_frac
        if not math.isfinite(pen):
            # A non-finite penalty must not improve the return: clamp to the
            # conservative positive floor (never 0). This is the ar08 NaN-defeats-
            # every-guard family applied to the haircut input.
            warnings.append(
                f"Non-finite live-execution penalty for asset_class={ac!r} "
                f"(penalty_frac={pen!r}); using conservative floor {_DEFAULT_PRIOR}."
            )
            pen = _DEFAULT_PRIOR
        # The penalty is a one-way COST: take its magnitude so a (defensive)
        # negative value can never improve a return.
        penalty_by_ac[ac] = abs(pen)

    adjusted: list[float] = []
    for rt in filtered:
        cost = penalty_by_ac[_trip_asset_class(rt)]
        adjusted.append(rt.realized_return - cost)
    return adjusted


# ---------------------------------------------------------------------------
# compute_gate_metrics
# ---------------------------------------------------------------------------
def compute_gate_metrics(
    round_trips: list[RoundTrip],
    *,
    t0: datetime,
    kill_switch_events: list[datetime] | None = None,
    bars_per_year: float = 252.0,
    n_resamples: int = _N_RESAMPLES,
    seed: int = _BOOTSTRAP_SEED,
    apply_haircut: bool = False,
    shadow_log: Path | None = None,
) -> GateMetrics:
    """Compute the ADR-0099 gate metrics over post-t0 settled round-trips.

    Pre-GATE-0 trips (asof_exit < t0) are DISCARDED — that data is poisoned
    and carries zero statistical weight.

    Finite-guard: non-finite realized_return is treated as missing and excluded.
    Thin/empty samples return a GateMetrics with all metrics NaN; every gate
    will then fail-CLOSED because NaN is not >= any threshold.

    b61c (ADR-0097 dishonest-evidence fail-open): by DEFAULT the metrics are
    computed on the RAW paper ``realized_return`` series — but Alpaca PAPER fills
    optimistically, so a paper-optimistic window can clear a promotion gate that
    LIVE would not. When ``apply_haircut=True`` (the caller wires this from
    ``HERMES_QUANT_SLIPPAGE_HAIRCUT`` via ``slippage_haircut.haircut_enabled()``),
    each trip's return is replaced by a 'live_realistic' adjusted return =
    ``raw_return - penalty`` where the per-asset-class penalty is the conservative
    ``estimate_live_penalty`` cost. The penalty is ALWAYS a positive cost, so the
    adjustment moves the return TOWARD zero/loss, NEVER improves it (haircut-toward-
    silence). A non-finite penalty fails toward the conservative ``_DEFAULT_PRIOR``
    floor (never 0 — a NaN penalty must not become a free pass). ``apply_haircut=False``
    is BYTE-IDENTICAL to the pre-b61c raw computation.

    Args:
        round_trips: List of RoundTrip objects (may include pre-t0 entries —
                     they are filtered here).
        t0: The GATE-0 anchor (UTC-aware; naive treated as UTC).
              If None is passed (caller checked read_clean_window_start first)
              this returns a thin/empty GateMetrics with a warning.
        kill_switch_events: Optional list of UTC-aware datetimes when the
                            kill-switch fired within the clean window.  Used
                            for GATE-3's "no kill-switch in 14d" check.
        bars_per_year: Annualization factor for Sharpe (default 252 daily).
        n_resamples: Bootstrap resample count (default 9999).
        seed: RNG seed for reproducibility (default 42).
        apply_haircut: When True, compute metrics on the ADR-0097 haircut-adjusted
                       ('live_realistic') return series instead of the raw paper
                       series. Default False => byte-identical raw metrics.
        shadow_log: Override the shadow-divergence log path the per-asset-class
                    ``estimate_live_penalty`` reads (testing). Only used when
                    apply_haircut=True; defaults to the canonical SHADOW path.

    Returns:
        GateMetrics with all fields populated (NaN when undetermined).
    """
    metrics = GateMetrics()

    # Normalize t0 to UTC-aware so comparisons are well-formed.
    if t0 is None:
        metrics.warnings.append(
            "t0 is None: GATE-0 anchor absent; all metrics NaN, all gates fail-CLOSED."
        )
        return metrics

    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)

    # --- Filter: keep only post-t0 trips with finite returns ---
    filtered: list[RoundTrip] = []
    for rt in round_trips:
        # Normalize asof_exit timezone
        asof = rt.asof_exit
        if asof is None:
            continue
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=timezone.utc)
        # PRE-GATE-0 EXCLUDED (strict >=; a trip exactly at t0 is included)
        if asof < t0:
            continue
        # Finite-return guard
        if not math.isfinite(rt.realized_return):
            metrics.warnings.append(
                f"Trip with asof_exit={asof.isoformat()!r} has non-finite "
                f"realized_return={rt.realized_return!r}; excluded."
            )
            continue
        filtered.append(rt)

    n = len(filtered)
    metrics.n = n

    if n == 0:
        metrics.warnings.append(
            f"No finite round-trips after t0={t0.isoformat()}; "
            "all metrics NaN, all gates fail-CLOSED."
        )
        return metrics

    # Sort chronologically (required for drawdown + consecutive-loss calculations)
    filtered.sort(key=lambda rt: rt.asof_exit)

    # --- b61c: build the metric return series (raw, or ADR-0097 haircut-adjusted) ---
    if apply_haircut:
        adjusted = _haircut_adjusted_returns(
            filtered, shadow_log=shadow_log or SHADOW_DIVERGENCE_PATH, warnings=metrics.warnings
        )
        returns = np.array(adjusted, dtype=float)
    else:
        returns = np.array([rt.realized_return for rt in filtered], dtype=float)

    # --- N_options ---
    metrics.n_options = sum(1 for rt in filtered if rt.is_options)

    # --- Win rate + Wilson lower bound ---
    wins = int(np.sum(returns > 0))
    metrics.win_rate = wins / n if n > 0 else float("nan")
    metrics.win_rate_wilson_lb = _wilson_lower_bound(wins, n)

    # --- Profit factor ---
    gross_wins = float(np.sum(returns[returns > 0])) if wins > 0 else 0.0
    losses = returns[returns < 0]
    if losses.size > 0:
        gross_losses = float(np.sum(np.abs(losses)))
        metrics.profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")
    else:
        # No losses at all: profit_factor is undefined (not 0 — no losses is GOOD).
        # Return inf so the profit_factor >= threshold gate passes, but mark it.
        metrics.profit_factor = float("inf")
        metrics.warnings.append(
            "No losing trips: profit_factor is inf (no-loss sample). "
            "Gate thresholds use math.isfinite guard — inf clears any >= threshold."
        )

    # --- Sharpe ---
    if n >= 2:
        metrics.sharpe = _sharpe(returns, bars_per_year=bars_per_year)
    else:
        metrics.sharpe = float("nan")
        metrics.warnings.append("n<2: Sharpe is NaN.")

    # --- Bootstrap sharpe_95ci_lower (GATE-3 uses this) ---
    if n >= 2 and math.isfinite(metrics.sharpe):
        rng = np.random.default_rng(seed)
        warnings_buf: list[str] = []
        try:
            ci: BootstrapCI = _bootstrap_ci(
                returns,
                lambda x: _sharpe(x, bars_per_year=bars_per_year),
                statistic_name="sharpe",
                block_length=_block_length_for_sharpe(returns),
                n_resamples=n_resamples,
                confidence_level=_CONFIDENCE_LEVEL,
                rng=rng,
                bca_seed=seed + 1,
                warnings=warnings_buf,
            )
            metrics.sharpe_95ci_lower = ci.ci_low
        except Exception as exc:  # pragma: no cover — defensive
            metrics.sharpe_95ci_lower = float("nan")
            metrics.warnings.append(f"Bootstrap CI failed: {exc!r}; sharpe_95ci_lower=NaN.")
        metrics.warnings.extend(warnings_buf)
    else:
        metrics.sharpe_95ci_lower = float("nan")

    # --- Max consecutive losses ---
    streak = 0
    max_streak = 0
    for r in returns:
        if r <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    metrics.max_consecutive_losses = max_streak

    # --- Max drawdown on cumulative P&L curve (fractional) ---
    #
    # We treat each realized_return as contributing to a running cumulative
    # P&L. The drawdown is the maximum peak-to-trough decline on that series,
    # expressed as a fraction of the peak.
    #
    # Approach: iterate through the cumulative return series (compound returns)
    # and track the running peak. Each point's drawdown = (peak - current)/peak.
    #
    # wave1-review FIX (fail-OPEN): prepend the implicit 1.0 initial basis. Without
    # it the series starts at the FIRST trip's post-return value, so running_peak[0]
    # == cum[0] and drawdown[0] == 0 — the entire first-trip loss is invisible. A
    # post-t0 book that opens with a -90% loser then recovered would report
    # max_drawdown=0.0 and PASS every drawdown gate (the GATE-1/2/3 dd thresholds)
    # for any threshold. Starting the peak at par (1.0) makes the opening loss count.
    cum = np.concatenate([[1.0], np.cumprod(1.0 + returns)])
    if not np.all(np.isfinite(cum)):
        metrics.max_drawdown = float("nan")
        metrics.warnings.append("Non-finite cumulative P&L: max_drawdown=NaN.")
    else:
        running_peak = np.maximum.accumulate(cum)
        drawdowns = (cum - running_peak) / running_peak  # always <= 0
        metrics.max_drawdown = float(np.min(drawdowns))  # most negative

    # --- Calendar days (first to last exit) ---
    first_exit = filtered[0].asof_exit
    last_exit = filtered[-1].asof_exit
    if first_exit.tzinfo is None:
        first_exit = first_exit.replace(tzinfo=timezone.utc)
    if last_exit.tzinfo is None:
        last_exit = last_exit.replace(tzinfo=timezone.utc)
    metrics.calendar_days = (last_exit - first_exit).total_seconds() / 86400.0

    # --- Kill-switch events ---
    if kill_switch_events is not None and len(kill_switch_events) > 0:
        # Normalize + filter to post-t0
        ks_in_window = []
        now = datetime.now(tz=timezone.utc)
        for ev in kill_switch_events:
            if ev.tzinfo is None:
                ev = ev.replace(tzinfo=timezone.utc)
            if ev >= t0:
                ks_in_window.append(ev)
        if ks_in_window:
            most_recent = max(ks_in_window)
            metrics.days_since_last_kill_switch = (
                now - most_recent
            ).total_seconds() / 86400.0

    return metrics


# ---------------------------------------------------------------------------
# evaluate_gate
# ---------------------------------------------------------------------------
def evaluate_gate(
    metrics: GateMetrics,
    gate_level: int,
    *,
    kill_switch_count: int = 0,
) -> bool:
    """Evaluate whether a gate level is cleared, given computed metrics.

    Fail-CLOSED: any non-finite metric, thin N, or absent t0 => gate NOT cleared.
    All thresholds are module-level EVAL-GATE-PENDING constants.

    Args:
        metrics: Output of :func:`compute_gate_metrics`.
        gate_level: 1, 2, or 3 (GATE-0 is an operator action, not evaluated here).
        kill_switch_count: Number of kill-switch fires in the clean window.
            For GATE-1: must be 0. For GATE-3: no kill-switch in the last 14d
            (use ``metrics.days_since_last_kill_switch`` for the 14d check — the
            caller may pass ``kill_switch_count`` to enforce zero in the window).

    Returns:
        True if and only if ALL criteria for the gate are met.
    """
    if gate_level not in (1, 2, 3):
        raise ValueError(f"gate_level must be 1, 2, or 3; got {gate_level!r}")

    # Helper: a non-finite metric auto-fails (NaN defeats every <= and >= comparison).
    def _finite(x: float) -> bool:
        return math.isfinite(x)

    # ------------------------------------------------------------------
    # GATE-1: Survival (N>=20, win_rate>=0.40, zero kill-switch, dd<=8%)
    # ------------------------------------------------------------------
    if gate_level == 1:
        # N threshold
        if metrics.n < _G1_MIN_N:
            return False

        # win_rate point estimate >= 0.40 AND Wilson lower bound > 0
        # (Wilson LB > 0 means the CI does not include 0 at 95% confidence)
        if not _finite(metrics.win_rate) or metrics.win_rate < _G1_WIN_RATE_MIN:
            return False
        if not _finite(metrics.win_rate_wilson_lb) or metrics.win_rate_wilson_lb <= 0.0:
            return False

        # Zero kill-switch fires
        if kill_switch_count > 0:
            return False

        # Rolling-30d max drawdown <= 8% (we use the full-window dd as a proxy;
        # the caller should pass the 30d-filtered subset for strict compliance)
        if not _finite(metrics.max_drawdown):
            return False
        if abs(metrics.max_drawdown) > _G1_DRAWDOWN_MAX:
            return False

        return True

    # ------------------------------------------------------------------
    # GATE-2: Options HITL-paper unlock (N>=50, >=60d, full metric suite)
    # ------------------------------------------------------------------
    if gate_level == 2:
        # N + calendar-days thresholds
        if metrics.n < _G2_MIN_N:
            return False
        if not _finite(metrics.calendar_days) or metrics.calendar_days < _G2_MIN_DAYS:
            return False

        # profit_factor >= 1.3 (inf is OK — no losses — but NaN is not)
        if not _finite(metrics.profit_factor) and not math.isinf(metrics.profit_factor):
            return False
        # Explicitly handle inf: inf >= 1.3 is True, so no extra guard needed.
        # But we must guard NaN explicitly.
        if math.isnan(metrics.profit_factor):
            return False
        if metrics.profit_factor < _G2_PROFIT_FACTOR_MIN:
            return False

        # win_rate >= 0.50
        if not _finite(metrics.win_rate) or metrics.win_rate < _G2_WIN_RATE_MIN:
            return False

        # Sharpe >= 0.8 (rolling-90d proxy — caller passes 90d-filtered subset)
        if not _finite(metrics.sharpe) or metrics.sharpe < _G2_SHARPE_MIN:
            return False

        # Max consecutive losses <= 8
        if metrics.max_consecutive_losses > _G2_MAX_CONSEC_LOSSES:
            return False

        # Drawdown <= 3%
        if not _finite(metrics.max_drawdown):
            return False
        if abs(metrics.max_drawdown) > _G2_DRAWDOWN_MAX:
            return False

        return True

    # ------------------------------------------------------------------
    # GATE-3: Options-live (N_options>=100, bootstrap CI lower >= 1.0, dd<=1%)
    # ------------------------------------------------------------------
    if gate_level == 3:
        # N_options threshold
        if metrics.n_options < _G3_MIN_N_OPTIONS:
            return False

        # Bootstrap sharpe_95ci_lower >= 1.0
        if not _finite(metrics.sharpe_95ci_lower):
            return False
        if metrics.sharpe_95ci_lower < _G3_SHARPE_CI_LOWER:
            return False

        # Drawdown <= 1%
        if not _finite(metrics.max_drawdown):
            return False
        if abs(metrics.max_drawdown) > _G3_DRAWDOWN_MAX:
            return False

        # No kill-switch in last 14d
        if metrics.days_since_last_kill_switch is not None:
            if metrics.days_since_last_kill_switch < _G3_NO_KS_DAYS:
                return False
        elif kill_switch_count > 0:
            # There were events but days_since wasn't set => treat as recent
            return False

        return True

    # Should be unreachable given the guard at the top, but be explicit.
    return False  # pragma: no cover
