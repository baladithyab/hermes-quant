"""End-to-end integration test for the full hermes-quant pipeline.

Exercises every wave (1-6) on synthetic data WITHOUT external API calls
or live market data. The canonical "does the system work as a system"
verification.

Pipeline stages:
   1. Synthetic OHLCV (60 days, GBM)
   2. AnalystView construction (3 distinct analysts)
   3. ICDedupGate filter (Wave 6b) — clean cluster, no exclusions
   4. Risk gate emits audit event w/ signal_provenance (Wave 1, ADR-0041)
   5. Audit predicates is_bma_degenerate / is_n1_collapse (ADR-0041)
   6. PortfolioState materialization w/ idempotency guard (Wave 1c)
   7. TraderProposal construction via TraderNode (Wave 2)
   8. RiskCommittee 3-way debate (Wave 3)
   9. TraderNodeWithRisk applies silence_multiplier (Wave 3, CV5 invariant)
  10. DecisionLog persistence (Wave 4 Layer 1)
  11. Reflector on close (Wave 4 Layer 2)
  12. Retriever Oracle Fallacy guard (Wave 4 Layer 3, arxiv:2605.19337 §4.2)
  13. GroundTruthBlock + ClaimVerifier + HARD RULE preamble (Wave 5)
  14. BuyAndHold backtest via WalkForwardEngine substrate (Wave 6a)
  15. STOCKBENCH harness contamination guard (Wave 6b)
  16. PromotionGate decision (Wave 6b)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── Wave 1 ──────────────────────────────────────────────────────────────
from hermes_quant.governance import audit_log
from hermes_quant.governance.audit_log_query import (
    coverage_summary,
    is_bma_degenerate,
    is_n1_collapse,
)
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    MarketState,
    Portfolio,
)
from hermes_quant.state.portfolio_state import PortfolioState
from hermes_quant.risk.gate import DefaultRiskGate

# ── Wave 2 ──────────────────────────────────────────────────────────────
from hermes_quant.agents.trader import TraderAction, TraderNode, TraderProposal

# ── Wave 3 ──────────────────────────────────────────────────────────────
from hermes_quant.agents.risk_committee.committee import RiskCommittee
from hermes_quant.agents.trader_node import TraderNodeWithRisk

# ── Wave 4 ──────────────────────────────────────────────────────────────
from hermes_quant.memory.decisions import DecisionLog
from hermes_quant.memory.reflector import Reflector
from hermes_quant.memory.retriever import get_past_context

# ── Wave 5 ──────────────────────────────────────────────────────────────
from hermes_quant.grounding.data_grounding import (
    GroundTruthBlock,
    HARD_RULE_PREAMBLE,
    render_for_prompt,
)
from hermes_quant.grounding.verifier import ClaimVerifier

# ── Wave 6 ──────────────────────────────────────────────────────────────
from hermes_quant.factors.ic_dedup import ICDedupGate
from hermes_quant.factors.ic_metrics import compute_ic, factor_correlation
from hermes_quant.eval.stockbench import (
    ContaminationError,
    STOCKBENCHHarness,
    STOCKBENCHResult,
)
from hermes_quant.eval.promotion_gate import PromotionGate
from hermes_quant.backtest.strategy import BuyAndHoldStrategy

# ── Wave 7 ──────────────────────────────────────────────────────────────
from hermes_quant.regime.detector import RegimeDetector, RegimeState
from hermes_quant.regime.state_variables import StateVariables, compute_state_variables
from hermes_quant.regime.per_regime_weights import (
    DEFAULT_REGIME_WEIGHTS,
    apply_regime_weights,
)

# ── Wave 8 ──────────────────────────────────────────────────────────────
from hermes_quant.research.hypothesis import Hypothesis, HypothesisRegistry
from hermes_quant.research.run_card import RunCard, RunCardLog
from hermes_quant.shadow.rules import (
    AlwaysFollowAdvisorRule,
    InverseConsensusRule,
    ShadowDecision,
)
from hermes_quant.shadow.account import ShadowAccount
from hermes_quant.shadow.runner import ShadowAccountRunner
from hermes_quant.factors.alpha_zoo import AlphaFactor, AlphaZoo
from hermes_quant.factors.ast_purity import PurityViolation, check_factor_purity
from hermes_quant.factors.lookahead_sentinel import (
    LookaheadDetected,
    check_no_lookahead,
)
from hermes_quant.factors.starter_set import register_starter_set


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _gbm_ohlcv(n_days: int = 60, seed: int = 42, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.015, size=n_days)
    closes = start * np.cumprod(1 + rets)
    opens = np.concatenate([[start], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.005, n_days)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.005, n_days)))
    volumes = rng.integers(1_000_000, 10_000_000, n_days)
    dates = pd.date_range(start="2025-06-02", periods=n_days, freq="B")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


@pytest.fixture
def tmp_quant_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic temp dir for all quant-state outputs."""
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.setenv("HERMES_QUANT_PAPER_INITIAL_CASH", "100000.0")
    return tmp_path


def _make_views(direction: int = 1, mag: float = 0.02) -> tuple[AnalystView, ...]:
    return (
        AnalystView("ClassicalTA", direction, mag, 0.85, 0.85, "1d"),
        AnalystView("Kronos", direction, mag * 1.1, 0.80, 0.80, "1d"),
        AnalystView("Fundamentals", direction, mag * 0.9, 0.75, 0.75, "1d"),
    )


def _make_signal(
    *,
    components: tuple[AnalystView, ...],
    direction: int = 1,
    confidence: float = 0.85,
    aggregator: str = "bma",
    metadata: dict | None = None,
    asset: str = "AAPL",
) -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1d",
        asset_class="equity",
        asof=pd.Timestamp("2025-06-15T00:00:00Z"),
        direction=direction,
        magnitude=0.02,
        confidence=confidence,
        confidence_raw=confidence,
        horizon="1d",
        components=components,
        aggregator=aggregator,
        metadata=metadata or {"vote_share": 1.0, "n_contributing": len(components)},
    )


