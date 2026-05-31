"""W4 factor-weight proposer — eval-gate acceptance criteria (ADR-0080 §D80.3/§D80.5).

AC-1..AC-10 from docs/plans/selfevolve-W4-factor-weight-proposer.md §4. These ARE the
held-out eval gate, the silence-only rail, the checkpoint-fallback, and the advisory-plane-only
guarantees as pytest acceptance criteria. Plus a flag-OFF-noop-companion check and the
advisory-plane-only import test (the module imports NONE of risk.gate / governance.kill_switch /
the discrete sizing ladder).
"""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from hermes_quant.factors.weight_proposer import (
    MAX_STEP_PER_CYCLE,
    MIN_OBSERVATIONS,
    WEIGHT_CAP,
    WEIGHT_FLOOR,
    FactorWeightProposalSet,
    append_rejected,
    evaluate_against_holdout,
    load_prior_best_dsr,
    propose_weights,
    write_candidates,
)


# --- lightweight verdict shim (mirrors FactorVerdict's tier + ic_panel.n_periods) ----------
def _verdict(tier: str, *, n_periods: int = 60):
    """A dict-shaped verdict the proposer reads via .get-style access (and attr fallback)."""
    return {"tier": tier, "ic_panel": {"n_periods": n_periods}}


def _code_only(src: str) -> str:
    """Return module source with docstrings + comments stripped (executable tokens only).

    The module docstring legitimately *names* the surfaces it must NOT touch (risk.gate,
    kill_switch, the sizing ladder) to explain the safety frame; the meaningful test is that
    none of those appear in EXECUTABLE code (imports/calls/literals), which this isolates.
    """
    tree = ast.parse(src)
    # Drop every string-literal expression statement (module/class/func docstrings).
    class _StripDocstrings(ast.NodeTransformer):
        def _strip(self, node):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
            return node

        def visit_Module(self, node):
            self.generic_visit(node)
            return self._strip(node)

        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            return self._strip(node)

        def visit_AsyncFunctionDef(self, node):
            self.generic_visit(node)
            return self._strip(node)

        def visit_ClassDef(self, node):
            self.generic_visit(node)
            return self._strip(node)

    stripped = _StripDocstrings().visit(tree)
    ast.fix_missing_locations(stripped)
    return ast.unparse(stripped)


# ---------------------------------------------------------------------------
# AC-1 — silence-only: never amplifies above the cap
# ---------------------------------------------------------------------------
def test_silence_only_never_amplifies_above_cap():
    rng = random.Random(1234)
    tiers = ["premium", "standard", "experimental", "rejected"]
    for _ in range(300):
        verdicts = {
            f"f{i}": _verdict(rng.choice(tiers), n_periods=rng.randint(0, 200))
            for i in range(rng.randint(1, 8))
        }
        # current weights span [0, CAP] incl. factors already pinned at the cap.
        current = {fid: rng.choice([0.0, 0.5, WEIGHT_CAP, 0.9, 0.2]) for fid in verdicts}
        ps = propose_weights(verdicts, current_weights=current)
        for p in ps.proposals:
            assert p.proposed_weight <= WEIGHT_CAP, p
            assert p.proposed_weight >= WEIGHT_FLOOR, p


# ---------------------------------------------------------------------------
# AC-2 — rejected drives toward zero
# ---------------------------------------------------------------------------
def test_rejected_verdict_drives_toward_zero():
    verdicts = {"f1": _verdict("rejected", n_periods=60)}
    ps = propose_weights(verdicts, current_weights={"f1": 0.8})
    p = ps.proposals[0]
    assert p.proposed_weight < 0.8        # moves down, never up
    assert p.proposed_weight >= WEIGHT_FLOOR
    # a rejected factor already at the floor cannot go below it.
    ps0 = propose_weights(verdicts, current_weights={"f1": 0.0})
    assert ps0.proposals[0].proposed_weight == WEIGHT_FLOOR


# ---------------------------------------------------------------------------
# AC-3 — premium raises within cap, bounded step
# ---------------------------------------------------------------------------
def test_premium_raises_within_cap_bounded_step():
    verdicts = {"f1": _verdict("premium", n_periods=60)}
    ps = propose_weights(verdicts, current_weights={"f1": 0.2})
    p = ps.proposals[0]
    assert p.proposed_weight > 0.2                       # raised
    assert p.proposed_weight - 0.2 <= MAX_STEP_PER_CYCLE + 1e-9   # bounded per cycle
    assert p.proposed_weight <= WEIGHT_CAP
    # already at cap → no amplification.
    ps_cap = propose_weights(verdicts, current_weights={"f1": WEIGHT_CAP})
    assert ps_cap.proposals[0].proposed_weight == WEIGHT_CAP


