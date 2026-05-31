"""W3 off-state + advisory-plane wall tests (plan §6).

  - Gate condition 4c: flag-OFF -> byte-identical no-op (returns 0, empty stdout, no writes).
  - Gate condition 3: NO aggregator (deliberative.py / bma.py) reads `persona_calibration`.
  - Gate condition 4d: meta_retro.py imports NOTHING from the risk-gate / sizing-ladder /
    kill-switch / seed-YAML code paths; its only persistent writes are meta_retros.jsonl,
    beliefs.jsonl, and the hypothesis registry.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from hermes_quant.memory import meta_retro

_REPO = Path(__file__).resolve().parents[2]
_CRON = _REPO / "ops" / "scripts" / "quant-monthly-meta-retro.py"


# ---------------------------------------------------------------------------
# GATE CONDITION 4c — byte-identical off-state
# ---------------------------------------------------------------------------


def test_offstate_is_noop(tmp_path) -> None:
    """Flag unset -> main() returns 0, writes nothing, prints nothing (subprocess capture)."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),  # isolate ~/.hermes so the test can NOT touch the real store
    }
    # Flag unset entirely.
    proc = subprocess.run(
        [sys.executable, str(_CRON)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == "", f"off-state must be empty stdout, got: {proc.stdout!r}"
    # No quant home created by the off-state path.
    assert not (tmp_path / ".hermes" / "quant" / "memory" / "meta_retros.jsonl").exists()

    # Explicit flag=0 is identical.
    proc0 = subprocess.run(
        [sys.executable, str(_CRON)],
        capture_output=True, text=True,
        env={**env, "HERMES_QUANT_MONTHLY_META_RETRO": "0"}, timeout=60,
    )
    assert proc0.returncode == 0
    assert proc0.stdout == ""


# ---------------------------------------------------------------------------
# GATE CONDITION 3 — no aggregator reads persona_calibration
# ---------------------------------------------------------------------------


def test_no_aggregator_reads_persona_calibration() -> None:
    """grep-assert: `persona_calibration` does NOT appear in the aggregators that would
    apply a weight. The advisory-plane wall — persona telemetry never reaches a vote."""
    for fname in ("deliberative.py", "bma.py"):
        src = (_REPO / "hermes_quant" / "aggregators" / fname).read_text()
        assert "persona_calibration" not in src, (
            f"{fname} must NOT reference persona_calibration (telemetry-only wall, gate 3)"
        )


# ---------------------------------------------------------------------------
# GATE CONDITION 4d — advisory-plane only (no risk / ladder / kill-switch / seed-YAML)
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "risk.gate",
    "risk_gate",
    "governance.kill_switch",
    "kill_switch",
    "react.live",
    "sizing",
    "propagation_graph",
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


def test_no_write_touches_risk_or_ladder() -> None:
    src_path = Path(meta_retro.__file__)
    imports = _collect_imports(src_path)
    for imp in imports:
        low = imp.lower()
        for forbidden in _FORBIDDEN_IMPORT_SUBSTRINGS:
            assert forbidden not in low, (
                f"meta_retro imports a forbidden module: {imp!r} matched {forbidden!r}"
            )

    text = src_path.read_text()
    # Never writes a position size / risk-control symbol. (NB: the persona-calibration
    # clamp legitimately uses the literal 0.10 as a TELEMETRY bound — distinct from the
    # sizing ladder, which is keyed on target_position_pct at the risk gate, never here.)
    for sizing_symbol in (
        "target_position_pct", "position_pct", "max_position", "fill_size_pct",
        "RiskGate", "risk_gate", "KillSwitch", "kill_switch",
        "sizing_ladder", "DISCRETE_SIZES", "signed_intensity",
    ):
        assert sizing_symbol not in text, (
            f"meta_retro references a sizing / risk-control symbol: {sizing_symbol!r}"
        )

    # Its only persistent writes are meta_retros.jsonl, beliefs.jsonl, and the registry.
    assert "META_RETROS_PATH" in text
    assert "BELIEFS_PATH" in text
    # No new audit EventKind: it only READS debate/promotion rows; the report goes to its
    # own JSONL (no audit_log.append in the module).
    assert "audit_log.append" not in text


def test_propose_only_invariant_in_source() -> None:
    """The propose-only invariant is stated in code: candidates are status='open' and the
    report is telemetry_only."""
    text = Path(meta_retro.__file__).read_text()
    assert 'status="open"' in text
    assert "telemetry_only" in text
    assert 'author="quant-monthly-meta-retro"' in text