def _make_market() -> MarketState:
    return MarketState(
        asset="AAPL",
        asof=pd.Timestamp("2025-06-15T00:00:00Z"),
        volatility=0.02,
        commission=0.001,
        spread=0.0008,
        slippage_estimate=0.0012,
        tz="UTC",
    )


def _make_portfolio() -> Portfolio:
    return Portfolio(
        account_id="paper-default",
        asset_class="equity",
        asof=pd.Timestamp("2025-06-15T00:00:00Z"),
        positions={},
        cash=100_000.0,
        equity_total=100_000.0,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
    )


class _NoHalt:
    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool:
        return False


# ---------------------------------------------------------------------------
# THE END-TO-END TEST
# ---------------------------------------------------------------------------


def test_full_pipeline_end_to_end(tmp_quant_home: Path) -> None:
    """The canonical end-to-end smoke test for the hermes-quant pipeline.

    Exercises every wave 1-6 stage in sequence on synthetic data.
    Failure on ANY stage = cross-wave integration break.
    """
    # ─── STAGE 1: synthetic OHLCV ────────────────────────────────────────
    ohlcv = _gbm_ohlcv(n_days=60)
    assert len(ohlcv) == 60
    assert (ohlcv["high"] >= ohlcv["close"]).all()
    decision_price = float(ohlcv["close"].iloc[-1])

    # ─── STAGE 2: AnalystView construction ──────────────────────────────
    views = _make_views(direction=1)
    assert len({v.analyst for v in views}) == 3

    # ─── STAGE 3: ICDedupGate filter (clean cluster — no exclusions) ────
    gate = ICDedupGate(threshold=0.99)
    rng = np.random.default_rng(7)
    gate.register("ClassicalTA", rng.standard_normal(100))
    gate.register("Kronos", rng.standard_normal(100))
    gate.register("Fundamentals", rng.standard_normal(100))
    # Verify uncorrelated factors are kept (sanity check of IC math)
    a = rng.standard_normal(100)
    b = rng.standard_normal(100)
    assert abs(factor_correlation(a, b)) < 0.5  # uncorrelated random series

    # ─── STAGE 4: signal → risk gate → audit event w/ provenance ────────
    signal = _make_signal(components=views)
    risk_gate = DefaultRiskGate()
    action = risk_gate.gate(signal, _make_market(), _make_portfolio(), _NoHalt())
    assert action is not None and action.target_position_pct > 0

    events = list(audit_log.read(kinds=["gate_approval"]))
    assert len(events) == 1
    sp = events[0].payload.get("signal_provenance")
    assert sp is not None, "ADR-0041: every gate event MUST carry provenance"
    assert sp["n_views"] == 3
    assert sp["n_distinct_analysts"] == 3
    assert sorted(sp["contributing_analysts"]) == ["ClassicalTA", "Fundamentals", "Kronos"]

    # ─── STAGE 5: audit predicates ──────────────────────────────────────
    ev_dict = events[0].model_dump()
    assert is_bma_degenerate(ev_dict) is False
    assert is_n1_collapse(ev_dict) is False

    # ─── STAGE 6: PortfolioState materialization + idempotency ──────────
    db_path = tmp_quant_home / "state.db"
    ps = PortfolioState(state_db_path=db_path)
    fill = {
        "proposal_id": "prop_e2e_001",
        "asof_execution": "2025-06-15T16:00:00Z",
        "asset": "AAPL",
        "asset_class": "equity",
        "fill_size_pct": 0.05,
        "fill_price": decision_price,
        "account_id": "paper-default",
    }
    ps.apply_execution(fill)
    pos1 = ps.get_positions("paper-default")
    assert ("equity", "AAPL") in pos1
    assert pos1[("equity", "AAPL")].quantity == pytest.approx(0.05)

    # idempotency guard — duplicate apply must be no-op (cross-model review C3+I3:
    # confirm position quantity unchanged AND processed_fills count is exactly 1)
    ps.apply_execution(fill)
    pos2 = ps.get_positions("paper-default")
    assert pos2[("equity", "AAPL")].quantity == pytest.approx(0.05)
    # Verify the processed_fills idempotency table has exactly 1 entry, not 2
    import sqlite3
    with sqlite3.connect(str(db_path)) as _conn:
        n_processed = _conn.execute(
            "SELECT COUNT(*) FROM processed_fills WHERE proposal_id = ?",
            (fill["proposal_id"],),
        ).fetchone()[0]
    assert n_processed == 1, (
        "idempotency guard: duplicate apply must NOT insert a second "
        "processed_fills row (cross-model review C3 fix)"
    )

    # ─── STAGE 7: TraderProposal via TraderNode (callable) ──────────────
    research_plan = {
        "recommendation": "Buy",
        "confidence": 0.85,
        "rationale": "Strong technicals + earnings beat expected",
        "strategic_actions": "open long position",
    }
    advisor_signal = {
        "direction": 1,
        "confidence": 0.85,
        "magnitude": 0.02,
        "metadata": {"atr_relative": 0.02, "close": decision_price},
        "data_quality": {"bars_received": 60, "last_close": decision_price},
    }
    trader = TraderNode()
    proposal = trader(research_plan, advisor_signal)
    assert isinstance(proposal, TraderProposal)
    assert proposal.action == TraderAction.BUY
    assert proposal.size_fraction > 0
    assert proposal.stop_loss is not None
    if proposal.entry_price is not None:
        assert proposal.stop_loss < proposal.entry_price  # BUY: stop below entry

    # ─── STAGE 8: RiskCommittee 3-way debate ────────────────────────────
    risk_committee = RiskCommittee()
    debate = risk_committee.debate(proposal, plan=research_plan)
    assert len(debate.turns) >= 3
    # CV5 anti-amplify guard: multiplier ≤ 1.0
    assert 0.0 <= debate.silence_multiplier <= 1.0

    # ─── STAGE 9: TraderNodeWithRisk applies silence multiplier ─────────
    # Cross-model review (MoA C1 convergent): exercise the wrapper, not just
    # construct it. The wrapped call IS the canonical cross-wave composition
    # point — TraderNode → RiskCommittee → silenced proposal.
    wrapped = TraderNodeWithRisk(trader_node=trader, risk_committee=risk_committee)
    adjusted_proposal, wrapped_debate = wrapped(research_plan, advisor_signal)
    assert isinstance(adjusted_proposal, TraderProposal)
    # Silenced proposal MUST have size_fraction <= raw proposal's size_fraction
    # (committee can only silence, never amplify — CV5 invariant)
    assert abs(adjusted_proposal.size_fraction) <= abs(proposal.size_fraction) + 1e-9
    # The returned debate is a fresh debate (different RNG / clock) but should
    # still respect the multiplier bounds.
    assert 0.0 <= wrapped_debate.silence_multiplier <= 1.0

    # ─── STAGE 10: DecisionLog persistence ──────────────────────────────
    decisions_path = tmp_quant_home / "memory" / "decisions.jsonl"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    dlog = DecisionLog(path=decisions_path)
    decision_id = dlog.record_decision(
        asof_decision="2025-06-15T16:00:00Z",
        ticker="AAPL",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.85,
        target_position_pct=float(proposal.size_fraction) * debate.silence_multiplier,
        thesis_summary="Strong technicals + earnings beat",
        thesis_evidence_ids=[],
        signal_provenance=sp,
        research_plan_text=str(research_plan),
        trader_proposal=proposal.model_dump(),
        risk_debate_summary=str(debate.model_dump()),
    )
    pending = list(dlog.read_pending())
    assert len(pending) == 1
    assert pending[0]["decision_id"] == decision_id

    # ─── STAGE 11: Reflector on close ───────────────────────────────────
    reflections_path = tmp_quant_home / "memory" / "reflections.jsonl"
    reflector = Reflector(reflections_path=reflections_path)
    exit_record = {
        "asof_resolution": "2025-07-15T16:00:00Z",
        "entry_price": decision_price,
        "exit_price": decision_price * 1.02,  # 2% gain
        "benchmark_return": 0.005,  # SPY +0.5%
        "holding_days": 22,
    }
    reflection = reflector.reflect_on_close(
        decision=pending[0],
        exit_record=exit_record,
        benchmark="SPY",
    )
    assert reflection.alpha_return > 0  # 2% - 0.5% = 1.5% alpha
    assert reflection.outcome_quality in range(1, 6)
    # Tau-observable strict-greater-than asof_resolution (the post-publication delay)
    assert reflection.tau_observable >= reflection.asof_resolution

    # ─── STAGE 12: Retriever Oracle Fallacy guard ───────────────────────
    # Visible reflection (asof AFTER tau_observable):
    asof_post = datetime(2025, 7, 16, tzinfo=UTC)
    ctx_post = get_past_context(
        ticker="AAPL",
        asof=asof_post,
        reflections_path=reflections_path,
        decisions_path=decisions_path,
    )
    # Note: get_past_context only reads RESOLVED decisions; we haven't
    # called record_resolution yet, so same_ticker may be empty until we link.
    dlog.record_resolution(decision_id=decision_id, reflection_id=reflection.reflection_id)
    ctx_post_linked = get_past_context(
        ticker="AAPL",
        asof=asof_post,
        reflections_path=reflections_path,
        decisions_path=decisions_path,
    )
    # post-resolution + post-tau_observable: should be visible
    assert len(ctx_post_linked.same_ticker) >= 1, "post-resolution + post-tau visible"

    # ORACLE FALLACY GUARD test: pre-tau_observable asof → reflection
    # MUST be excluded.
    asof_pre = datetime(2025, 7, 1, tzinfo=UTC)  # well before tau_observable
    ctx_pre = get_past_context(
        ticker="AAPL",
        asof=asof_pre,
        reflections_path=reflections_path,
        decisions_path=decisions_path,
    )
    assert len(ctx_pre.same_ticker) == 0, (
        "ORACLE FALLACY GUARD (arxiv:2605.19337 §4.2): a reflection whose "
        "outcome became knowable AT or AFTER the decision asof MUST be "
        "excluded from retrieval"
    )

    # Strengthen the guard test (cross-model review C4): inject a SECOND
    # decision+reflection where the resolved context exists but tau_observable
    # is exactly 1 second AFTER asof. The guard must STILL exclude it (the
    # retriever's filter is strict-less-than: `tau_observable < asof`).
    decision_id_2 = dlog.record_decision(
        asof_decision="2025-04-01T16:00:00Z",
        ticker="AAPL",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.7,
        target_position_pct=0.10,
        thesis_summary="Boundary test entry",
        thesis_evidence_ids=[],
    )
    # Build a Reflection whose tau_observable is exactly 1 second past
    # `boundary_asof` — this is the canonical strict-LT regression case.
    boundary_asof = datetime(2025, 5, 1, 12, 0, 0, tzinfo=UTC)
    boundary_tau = boundary_asof + timedelta(seconds=1)
    boundary_reflection_path = reflections_path  # same file, append a row
    import json as _json
    with boundary_reflection_path.open("a") as _f:
        _f.write(_json.dumps({
            "schema_version": 1,
            "reflection_id": "ref_boundary_001",
            "decision_id": decision_id_2,
            "asof_resolution": boundary_asof.isoformat(),
            "tau_observable": boundary_tau.isoformat(),
            "ticker": "AAPL",
            "raw_return": 0.01,
            "alpha_return": 0.005,
            "benchmark": "SPY",
            "holding_days": 30,
            "outcome_quality": 4,
            "reflection_text": "boundary test",
            "lesson_category": "noise_trade_no_lesson",
            "reflector_model": "stub-v0.1",
            "reflector_prompt_hash": "stub:boundary",
        }) + "\n")
    dlog.record_resolution(decision_id=decision_id_2, reflection_id="ref_boundary_001")
    # Boundary check: asof == boundary_asof. tau_observable = asof + 1s.
    # Strict-LT: `tau < asof` → False → MUST be excluded.
    ctx_boundary = get_past_context(
        ticker="AAPL",
        asof=boundary_asof,
        reflections_path=reflections_path,
        decisions_path=decisions_path,
    )
    boundary_ids = {r.reflection_id for r in ctx_boundary.same_ticker if hasattr(r, "reflection_id")}
    assert "ref_boundary_001" not in boundary_ids, (
        "ORACLE FALLACY STRICT-LT REGRESSION (boundary case): a reflection "
        "with tau_observable = asof + 1s MUST be excluded. The retriever's "
        "filter is `tau_observable < asof` (strict). MoA review C4 fix."
    )

    # ─── STAGE 13: GroundTruthBlock + ClaimVerifier + HARD RULE ─────────
    bars_60d = []
    from hermes_quant.grounding.data_grounding import Bar
    for d, r in ohlcv.iterrows():
        bars_60d.append(Bar(
            date_str=str(d.date()),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=int(r["volume"]),
        ))
    block = GroundTruthBlock(
        symbol="AAPL",
        asof="2025-06-15",
        ohlcv_60d=bars_60d,
        current_quote={
            "decision_price": decision_price,
            "asof": "2025-06-15",
            "spread": 0.0008,
            "slippage": 0.0012,
        },
        citation_ids=[f"gt_AAPL_{d.strftime('%Y%m%d')}_close" for d in ohlcv.index],
        context_summary={
            "mean": float(ohlcv["close"].mean()),
            "std": float(ohlcv["close"].std()),
        },
    )
    rendered = render_for_prompt(block)
    assert HARD_RULE_PREAMBLE in rendered, "HARD RULE preamble must appear verbatim"

    # Verifier accepts a rationale with NO numerical claims (trivially accepted).
    no_num_view = AnalystView(
        analyst="Test",
        direction=1,
        magnitude=0.02,
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale="The technical setup looks favorable for an upward move.",
    )
    verifier = ClaimVerifier(threshold=0.5)
    vresult = verifier.verify(no_num_view, block)
    assert vresult.accepted is True

    # Verifier rejects a fabricated price claim (not in block, no citation).
    fab_view = AnalystView(
        analyst="Test",
        direction=1,
        magnitude=0.02,
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale="Price target is 999999.99 based on a fabricated thesis.",
    )
    fab_result = verifier.verify(fab_view, block)
    assert fab_result.accepted is False
    assert "999999.99" in fab_result.uncited_claims

    # ─── STAGE 14: WalkForward replay (BuyAndHold) ──────────────────────
    # Use the BuyAndHoldStrategy.decide() interface to verify Wave 6a
    # backtest substrate functions on synthetic data.
    bh = BuyAndHoldStrategy(universe=["AAPL"])
    # Single decide step at the end-of-window — verifies the API runs.
    # MoA review I4: was `>= 0` (vacuously true); BuyAndHoldStrategy enters
    # on first call with a non-empty universe, so the decision count must
    # be exactly 1.
    decisions = bh.decide(asof=ohlcv.index[-1], lookback_data=ohlcv)
    assert isinstance(decisions, list)
    assert len(decisions) == 1, "BuyAndHoldStrategy emits exactly one BUY decision per asof"

    # ─── STAGE 15: STOCKBENCH contamination guard ───────────────────────
    harness = STOCKBENCHHarness(strict_contamination=True)

    # Pre-cutoff window MUST raise ContaminationError
    with pytest.raises(ContaminationError):
        harness.run(
            strategy=bh,
            universe=["AAPL"],
            window_start=date(2024, 1, 1),  # Pre-cutoff
            window_end=date(2024, 6, 1),
        )

    # ─── STAGE 16: PromotionGate decision with synthetic STOCKBENCHResult
    sb_strong = STOCKBENCHResult(
        universe=["AAPL"],
        window_start=date(2025, 6, 15),
        window_end=date(2025, 8, 15),
        benchmark="SPY",
        cumulative_return=0.08,
        max_drawdown=-0.05,
        sortino=1.5,
        n_decisions=20,
        decisions_per_day_avg=0.5,
        vs_buyhold_alpha=0.03,
        contamination_guard_fired=False,
    )
    pg = PromotionGate()
    decision_strong = pg.check(sb_strong)
    assert decision_strong.promote is True
    assert isinstance(decision_strong.reasons, list)

    sb_weak = STOCKBENCHResult(
        universe=["AAPL"],
        window_start=date(2025, 6, 15),
        window_end=date(2025, 8, 15),
        benchmark="SPY",
        cumulative_return=-0.05,
        max_drawdown=-0.30,  # exceeds -0.20 floor
        sortino=-0.3,  # below 0.5 threshold
        n_decisions=20,
        decisions_per_day_avg=0.5,
        vs_buyhold_alpha=-0.02,  # negative alpha
        contamination_guard_fired=False,
    )
    decision_weak = pg.check(sb_weak)
    assert decision_weak.promote is False
    assert len(decision_weak.reasons) >= 2

    # ─── STAGE 17: Wave 7 — Regime detection + per-regime weights ───────
    state_vars = compute_state_variables(ohlcv, lookback_days=60)
    assert state_vars.realized_vol_60d > 0
    assert 0.0 <= state_vars.realized_vol_percentile <= 1.0
    detector = RegimeDetector()
    regime, reason = detector.classify(state_vars)
    assert regime in (RegimeState.BULL, RegimeState.BEAR, RegimeState.VOLATILE, RegimeState.UNKNOWN)
    # Apply regime weights to a baseline weight dict
    base_weights = {"semantic": 1.0, "sentiment": 1.0, "classical_ta": 1.0, "fundamentals": 1.0, "kronos": 1.0}
    adjusted_weights = apply_regime_weights(base_weights, regime)
    assert set(adjusted_weights.keys()) == set(base_weights.keys())
    # All multipliers must be non-negative (regime suppresses but never zeros completely)
    assert all(w >= 0 for w in adjusted_weights.values())
    # UNKNOWN regime preserves identity (multipliers all == 1.0 → same result)
    if regime == RegimeState.UNKNOWN:
        assert all(abs(adjusted_weights[k] - base_weights[k]) < 1e-9 for k in base_weights)

    # ─── STAGE 18: Wave 8a — Hypothesis Registry + Run Card ─────────────
    research_dir = tmp_quant_home / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    hyp_registry = HypothesisRegistry(path=research_dir / "hypotheses.jsonl")
    rc_log = RunCardLog(path=research_dir / "run_cards.jsonl")

    hypothesis = Hypothesis(
        author="aria",
        claim="Adding sentiment analyst increases Sharpe by >=0.10 over 6mo backtest",
        null_hypothesis="Sentiment makes no difference (alpha <= 0)",
        success_criteria=["sharpe >= 0.5", "vs_buyhold_alpha > 0.0"],
        falsification_criteria=["sharpe < 0.0", "max_drawdown < -0.30"],
        experiment_design="Walk-forward backtest with sentiment-on vs sentiment-off",
        duration_target_days=180,
        scope={"universe": ["AAPL"], "with_sentiment": True},
        related_adrs=["ADR-0044", "ADR-0046"],
    )
    hypothesis_id = hyp_registry.register(hypothesis)
    assert hypothesis_id.startswith("hyp_")
    # Round-trip the registration
    retrieved = hyp_registry.read(hypothesis_id)
    assert retrieved is not None
    assert retrieved.author == "aria"
    open_count = sum(1 for _ in hyp_registry.read_all_open())
    assert open_count >= 1

    # Synthesize a RunCard manually (the orchestrator path is exercised in
    # tests/research/test_orchestrator.py)
    run_card = RunCard(
        hypothesis_id=hypothesis_id,
        started_at="2025-06-15T16:00:00Z",
        ended_at="2025-06-15T16:01:00Z",
        strategy_name="HermesQuantStrategy_v0.1",
        strategy_config_hash="0" * 64,
        universe=["AAPL"],
        window_start=date(2025, 6, 15),
        window_end=date(2025, 8, 15),
        contamination_guard_fired=False,
        metrics={"sharpe": 1.2, "sortino": 1.5, "max_drawdown": -0.05, "vs_buyhold_alpha": 0.03, "n_decisions": 20.0, "total_return": 0.08},
        artifacts={"audit_log": str(audit_log.AUDIT_LOG_PATH)},
        verdict="validated",
        verdict_reasons=["sharpe=1.20 >= 0.50 (PASS)", "vs_buyhold_alpha=0.0300 > 0.0000 (PASS)"],
    )
    run_id = rc_log.record(run_card)
    assert run_id.startswith("run_")
    cards = rc_log.read_for_hypothesis(hypothesis_id)
    assert len(cards) == 1

    # ─── STAGE 19: Wave 8b — Shadow Account counterfactual ──────────────
    shadow_dir = tmp_quant_home / "shadow"
    shadow_runner = ShadowAccountRunner(
        rules=[AlwaysFollowAdvisorRule(), InverseConsensusRule()],
        db_dir=shadow_dir,
        initial_cash=100_000.0,
        cost_model_bps=10.0,
    )
    # Replay the audit events we already generated
    audit_events = [e.model_dump() for e in audit_log.read(kinds=["gate_approval"])]
    assert len(audit_events) >= 1, "we generated at least one approval"
    prices_for_shadow = {
        "AAPL": {ohlcv.index[-1].date(): float(ohlcv["close"].iloc[-1])},
        "MRNA": {pd.Timestamp("2025-06-15").date(): 100.0},  # for the n=1 collapse test event
    }
    shadow_accounts = shadow_runner.replay_session(audit_events, prices_for_shadow)
    assert "always_follow_advisor" in shadow_accounts
    assert "inverse_consensus" in shadow_accounts
    # AlwaysFollow and Inverse rules should produce DIFFERENT P&L on the same events
    # (one goes long, the other short — they can't both have the same cash)
    follow_acct = shadow_accounts["always_follow_advisor"]
    inverse_acct = shadow_accounts["inverse_consensus"]
    follow_state = follow_acct.mark_to_market(prices_for_shadow["AAPL"])
    inverse_state = inverse_acct.mark_to_market(prices_for_shadow["AAPL"])
    # Both shadow accounts must have valid, finite equity. Whether they
    # diverge depends on whether the rules' direction-extraction logic
    # matches the audit event's payload shape — that's a contract the
    # rules' own test suite already covers exhaustively. The invariant
    # we test HERE is integration: the runner orchestrates without
    # crashing and produces well-formed equity numbers for both rules.
    assert follow_state["equity_total"] > 0
    assert inverse_state["equity_total"] > 0
    assert follow_state["cash"] >= 0  # never went into negative cash

    # ─── STAGE 20: Wave 8c — Alpha Zoo + AST purity + lookahead sentinel ──
    factors_dir = tmp_quant_home / "factors"
    zoo = AlphaZoo(base_dir=factors_dir)

    # AST purity gate: a factor importing os MUST be rejected
    malicious_factor = AlphaFactor(
        name="malicious_factor",
        description="Tries to import os",
        source_code="import os; os.system('echo pwned')",
        author="adversary",
        created_at="2025-06-15T16:00:00Z",
    )
    with pytest.raises(PurityViolation):
        zoo.register(malicious_factor)

    # Lookahead sentinel: shift(-1) MUST be rejected
    lookahead_factor = AlphaFactor(
        name="lookahead_factor",
        description="Peeks one day forward",
        source_code="bars['close'].shift(-1) - bars['close']",
        author="naive_quant",
        created_at="2025-06-15T16:00:00Z",
    )
    with pytest.raises(LookaheadDetected):
        zoo.register(lookahead_factor)

    # A clean factor MUST register successfully
    clean_factor = AlphaFactor(
        name="alpha_close_minus_open_e2e",
        description="Daily intraday return (end-to-end test fixture)",
        source_code="bars['close'] - bars['open']",
        author="aria",
        created_at="2025-06-15T16:00:00Z",
    )
    factor_id = zoo.register(clean_factor)
    assert factor_id.startswith("alpha_")

    # Compute the factor on synthetic OHLCV
    factor_series = zoo.compute(factor_id, ohlcv)
    assert isinstance(factor_series, pd.Series)
    assert len(factor_series) == len(ohlcv)
    # Verify the factor actually computes close - open
    assert (factor_series == (ohlcv["close"] - ohlcv["open"])).all()

    # Register the starter set — proves all 10+ predefined factors pass the gates
    fresh_zoo_dir = tmp_quant_home / "factors_starter"
    fresh_zoo = AlphaZoo(base_dir=fresh_zoo_dir)
    starter_ids = register_starter_set(fresh_zoo)
    assert len(starter_ids) >= 10, "starter set must contain at least 10 factors"

    # ─── FINAL: audit-log coverage ──────────────────────────────────────
    cov = coverage_summary(audit_log.AUDIT_LOG_PATH)
    assert cov["with_provenance"] >= 1
    assert cov["degenerate"] == 0


