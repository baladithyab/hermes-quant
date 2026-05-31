"""PDR-4 SaturationScore backtest driver — sat-on-vs-off social-arb Sharpe (plan §3.4 / §6.5).

Read-only measurement: loads the VERSIONED labeled exit-set fixture
(tests/fixtures/pdr4_saturation/exit_set.v1.json), replays each labeled social-arb
EXIT case TWICE — saturation OFF (m=1.0, today's behaviour) and saturation ON (the
PDR-4 edge-decay) — and reports the social-arb-slice Sharpe each way. It mutates
NOTHING live (no env flips, no packet store writes, no provider calls): the decay is
computed by the PURE primitive (hermes_quant.perception.saturation.compute_saturation)
directly on the fixture's labeled (packet_asof, decision_asof, peak_asof, confirm_date),
mirroring how the builder produces frame.saturation at the bar-asof anchor.

Edge model (offline, deterministic): a labeled exit case carries a pre-saturation
semantic confidence (== the discrete-ladder position scaler in [0,1]) and the realized
forward return AFTER the saturation point. The per-case PnL is the position-scaled
return: pnl = sized_confidence * realized_forward_return_pct, where sized_confidence is
the confidence AFTER apply_saturation (which is the no-op identity m=1.0 when sat is OFF).
Because the edge is time-decaying (a saturated bull stance bleeds), shrinking the stale
position lifts the mean and trims the tail -> higher Sharpe. A fresh case (decay ~ 1.0)
is barely touched, so still-live winners are preserved.

The §6.5 acceptance is sharpe_on >= sharpe_off on the v1 fixture (the mechanism gate).
The live-influence flip is a SEPARATE human decision after a larger labeled set clears
(B09). This script just hands the operator the number; it never arms the flag.
"""
from __future__ import annotations

import json
import math
import pathlib as _pathlib
from typing import Any

import pandas as pd

from hermes_quant.perception.saturation import apply_saturation, compute_saturation

# N13: read the VERSIONED fixture (committed, offline-deterministic), NEVER /tmp.
EXIT_SET_PATH = (
    _pathlib.Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "pdr4_saturation" / "exit_set.v1.json"
)


def load_exit_set(path: _pathlib.Path | str = EXIT_SET_PATH) -> dict[str, Any]:
    """Load the labeled exit-set fixture (pure read, no mutation)."""
    return json.loads(_pathlib.Path(path).read_text())


def _saturation_for_case(case: dict[str, Any], *, half_life_days: float) -> dict[str, Any]:
    """Compute the PDR-4 saturation dict for one labeled case at its decision_asof.

    Anchors the decay at the BAR/decision asof (== how the builder stamps it with
    last_bar_ts_utc), reading only the case's past observations. PDR-2's velocity
    peak is passed via trend_velocity when the case labels a peak_asof; the
    confirm_date is the hard-exit basis.
    """
    decision_asof = pd.Timestamp(case["decision_asof"])
    peak_asof = case.get("peak_asof")
    trend_velocity = {"peak_asof": peak_asof} if peak_asof else None
    return compute_saturation(
        packet_asof=case.get("packet_asof"),
        asof=decision_asof,
        trend_velocity=trend_velocity,
        confirm_date=case.get("confirm_date"),
        half_life_days=half_life_days,
    )


def case_pnls(cases: list[dict[str, Any]], *, saturation: bool, half_life_days: float) -> list[float]:
    """Per-case position-scaled PnL series. sized_confidence is the confidence after
    the silence-only decay (no-op identity when ``saturation`` is False)."""
    pnls: list[float] = []
    for case in cases:
        conf = float(case["pre_sat_confidence"])
        if saturation:
            sat = _saturation_for_case(case, half_life_days=half_life_days)
            conf = apply_saturation(conf, sat)
        sized = max(0.0, min(1.0, conf))           # position scaler in [0,1] (silence-only)
        pnls.append(sized * float(case["realized_forward_return_pct"]))
    return pnls


def sharpe(pnls: list[float]) -> float:
    """Sharpe-like ratio over the case-level PnL series (mean / std). Degenerate
    (n<2 or zero variance) -> mean (so an all-identical series still ranks by mean)."""
    n = len(pnls)
    if n == 0:
        return 0.0
    mean = sum(pnls) / n
    if n < 2:
        return mean
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    std = math.sqrt(var)
    if std == 0.0:
        return mean
    return mean / std


def run(path: _pathlib.Path | str = EXIT_SET_PATH) -> dict[str, Any]:
    """Compute sat-on-vs-off Sharpe on the labeled exit set. Returns a report dict."""
    fixture = load_exit_set(path)
    cases = fixture["cases"]
    hl = float(fixture.get("half_life_days", 14.0))
    pnl_off = case_pnls(cases, saturation=False, half_life_days=hl)
    pnl_on = case_pnls(cases, saturation=True, half_life_days=hl)
    sharpe_off = sharpe(pnl_off)
    sharpe_on = sharpe(pnl_on)
    per_case = []
    for case, p_off, p_on in zip(cases, pnl_off, pnl_on, strict=True):
        sat = _saturation_for_case(case, half_life_days=hl)
        per_case.append({
            "symbol": case["symbol"],
            "stance": case["stance"],
            "realized_pct": case["realized_forward_return_pct"],
            "basis": sat["basis"],
            "decay_multiplier": sat["decay_multiplier"],
            "pnl_off": round(p_off, 4),
            "pnl_on": round(p_on, 4),
        })
    return {
        "n_cases": len(cases),
        "half_life_days": hl,
        "sharpe_off": sharpe_off,
        "sharpe_on": sharpe_on,
        "improves": sharpe_on >= sharpe_off,
        "per_case": per_case,
    }


def main() -> None:
    report = run()
    print("=" * 70)
    print("PDR-4 SATURATION BACKTEST — social-arb EXIT set (sat-on vs sat-off)")
    print("=" * 70)
    print(f"\nfixture cases = {report['n_cases']}  half_life_days = {report['half_life_days']}")
    print("\nPer-case (position-scaled PnL = sized_confidence * realized_return_pct):")
    print(f"  {'SYM':6s} {'STANCE':8s} {'REALIZED%':>9s} {'BASIS':20s} {'m':>6s} {'PnL_off':>9s} {'PnL_on':>9s}")
    for c in report["per_case"]:
        print(
            f"  {c['symbol']:6s} {c['stance']:8s} {c['realized_pct']:+9.1f} "
            f"{c['basis']:20s} {c['decay_multiplier']:6.3f} "
            f"{c['pnl_off']:+9.3f} {c['pnl_on']:+9.3f}"
        )
    print(f"\nSharpe OFF (m=1.0, today)  = {report['sharpe_off']:+.4f}")
    print(f"Sharpe ON  (PDR-4 decay)   = {report['sharpe_on']:+.4f}")
    print("\n" + "=" * 70)
    verdict = "PASS — decay does NOT hurt the exit set" if report["improves"] else "FAIL — decay hurt Sharpe"
    print(f"MECHANISM GATE (sharpe_on >= sharpe_off): {verdict}")
    print("=" * 70)
    print(
        "\nNOTE: this is the mechanism gate on the v1 fixture. The live-influence flip "
        "of HERMES_QUANT_SATURATION is a SEPARATE human decision after a larger labeled "
        "exit set clears (B09) + a side-by-side audit. This script arms nothing."
    )


if __name__ == "__main__":
    main()
