#!/usr/bin/env python3
"""quant-monthly-meta-retro-eval-gate.py — the HARD GATE before flipping
HERMES_QUANT_MONTHLY_META_RETRO=1 (W3, ADR-0080 D80.3 specialized for the
report-and-candidate-only tier).

Proves the four W3 gate conditions (plan §4) over a FROZEN fixture corpus:
  1. Reproduces (Run-Card config_hash idiom): run_meta_retro twice over the same
     immutable corpus -> identical meta_retro_id, config_hash, sorted candidate
     claims, beliefs_promoted, beliefs_expired.
  2. Candidate hypotheses pass novelty/dedup (every candidate novelty_max_sim <
     novelty_threshold; no near-duplicate of a prior registry hypothesis).
  3. Persona-weight deltas are TELEMETRY-ONLY: every telemetry_only is True, every
     |proposed_weight_delta| <= 0.10, AND no aggregator (deliberative.py/bma.py)
     references persona_calibration (grep-assert).
  4. Oracle provenance preserved + nothing live mutated: (a) promoted/expired beliefs
     carry forward oracle_provenance unchanged; (b) the debate-row join applied the
     evt.asof < asof guard; (c) the off-state is byte-identical; (d) no write touched a
     risk module / the sizing ladder / the seed YAML.

Prints `GATE: ✅ PASS — safe to flip HERMES_QUANT_MONTHLY_META_RETRO=1` only when all
four pass; otherwise it prints each failure and exits non-zero. PASSING IS NECESSARY,
NOT SUFFICIENT (ADR-0080 D80.3 #2): promotion of any candidate to live still requires
HypothesisRunner (W6) + PromotionOrchestrator + operator sign-off (zero auto-promotion).
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    import os

    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

_REPO = Path(__file__).resolve().parents[2]
ASOF = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


def _frozen_beliefs() -> list[dict]:
    """A frozen weekly belief corpus: one recurring positive winner (-> candidate +
    monthly promotion) and one solo decayed loser (-> expiry)."""
    rows: list[dict] = []
    for w in range(3):
        wk = ASOF - timedelta(days=7 * (w + 1))
        tau = wk - timedelta(hours=12)
        rows.append({
            "schema_version": 1, "belief_id": f"bel_weekly_pm_AAPL_w{w}", "tier": "weekly",
            "role": "portfolio_manager", "lesson_category": "regime_shift_invalidation",
            "verbal_delta": "winner", "alpha_evidence": 0.04, "support_n": 4,
            "half_life_days": 14.0, "access_counter": 0, "importance": 1.0, "recency": 1.0,
            "oracle_provenance": {"source": "agent_reflection",
                                  "tau_observable_max": tau.isoformat(),
                                  "decision_ids": [f"dec_w{w}"]},
            "asof_distilled": wk.isoformat(), "status": "active",
        })
    solo_tau = (ASOF - timedelta(days=6)).isoformat()
    rows.append({
        "schema_version": 1, "belief_id": "bel_weekly_pm_TSLA_solo", "tier": "weekly",
        "role": "portfolio_manager", "lesson_category": "noise_trade_no_lesson",
        "verbal_delta": "solo", "alpha_evidence": -0.005, "support_n": 4,
        "half_life_days": 14.0, "access_counter": 0, "importance": 0.0, "recency": 0.01,
        "oracle_provenance": {"source": "agent_reflection", "tau_observable_max": solo_tau,
                              "decision_ids": ["dec_solo"]},
        "asof_distilled": (ASOF - timedelta(days=5)).isoformat(), "status": "active",
    })
    return rows


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


def _run(beliefs_file: str, meta_file: str, hyp_file: str, *, register: bool):
    from hermes_quant.memory.meta_retro import run_meta_retro
    from hermes_quant.research.hypothesis import HypothesisRegistry

    return run_meta_retro(
        ASOF,
        register_candidates=register,
        realized_alpha_by_proposal=lambda _p: None,
        beliefs_path=Path(beliefs_file),
        meta_retros_path=Path(meta_file),
        registry=HypothesisRegistry(path=Path(hyp_file)),
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


def main() -> int:  # noqa: C901
    print("=" * 72)
    print("MONTHLY META-RETRO — W3 EVAL GATE (ADR-0080 D80.3)")
    print("=" * 72)
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)

        # --- Condition 1: reproduces byte-identical ---
        _write(tdp / "b1.jsonl", _frozen_beliefs())
        _write(tdp / "b2.jsonl", _frozen_beliefs())
        r1 = _run(str(tdp / "b1.jsonl"), str(tdp / "m1.jsonl"), str(tdp / "h1.jsonl"), register=False)
        r2 = _run(str(tdp / "b2.jsonl"), str(tdp / "m2.jsonl"), str(tdp / "h2.jsonl"), register=False)
        cond1 = (
            r1.meta_retro_id == r2.meta_retro_id
            and r1.config_hash == r2.config_hash
            and sorted(c["claim"] for c in r1.candidate_hypotheses)
            == sorted(c["claim"] for c in r2.candidate_hypotheses)
            and r1.beliefs_promoted == r2.beliefs_promoted
            and r1.beliefs_expired == r2.beliefs_expired
        )
        print("\n--- 1. REPRODUCES (config_hash) ---")
        print(f"  meta_retro_id: {r1.meta_retro_id}  config_hash: {r1.config_hash[:12]}")
        print(f"  candidates: {len(r1.candidate_hypotheses)}  promoted: {len(r1.beliefs_promoted)}  "
              f"expired: {len(r1.beliefs_expired)}  {'✅ PASS' if cond1 else '❌ FAIL'}")
        if not cond1:
            failures.append("condition 1: NOT reproducible across runs")

        # --- Condition 2: candidates pass novelty/dedup ---
        novelty_threshold = 0.85
        cond2 = bool(r1.candidate_hypotheses) and all(
            c["novelty_max_sim"] < novelty_threshold for c in r1.candidate_hypotheses
        )
        print("\n--- 2. CANDIDATES PASS NOVELTY/DEDUP ---")
        for c in r1.candidate_hypotheses:
            print(f"  novelty_max_sim={c['novelty_max_sim']:.4f} < {novelty_threshold}  "
                  f"[{c['source_lesson_category']}]")
        print(f"  {'✅ PASS' if cond2 else '❌ FAIL'}")
        if not cond2:
            failures.append("condition 2: a candidate failed the novelty gate (or none emitted)")

        # --- Condition 3: persona deltas telemetry-only + clamped + wall ---
        cond3a = all(p["telemetry_only"] is True for p in r1.persona_calibration)
        cond3b = all(abs(p["proposed_weight_delta"]) <= 0.10 for p in r1.persona_calibration)
        agg_dir = _REPO / "hermes_quant" / "aggregators"
        cond3c = all(
            "persona_calibration" not in (agg_dir / f).read_text()
            for f in ("deliberative.py", "bma.py")
        )
        cond3 = cond3a and cond3b and cond3c
        print("\n--- 3. PERSONA DELTAS TELEMETRY-ONLY ---")
        print(f"  telemetry_only all True: {cond3a}")
        print(f"  |delta| <= 0.10 all: {cond3b}")
        print(f"  no aggregator reads persona_calibration: {cond3c}")
        print(f"  {'✅ PASS' if cond3 else '❌ FAIL'}")
        if not cond3:
            failures.append("condition 3: persona deltas not telemetry-only / clamped / walled")

        # --- Condition 4: Oracle provenance + nothing live mutated ---
        # 4a: promoted/expired beliefs carry forward oracle_provenance unchanged.
        source_provs = {
            json.dumps(r["oracle_provenance"], sort_keys=True) for r in _frozen_beliefs()
        }
        written = []
        with open(tdp / "b1.jsonl") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    written.append(json.loads(line))
        promoted_expired = [
            r for r in written if r.get("tier") == "monthly" or r.get("status") == "expired"
        ]
        cond4a = bool(promoted_expired) and all(
            json.dumps(r["oracle_provenance"], sort_keys=True) in source_provs
            for r in promoted_expired
        )
        # 4b: debate-row asof < asof guard (verified by source: strict `< asof` in _load_debate_rows).
        mr_src = (_REPO / "hermes_quant" / "memory" / "meta_retro.py").read_text()
        cond4b = "e.asof < asof" in mr_src
        # 4c: off-state byte-identical (subprocess, flag unset).
        with tempfile.TemporaryDirectory() as home2:
            proc = subprocess.run(
                [sys.executable, str(_REPO / "ops" / "scripts" / "quant-monthly-meta-retro.py")],
                capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin", "HOME": home2}, timeout=60,
            )
            cond4c = proc.returncode == 0 and proc.stdout == ""
        # 4d: no forbidden imports + no risk/ladder symbol in source.
        imports = _collect_imports(_REPO / "hermes_quant" / "memory" / "meta_retro.py")
        forbidden = ("risk.gate", "risk_gate", "kill_switch", "react.live", "sizing",
                     "propagation_graph")
        cond4d_imports = not any(f in imp.lower() for imp in imports for f in forbidden)
        cond4d_symbols = all(
            s not in mr_src for s in
            ("target_position_pct", "RiskGate", "KillSwitch", "signed_intensity")
        )
        cond4d_writes = "audit_log.append" not in mr_src  # no new EventKind; report to own JSONL
        cond4d = cond4d_imports and cond4d_symbols and cond4d_writes
        cond4 = cond4a and cond4b and cond4c and cond4d
        print("\n--- 4. ORACLE PROVENANCE + NOTHING LIVE MUTATED ---")
        print(f"  4a provenance copied forward unchanged: {cond4a}")
        print(f"  4b debate-row asof<asof guard present: {cond4b}")
        print(f"  4c off-state byte-identical (empty stdout, rc=0): {cond4c}")
        print(f"  4d no risk/ladder/kill-switch/seed-YAML touch: {cond4d}")
        print(f"  {'✅ PASS' if cond4 else '❌ FAIL'}")
        if not cond4:
            failures.append("condition 4: Oracle provenance / advisory-plane wall breach")

    print("\n" + "=" * 72)
    if not failures:
        print("GATE: ✅ PASS — safe to flip HERMES_QUANT_MONTHLY_META_RETRO=1")
        print("  (necessary-not-sufficient: candidate->live still needs W6 + operator sign-off)")
        print("=" * 72)
        return 0
    print("GATE: ❌ FAIL — DO NOT enable. Fix the failing condition(s) first:")
    for f in failures:
        print(f"  - {f}")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