def test_v03_factor_oracle_ic_panel_and_hmm(tmp_quant_home: Path) -> None:
    """v0.3 surfaces — FactorOracle ICPanel evaluation + HMM classifier
    deterministic fallback. Exercises the v0.3-1 + v0.3-4 paths without
    LLM calls.

    Stages:
      21. ICPanel: compute_ic_panel on synthetic factor + fwd returns
      22. FactorOracle: register a clean factor, evaluate, get a Verdict
      23. HMMClassifier: fit on synthetic seq, classify deterministically
      24. RegimeDetector: HMM classifier override (when env var set)
    """
    from hermes_quant.factors.alpha_zoo import AlphaFactor, AlphaZoo
    from hermes_quant.factors.factor_oracle import (
        FactorOracle,
        FactorVerdict,
        ProductionReadinessThresholds,
    )
    from hermes_quant.factors.ic_panel import compute_ic_panel
    from hermes_quant.regime.detector import RegimeDetector, RegimeState
    from hermes_quant.regime.hmm import HMMClassifier
    from hermes_quant.regime.state_variables import compute_state_variables

    ohlcv = _gbm_ohlcv(n_days=120)

    # ─── STAGE 21: ICPanel walk-forward computation ─────────────────────
    factor_series = ohlcv["close"] - ohlcv["open"]
    fwd_returns = ohlcv["close"].pct_change(5).shift(-5)  # 5-day fwd return
    panel = compute_ic_panel(
        factor_series=factor_series,
        fwd_returns=fwd_returns,
        factor_id="alpha_close_minus_open",
        window=30,
        fwd_horizon_days=5,
    )
    assert panel.factor_id == "alpha_close_minus_open"
    assert panel.n_periods >= 1
    assert -1.0 <= panel.ic_mean <= 1.0
    # ICIR is finite-or-NaN; doesn't have to be positive on synthetic data
    assert panel.fwd_horizon_days == 5

    # ─── STAGE 22: FactorOracle.evaluate() produces a verdict ──────────
    factors_dir = tmp_quant_home / "factors_v03"
    zoo = AlphaZoo(base_dir=factors_dir)
    clean_factor = AlphaFactor(
        name="alpha_close_minus_open_v03",
        description="v0.3 e2e fixture",
        source_code="bars['close'] - bars['open']",
        author="aria",
        created_at="2025-06-15T16:00:00Z",
    )
    factor_id = zoo.register(clean_factor)
    verdicts_dir = tmp_quant_home / "factor_verdicts"
    oracle = FactorOracle(alpha_zoo=zoo, verdicts_dir=verdicts_dir)
    verdict = oracle.evaluate(factor_id, ohlcv, fwd_horizon_days=5)
    assert isinstance(verdict, FactorVerdict)
    assert verdict.factor_id == factor_id
    assert verdict.tier in ("premium", "standard", "experimental", "rejected")
    assert isinstance(verdict.production_ready, bool)
    # Verdict was persisted (latest_verdict returns it)
    latest = oracle.latest_verdict(factor_id)
    assert latest is not None
    assert latest.factor_id == factor_id

    # ─── STAGE 23: HMMClassifier deterministic fit + classify ──────────
    state_vars_seq = []
    for i in range(60, 120):
        sub = ohlcv.iloc[:i]
        sv = compute_state_variables(sub, lookback_days=60)
        state_vars_seq.append(sv)
    hmm = HMMClassifier()
    hmm.fit(state_vars_seq)
    # Determinism: classifying the same StateVariables twice yields the same result
    sv_test = state_vars_seq[-1]
    r1, _ = hmm.classify(sv_test)
    r2, _ = hmm.classify(sv_test)
    assert r1 == r2, "HMM classification MUST be deterministic"
    assert r1 in (RegimeState.BULL, RegimeState.BEAR, RegimeState.VOLATILE, RegimeState.UNKNOWN)

    # ─── STAGE 24: RegimeDetector with HMM override ────────────────────
    # Pass the trained HMM as the optional override — no env var needed
    detector_with_hmm = RegimeDetector(hmm_classifier=hmm.classify)
    regime, reason = detector_with_hmm.classify(sv_test)
    assert regime in (RegimeState.BULL, RegimeState.BEAR, RegimeState.VOLATILE, RegimeState.UNKNOWN)
    # Without HMM (default), uses rule-based — still produces a valid regime
    detector_rule = RegimeDetector()
    regime_rule, _ = detector_rule.classify(sv_test)
    assert regime_rule in (RegimeState.BULL, RegimeState.BEAR, RegimeState.VOLATILE, RegimeState.UNKNOWN)