# ---------------------------------------------------------------------------
# AC-4 — insufficient observations → no move
# ---------------------------------------------------------------------------
def test_insufficient_observations_no_move():
    verdicts = {"f1": _verdict("premium", n_periods=MIN_OBSERVATIONS - 1)}
    ps = propose_weights(verdicts, current_weights={"f1": 0.3})
    p = ps.proposals[0]
    assert p.proposed_weight == 0.3
    assert "insufficient" in p.reason.lower()


# ---------------------------------------------------------------------------
# AC-5 — eval gate requires STRICTLY beating prior-best (checkpoint-fallback)
# ---------------------------------------------------------------------------
def test_eval_gate_requires_strictly_beat_prior_best():
    ps = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    # strictly above prior-best AND plateau_stable → pass
    out = evaluate_against_holdout(
        ps, holdout_dsr=0.80, holdout_sharpe_delta=0.3, prior_best_dsr=0.70, plateau_stable=True
    )
    assert out.eval_passed is True
    assert out.beats_prior_best is True
    # a TIE reverts (strict >, not >=)
    ps2 = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    out2 = evaluate_against_holdout(
        ps2, holdout_dsr=0.70, holdout_sharpe_delta=0.3, prior_best_dsr=0.70, plateau_stable=True
    )
    assert out2.eval_passed is False
    assert out2.beats_prior_best is False
    # strictly below → reverts
    ps3 = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    out3 = evaluate_against_holdout(
        ps3, holdout_dsr=0.60, holdout_sharpe_delta=0.3, prior_best_dsr=0.70, plateau_stable=True
    )
    assert out3.eval_passed is False


# ---------------------------------------------------------------------------
# AC-6 — eval gate requires plateau-stable (robustness-not-peak)
# ---------------------------------------------------------------------------
def test_eval_gate_requires_plateau_stable():
    ps = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    out = evaluate_against_holdout(
        ps, holdout_dsr=0.90, holdout_sharpe_delta=0.5, prior_best_dsr=0.10, plateau_stable=False
    )
    # beats prior-best handily, but not jitter-stable across folds → rejected (AMZN-weight lesson)
    assert out.beats_prior_best is True
    assert out.eval_passed is False


# ---------------------------------------------------------------------------
# AC-7 — failed eval appends to rejected buffer, NOT candidates (cron path)
# ---------------------------------------------------------------------------
def test_failed_eval_appends_to_rejected_buffer_not_candidates(tmp_path):
    cand = tmp_path / "weight-candidates.json"
    rej = tmp_path / "weight-rejected-buffer.jsonl"
    ps = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    out = evaluate_against_holdout(
        ps, holdout_dsr=0.10, holdout_sharpe_delta=0.0, prior_best_dsr=0.50, plateau_stable=True
    )
    assert out.eval_passed is False
    # mirror the cron's else-branch: append rejected, do NOT write candidates
    append_rejected(out, path=rej)
    assert rej.exists()
    assert not cand.exists()
    line = json.loads(rej.read_text().splitlines()[0])
    assert line["eval_passed"] is False


# ---------------------------------------------------------------------------
# AC-8 — passing eval writes advisory candidates only, touches no live config
# ---------------------------------------------------------------------------
def test_passed_eval_writes_advisory_candidates_only(tmp_path):
    cand = tmp_path / "weight-candidates.json"
    registry = tmp_path / "registry.json"
    registry.write_text('{"live": true}')
    registry_before = registry.read_text()

    ps = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    out = evaluate_against_holdout(
        ps, holdout_dsr=0.80, holdout_sharpe_delta=0.4, prior_best_dsr=0.10, plateau_stable=True
    )
    assert out.eval_passed is True
    written = write_candidates(out, path=cand)
    assert written.exists()
    data = json.loads(cand.read_text())
    assert data["eval_passed"] is True
    assert data["proposals"]
    # no live config mutated
    assert registry.read_text() == registry_before
    # advisory-plane-only: the proposer module never CALLS aggregator.update (no live mutation).
    # (the import-level guarantee is asserted structurally in
    # test_module_touches_no_risk_gate_or_ladder_or_kill_switch via the AST.)
    import hermes_quant.factors.weight_proposer as wp

    code_tokens = _code_only(Path(wp.__file__).read_text())
    assert "aggregator.update" not in code_tokens


