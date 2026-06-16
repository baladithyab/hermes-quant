"""quant-factor-weight-propose.py — W4 weekly factor-weight proposer cron (DEFAULT-OFF).

Flag-gated by HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1 (default-OFF: byte-identical no-op when unset).
Splits real OHLCV bars TIME-ORDERED into TRAIN/HOLDOUT, runs FactorOracle.evaluate_all → a
CANDIDATE weight diff (silence-only, capped) on the TRAIN bars ONLY, then scores that PROPOSED set
on the strictly-later HOLDOUT window the proposer never saw (genuine OOS DSR + cross-fold Sharpe
jitter → plateau_stable), and writes an ADVISORY-PLANE candidate JSON for OPERATOR review only when
the set STRICTLY beats prior-best AND is plateau-stable. Promotes NOTHING. Mirrors the
catalyst-profitability watchdog: silent unless a factor crosses a tier boundary or the eval flips.

No lookahead: the TRAIN/HOLDOUT split is positional on chronologically-ordered bars, so every
HOLDOUT timestamp strictly post-dates TRAIN, and the proposer is handed TRAIN only. External-truth:
forward returns / OOS DSR come from market bars — never the proposer's own verdict re-ingested as
truth (ADR-0080 §D80.3). Honesty rails = graph_mining.py. Promotion path = operator + ADR-0052 only.

Cron registration (operator-applied): job ``quant-factor-weight-propose-weekly``, ``0 7 * * 6``
(Sat 07:00 PT — AFTER the catalyst-graph-mine slot at 06:00 so the two weekly miners don't
collide), ``deliver=origin`` no_agent. Silent unless a factor crosses a tier boundary or the eval
verdict flips. No-op until HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1 is set in the env.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# venv re-exec (copy quant-catalyst-profitability.py:17-19 verbatim)
_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

# State baseline for the change-detecting no_agent watchdog (mirrors the profitability
# probe pattern). Persisted per-factor: {tier, eval_passed}.
_BASELINE = Path.home() / ".hermes" / "quant" / "factors" / "weight-propose-baseline.json"


def _flag_on() -> bool:
    """DEFAULT-OFF flag, read at call time so tests can monkeypatch env.

    Off-state = byte-identical no-op (ADR-0080 §D80.8): with the flag unset/"0" the cron
    exits 0 having read nothing, written nothing.
    """
    return os.environ.get("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "0") == "1"


def _load_baseline() -> dict[str, dict]:
    """Load the per-factor watchdog baseline. Missing/corrupt -> {} (first run)."""
    try:
        return json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_baseline(state: dict) -> None:
    """Persist the watchdog baseline. Best-effort (never raises)."""
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(state, sort_keys=True))
    except OSError:
        pass


def _current_state(proposal_set, *, eval_passed: bool) -> dict[str, dict]:
    """Project a FactorWeightProposalSet -> {factor_id: {tier}} + a top-level eval flag.

    A transition is a factor crossing a tier boundary vs baseline, or the eval verdict
    flipping. Standing-state (same tiers + same eval verdict) produces nothing -> silence.
    """
    state: dict[str, dict] = {
        p.factor_id: {"tier": p.verdict_tier} for p in proposal_set.proposals
    }
    state["__eval__"] = {"eval_passed": bool(eval_passed)}
    return state


def _transitions(cur: dict[str, dict], baseline: dict[str, dict]) -> list[str]:
    """Pure state-transition diff: emit a line ONLY when a factor crosses a tier boundary
    vs baseline, or the eval verdict flips. Standing state produces nothing (no_agent)."""
    out: list[str] = []
    for key, c in cur.items():
        if key == "__eval__":
            b = baseline.get("__eval__")
            if b is not None and c.get("eval_passed") != b.get("eval_passed"):
                out.append(f"eval verdict {b.get('eval_passed')} -> {c.get('eval_passed')}")
            continue
        b = baseline.get(key)
        if b is None:
            continue  # a newly-seen factor is not itself a transition (silent until it moves)
        if c.get("tier") != b.get("tier"):
            out.append(f"{key} tier {b.get('tier')} -> {c.get('tier')}")
    return out


def _yf_bars(symbol: str, *, period: str = "5y", interval: str = "1d"):
    """Fetch real OHLCV bars (lowercased cols for FactorOracle). Network at cron time only."""
    import contextlib
    import io
    import warnings

    warnings.filterwarnings("ignore")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            import yfinance as yf

            df = yf.download(
                symbol, period=period, interval=interval, auto_adjust=True, progress=False
            )
        except Exception:
            return None
    if df is None or len(df) == 0:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    return df.dropna()


def _build_proposal_set(bars):
    """evaluate_all → propose_weights on the TRAIN bars ONLY. Pure given bars.

    Returns ``(proposal_set, zoo)``: the zoo is handed back so the held-out scorer can recompute
    the SAME factor series on the HOLDOUT tail (the proposer itself never sees the holdout — the
    split happens in main() before this is called). Current weights default to zero.

    IC-dedup-at-ingest (B38 / ar23): the IC-dedup gate inside ``AlphaZoo.register`` only fires when
    the operator flag ``HERMES_QUANT_IC_DEDUP_AT_INGEST=1`` is set AND a per-factor ``factor_returns``
    mapping is supplied at register time. We therefore compute each starter factor's return series on
    the bars FIRST (``compute_starter_factor_returns``), then hand that mapping to
    ``register_starter_set`` so the gate can reject near-duplicates at ingest. Without this ordering
    the returns were only available AFTER register (from ``evaluate_all``), so ``run_ic_gate`` was
    always False and the flag was inert in production. Flag-OFF is byte-identical: with the flag unset
    the gate is a no-op regardless of the mapping, so every factor still registers exactly as before.
    """
    from hermes_quant.factors.alpha_zoo import AlphaZoo
    from hermes_quant.factors.factor_oracle import FactorOracle
    from hermes_quant.factors.starter_set import (
        compute_starter_factor_returns,
        register_starter_set,
    )
    from hermes_quant.factors.weight_proposer import propose_weights

    zoo = AlphaZoo()
    # Compute factor returns BEFORE register so the IC-dedup gate has its input at ingest.
    factor_returns = compute_starter_factor_returns(bars)
    register_starter_set(zoo, factor_returns=factor_returns)
    verdicts = FactorOracle(zoo).evaluate_all(bars)
    return propose_weights(verdicts, current_weights=None), zoo


def run_once(
    *, bars, holdout_dsr, holdout_sharpe_delta, plateau_stable, proposal_set=None, verbose=False
) -> int:
    """Core watchdog logic, decoupled from network so it is unit-testable.

    1. propose from TRAIN verdicts (``proposal_set`` is built on TRAIN bars by the caller — main()
       supplies it after the time-ordered split so the proposer never sees the holdout; when None
       it is built from ``bars`` for unit tests); 2. score against held-out (caller supplies the
       OOS numbers the proposer never saw); 3. if eval_passed → write candidates + update
       checkpoint, else → append to the rejected buffer (checkpoint-fallback: do NOT write
       candidates); 4. emit ONLY on a tier transition or eval-verdict flip (no_agent watchdog).
    """
    from hermes_quant.factors.weight_proposer import (
        append_rejected,
        evaluate_against_holdout,
        load_prior_best_dsr,
        save_prior_best_dsr,
        write_candidates,
    )

    if proposal_set is None:
        proposal_set = _build_proposal_set(bars)
    if not proposal_set.proposals:
        return 0  # silence-by-default: no factors evaluated

    prior_best = load_prior_best_dsr()
    proposal_set = evaluate_against_holdout(
        proposal_set,
        holdout_dsr=holdout_dsr,
        holdout_sharpe_delta=holdout_sharpe_delta,
        prior_best_dsr=prior_best,
        plateau_stable=plateau_stable,
    )

    if proposal_set.eval_passed:
        write_candidates(proposal_set)
        save_prior_best_dsr(holdout_dsr)
    else:
        append_rejected(proposal_set)  # checkpoint-fallback: do NOT write candidates

    cur = _current_state(proposal_set, eval_passed=proposal_set.eval_passed)
    baseline = _load_baseline()
    transitions = _transitions(cur, baseline)
    _save_baseline(cur)

    if verbose:
        print(f"📊 factor-weight-propose: eval_passed={proposal_set.eval_passed} "
              f"holdout_dsr={proposal_set.held_out_dsr} prior_best={proposal_set.prior_best_dsr}")
        for p in proposal_set.proposals:
            print(f"  {p.factor_id:30s} {p.verdict_tier:12s} "
                  f"{p.current_weight:.2f} -> {p.proposed_weight:.2f}")
        return 0

    if not transitions:
        return 0  # standing state, unchanged -> silent (no_agent watchdog)

    print("📊 factor-weight-propose: " + "; ".join(transitions))
    for p in proposal_set.proposals:
        print(f"  {p.factor_id:30s} {p.verdict_tier:12s} "
              f"{p.current_weight:.2f} -> {p.proposed_weight:.2f}")
    return 0


TRAIN_FRAC: float = 0.7  # the first 70% of bars (time-ordered) is TRAIN; the later 30% is HOLDOUT.


def _split_train_holdout(bars):
    """Time-ordered TRAIN/HOLDOUT split. The HOLDOUT is a STRICTLY-LATER window; the proposer
    only ever sees TRAIN. No lookahead: rows are in chronological order on entry (yfinance
    returns ascending dates), and the cut is positional so HOLDOUT timestamps all post-date TRAIN.
    """
    n = len(bars)
    cut = int(n * TRAIN_FRAC)
    train = bars.iloc[:cut]
    holdout = bars.iloc[cut:]
    return train, holdout


def main() -> int:
    # DEFAULT-OFF flag gate (read at call time). Off-state = byte-identical no-op.
    if not _flag_on():
        return 0  # silence-by-default; the cron is a no-op until explicitly enabled

    verbose = "--verbose" in sys.argv
    bars = _yf_bars("SPY")
    if bars is None or len(bars) < 80:
        return 0  # silence-by-default: insufficient bars to evaluate

    # NO-LOOKAHEAD: split TIME-ORDERED into TRAIN/HOLDOUT *before* the proposer runs. The proposer
    # (evaluate_all → propose_weights) sees TRAIN bars ONLY; the HOLDOUT is a strictly-later window.
    train, holdout = _split_train_holdout(bars)
    # holdout needs >= 31 bars: score_holdout's .shift(-1) (next-bar return) consumes 1 bar, so a
    # 30-bar holdout yields only 29 usable obs < MIN_OBSERVATIONS=30 and would always
    # conservative-fail. Require 31 so a holdout clearing this gate has >= 30 scored observations.
    if len(train) < 60 or len(holdout) < 31:
        return 0  # silence-by-default: not enough bars for an honest train/holdout split

    proposal_set, zoo = _build_proposal_set(train)
    if not proposal_set.proposals:
        return 0  # silence-by-default: no factors evaluated

    # HELD-OUT: run a genuine walk-forward on the PROPOSED set over the holdout the proposer never
    # saw, and compute plateau_stable from cross-fold Sharpe jitter (robustness, NOT the IS peak).
    holdout_dsr, holdout_sharpe_delta, plateau_stable = _compute_holdout(
        proposal_set, holdout, zoo
    )
    return run_once(
        bars=train,
        holdout_dsr=holdout_dsr,
        holdout_sharpe_delta=holdout_sharpe_delta,
        plateau_stable=plateau_stable,
        proposal_set=proposal_set,
        verbose=verbose,
    )


def _compute_holdout(proposal_set, holdout_bars, zoo):
    """Genuine held-out OOS score of the PROPOSED weight set on a strictly-later window the
    proposer never saw. Builds the proposed-weight factor composite as a long/short position,
    realizes it against NEXT-bar returns (no lookahead), scores OOS DSR, and derives
    plateau_stable from cross-fold Sharpe jitter (robustness, NOT the in-sample peak — the
    AMZN-weight lesson). Returns ``(holdout_dsr, holdout_sharpe, plateau_stable)``; conservative
    (-inf, 0.0, False) on insufficient/degenerate data so the cron buffers rather than promotes.
    """
    from hermes_quant.factors.weight_proposer import score_holdout

    return score_holdout(proposal_set, holdout_bars, zoo.compute)


if __name__ == "__main__":
    sys.exit(main())