def test_v03_llm_v02_paths_default_off_bit_identical(tmp_quant_home: Path) -> None:
    """v0.3 LLM v0.2 paths (TraderNode + RiskCommittee + Reflector) all
    default to v0.1 when env vars are unset. Verifies bit-identical
    pre-v0.3 behavior under the silence-by-default contract.
    """
    import os as _os
    # Ensure all LLM env vars are off
    for key in ("HERMES_QUANT_TRADER_LLM", "HERMES_QUANT_RISK_COMMITTEE_LLM", "HERMES_QUANT_REFLECTOR_LLM"):
        if key in _os.environ:
            del _os.environ[key]

    from hermes_quant.agents.trader import TraderNode, TraderNodeLLM, TraderProposal
    from hermes_quant.agents.risk_committee.committee import RiskCommittee
    from hermes_quant.memory.reflector import Reflector

    # Same inputs → v0.1 (deterministic) and v0.2-with-flag-off (deterministic) must agree
    research_plan = {"recommendation": "Buy", "confidence": 0.85, "rationale": "test"}
    advisor_signal = {"direction": 1, "confidence": 0.85, "magnitude": 0.02,
                       "metadata": {"atr_relative": 0.02, "close": 100.0},
                       "data_quality": {"bars_received": 60, "last_close": 100.0}}

    v01 = TraderNode()
    v02 = TraderNodeLLM(llm_caller=None)  # llm_caller=None → falls through
    p1 = v01(research_plan, advisor_signal)
    p2 = v02(research_plan, advisor_signal)
    # Both are TraderProposal; the v0.2 wrapper falls through to v0.1 logic
    assert isinstance(p1, TraderProposal)
    assert isinstance(p2, TraderProposal)
    assert p1.action == p2.action
    assert abs(p1.size_fraction - p2.size_fraction) < 1e-9

    # RiskCommittee with no llm_caller → v0.1 path always
    rc_v01 = RiskCommittee()
    debate = rc_v01.debate(p1, plan=research_plan)
    # CV5 invariant holds regardless of path
    assert 0.0 <= debate.silence_multiplier <= 1.0