# ---------------------------------------------------------------------------
# AC-9 — proposer reads no env and is pure
# ---------------------------------------------------------------------------
def test_proposer_reads_no_env_and_is_pure(monkeypatch):
    # scrub the flag both ways; output must be identical (library is flag-agnostic)
    verdicts = {"f1": _verdict("standard"), "f2": _verdict("rejected")}
    monkeypatch.delenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", raising=False)
    a = propose_weights(verdicts, current_weights={"f1": 0.1, "f2": 0.5})
    monkeypatch.setenv("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "1")
    b = propose_weights(verdicts, current_weights={"f1": 0.1, "f2": 0.5})
    assert [p.to_dict() for p in a.proposals] == [p.to_dict() for p in b.proposals]
    # the source reads no os.environ at all
    import hermes_quant.factors.weight_proposer as wp

    assert "os.environ" not in Path(wp.__file__).read_text()
    assert "import os" not in Path(wp.__file__).read_text()


# ---------------------------------------------------------------------------
# AC-10 — external-truth only: no self-score path (structural)
# ---------------------------------------------------------------------------
def test_external_truth_only_no_self_score():
    import inspect

    sig = inspect.signature(evaluate_against_holdout)
    # the proposal set is positional; every grading kwarg is float/bool — never a tier/reason.
    kw = {n: p for n, p in sig.parameters.items() if p.kind == p.KEYWORD_ONLY}
    assert set(kw) == {"holdout_dsr", "holdout_sharpe_delta", "prior_best_dsr", "plateau_stable"}
    # (PEP 563: `from __future__ import annotations` makes annotations strings.)
    for name in ("holdout_dsr", "holdout_sharpe_delta", "prior_best_dsr"):
        assert kw[name].annotation == "float"
    assert kw["plateau_stable"].annotation == "bool"
    # structural: the verdict-derived fields (tier, reason) on a proposal can NOT influence the
    # grading number — eval_passed depends only on the supplied floats/bool.
    ps_premium = propose_weights({"f1": _verdict("premium")}, current_weights={"f1": 0.2})
    ps_rejected = propose_weights({"f1": _verdict("rejected")}, current_weights={"f1": 0.2})
    out_p = evaluate_against_holdout(
        ps_premium, holdout_dsr=0.4, holdout_sharpe_delta=0.0, prior_best_dsr=0.9, plateau_stable=True
    )
    out_r = evaluate_against_holdout(
        ps_rejected, holdout_dsr=0.4, holdout_sharpe_delta=0.0, prior_best_dsr=0.9, plateau_stable=True
    )
    # same OOS numbers → same eval verdict regardless of the proposer's own tier text
    assert out_p.eval_passed == out_r.eval_passed is False


# ---------------------------------------------------------------------------
# Advisory-plane-only: the module imports NONE of the immutable risk surfaces.
# (the central safety-frame assertion: the loop cannot reach the gate/ladder/kill-switch)
# ---------------------------------------------------------------------------
def test_module_touches_no_risk_gate_or_ladder_or_kill_switch():
    import hermes_quant.factors.weight_proposer as wp

    src = Path(wp.__file__).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.append(mod)
            imported += [f"{mod}.{a.name}" for a in node.names]

    forbidden_substrings = (
        "risk.gate",
        "risk_gate",
        "risk.config",
        "governance.kill_switch",
        "kill_switch",
        "killswitch",
        "sizing_ladder",
        "position_ladder",
        "aggregators",  # never mutates BMA weights
        "settlement_loop",
    )
    for name in imported:
        low = name.lower()
        for bad in forbidden_substrings:
            assert bad not in low, f"forbidden import {name!r} (matched {bad!r})"

    # In EXECUTABLE code (docstrings/comments stripped), no reference to the immutable surfaces
    # or the discrete sizing ladder. The docstring may *name* them to explain the safety frame;
    # code may not touch them.
    code = _code_only(src)
    for token in ("0.05", "0.15", "0.20", "RiskGate", "RiskConfig", "kill", "ladder"):
        assert token not in code, f"forbidden token {token!r} present in weight_proposer.py code"


# ---------------------------------------------------------------------------
# checkpoint-fallback: missing prior-best → -inf (first run, any pass beats it)
# ---------------------------------------------------------------------------
def test_load_prior_best_missing_is_neg_inf(tmp_path):
    assert load_prior_best_dsr(path=tmp_path / "nope.json") == float("-inf")


def test_empty_proposal_set_to_dict_roundtrips():
    ps = FactorWeightProposalSet(generated_at="2026-05-30T00:00:00+00:00")
    d = ps.to_dict()
    assert d["proposals"] == []
    assert d["eval_passed"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
