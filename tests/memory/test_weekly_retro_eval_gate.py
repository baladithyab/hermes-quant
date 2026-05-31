"""W2 eval gate — the SkillOpt held-out acceptance criteria (ADR-0081 §rail-compliance).

This is the FLAG-FLIP gate: HERMES_QUANT_WEEKLY_RETRO may NOT be enabled in production
until these pass. The gate is necessary-not-sufficient (operator promotion stays).

The five enforcement primitives (plan §7), as pytest-verifiable criteria:
  - held-out OOS digest must NOT regress hit-rate/alpha vs the no-digest baseline;
  - belief count under the per-role budget cap;
  - every active belief Oracle-tagged + decaying;
  - half-life is plateau-stable under +/-20% jitter (NOT a decimal peak);
  - propose-only: the module touches NONE of risk gate / kill-switch / sizing ladder.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hermes_quant.memory import weekly_retro
from hermes_quant.memory.retriever import format_beliefs_digest, load_active_beliefs
from hermes_quant.memory.weekly_retro import (
    BELIEF_BUDGET_PER_ROLE,
    HALF_LIFE_DAYS,
    MIN_SUPPORT_N,
    materialize_active,
    run_weekly_retro,
)

# In-sample window the distiller reads; OOS window it never sees.
IN_SAMPLE_ASOF = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
OOS_ASOF = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _reflection(*, decision_id: str, ticker: str, alpha_return: float,
                lesson_category: str, asof_resolution: datetime,
                tau_observable: datetime) -> dict:
    return {
        "schema_version": 1,
        "reflection_id": f"ref_{decision_id}",
        "decision_id": decision_id,
        "asof_resolution": asof_resolution.isoformat(),
        "tau_observable": tau_observable.isoformat(),
        "ticker": ticker.upper(),
        "raw_return": alpha_return,
        "alpha_return": alpha_return,
        "benchmark": "SPY",
        "holding_days": 5,
        "outcome_quality": 3,
        "reflection_text": f"reflection {ticker} {lesson_category}",
        "lesson_category": lesson_category,
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:abc",
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


def _in_sample_corpus(asof: datetime) -> list[dict]:
    """A corpus where (AAPL, regime_shift_invalidation) is a consistent winner and
    (TSLA, thesis_invalidation_at_earnings) is a consistent loser — a learnable edge."""
    rows: list[dict] = []
    res = asof - timedelta(days=2)
    tau = asof - timedelta(days=1)
    for i in range(MIN_SUPPORT_N + 2):
        rows.append(_reflection(decision_id=f"win_{i}", ticker="AAPL", alpha_return=0.04,
                                lesson_category="regime_shift_invalidation",
                                asof_resolution=res, tau_observable=tau))
    for i in range(MIN_SUPPORT_N + 2):
        rows.append(_reflection(decision_id=f"lose_{i}", ticker="TSLA", alpha_return=-0.03,
                                lesson_category="thesis_invalidation_at_earnings",
                                asof_resolution=res, tau_observable=tau))
    return rows


def _oos_outcomes() -> list[dict]:
    """Held-out OOS realized outcomes (the distiller never reads these). The learnable
    edge persists OOS: AAPL/regime keeps winning, TSLA/earnings keeps losing."""
    return [
        {"ticker": "AAPL", "lesson_category": "regime_shift_invalidation", "alpha_return": 0.035},
        {"ticker": "AAPL", "lesson_category": "regime_shift_invalidation", "alpha_return": 0.05},
        {"ticker": "TSLA", "lesson_category": "thesis_invalidation_at_earnings", "alpha_return": -0.04},
        {"ticker": "TSLA", "lesson_category": "thesis_invalidation_at_earnings", "alpha_return": -0.02},
    ]


def _score_with_digest(beliefs: list[weekly_retro.Belief], oos: list[dict]) -> tuple[float, float]:
    """Deterministic scorer: a digest-aware PM uses each belief's alpha-evidence sign as
    its directional prior for the matching (ticker, category) OOS setup; absent a belief
    it abstains (contributes the baseline's neutral outcome). Returns (hit_rate, mean_alpha).

    Reward = realized alpha from the OOS rows (external truth) — never an LLM self-score.
    """
    by_key = {}
    for b in beliefs:
        # recover ticker from the stable belief_id token (bel_<tier>_<role>_<TICKER>_<hash>)
        tk = b.belief_id.split("_")[-2]
        by_key[(tk, b.lesson_category)] = b

    hits = 0
    realized: list[float] = []
    for row in oos:
        key = (row["ticker"].upper(), row["lesson_category"])
        b = by_key.get(key)
        actual = float(row["alpha_return"])
        if b is None:
            # no belief: abstain -> neutral baseline outcome (0 realized, not a hit/miss)
            realized.append(0.0)
            continue
        prior = 1 if b.alpha_evidence > 0 else -1
        # If we act in the belief's direction we capture +actual (long) or -actual (short).
        captured = actual if prior > 0 else -actual
        realized.append(captured)
        if captured > 0:
            hits += 1
    n_acted = sum(1 for row in oos if (row["ticker"].upper(), row["lesson_category"]) in by_key)
    hit_rate = hits / n_acted if n_acted else 0.0
    mean_alpha = sum(realized) / len(realized) if realized else 0.0
    return hit_rate, mean_alpha


def _score_baseline(oos: list[dict]) -> tuple[float, float]:
    """No-digest baseline: the PM has no distilled prior -> abstains everywhere -> neutral."""
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# THE gate
# ---------------------------------------------------------------------------


def test_digest_does_not_regress_on_held_out_oos(tmp_path) -> None:
    """Distill on in-sample; score digest vs no-digest on a held-out OOS window.

    Assert the digest variant's hit-rate AND mean alpha are >= the no-digest baseline
    (NOT a regression). Necessary, not sufficient — checkpoint-fallback: if it regresses,
    the flag stays OFF.
    """
    refl_path = tmp_path / "reflections.jsonl"
    bpath = tmp_path / "beliefs.jsonl"
    _write(refl_path, _in_sample_corpus(IN_SAMPLE_ASOF))

    run_weekly_retro(IN_SAMPLE_ASOF, reflections_path=refl_path, beliefs_path=bpath,
                     emit_promotion=False)
    beliefs = materialize_active(weekly_retro.load_belief_rows(path=bpath),
                                 IN_SAMPLE_ASOF + timedelta(hours=1))

    oos = _oos_outcomes()
    hit_digest, alpha_digest = _score_with_digest(beliefs, oos)
    hit_base, alpha_base = _score_baseline(oos)

    assert hit_digest >= hit_base, (
        f"held-out hit-rate regressed: digest={hit_digest} < baseline={hit_base}"
    )
    assert alpha_digest >= alpha_base, (
        f"held-out mean alpha regressed: digest={alpha_digest} < baseline={alpha_base}"
    )
    # And the edge is real (not a vacuous tie): the digest strictly helps on this corpus.
    assert alpha_digest > alpha_base


def test_belief_count_under_budget_cap(tmp_path) -> None:
    """After a representative multi-week distillation, active total <= cap * n_roles."""
    refl_path = tmp_path / "reflections.jsonl"
    bpath = tmp_path / "beliefs.jsonl"

    for week in range(4):
        asof = IN_SAMPLE_ASOF + timedelta(days=7 * week)
        rows: list[dict] = []
        res = asof - timedelta(days=2)
        tau = asof - timedelta(days=1)
        for g in range(5):
            for i in range(MIN_SUPPORT_N + 1):
                rows.append(_reflection(
                    decision_id=f"w{week}_g{g}_{i}", ticker=f"TK{g}",
                    alpha_return=0.01 * (g + 1),
                    lesson_category="regime_shift_invalidation",
                    asof_resolution=res, tau_observable=tau,
                ))
        _write(refl_path, rows)
        run_weekly_retro(asof, reflections_path=refl_path, beliefs_path=bpath,
                         emit_promotion=False)

    final = materialize_active(weekly_retro.load_belief_rows(path=bpath),
                               IN_SAMPLE_ASOF + timedelta(days=28))
    n_roles = max(1, len(weekly_retro.INJECTION_ROLES))
    assert len(final) <= BELIEF_BUDGET_PER_ROLE * n_roles


def test_every_active_belief_oracle_tagged_and_decaying(tmp_path) -> None:
    refl_path = tmp_path / "reflections.jsonl"
    bpath = tmp_path / "beliefs.jsonl"
    _write(refl_path, _in_sample_corpus(IN_SAMPLE_ASOF))
    run_weekly_retro(IN_SAMPLE_ASOF, reflections_path=refl_path, beliefs_path=bpath,
                     emit_promotion=False)
    active = materialize_active(weekly_retro.load_belief_rows(path=bpath),
                                IN_SAMPLE_ASOF + timedelta(hours=1))
    assert active
    for b in active:
        tau_max = weekly_retro._parse_dt(b.oracle_provenance.get("tau_observable_max"))
        assert tau_max is not None, "every belief carries a parseable tau_observable_max"
        assert b.oracle_provenance.get("source") == "agent_reflection"
        assert b.half_life_days > 0
        assert 0 < b.recency <= 1.0


def test_weekly_retro_halflife_is_plateau_not_peak(tmp_path, monkeypatch) -> None:
    """Jitter half_life_days by +/-20%; the held-out edge must be STABLE across the band
    (no single decimal point dominates). Encodes the MT3 / AMZN-weight 'use a RANGE' rule."""
    refl_path = tmp_path / "reflections.jsonl"
    _write(refl_path, _in_sample_corpus(IN_SAMPLE_ASOF))
    oos = _oos_outcomes()

    base = HALF_LIFE_DAYS["weekly"]
    band = [base * 0.8, base, base * 1.2]
    edges: list[float] = []
    for hl in band:
        bpath = tmp_path / f"beliefs_{hl:.2f}.jsonl"
        monkeypatch.setitem(weekly_retro.HALF_LIFE_DAYS, "weekly", hl)
        run_weekly_retro(IN_SAMPLE_ASOF, reflections_path=refl_path, beliefs_path=bpath,
                         emit_promotion=False)
        beliefs = materialize_active(weekly_retro.load_belief_rows(path=bpath),
                                     IN_SAMPLE_ASOF + timedelta(hours=1))
        _hit, alpha = _score_with_digest(beliefs, oos)
        edges.append(alpha)

    # Plateau: the OOS edge does not collapse anywhere in the band, and the spread is
    # negligible (the half-life is a range, not a tuned peak).
    assert all(e > 0 for e in edges), f"edge collapsed somewhere in the jitter band: {edges}"
    assert max(edges) - min(edges) < 1e-6, f"edge is peak-sensitive across jitter: {edges}"


# ---------------------------------------------------------------------------
# Propose-only / advisory-plane-only — the safety-frame regression guard
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "risk.gate",
    "risk_gate",
    "governance.kill_switch",
    "kill_switch",
    "react.live",
    "sizing",
)


def _collect_imports(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            for alias in node.names:
                names.add(f"{mod}.{alias.name}")
    return names


def test_propose_only_never_touches_gate_or_ladder() -> None:
    """The module imports/calls NOTHING from the risk-gate / kill-switch / sizing-ladder
    code paths; the only writes are beliefs.jsonl and a promotion_event row.

    This is the safety-frame regression guard (advisory-plane-only). It asserts BOTH the
    static import surface and that the source text never references the discrete sizing
    ladder constants or a risk-gate symbol.
    """
    src_path = Path(weekly_retro.__file__)
    imports = _collect_imports(src_path)
    for imp in imports:
        low = imp.lower()
        for forbidden in _FORBIDDEN_IMPORT_SUBSTRINGS:
            assert forbidden not in low, (
                f"weekly_retro imports a forbidden module: {imp!r} matched {forbidden!r}"
            )

    # The only audit kind it may emit is promotion_event (already a VALID_KIND).
    text = src_path.read_text()
    assert "kill_switch_fired" not in text
    assert "gate_approval" not in text
    assert "gate_rejection" not in text
    # Never writes a position size: no reference to the sizing-write surface symbols
    # (the discrete ladder is applied at the risk gate / reactor, never here).
    for sizing_symbol in (
        "target_position_pct", "position_pct", "max_position", "fill_size_pct",
        "RiskGate", "risk_gate", "KillSwitch", "kill_switch",
    ):
        assert sizing_symbol not in text, (
            f"weekly_retro references a sizing / risk-control symbol: {sizing_symbol!r}"
        )

    # The module's only persistent writes are beliefs.jsonl + the promotion_event row.
    assert "BELIEFS_PATH" in text
    assert 'kind="promotion_event"' in text


def test_default_off_is_byte_identical_noop(tmp_path, monkeypatch) -> None:
    """Flag-OFF: load_active_beliefs returns [] when beliefs.jsonl is absent, and the
    digest is empty -> the lessons_block is byte-identical to the W1/today value."""
    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", tmp_path / "absent_beliefs.jsonl")

    beliefs = load_active_beliefs("portfolio_manager", IN_SAMPLE_ASOF)
    assert beliefs == [], "absent belief store -> empty (default-OFF byte-identical path)"
    assert format_beliefs_digest(beliefs) == "", "empty digest so the caller skips prepend"