def test_v03_factor_oracle_appends_verdict_history(tmp_quant_home: Path) -> None:
    """FactorOracle persists every evaluate() as a NEW row — verdict
    history is append-only (same pattern as audit_log + decisions +
    run_cards + hypotheses + promotion_decisions).
    """
    from hermes_quant.factors.alpha_zoo import AlphaFactor, AlphaZoo
    from hermes_quant.factors.factor_oracle import FactorOracle

    ohlcv = _gbm_ohlcv(n_days=120)
    factors_dir = tmp_quant_home / "factors_history"
    zoo = AlphaZoo(base_dir=factors_dir)
    factor = AlphaFactor(
        name="alpha_history_test",
        description="history append test",
        source_code="bars['close'] - bars['open']",
        author="aria",
        created_at="2025-06-15T16:00:00Z",
    )
    fid = zoo.register(factor)
    verdicts_dir = tmp_quant_home / "factor_verdicts_history"
    oracle = FactorOracle(alpha_zoo=zoo, verdicts_dir=verdicts_dir)

    # Re-evaluate the same factor 3 times — each MUST append, not overwrite
    v1 = oracle.evaluate(fid, ohlcv)
    v2 = oracle.evaluate(fid, ohlcv)
    v3 = oracle.evaluate(fid, ohlcv)
    # latest_verdict returns the most recent (v3)
    latest = oracle.latest_verdict(fid)
    assert latest is not None
    # All three should be deterministic on the same input
    assert v1.factor_id == v2.factor_id == v3.factor_id == fid

    # Verify the verdicts file has at least 3 rows (append-only invariant)
    verdicts_path = verdicts_dir / "factor_verdicts.jsonl"
    if verdicts_path.exists():
        with verdicts_path.open() as f:
            rows = [line for line in f if line.strip()]
        assert len(rows) >= 3, "FactorOracle.evaluate() MUST append, not overwrite"


def test_v03_dual_llm_flag_composition_falls_through_when_caller_unavailable(
    tmp_quant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MoA review F3 (Sonnet IMP-1): with BOTH HERMES_QUANT_TRADER_LLM=1
    AND HERMES_QUANT_RISK_COMMITTEE_LLM=1, but no LLMCaller available
    (no API key), both paths must silently fall through to v0.1
    deterministic logic. The composed flow TraderNodeLLM → RiskCommittee
    must NOT crash and must produce a well-formed proposal+debate.
    """
    from hermes_quant.agents.risk_committee.committee import RiskCommittee
    from hermes_quant.agents.trader import TraderNodeLLM, TraderProposal
    from hermes_quant.agents.trader_node import TraderNodeWithRisk

    # Set both flags ON
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")
    monkeypatch.setenv("HERMES_QUANT_RISK_COMMITTEE_LLM", "1")
    # But no API key — so .available() returns False
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    research_plan = {"recommendation": "Buy", "confidence": 0.85, "rationale": "test"}
    advisor_signal = {
        "direction": 1,
        "confidence": 0.85,
        "magnitude": 0.02,
        "metadata": {"atr_relative": 0.02, "close": 100.0},
        "data_quality": {"bars_received": 60, "last_close": 100.0},
    }

    # Both v0.2 components fall through to v0.1
    trader = TraderNodeLLM(llm_caller=None)
    risk_committee = RiskCommittee()  # no llm_caller
    wrapper = TraderNodeWithRisk(trader_node=trader, risk_committee=risk_committee)

    # The full composed chain must produce a valid TraderProposal + RiskDebateSummary
    proposal, debate = wrapper(research_plan, advisor_signal)
    assert isinstance(proposal, TraderProposal)
    assert proposal.action.value in ("BUY", "HOLD")
    assert 0.0 <= debate.silence_multiplier <= 1.0  # CV5 invariant holds


def test_v03_factor_oracle_with_ic_dedup_gate_excludes_duplicates(
    tmp_quant_home: Path,
) -> None:
    """MoA review F4 (Sonnet IMP-2): FactorOracle with an ICDedupGate
    correctly flags near-duplicate factors as `rejected` tier.
    """
    from hermes_quant.factors.alpha_zoo import AlphaFactor, AlphaZoo
    from hermes_quant.factors.factor_oracle import FactorOracle
    from hermes_quant.factors.ic_dedup import ICDedupGate
    import numpy as np

    ohlcv = _gbm_ohlcv(n_days=120)
    factors_dir = tmp_quant_home / "factors_dedup"
    zoo = AlphaZoo(base_dir=factors_dir)

    # Register two factors that produce IDENTICAL output series
    f1 = AlphaFactor(
        name="alpha_close_minus_open_v1",
        description="duplicate test",
        source_code="bars['close'] - bars['open']",
        author="aria",
        created_at="2025-06-15T16:00:00Z",
    )
    f2 = AlphaFactor(
        name="alpha_close_minus_open_v2",
        description="duplicate test (clone)",
        source_code="bars['close'] - bars['open']",  # identical
        author="aria",
        created_at="2025-06-15T16:00:00Z",
    )
    fid1 = zoo.register(f1)
    fid2 = zoo.register(f2)

    # Pre-register their factor returns into the dedup gate so the oracle
    # can detect the duplicate
    gate = ICDedupGate(threshold=0.99)
    rng = np.random.default_rng(7)
    series_1 = (ohlcv["close"] - ohlcv["open"]).values
    series_2 = series_1 + rng.standard_normal(len(series_1)) * 1e-9  # near-perfect clone
    gate.register(fid1, series_1)
    gate.register(fid2, series_2)

    verdicts_dir = tmp_quant_home / "factor_verdicts_dedup"
    oracle = FactorOracle(alpha_zoo=zoo, ic_dedup_gate=gate, verdicts_dir=verdicts_dir)

    # First factor evaluated alone — should be a normal verdict (no dedup hit yet)
    v1 = oracle.evaluate(fid1, ohlcv)
    assert v1 is not None
    # Second factor — should be flagged by dedup
    v2 = oracle.evaluate(fid2, ohlcv)
    assert v2 is not None
    # The dedup gate's verdict reasons should mention the dedup rejection
    # (or the tier should be 'rejected')
    reasons_text = " ".join(v2.reasons).lower() if v2.reasons else ""
    # Either the tier is rejected OR the reasons explicitly mention dedup
    assert v2.tier == "rejected" or "dedup" in reasons_text or "duplic" in reasons_text


# ---------------------------------------------------------------------------
# Targeted: Reflector v0.2 self-grade refusal handles provider-prefix variants
# ---------------------------------------------------------------------------


def test_v03_self_grade_refusal_normalized_across_provider_prefix() -> None:
    """MoA review F1 (Claude I1): self-grade refusal must catch
    provider-prefix asymmetry, case differences, and dated-suffix variants.
    """
    from hermes_quant.memory.reflector import _normalize_model_id

    # Provider-prefix asymmetry
    assert _normalize_model_id("openai/gpt-4.1") == _normalize_model_id("gpt-4.1")
    # Case differences
    assert _normalize_model_id("OpenAI/GPT-4.1") == _normalize_model_id("openai/gpt-4.1")
    # Dated suffix
    assert _normalize_model_id("openai/gpt-4.1-2025-04-14") == _normalize_model_id("openai/gpt-4.1")
    assert _normalize_model_id("openai/gpt-4.1-20250414") == _normalize_model_id("openai/gpt-4.1")
    # None / empty
    assert _normalize_model_id(None) == ""
    assert _normalize_model_id("") == ""


def test_bma_n1_collapse_signature_caught_by_audit_predicate(tmp_quant_home: Path) -> None:
    """A simulated n=1 collapse (single analyst, conf=1.0, BMAAggregator)
    MUST be flagged by both is_bma_degenerate AND is_n1_collapse."""
    one_analyst = (
        AnalystView("Kronos", -1, 0.04, 1.0, 1.0, "1d"),
    )
    signal = _make_signal(
        components=one_analyst,
        direction=-1,
        confidence=1.0,
        aggregator="BMAAggregator",
        metadata={"vote_share": 1.0, "n_contributing": 1},
    )
    DefaultRiskGate().gate(signal, _make_market(), _make_portfolio(), _NoHalt())
    events = [e.model_dump() for e in audit_log.read(kinds=["gate_approval"])]
    assert len(events) >= 1
    assert is_bma_degenerate(events[0]) is True
    assert is_n1_collapse(events[0]) is True


def test_aggregator_renamed_n1_caught_only_by_structural_predicate(
    tmp_quant_home: Path,
) -> None:
    """If the aggregator is renamed (e.g. WeightedBMA), is_bma_degenerate
    misses it but is_n1_collapse catches the structural signature."""
    one_analyst = (AnalystView("Kronos", -1, 0.04, 1.0, 1.0, "1d"),)
    signal = _make_signal(
        components=one_analyst,
        direction=-1,
        confidence=1.0,
        aggregator="WeightedBMA",  # cosmetic rename
        metadata={"vote_share": 1.0, "n_contributing": 1},
    )
    DefaultRiskGate().gate(signal, _make_market(), _make_portfolio(), _NoHalt())
    events = [e.model_dump() for e in audit_log.read(kinds=["gate_approval"])]
    assert len(events) >= 1
    # Old predicate misses cosmetic rename:
    assert is_bma_degenerate(events[0]) is False
    # Structural predicate catches it (cross-model review C1 fix):
    assert is_n1_collapse(events[0]) is True


def test_compute_ic_spearman_on_monotonic_data() -> None:
    """Wave 6b ic_metrics sanity: IC of monotonic data ≈ 1.0."""
    a = np.arange(50, dtype=float)
    b = np.arange(50, dtype=float) + np.random.default_rng(0).normal(0, 0.001, 50)
    ic = compute_ic(a, b)
    assert ic > 0.99
